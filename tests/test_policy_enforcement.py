"""Tests for security policy enforcement across multi-host orchestration tools.

Original audit finding (Critical)
---------------------------------
``ssh_group_exec`` / ``ssh_rolling`` / ``ssh_broadcast_batch`` /
``ssh_playbook_on_group`` did not call ``_gate()`` before dispatching, which
let an LLM bypass both the host allowlist and the command blocklist by
choosing a multi-host tool instead of ``ssh_run``. ``ssh_session_exec``
also bypassed the command blocklist once a session existed.

These tests pin the new behaviour: every multi-host tool gates the command
against the policy, and playbook steps are individually checked.
"""
from __future__ import annotations

import pytest

from mcp.server.fastmcp.exceptions import ToolError


@pytest.fixture
def restrictive_policy(monkeypatch, tmp_path):
    """Install a policy that blocks ``rm -rf /`` and only allows hosts
    matching ``safe-*``.
    """
    from portal_mcp_server import security, cli

    policy_yaml = tmp_path / "policies.yaml"
    policy_yaml.write_text(
        "policies:\n"
        "  host_allowlist:\n"
        "    - 'safe-*'\n"
        "  command_blocklist:\n"
        "    - 'rm -rf*'\n"
        "  rate_limit_rps: 1000\n"
    )
    pol = security.SecurityPolicy(policies_yaml=policy_yaml)
    monkeypatch.setattr(security, "_policy", pol)
    monkeypatch.setattr(cli, "get_policy", lambda: pol)
    return pol


@pytest.fixture
def populated_manager(monkeypatch, tmp_path):
    """ConnectionManager with three hosts in tag 'fleet': two safe-*, one
    danger-*. Rebound into cli.get_manager so ``_resolve_group`` sees them.
    """
    from portal_mcp_server import connection_manager, cli

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = connection_manager.ConnectionManager(hosts_yaml=yml)
    m.register_host("safe-01", "10.0.0.1", tags=["fleet"])
    m.register_host("safe-02", "10.0.0.2", tags=["fleet"])
    m.register_host("danger-01", "10.0.0.99", tags=["fleet"])
    monkeypatch.setattr(connection_manager, "_manager", m)
    monkeypatch.setattr(cli, "get_manager", lambda: m)
    return m


# ════════════════════════════════════════════════════════════════════════════
#  remote_exec(group_tag=) — must gate command + every host in the group
# ════════════════════════════════════════════════════════════════════════════

class TestGroupExecGate:
    @pytest.mark.asyncio
    async def test_blocked_command_rejected(self, restrictive_policy,
                                             populated_manager, monkeypatch):
        from portal_mcp_server import cli

        called = []

        async def fake_exec(*a, **k):
            called.append((a, k))
            return {"host": "x", "exit_code": 0}

        monkeypatch.setattr(cli, "ssh_exec", fake_exec)

        with pytest.raises(ToolError) as exc_info:
            await cli.remote_exec(group_tag="fleet",
                                  command="rm -rf /", timeout=5)
        assert "BLOCKED" in str(exc_info.value)
        assert "blocked by policy" in str(exc_info.value).lower()
        assert called == [], "exec must NOT run when command is blocked"

    @pytest.mark.asyncio
    async def test_disallowed_host_in_group_rejected(self, restrictive_policy,
                                                      populated_manager,
                                                      monkeypatch):
        from portal_mcp_server import cli

        called = []

        async def fake_exec(*a, **k):
            called.append((a, k))
            return {"host": "x", "exit_code": 0}

        monkeypatch.setattr(cli, "ssh_exec", fake_exec)

        # 'danger-01' is in the group but not in host_allowlist — must block.
        with pytest.raises(ToolError) as exc_info:
            await cli.remote_exec(group_tag="fleet",
                                  command="uptime", timeout=5)
        assert "BLOCKED" in str(exc_info.value)
        assert "danger-01" in str(exc_info.value)
        assert called == []


# ════════════════════════════════════════════════════════════════════════════
#  remote_exec(host=[...], serialize=True) — rolling: gate command + every host
# ════════════════════════════════════════════════════════════════════════════

class TestRollingGate:
    @pytest.mark.asyncio
    async def test_blocked_command_rejected(self, restrictive_policy,
                                             populated_manager, monkeypatch):
        from portal_mcp_server import cli

        called = []

        async def fake_exec(*a, **k):
            called.append((a, k))
            return {"host": "x", "exit_code": 0}

        monkeypatch.setattr(cli, "ssh_exec", fake_exec)

        with pytest.raises(ToolError, match="BLOCKED"):
            await cli.remote_exec(
                host=["safe-01", "safe-02"],
                command="rm -rf /tmp/x", serialize=True, timeout=5,
            )
        assert called == []

    @pytest.mark.asyncio
    async def test_disallowed_host_rejected(self, restrictive_policy,
                                             populated_manager, monkeypatch):
        from portal_mcp_server import cli

        async def fake_exec(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "ssh_exec", fake_exec)

        with pytest.raises(ToolError) as exc_info:
            await cli.remote_exec(
                host=["safe-01", "danger-01"],
                command="uptime", serialize=True, timeout=5,
            )
        assert "BLOCKED" in str(exc_info.value)
        assert "danger-01" in str(exc_info.value)


# ════════════════════════════════════════════════════════════════════════════
#  remote_exec(commands=[...]) — every command in the sequence must pass
# ════════════════════════════════════════════════════════════════════════════

class TestBroadcastBatchGate:
    @pytest.mark.asyncio
    async def test_blocked_command_in_list_rejected(
        self, restrictive_policy, populated_manager, monkeypatch
    ):
        from portal_mcp_server import cli

        async def fake_exec(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "ssh_exec", fake_exec)

        with pytest.raises(ToolError, match="BLOCKED"):
            await cli.remote_exec(
                host=["safe-01", "safe-02"],
                commands=["uptime", "rm -rf /opt"],
                timeout=5,
            )

    @pytest.mark.asyncio
    async def test_non_string_command_rejected(self, restrictive_policy,
                                                populated_manager,
                                                monkeypatch):
        from portal_mcp_server import cli

        async def fake_exec(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "ssh_exec", fake_exec)

        with pytest.raises(ToolError, match="commands must be a list of strings"):
            await cli.remote_exec(
                host=["safe-01"],
                commands=["uptime", 42],
                timeout=5,
            )



# ════════════════════════════════════════════════════════════════════════════
#  remote_exec(group_tag=, commands=[...]) — multi-command over a group still
#  gates every command + every resolved host (the old playbook gate semantics)
# ════════════════════════════════════════════════════════════════════════════

class TestGroupMultiCommandGate:
    @pytest.mark.asyncio
    async def test_group_multicommand_blocks_disallowed_host(
        self, restrictive_policy, populated_manager, monkeypatch
    ):
        from portal_mcp_server import cli

        async def fake_exec(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "ssh_exec", fake_exec)

        # 'danger-01' is in the fleet group but not allowlisted → blocked even
        # before any of the sequence's commands run.
        with pytest.raises(ToolError) as exc_info:
            await cli.remote_exec(group_tag="fleet",
                                  commands=["uptime", "echo done"], timeout=5)
        assert "BLOCKED" in str(exc_info.value)
        assert "danger-01" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_group_multicommand_blocks_dangerous_step(
        self, restrictive_policy, populated_manager, monkeypatch
    ):
        from portal_mcp_server import cli

        async def fake_exec(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "ssh_exec", fake_exec)

        # A blocked command anywhere in the sequence rejects the whole run.
        with pytest.raises(ToolError) as exc_info:
            await cli.remote_exec(
                host=["safe-01"],
                commands=["uptime", "rm -rf /var", "echo done"], timeout=5)
        msg = str(exc_info.value)
        assert "BLOCKED" in msg
        assert "rm -rf" in msg.lower() or "blocked by policy" in msg.lower()


# ════════════════════════════════════════════════════════════════════════════
#  Note: the ssh_run/ssh_session_*/ssh_playbook families were removed during the
#  agent-first redesign. Use remote_shell for a single persistent session per
#  host (policy-gated per command) or remote_exec for one-shot / fan-out /
#  multi-command execution (gated here). The session_manager module remains.
# ════════════════════════════════════════════════════════════════════════════

