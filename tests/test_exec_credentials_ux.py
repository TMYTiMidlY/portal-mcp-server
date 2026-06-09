"""UX/safety around credentialed exec:
  * sudo / secret exec results are flagged high_risk so the agent reports it;
  * the missing-sudo-password error names both the temporary and permanent
    password sources;
  * `portal <kind> set` auto-installs the credential agent when it's not up.
"""
from __future__ import annotations

import json

import pytest

from portal_mcp_server import cli, sudo_creds


# ── high-risk marker on credentialed exec ────────────────────────────────────

@pytest.mark.asyncio
async def test_exec_sudo_result_is_flagged_high_risk(monkeypatch):
    async def fake_resolve(host):
        return "pw"

    async def fake_sudo(h, cmd, password, timeout=0):
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "root", "stderr": ""}

    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", fake_resolve)
    monkeypatch.setattr(cli, "_re_sudo_exec", fake_sudo)
    out = json.loads(await cli.portal_exec("web01", "id", use_sudo=True))
    assert out["high_risk"] is True
    assert "high_risk_note" in out and out["high_risk_note"]


@pytest.mark.asyncio
async def test_exec_secrets_result_is_flagged_high_risk(monkeypatch):
    async def fake_resolve_secrets(names):
        return ({"X": "v"}, ["v"], None)

    async def fake_exec_env(h, cmd, env, timeout=0):
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(cli, "_resolve_secrets", fake_resolve_secrets)
    monkeypatch.setattr(cli, "_re_exec_env", fake_exec_env)
    out = json.loads(await cli.portal_exec("web01", "echo $X", secrets=["X"]))
    assert out["high_risk"] is True


@pytest.mark.asyncio
async def test_plain_exec_is_not_flagged_high_risk(monkeypatch):
    async def fake_exec(h, cmd, timeout=0):
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "hi", "stderr": ""}

    monkeypatch.setattr(cli, "ssh_exec", fake_exec)
    out = json.loads(await cli.portal_exec("web01", "echo hi"))
    assert "high_risk" not in out


# ── missing-sudo-password guidance names both sources ────────────────────────

def test_sudo_missing_message_names_temp_and_permanent_sources():
    msg = cli._sudo_missing_message("web01")
    assert "portal sudo set web01" in msg          # temporary (no-echo)
    assert "sudo_password_command" in msg          # permanent (password manager)
    assert "no-echo" in msg.lower()
    # never invite pasting the password into the conversation
    assert "paste" in msg.lower()


# ── `portal <kind> set` auto-installs the agent when it's not up ──────────────

def test_set_autoinstalls_agent_when_socket_absent(monkeypatch, capsys, tmp_path):
    missing = tmp_path / "credentials.sock"
    calls = {}

    def fake_install(*, socket_path=None, enable_now=False):
        calls["enable_now"] = enable_now
        missing.write_text("x")  # socket activation creates it
        return {"socket_unit": "portal-credential-agent.socket",
                "service_unit": "portal-credential-agent.service",
                "config_path": "/cfg", "socket_path": str(missing)}

    monkeypatch.setattr("portal_mcp_server.credential_agent.install_agent",
                        fake_install)
    monkeypatch.setattr("portal_mcp_server.paths.credential_agent_platform",
                        lambda: "systemd")

    returned = cli._ensure_agent_for_write(lambda: missing)
    assert returned == missing
    assert calls["enable_now"] is True
    out = capsys.readouterr().out
    assert "installing and starting it now" in out
    assert "portal-credential-agent.socket" in out  # install output is included


def test_set_does_not_reinstall_when_socket_present(monkeypatch, tmp_path):
    present = tmp_path / "credentials.sock"
    present.write_text("x")
    called = {"install": False}

    def fake_install(**kwargs):
        called["install"] = True
        return {}

    monkeypatch.setattr("portal_mcp_server.credential_agent.install_agent",
                        fake_install)
    assert cli._ensure_agent_for_write(lambda: present) == present
    assert called["install"] is False


# ── multi-step under sudo: commands[] runs separately; newlines never collapsed

@pytest.mark.asyncio
async def test_commands_under_sudo_run_each_separately(monkeypatch):
    """The human-friendly multi-step path: commands=[...] + use_sudo runs each
    line as its own sudo exec, verbatim — no flattening into one arg list."""
    seen = []

    async def fake_resolve(host):
        return "pw"

    async def fake_sudo(h, cmd, password, timeout=0):
        seen.append(cmd)
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "", "stderr": ""}

    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", fake_resolve)
    monkeypatch.setattr(cli, "_re_sudo_exec", fake_sudo)
    await cli.portal_exec("web01",
                          commands=["systemctl restart caddy", "sleep 4",
                                    "echo ok"],
                          use_sudo=True)
    assert seen == ["systemctl restart caddy", "sleep 4", "echo ok"]


@pytest.mark.asyncio
async def test_multiline_sudo_command_newlines_preserved(monkeypatch):
    """A multi-line `command` string reaches the sudo exec with newlines intact
    (the server never collapses them to spaces); remote_sudo_exec then runs it
    as a `bash -c` script."""
    seen = []

    async def fake_resolve(host):
        return "pw"

    async def fake_sudo(h, cmd, password, timeout=0):
        seen.append(cmd)
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "", "stderr": ""}

    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", fake_resolve)
    monkeypatch.setattr(cli, "_re_sudo_exec", fake_sudo)
    await cli.portal_exec("web01",
                          command="systemctl restart caddy\nsleep 4\necho ok",
                          use_sudo=True)
    assert seen[0] == "systemctl restart caddy\nsleep 4\necho ok"
