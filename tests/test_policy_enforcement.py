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

import json

import pytest


@pytest.fixture
def restrictive_policy(monkeypatch, tmp_path):
    """Install a policy that blocks ``rm -rf /`` and only allows hosts
    matching ``safe-*``.
    """
    from ssh_remote_mcp import security, cli, orchestrator

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
    danger-*. The orchestrator's ``get_manager`` is rebound to it so
    ``_resolve_group`` and the underlying orchestrator agree on which hosts
    belong to the tag.
    """
    from ssh_remote_mcp import connection_manager, cli, orchestrator

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = connection_manager.ConnectionManager(hosts_yaml=yml)
    m.register_host("safe-01", "10.0.0.1", tags=["fleet"])
    m.register_host("safe-02", "10.0.0.2", tags=["fleet"])
    m.register_host("danger-01", "10.0.0.99", tags=["fleet"])
    monkeypatch.setattr(connection_manager, "_manager", m)
    monkeypatch.setattr(cli, "get_manager", lambda: m)
    monkeypatch.setattr(orchestrator, "get_manager", lambda: m)
    return m


# ════════════════════════════════════════════════════════════════════════════
#  ssh_group_exec — must gate command + every host in the resolved group
# ════════════════════════════════════════════════════════════════════════════

class TestGroupExecGate:
    @pytest.mark.asyncio
    async def test_blocked_command_rejected(self, restrictive_policy,
                                             populated_manager, monkeypatch):
        from ssh_remote_mcp import cli

        called = []

        async def fake_parallel(*a, **k):
            called.append((a, k))
            return [{"host": "x", "exit_code": 0}]

        monkeypatch.setattr(cli, "ssh_parallel_exec", fake_parallel)

        result = await cli.portal_multi_exec(mode="parallel",
                                              group_tag="fleet",
                                              command="rm -rf /", timeout=5)
        assert "BLOCKED" in result
        assert "blocked by policy" in result.lower()
        assert called == [], "exec must NOT run when command is blocked"

    @pytest.mark.asyncio
    async def test_disallowed_host_in_group_rejected(self, restrictive_policy,
                                                      populated_manager,
                                                      monkeypatch):
        from ssh_remote_mcp import cli

        called = []

        async def fake_parallel(*a, **k):
            called.append((a, k))
            return []

        monkeypatch.setattr(cli, "ssh_parallel_exec", fake_parallel)

        # 'danger-01' is in the group but not in host_allowlist — must block.
        result = await cli.portal_multi_exec(mode="parallel",
                                              group_tag="fleet",
                                              command="uptime", timeout=5)
        assert "BLOCKED" in result
        assert "danger-01" in result
        assert called == []


# ════════════════════════════════════════════════════════════════════════════
#  ssh_rolling — must gate command + every host
# ════════════════════════════════════════════════════════════════════════════

class TestRollingGate:
    @pytest.mark.asyncio
    async def test_blocked_command_rejected(self, restrictive_policy,
                                             populated_manager, monkeypatch):
        from ssh_remote_mcp import cli

        called = []

        async def fake_rolling(*a, **k):
            called.append((a, k))
            return []

        monkeypatch.setattr(cli, "ssh_rolling_exec", fake_rolling)

        result = await cli.portal_multi_exec(
            mode="rolling",
            hosts_json=json.dumps(["safe-01", "safe-02"]),
            command="rm -rf /tmp/x", timeout=5
        )
        assert "BLOCKED" in result
        assert called == []

    @pytest.mark.asyncio
    async def test_disallowed_host_rejected(self, restrictive_policy,
                                             populated_manager, monkeypatch):
        from ssh_remote_mcp import cli

        async def fake_rolling(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "ssh_rolling_exec", fake_rolling)

        result = await cli.portal_multi_exec(
            mode="rolling",
            hosts_json=json.dumps(["safe-01", "danger-01"]),
            command="uptime", timeout=5
        )
        assert "BLOCKED" in result
        assert "danger-01" in result


# ════════════════════════════════════════════════════════════════════════════
#  ssh_broadcast_batch — every command in the array must pass
# ════════════════════════════════════════════════════════════════════════════

class TestBroadcastBatchGate:
    @pytest.mark.asyncio
    async def test_blocked_command_in_list_rejected(
        self, restrictive_policy, populated_manager, monkeypatch
    ):
        from ssh_remote_mcp import cli

        async def fake_broadcast(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "ssh_broadcast", fake_broadcast)

        result = await cli.portal_multi_exec(
            mode="broadcast",
            hosts_json=json.dumps(["safe-01", "safe-02"]),
            commands_json=json.dumps(["uptime", "rm -rf /opt"]),
            timeout=5,
        )
        assert "BLOCKED" in result

    @pytest.mark.asyncio
    async def test_non_string_command_rejected(self, restrictive_policy,
                                                populated_manager,
                                                monkeypatch):
        from ssh_remote_mcp import cli

        async def fake_broadcast(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "ssh_broadcast", fake_broadcast)

        result = await cli.portal_multi_exec(
            mode="broadcast",
            hosts_json=json.dumps(["safe-01"]),
            commands_json=json.dumps(["uptime", 42]),
            timeout=5,
        )
        assert "Invalid commands_json" in result


# ════════════════════════════════════════════════════════════════════════════
#  ssh_playbook + ssh_playbook_on_group — every step must pass blocklist
# ════════════════════════════════════════════════════════════════════════════

class TestPlaybookGate:
    @pytest.mark.asyncio
    async def test_single_host_playbook_blocks_dangerous_step(
        self, restrictive_policy, populated_manager, monkeypatch
    ):
        from ssh_remote_mcp import cli

        async def fake_run(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "run_playbook", fake_run)

        playbook = {
            "name": "evil",
            "steps": ["uptime", "rm -rf /var", "echo done"],
        }
        result = await cli.portal_playbook(json.dumps(playbook), host="safe-01")
        assert "BLOCKED" in result
        assert "rm -rf" in result.lower() or "blocked by policy" in result.lower()

    @pytest.mark.asyncio
    async def test_group_playbook_blocks_disallowed_host(
        self, restrictive_policy, populated_manager, monkeypatch
    ):
        from ssh_remote_mcp import cli

        async def fake_run(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(cli, "run_playbook_on_group", fake_run)

        playbook = {"name": "ok", "steps": ["uptime"]}
        result = await cli.portal_playbook(json.dumps(playbook), group_tag="fleet")
        assert "BLOCKED" in result
        assert "danger-01" in result


# ════════════════════════════════════════════════════════════════════════════
#  Note: the multi-session ssh_session_* tools were removed in 0.3.0 along with
#  ssh_run/ssh_run_batch/ssh_run_script/ssh_run_with_env. Use portal_bash for
#  single persistent session per host (which is policy-gated per-command in
#  remote_bash.py) or portal_multi_exec for orchestrated parallel/rolling
#  command execution. The session_manager module remains for any future caller.
# ════════════════════════════════════════════════════════════════════════════
