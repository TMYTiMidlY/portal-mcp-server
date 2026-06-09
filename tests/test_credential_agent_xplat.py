"""W-XPLAT: cross-platform credential-agent install dispatch.

The agent's wire protocol + unix-socket client work on Linux and macOS alike
(AF_UNIX; the same-uid peer check degrades to filesystem perms off Linux). The
only OS-specific piece is the *install* (which service manager keeps the agent
alive). These tests pin the platform dispatch, the macOS launchd LaunchAgent
generation, and the actionable error on platforms with no automated install.

(Real launchd activation needs a Mac, exactly as the systemd live path needs a
real systemd session — both are mocked here; the generated artifacts + dispatch
are fully asserted.)
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_xdg(monkeypatch):
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)


# ── platform detection ──────────────────────────────────────────────────────

@pytest.mark.parametrize("plat,expected", [
    ("linux", "systemd"),
    ("linux2", "systemd"),
    ("darwin", "launchd"),
    ("win32", "unsupported"),
    ("cygwin", "unsupported"),
])
def test_credential_agent_platform(monkeypatch, plat, expected):
    from portal_mcp_server import paths
    monkeypatch.setattr(paths.sys, "platform", plat)
    assert paths.credential_agent_platform() == expected


def test_unsupported_hint_is_actionable(monkeypatch):
    from portal_mcp_server import paths
    monkeypatch.setattr(paths.sys, "platform", "win32")
    hint = paths.credential_agent_unsupported_hint()
    assert "password_command" in hint
    assert "secrets.yaml" in hint
    assert "win32" in hint


def test_launchd_socket_path_uses_tmpdir(monkeypatch):
    from portal_mcp_server import paths
    monkeypatch.setenv("TMPDIR", "/var/folders/xx/T")
    p = paths.default_launchd_credential_agent_socket_path()
    assert str(p) == "/var/folders/xx/T/portal-mcp-server/credentials.sock"


# ── install dispatch ────────────────────────────────────────────────────────

def test_install_agent_dispatches_to_systemd(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent, paths
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    res = credential_agent.install_agent(enable_now=False)
    assert res["backend"] == "systemd"
    assert "socket_unit" in res


def test_install_agent_unsupported_raises_hint(monkeypatch):
    from portal_mcp_server import credential_agent, paths
    monkeypatch.setattr(paths.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="password_command"):
        credential_agent.install_agent(enable_now=False)


# ── macOS launchd LaunchAgent ───────────────────────────────────────────────

def test_launchd_plist_text_shape():
    from pathlib import Path
    from portal_mcp_server import credential_agent
    text = credential_agent._launchd_plist_text(
        Path("/tmp/p/credentials.sock"),
        ["py", "-m", "portal_mcp_server", "agent", "run", "--socket",
         "/tmp/p/credentials.sock"])
    assert f"<string>{credential_agent.LAUNCHD_LABEL}</string>" in text
    assert "<key>RunAtLoad</key>" in text and "<true/>" in text
    assert "<key>KeepAlive</key>" in text
    assert "<string>--socket</string>" in text
    assert "/tmp/p/credentials.sock" in text
    assert "PORTAL_CREDENTIAL_AGENT_SOCKET" in text


def test_install_launchd_writes_plist_and_config(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent, paths
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "run"))

    calls = []
    monkeypatch.setattr(credential_agent.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))

    res = credential_agent.install_agent(enable_now=True)
    assert res["backend"] == "launchd"
    plist = (home / "Library" / "LaunchAgents"
             / f"{credential_agent.LAUNCHD_LABEL}.plist")
    assert plist.exists()
    assert credential_agent.LAUNCHD_LABEL in plist.read_text()
    cfg = json.loads((home / ".config" / "portal-mcp-server"
                      / "agent.json").read_text())
    assert cfg["socket_path"].endswith("portal-mcp-server/credentials.sock")
    # enable_now=True -> launchctl load -w <plist>
    assert any("launchctl" in c[0] and "load" in c for c in calls)


def test_install_launchd_no_enable_skips_launchctl(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent, paths
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "run"))
    calls = []
    monkeypatch.setattr(credential_agent.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))
    credential_agent.install_agent(enable_now=False)
    assert calls == []


def test_uninstall_launchd_removes_plist(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent, paths
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "run"))
    monkeypatch.setattr(credential_agent.subprocess, "run", lambda *a, **k: None)

    credential_agent.install_agent(enable_now=False)
    plist = credential_agent.launchd_plist_path()
    assert plist.exists()
    res = credential_agent.uninstall_agent(stop_now=True, remove_config=True)
    assert res["backend"] == "launchd"
    assert not plist.exists()
    assert any("plist" in r for r in res["removed"])
