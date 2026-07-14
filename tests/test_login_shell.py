"""#5 — remote_exec / remote_job run in a LOGIN shell (bash -lc) by default,
with graceful degrade on hosts without bash and a login=False escape.
"""
import types

import pytest

from portal_mcp_server import cli, connection_manager, job_manager, shell_engine


def _mock_conn(monkeypatch, recorder, bash="y"):
    """Patch the connection pool with a fake conn that answers the bash probe
    and records every OTHER command string passed to conn.run."""
    class _FakeConn:
        async def run(self, cmd, *a, **k):
            if "command -v bash" in cmd:
                return types.SimpleNamespace(stdout=bash, stderr="", returncode=0)
            recorder.append(cmd)
            return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    async def fake_get(self, host):
        return _FakeConn()

    def fake_release(self, host, conn):
        pass

    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "release_connection", fake_release)


@pytest.fixture(autouse=True)
def _clear_bash_cache():
    shell_engine._HOST_HAS_BASH.clear()
    yield
    shell_engine._HOST_HAS_BASH.clear()


# ── _login_shell_default env parsing (PORTAL_LOGIN_SHELL) ────────────────────
@pytest.mark.parametrize("val,expect", [
    ("", True), ("1", True), ("true", True), ("on", True), ("weird", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("OFF", False),
])
def test_login_shell_default(monkeypatch, val, expect):
    if val == "":
        monkeypatch.delenv("PORTAL_LOGIN_SHELL", raising=False)
    else:
        monkeypatch.setenv("PORTAL_LOGIN_SHELL", val)
    assert cli._login_shell_default() is expect


# ── _resolve_login precedence: per-call param > host login_shell > env ───────
def test_resolve_login_param_wins(monkeypatch):
    monkeypatch.setenv("PORTAL_LOGIN_SHELL", "0")  # env off
    assert cli._resolve_login(True, "nohost") is True
    assert cli._resolve_login(False, "nohost") is False


def test_resolve_login_host_over_env(monkeypatch):
    monkeypatch.setenv("PORTAL_LOGIN_SHELL", "0")  # env off

    class _M:
        def login_shell_for(self, name):
            return True

    monkeypatch.setattr(cli, "get_manager", lambda: _M())
    assert cli._resolve_login(None, "h") is True


def test_resolve_login_falls_back_to_env(monkeypatch):
    monkeypatch.delenv("PORTAL_LOGIN_SHELL", raising=False)

    class _M:
        def login_shell_for(self, name):
            return None

    monkeypatch.setattr(cli, "get_manager", lambda: _M())
    assert cli._resolve_login(None, "h") is True


# ── ssh_exec login wrap + graceful degrade ──────────────────────────────────
@pytest.mark.asyncio
async def test_ssh_exec_login_wraps_bash_lc(monkeypatch):
    rec: list = []
    _mock_conn(monkeypatch, rec, bash="y")
    await shell_engine.ssh_exec("h", "echo hi", timeout=5, login=True)
    assert rec and rec[0].startswith("bash -lc ")
    assert "echo hi" in rec[0]


@pytest.mark.asyncio
async def test_ssh_exec_login_degrades_without_bash(monkeypatch):
    rec: list = []
    _mock_conn(monkeypatch, rec, bash="n")
    await shell_engine.ssh_exec("h", "echo hi", timeout=5, login=True)
    assert rec == ["echo hi"]  # sh-only host → not wrapped


@pytest.mark.asyncio
async def test_ssh_exec_no_login_no_wrap(monkeypatch):
    rec: list = []
    _mock_conn(monkeypatch, rec, bash="y")
    await shell_engine.ssh_exec("h", "echo hi", timeout=5, login=False)
    assert rec == ["echo hi"]


# ── job_manager spawn uses bash -lc / bash -c per login ──────────────────────
@pytest.mark.asyncio
async def test_job_spawn_login_flag(monkeypatch):
    spawned: list = []

    class _FakeConn:
        async def run(self, cmd, *a, **k):
            spawned.append(cmd)
            return types.SimpleNamespace(stdout="12345", stderr="", returncode=0)

    async def fake_get(self, host):
        return _FakeConn()

    def fake_release(self, host, conn):
        pass

    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "release_connection", fake_release)

    jm = job_manager.JobManager()
    await jm._spawn_and_record("h", "sleep 1", login=True)
    assert spawned and "bash -lc " in spawned[0]

    spawned.clear()
    await jm._spawn_and_record("h", "sleep 1", login=False)
    assert "bash -c " in spawned[0] and "bash -lc " not in spawned[0]
