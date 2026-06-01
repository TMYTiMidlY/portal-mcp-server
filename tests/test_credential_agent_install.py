from __future__ import annotations

import json
import subprocess
import sys

import pytest


def test_install_user_units_uses_systemd_percent_t(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent

    home = tmp_path / "home"
    runtime = tmp_path / "run"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))

    res = credential_agent.install_user_units(enable_now=False)

    socket_unit = (home / ".config/systemd/user"
                   / credential_agent.SOCKET_UNIT).read_text()
    service_unit = (home / ".config/systemd/user"
                    / credential_agent.SERVICE_UNIT).read_text()
    agent_config = json.loads((home / ".config/portal-mcp-server"
                               / "agent.json").read_text())

    assert "ListenStream=%t/portal-mcp-server/credentials.sock" in socket_unit
    assert "SocketMode=0600" in socket_unit
    assert "DirectoryMode=0700" in socket_unit
    assert (
        f"ExecStart={sys.executable} -m portal_mcp_server agent run"
        in service_unit
    )
    assert agent_config == {
        "socket_path": str(runtime / "portal-mcp-server/credentials.sock"),
    }
    assert res["socket_path"] == agent_config["socket_path"]


def test_install_user_units_unit_basename_pinned():
    """Pin the systemd unit basename so an accidental rename is caught
    in CI rather than at a user's `systemctl --user enable` time."""
    from portal_mcp_server import credential_agent
    assert credential_agent.UNIT_BASENAME == "portal-credential-agent"
    assert credential_agent.SOCKET_UNIT == "portal-credential-agent.socket"
    assert credential_agent.SERVICE_UNIT == "portal-credential-agent.service"


def test_install_user_units_enable_now_runs_systemctl(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    calls: list[list[str]] = []

    def fake_run(args, check):
        calls.append(args)
        assert check is True
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(credential_agent.subprocess, "run", fake_run)

    credential_agent.install_user_units(enable_now=True)

    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", credential_agent.SOCKET_UNIT],
    ]


def test_uninstall_user_units_removes_units_and_config(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    credential_agent.install_user_units(enable_now=False)

    calls: list[list[str]] = []

    def fake_run(args, check):
        calls.append(args)
        assert check is False
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(credential_agent.subprocess, "run", fake_run)

    res = credential_agent.uninstall_user_units()

    assert calls == [
        ["systemctl", "--user", "disable", "--now", credential_agent.SOCKET_UNIT],
        ["systemctl", "--user", "stop", credential_agent.SERVICE_UNIT],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert res["errors"] == []
    assert not (home / ".config/systemd/user" / credential_agent.SOCKET_UNIT).exists()
    assert not (home / ".config/systemd/user" / credential_agent.SERVICE_UNIT).exists()
    assert not (home / ".config/portal-mcp-server/agent.json").exists()


def test_uninstall_user_units_can_keep_config(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    credential_agent.install_user_units(enable_now=False)

    res = credential_agent.uninstall_user_units(stop_now=False, remove_config=False)

    assert res["errors"] == []
    assert res["config_removed"] is False
    assert not (home / ".config/systemd/user" / credential_agent.SOCKET_UNIT).exists()
    assert not (home / ".config/systemd/user" / credential_agent.SERVICE_UNIT).exists()
    assert (home / ".config/portal-mcp-server/agent.json").exists()


def test_cli_exits_when_agent_is_unconfigured(monkeypatch, capsys):
    from portal_mcp_server import cli
    from portal_mcp_server.paths import CredentialAgentNotConfigured

    with pytest.raises(SystemExit):
        cli._agent_path_or_exit(
            lambda: (_ for _ in ()).throw(CredentialAgentNotConfigured("missing"))
        )

    captured = capsys.readouterr()
    assert "missing" in captured.err
    assert "agent install --now" in captured.err


def test_cli_exits_when_configured_socket_is_missing(monkeypatch, tmp_path, capsys):
    from portal_mcp_server import cli

    missing = tmp_path / "missing.sock"
    monkeypatch.setenv("PORTAL_CREDENTIAL_AGENT_SOCKET", str(missing))

    with pytest.raises(SystemExit):
        cli._agent_path_or_exit(lambda: missing)

    captured = capsys.readouterr()
    assert str(missing) in captured.err
    assert "agent install --now" in captured.err
