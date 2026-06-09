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
from pathlib import Path

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
    ("win32", "schtasks"),
    ("cygwin", "unsupported"),
    ("freebsd13", "unsupported"),
])
def test_credential_agent_platform(monkeypatch, plat, expected):
    from portal_mcp_server import paths
    monkeypatch.setattr(paths.sys, "platform", plat)
    assert paths.credential_agent_platform() == expected


def test_unsupported_hint_is_actionable(monkeypatch):
    from portal_mcp_server import paths
    monkeypatch.setattr(paths.sys, "platform", "freebsd13")
    hint = paths.credential_agent_unsupported_hint()
    assert "password_command" in hint
    assert "secrets.yaml" in hint
    assert "freebsd13" in hint


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
    monkeypatch.setattr(paths.sys, "platform", "freebsd13")
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


# ── Windows per-user logon scheduled task ───────────────────────────────────
#
# A real schtasks run needs Windows (the windows-latest CI job does an actual
# register/query/delete). Here we mock subprocess + monkeypatch the platform so
# the install/uninstall *logic* and the generated Task Scheduler XML are fully
# asserted on any OS.

def test_scheduled_task_xml_is_per_user_and_unkillable():
    from portal_mcp_server import credential_agent
    xml = credential_agent._scheduled_task_xml_text(
        r"C:\Python\pythonw.exe",
        r"-m portal_mcp_server agent run --socket \\.\pipe\portal-x",
        user_id=r"DESKTOP\me", task_name="portal-mcp-server-credential-agent")
    # Per-user: runs as the interactive logon user, least privilege, never SYSTEM.
    assert "<LogonTrigger>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert "<UserId>DESKTOP\\me</UserId>" in xml
    # Unkillable + keepalive: no 72h default kill, restart on crash.
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    assert "<RestartOnFailure>" in xml
    # The named pipe is carried through to the agent's argv (XML-escaped).
    assert "--socket \\\\.\\pipe\\portal-x" in xml


def test_install_agent_dispatches_to_schtasks(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent, paths
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))

    calls = []
    monkeypatch.setattr(credential_agent.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))

    res = credential_agent.install_agent(enable_now=False)
    assert res["backend"] == "schtasks"
    assert res["task_name"] == "portal-mcp-server-credential-agent"
    # The task was registered from the generated XML.
    assert any(c[:2] == ["schtasks", "/Create"] and "/XML" in c for c in calls)
    # enable_now=False -> no /Run.
    assert not any("/Run" in c for c in calls)
    # XML + config landed on disk, config records the named-pipe address.
    assert Path(res["task_xml"]).exists()
    cfg = json.loads(Path(res["config_path"]).read_text())
    assert cfg["socket_path"].startswith(r"\\.\pipe\portal-mcp-server-credentials")


def test_install_scheduled_task_enable_now_runs_task(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent, paths
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    calls = []
    monkeypatch.setattr(credential_agent.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))

    credential_agent.install_agent(enable_now=True)
    assert any(c[:2] == ["schtasks", "/Run"] for c in calls)


def test_uninstall_scheduled_task_deletes_and_cleans(monkeypatch, tmp_path):
    from portal_mcp_server import credential_agent, paths
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    calls = []
    monkeypatch.setattr(credential_agent.subprocess, "run",
                        lambda *a, **k: calls.append(a[0]))

    credential_agent.install_agent(enable_now=False)
    xml_path = credential_agent.scheduled_task_xml_path()
    assert xml_path.exists()
    res = credential_agent.uninstall_agent(stop_now=True, remove_config=True)
    assert res["backend"] == "schtasks"
    assert any(c[:2] == ["schtasks", "/Delete"] for c in calls)
    assert not xml_path.exists()
    assert any("credential-agent-task.xml" in r for r in res["removed"])
