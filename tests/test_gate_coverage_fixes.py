"""Tests for the gate coverage fixes applied after the systematic review.

Findings addressed
------------------
1. **portal_host register/remove bypass _gate** — an agent could register
   an alias pointing at any IP, then operate on that alias unimpeded
   because host_allowlist only sees the alias name, not the target IP.

2. **portal_tunnel_close has no gate** — once tunnels exist, anyone with
   tunnel_id could dismantle them even after losing host access.

3. **_gate_many burns rate-limit quota on hosts that pass before a later
   host fails** — the per-host loop incremented the rate-limit counter
   before validating every host, so a multi-host call that ultimately
   gets blocked still consumed quota on the hosts that happened to be
   checked first.
"""
from __future__ import annotations

import pytest

from mcp.server.fastmcp.exceptions import ToolError


# ─── Common policy fixture ─────────────────────────────────────────────────

@pytest.fixture
def policy(monkeypatch, tmp_path):
    """Policy: only ``safe-*`` hosts allowed; rate limit 1000/s."""
    from portal_mcp_server import security, cli

    pol_yaml = tmp_path / "p.yaml"
    pol_yaml.write_text(
        "policies:\n"
        "  host_allowlist:\n"
        "    - 'safe-*'\n"
        "  rate_limit_rps: 1000\n"
    )
    pol = security.SecurityPolicy(policies_yaml=pol_yaml)
    monkeypatch.setattr(security, "_policy", pol)
    monkeypatch.setattr(cli, "get_policy", lambda: pol)
    return pol


@pytest.fixture
def fresh_mgr(monkeypatch, tmp_path):
    """Fresh ConnectionManager wired into both the connection_manager
    singleton AND cli.get_manager so the gate checks see the same state.
    """
    from portal_mcp_server import connection_manager, cli

    yml = tmp_path / "h.yaml"
    yml.write_text("hosts: {}\n")
    m = connection_manager.ConnectionManager(hosts_yaml=yml)
    monkeypatch.setattr(connection_manager, "_manager", m)
    monkeypatch.setattr(cli, "get_manager", lambda: m)
    return m


# ════════════════════════════════════════════════════════════════════════════
# 1. portal_host register/remove now go through _gate
# ════════════════════════════════════════════════════════════════════════════

class TestPortalHostGate:
    def test_register_with_target_in_allowlist_succeeds(self, policy, fresh_mgr):
        from portal_mcp_server import cli
        # Target host alias matches 'safe-*' allowlist
        out = cli.portal_host(action="register", name="alias1", host="safe-target")
        assert "registered" in out.lower(), out

    def test_register_with_target_outside_allowlist_blocked(self, policy, fresh_mgr):
        """Even with a benign alias name, registering a target host that's
        NOT in the allowlist must be blocked. Without the gate fix, an
        agent could register 'safe-pivot' → '10.0.0.99' and operate on
        '10.0.0.99' via the alias.
        """
        from portal_mcp_server import cli
        with pytest.raises(ToolError, match="BLOCKED:"):
            cli.portal_host(action="register", name="safe-pivot", host="evil-host")
        assert "evil-host" not in [h["name"] for h in fresh_mgr.list_hosts()]
        assert "safe-pivot" not in [h["name"] for h in fresh_mgr.list_hosts()]

    def test_remove_blocked_alias_is_gated(self, policy, fresh_mgr):
        """Remove gates against the alias name. If the alias isn't in the
        allowlist (e.g. pre-existing in hosts.yaml from before the policy
        tightened), removal must be blocked too.
        """
        from portal_mcp_server import cli
        # Force a non-allowlisted host into the registry directly.
        fresh_mgr._registry["evil-host"] = fresh_mgr._registry.get(
            "evil-host"
        ) or _make_hostconfig("evil-host")
        with pytest.raises(ToolError, match="BLOCKED:"):
            cli.portal_host(action="remove", name="evil-host")


def _make_hostconfig(name: str):
    from portal_mcp_server.connection_manager import HostConfig
    return HostConfig(name=name, host=name)


# ════════════════════════════════════════════════════════════════════════════
# 2. portal_tunnel_close now goes through _gate
# ════════════════════════════════════════════════════════════════════════════

class TestPortalTunnelCloseGate:
    @pytest.mark.asyncio
    async def test_close_blocked_when_host_not_in_allowlist(
        self, policy, fresh_mgr, monkeypatch,
    ):
        """Insert a tunnel for a non-allowlisted host directly into the
        TunnelManager registry, then attempt to close it via the MCP
        wrapper. The gate must reject it.
        """
        from portal_mcp_server import cli, network_tools

        tm = network_tools.TunnelManager()
        monkeypatch.setattr(network_tools, "_tunnel_mgr", tm)
        monkeypatch.setattr(cli, "get_tunnel_manager", lambda: tm)

        tm._tunnels["t1"] = network_tools.ActiveTunnel(
            tunnel_id="t1", tunnel_type="local",
            host_name="evil-host",
            local_host="127.0.0.1", local_port=1234,
            remote_host="x", remote_port=80,
            listener=_FakeListener(),
            conn=object(),  # type: ignore[arg-type]
            description="t1",
        )

        with pytest.raises(ToolError, match="BLOCKED:"):
            await cli.portal_tunnel_close("t1")
        # Tunnel still alive after blocked close.
        assert "t1" in tm._tunnels


class TestPortalBashCloseGate:
    @pytest.mark.asyncio
    async def test_bash_close_blocked_when_host_not_in_allowlist(
        self, policy, monkeypatch,
    ):
        """portal_bash_close is state-changing (tears down a session) and
        must respect the same host allowlist as every other gated entry.
        """
        from portal_mcp_server import cli

        # Sentinel: if the gate fails we should never reach the underlying
        # close. Patch _re_bash_close to blow up if invoked.
        async def _must_not_be_called(host):
            raise AssertionError(
                f"_re_bash_close called for blocked host {host!r}"
            )

        monkeypatch.setattr(cli, "_re_bash_close", _must_not_be_called)

        with pytest.raises(ToolError, match="BLOCKED:"):
            await cli.portal_bash_close("evil-host")

    @pytest.mark.asyncio
    async def test_bash_close_passes_when_host_in_allowlist(
        self, policy, monkeypatch,
    ):
        from portal_mcp_server import cli

        called = {"with": None}
        async def _ok(host):
            called["with"] = host
            return f"closed {host}"

        monkeypatch.setattr(cli, "_re_bash_close", _ok)
        out = await cli.portal_bash_close("safe-01")
        assert out == "closed safe-01"
        assert called["with"] == "safe-01"


class _FakeListener:
    def close(self):
        pass
    async def wait_closed(self):
        pass


# ════════════════════════════════════════════════════════════════════════════
# 3. _gate_many no longer burns rate-limit quota on early-passing hosts
# ════════════════════════════════════════════════════════════════════════════

class TestGateManyTwoPhase:
    def test_failed_host_does_not_burn_others_rate_limit(self, policy):
        """If host3 is outside the allowlist, hosts h1 and h2 must still
        have full rate-limit quota afterwards. Old buggy implementation
        called check_rate_limit (which mutates the sliding window) for
        h1 and h2 *before* discovering h3 was bad.
        """
        from portal_mcp_server import cli

        err = cli._gate_many(
            ["safe-01", "safe-02", "evil-host"],
            command="echo hi",
        )
        assert err is not None and "evil-host" in err, err
        # Rate-limit counters for the safe hosts must be empty: nothing
        # mutated them.
        rc = policy._rate_counters
        assert rc.get("safe-01", []) == []
        assert rc.get("safe-02", []) == []

    def test_all_hosts_pass_consumes_one_token_per_host(self, policy):
        """Sanity: when every host validates, each gets exactly one token
        committed. No double-counting.
        """
        from portal_mcp_server import cli

        err = cli._gate_many(["safe-01", "safe-02"], command="echo hi")
        assert err is None
        rc = policy._rate_counters
        assert len(rc["safe-01"]) == 1
        assert len(rc["safe-02"]) == 1


# ════════════════════════════════════════════════════════════════════════════
# 4. _gate_playbook two-phase + non-string step rejection
# ════════════════════════════════════════════════════════════════════════════

class TestGatePlaybook:
    def test_non_string_step_rejected(self, policy):
        """Old code silently `continue`d on non-string steps, letting the
        bad playbook through the gate to crash later inside ssh_exec.
        """
        from portal_mcp_server import cli

        err = cli._gate_playbook(
            ["safe-01"],
            {"name": "p", "steps": ["echo a", {"cmd": "rm -rf /"}]},
        )
        assert err is not None and "invalid type" in err, err

    def test_failed_step_does_not_burn_host_rate_limit(self, policy, monkeypatch):
        """A blocked step must not consume rate-limit tokens on any host.
        Old implementation incremented per-host counters BEFORE rejecting
        the step.
        """
        from portal_mcp_server import cli

        # Block "rm -rf /" specifically.
        policy.command_blocklist = ["rm -rf*"]

        err = cli._gate_playbook(
            ["safe-01"],
            {"name": "p", "steps": ["echo a", "rm -rf /"]},
        )
        assert err is not None
        assert policy._rate_counters.get("safe-01", []) == []
