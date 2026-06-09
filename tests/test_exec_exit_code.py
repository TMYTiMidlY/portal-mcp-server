"""P0: exec primitives must report exit codes + structured results.

The persistent-session path (``portal_bash`` → ``remote_bash`` →
``SessionManager.execute_in_session``) historically returned only combined
output with no exit status — the agent could not tell whether a command
succeeded. The one-shot exec path (``shell_engine.ssh_exec``) already
returns split stdout/stderr + exit_code. These tests pin both:

  * ``execute_in_session`` parses ``echo {sentinel}:$?`` and returns
    ``(output, exit_code)``, robust to the sentinel line being split across
    read chunks.
  * ``remote_bash`` surfaces ``exit_code`` / ``command`` / ``duration_s`` in
    its result dict.
  * ``ssh_exec`` returns ``exit_code`` + split ``stdout`` / ``stderr``.
"""
from __future__ import annotations

import uuid as _uuid

import pytest

from test_encoding_resilience import _FakeProc, _install_fake_conn


def _fixed_sentinel(monkeypatch):
    """Pin the uuid so the sentinel string is deterministic for scripting."""
    from portal_mcp_server import session_manager

    fixed = _uuid.UUID("00000000000000000000000000000abc")
    monkeypatch.setattr(session_manager.uuid, "uuid4", lambda: fixed)
    return f"__DONE_{fixed.hex}__"


# ════════════════════════════════════════════════════════════════════════════
#  Session path: execute_in_session returns (output, exit_code)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_execute_in_session_returns_exit_code(monkeypatch):
    from portal_mcp_server import session_manager

    sentinel = _fixed_sentinel(monkeypatch)
    # First chunk "" satisfies create_session's drain; the second carries the
    # command output followed by the sentinel + exit code.
    chunk = f"hello world\r\n{sentinel}:7\r\n"
    _install_fake_conn(monkeypatch, lambda: _FakeProc(["", chunk]))

    sm = session_manager.SessionManager()
    sid = await sm.create_session("h")
    out, code = await sm.execute_in_session(sid, "exit 7")

    assert code == 7
    assert "hello world" in out
    assert sentinel not in out, "the sentinel marker must be stripped from output"


@pytest.mark.asyncio
async def test_execute_in_session_zero_exit(monkeypatch):
    from portal_mcp_server import session_manager

    sentinel = _fixed_sentinel(monkeypatch)
    _install_fake_conn(monkeypatch, lambda: _FakeProc(["", f"{sentinel}:0\r\n"]))

    sm = session_manager.SessionManager()
    sid = await sm.create_session("h")
    out, code = await sm.execute_in_session(sid, "true")
    assert code == 0
    assert out == ""


@pytest.mark.asyncio
async def test_execute_in_session_sentinel_split_across_chunks(monkeypatch):
    """The exit code must not be truncated if the sentinel line is split by
    a read-buffer boundary (digits/newline arriving in a later chunk)."""
    from portal_mcp_server import session_manager

    sentinel = _fixed_sentinel(monkeypatch)
    # The ":42\r\n" tail arrives in a separate read than the sentinel itself.
    chunks = ["", f"out\r\n{sentinel}", ":42\r\n"]
    _install_fake_conn(monkeypatch, lambda: _FakeProc(chunks))

    sm = session_manager.SessionManager()
    sid = await sm.create_session("h")
    out, code = await sm.execute_in_session(sid, "exit 42")
    assert code == 42
    assert "out" in out


@pytest.mark.asyncio
async def test_execute_in_session_multidigit_code_split_mid_number(monkeypatch):
    """Regression: a chunk boundary falling BETWEEN the digits of a multi-digit
    exit code (e.g. ``:13`` before the trailing ``0`` of ``130``) must NOT
    return the truncated value — wait for the newline terminator."""
    from portal_mcp_server import session_manager

    sentinel = _fixed_sentinel(monkeypatch)
    # Real exit code is 130 (SIGINT); the buffer briefly holds "...:13".
    chunks = ["", f"{sentinel}:13", "0\r\n"]
    _install_fake_conn(monkeypatch, lambda: _FakeProc(chunks))

    sm = session_manager.SessionManager()
    sid = await sm.create_session("h")
    out, code = await sm.execute_in_session(sid, "exit 130")
    assert code == 130, "must not truncate 130 -> 13 on a mid-number chunk split"


@pytest.mark.asyncio
async def test_execute_in_session_timeout_returns_none_code(monkeypatch):
    import asyncio as _aio
    from portal_mcp_server import session_manager

    # Capture the real wait_for BEFORE _install_fake_conn replaces it with a
    # passthrough; the timeout path needs the genuine per-read 0.3s timeout
    # plus the outer deadline to fire.
    real_wait_for = _aio.wait_for
    _fixed_sentinel(monkeypatch)
    # No sentinel ever arrives → the read loop hits its deadline.
    _install_fake_conn(monkeypatch, lambda: _FakeProc(["", "partial output\r\n"]))
    monkeypatch.setattr(
        "portal_mcp_server.session_manager.asyncio.wait_for", real_wait_for)

    sm = session_manager.SessionManager()
    sid = await sm.create_session("h")
    out, code = await sm.execute_in_session(sid, "sleep 999", timeout=0.4)
    assert code is None
    assert "[timeout]" in out


# ════════════════════════════════════════════════════════════════════════════
#  remote_bash surfaces the richer structured result
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_remote_bash_result_shape(monkeypatch):
    from portal_mcp_server import remote_bash, session_manager

    remote_bash._HOST_SESSIONS.clear()
    remote_bash._HOST_LOCKS.clear()

    async def fake_ensure(host):
        return "sid-x"

    async def fake_execute(self, sid, cmd, timeout=60.0):
        return "command output", 3

    monkeypatch.setattr(remote_bash, "_ensure_session", fake_ensure)
    monkeypatch.setattr(session_manager.SessionManager,
                        "execute_in_session", fake_execute)

    res = await remote_bash.remote_bash("h", "exit 3")
    assert res["host"] == "h"
    assert res["session_id"] == "sid-x"
    assert res["command"] == "exit 3"
    assert res["exit_code"] == 3
    assert res["output"] == "command output"
    assert isinstance(res["duration_s"], float)


# ════════════════════════════════════════════════════════════════════════════
#  Exec path (ssh_exec) returns split stdout/stderr + exit_code
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ssh_exec_returns_split_streams_and_exit_code(monkeypatch):
    from portal_mcp_server import connection_manager, shell_engine

    class _Result:
        stdout = "the stdout"
        stderr = "the stderr"
        returncode = 5

    class _FakeConn:
        async def run(self, *a, **k):
            return _Result()

    async def fake_get(self, host):
        return _FakeConn()

    def fake_release(self, host, conn):
        pass

    monkeypatch.setattr(connection_manager.ConnectionManager, "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager, "release_connection", fake_release)

    res = await shell_engine.ssh_exec("h", "somecmd", timeout=5)
    assert res["exit_code"] == 5
    assert res["stdout"] == "the stdout"
    assert res["stderr"] == "the stderr"
    assert res["host"] == "h"
    assert res["command"] == "somecmd"
    assert "elapsed_s" in res
