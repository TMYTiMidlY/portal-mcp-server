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

        async def fake_group(*a, **k):
            called.append((a, k))
            return [{"host": "x", "exit_code": 0}]

        monkeypatch.setattr(cli, "ssh_exec_on_group", fake_group)

        result = await cli.ssh_group_exec("fleet", "rm -rf /", timeout=5)
        assert "BLOCKED" in result
        assert "blocked by policy" in result.lower()
        assert called == [], "exec must NOT run when command is blocked"

    @pytest.mark.asyncio
    async def test_disallowed_host_in_group_rejected(self, restrictive_policy,
                                                      populated_manager,
                                                      monkeypatch):
        from ssh_remote_mcp import cli

        called = []

        async def fake_group(*a, **k):
            called.append((a, k))
            return []

        monkeypatch.setattr(cli, "ssh_exec_on_group", fake_group)

        # 'danger-01' is in the group but not in host_allowlist — must block.
        result = await cli.ssh_group_exec("fleet", "uptime", timeout=5)
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

        result = await cli.ssh_rolling(
            json.dumps(["safe-01", "safe-02"]), "rm -rf /tmp/x", timeout=5
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

        result = await cli.ssh_rolling(
            json.dumps(["safe-01", "danger-01"]), "uptime", timeout=5
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

        result = await cli.ssh_broadcast_batch(
            json.dumps(["safe-01", "safe-02"]),
            json.dumps(["uptime", "rm -rf /opt"]),
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

        result = await cli.ssh_broadcast_batch(
            json.dumps(["safe-01"]),
            json.dumps(["uptime", 42]),
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
        result = await cli.ssh_playbook("safe-01", json.dumps(playbook))
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
        result = await cli.ssh_playbook_on_group("fleet", json.dumps(playbook))
        assert "BLOCKED" in result
        assert "danger-01" in result


# ════════════════════════════════════════════════════════════════════════════
#  ssh_session_exec — gates the command (was a bypass before)
# ════════════════════════════════════════════════════════════════════════════

class TestSessionExecGate:
    @pytest.mark.asyncio
    async def test_blocked_command_rejected_inside_session(
        self, restrictive_policy, populated_manager, monkeypatch
    ):
        from ssh_remote_mcp import cli, session_manager

        # Fabricate a fake session pointing at a safe host.
        sm = session_manager.SessionManager()
        fake_sess = session_manager.ShellSession(
            session_id="abc12345",
            host_name="safe-01",
            process=None,  # type: ignore
        )
        sm._sessions["abc12345"] = fake_sess
        monkeypatch.setattr(cli, "get_session_manager", lambda: sm)

        async def fake_exec(*a, **k):
            raise AssertionError("must not be called")

        monkeypatch.setattr(sm, "execute_in_session", fake_exec)

        result = await cli.ssh_session_exec("abc12345", "rm -rf /", timeout=1)
        assert "BLOCKED" in result

    @pytest.mark.asyncio
    async def test_unknown_session_returns_error(self, restrictive_policy,
                                                  populated_manager,
                                                  monkeypatch):
        from ssh_remote_mcp import cli, session_manager

        sm = session_manager.SessionManager()
        monkeypatch.setattr(cli, "get_session_manager", lambda: sm)

        result = await cli.ssh_session_exec("missing", "uptime", timeout=1)
        assert "not found" in result
