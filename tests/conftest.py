"""Shared pytest configuration / fixtures for portal-mcp-server.

Goals
-----
1. Register the ``ssh`` marker so the existing live-SSH tests can be skipped
   in environments without a reachable SSH server (CI, sandbox).
2. Auto-skip the live tests in ``test_live_ssh.py`` if ``PORTAL_TEST_LIVE`` is
   not set — they require a real SSH server and credentials.
3. Reset module-level singletons between tests so independent tests stay
   independent (the previous suite mutated the global ConnectionManager
   inside its module-level import, which leaked state across tests).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────────────────────
#  Test isolation — route EVERY portal-mcp-server runtime path (server log, audit
#  log, config yamls, job state, XDG dirs) into ONE gitignored in-repo directory,
#  set up HERE at conftest import, *before* any test module imports the package.
#
#  Why this can't be a fixture: cli.py and audit.py bind their FileHandler /
#  rotating-audit-handler from default_log_dir() at *import* time, so by the time
#  a function-scoped monkeypatch runs the handlers already point at the real
#  ~/.local/state/portal-mcp-server/log/ — i.e. the very audit.jsonl / server.log
#  the developer's live MCP server is writing. Pinning the paths before the first
#  import keeps the real ~/.config & ~/.local/state pristine across a test run.
#
#  PORTAL_* overrides are the cross-platform lever: paths._resolve() honours them
#  ahead of platformdirs on every OS, whereas XDG_* only steer platformdirs on
#  Linux (macOS/Windows ignore them). XDG_* are set too, for the few Linux
#  config/state-dir reads not behind a PORTAL_* knob (e.g. agent.json). setdefault
#  is used so a deliberate ambient override (CI / your shell) still wins.
#
#  NOT routed here: PORTAL_CREDENTIAL_AGENT_SOCKET (kept on a short /tmp path by
#  the agent_socket fixture — an in-repo path overflows macOS's 104-byte AF_UNIX
#  sun_path) and PORTAL_JOB_STATE_FILE / the socket are *also* re-pointed per-test
#  by the autouse fixtures below for stronger per-test isolation.
# ──────────────────────────────────────────────────────────────────────────────
_TEST_RUNTIME_ROOT = Path(__file__).resolve().parent.parent / ".pytest-portal"


def _isolate_runtime_paths() -> None:
    log_dir = _TEST_RUNTIME_ROOT / "log"
    cfg_dir = _TEST_RUNTIME_ROOT / "config"
    state_dir = _TEST_RUNTIME_ROOT / "state"
    xdg_cfg = _TEST_RUNTIME_ROOT / "xdg-config"
    xdg_state = _TEST_RUNTIME_ROOT / "xdg-state"
    for d in (log_dir, cfg_dir, state_dir, xdg_cfg, xdg_state):
        d.mkdir(parents=True, exist_ok=True)
    for key, value in {
        "PORTAL_LOG_DIR": str(log_dir),
        "PORTAL_HOSTS_YAML": str(cfg_dir / "hosts.yaml"),
        "PORTAL_POLICIES_YAML": str(cfg_dir / "policies.yaml"),
        "PORTAL_SECRETS_YAML": str(cfg_dir / "secrets.yaml"),
        "PORTAL_JOB_STATE_FILE": str(state_dir / "jobs.json"),
        "XDG_CONFIG_HOME": str(xdg_cfg),
        "XDG_STATE_HOME": str(xdg_state),
    }.items():
        os.environ.setdefault(key, value)


_isolate_runtime_paths()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "ssh: requires a reachable SSH server "
                  "(set PORTAL_TEST_LIVE=1 to run)"
    )


def pytest_collection_modifyitems(config, items):
    """Auto-skip live SSH tests unless PORTAL_TEST_LIVE is set."""
    if os.environ.get("PORTAL_TEST_LIVE", "").lower() in ("1", "true", "yes"):
        return
    skip = pytest.mark.skip(
        reason="live SSH test — set PORTAL_TEST_LIVE=1 to run"
    )
    for item in items:
        # Anything in the test_live_ssh.py module that exercises a real SSH
        # connection — i.e. all classes EXCEPT TestSecurity which is pure
        # in-memory policy.
        path = str(item.fspath)
        if path.endswith("test_live_ssh.py"):
            cls_name = getattr(item.parent, "name", "")
            if cls_name != "TestSecurity":
                item.add_marker(skip)


@pytest.fixture
def agent_socket(_isolate_credential_agent, tmp_path, monkeypatch):
    """Start a test credential agent on a private Unix socket.

    Declares `_isolate_credential_agent` as an explicit dependency so the
    later `setenv` below deterministically wins regardless of whether
    `_isolate_credential_agent` keeps its autouse/function scope. Without
    this dependency the override order relies on pytest's implicit rule
    "autouse runs before requested fixtures of the same scope", which is
    invisible to readers and silently inverts if the isolation fixture is
    ever refactored.
    """
    if sys.platform == "win32":
        pytest.skip(
            "AF_UNIX credential-agent fixture; Windows named-pipe transport is "
            "covered by test_credential_agent_namedpipe.py"
        )
    from portal_mcp_server import credential_agent

    # macOS AF_UNIX sun_path is 104 bytes, and pytest's tmp_path (under
    # $TMPDIR=/var/folders/...) overflows it → bind() raises "AF_UNIX path too
    # long" and the socket never appears. Bind under a short base instead so the
    # path fits on every POSIX platform (Linux's /tmp is short too).
    base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
    sock_dir = tempfile.mkdtemp(prefix="pmcp-", dir=base)
    sock = Path(sock_dir) / "a.sock"
    monkeypatch.setenv("PORTAL_CREDENTIAL_AGENT_SOCKET", str(sock))
    thread = threading.Thread(
        target=credential_agent.serve_forever,
        kwargs={"socket_path": sock},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sock.exists(), "credential agent socket never appeared"
    try:
        yield sock
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate_credential_agent(tmp_path, monkeypatch):
    """Avoid leaking a developer's real agent into unit tests."""
    monkeypatch.setenv("PORTAL_CREDENTIAL_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))


@pytest.fixture(autouse=True)
def _isolate_job_state(tmp_path, monkeypatch):
    """Point the background-job persistence file at a per-test tmp path so the
    JobManager can never read or clobber a developer's real ~/.local/state."""
    monkeypatch.setenv("PORTAL_JOB_STATE_FILE", str(tmp_path / "jobs.json"))


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Wipe module-level singletons before each test so tests cannot bleed
    state into one another (e.g. via the global ConnectionManager).
    """
    yield
    # Post-test cleanup: clear anything we know to be a process-wide cache.
    try:
        from portal_mcp_server import remote_bash as rb
        rb._HOST_SESSIONS.clear()
        rb._HOST_LOCKS.clear()
    except Exception:
        pass
    # Reset the background-job singleton so a persisted/in-memory table from one
    # test can't leak into the next.
    try:
        from portal_mcp_server import job_manager as _jm
        _jm._job_mgr = None
    except Exception:
        pass
    # Clear credential caches (sudo / ssh / key passphrase / named-secret).
    # Each is consulted on the SSH connect path or by command execution, so a
    # leaked entry from one test can silently override another test's
    # `password_command` / value resolution. Clearing here is cheap and makes
    # test order irrelevant.
    for mod_name, clearer in (
        ("portal_mcp_server.sudo_creds", "clear_sudo_password"),
        ("portal_mcp_server.ssh_creds", "clear_ssh_password"),
        ("portal_mcp_server.passphrase_creds", "clear_passphrase"),
        ("portal_mcp_server.secrets_store", "clear_secret"),
    ):
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            getattr(mod, clearer)()  # None -> clear all
        except Exception:
            pass
