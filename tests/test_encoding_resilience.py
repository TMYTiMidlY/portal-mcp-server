"""Regression tests for the GBK / non-UTF-8 stdout bug.

Background
----------
Before the fix, ``asyncssh.SSHClientConnection.create_process`` was called
with the library default of ``errors='strict'``. If the remote command
emitted bytes that aren't valid UTF-8 — e.g. a ``powershell.exe`` reached
through WSL on a Chinese Windows host whose console codepage is 936 (GBK)
— asyncssh's internal stream decoder raised ``UnicodeDecodeError`` and the
SSH channel was torn down. ``execute_in_session`` swallowed the exception
and returned ``"Error: 'utf-8' codec can't decode byte 0xd3..."``, but
*every* subsequent call to that session failed with ``Channel not open for
sending`` until the caller manually invoked ``portal_bash_close``.

The fix is in three layers:

  1. ``connection_manager.DEFAULT_DECODE_ERRORS = "backslashreplace"`` is passed
     to every ``create_process`` / ``conn.run`` so undecodable bytes show up
     as ``\\xd3\\xd0`` escapes instead of killing the channel.
  2. ``session_manager.execute_in_session`` catches channel-level errors,
     removes the session from the registry, and raises a typed
     ``SessionDead`` (no more silent ``return "Error: ..."``).
  3. ``remote_bash.remote_bash`` catches ``SessionDead`` and transparently
     rebuilds the session, retrying the command once.

These tests pin all three pieces.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ─── Helpers ────────────────────────────────────────────────────────────────

class _FakeStdin:
    def __init__(self):
        self.written: list[str] = []
        self.fail_with: BaseException | None = None

    def write(self, data: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.written.append(data)


class _ScriptedStdout:
    """Yields a queue of chunks to ``.read()``; ``None`` raises ``exc``."""

    def __init__(self, chunks: list, exc: BaseException | None = None):
        self._chunks = list(chunks)
        self._exc = exc

    async def read(self, _size: int) -> str:
        if not self._chunks:
            # Simulate blocking forever — the caller's wait_for(0.3) will
            # time out and loop. Use a long sleep so the per-iteration
            # timeout fires before this resolves.
            await asyncio.sleep(10)
            return ""
        chunk = self._chunks.pop(0)
        if chunk is None and self._exc is not None:
            raise self._exc
        return chunk


class _FakeProc:
    def __init__(self, stdout_chunks: list, read_exc: BaseException | None = None):
        self.stdin = _FakeStdin()
        self.stdout = _ScriptedStdout(stdout_chunks, exc=read_exc)
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait(self) -> int:
        return 0


def _install_fake_conn(monkeypatch, proc_factory):
    """Make ``ConnectionManager.get_connection`` hand back a conn that
    produces ``proc_factory()`` on every ``create_process`` call.

    Returns a list that records each created _FakeProc instance, so tests
    can introspect what bytes were written / whether close() was called.
    """
    from portal_mcp_server import connection_manager

    procs: list[_FakeProc] = []
    create_kwargs: list[dict] = []

    class _FakeConn:
        async def create_process(self, *args, **kwargs):
            create_kwargs.append(kwargs)
            proc = proc_factory()
            procs.append(proc)
            return proc

    async def fake_get(self, host_name):
        return _FakeConn()

    def fake_release(self, host_name, conn):
        pass

    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "release_connection", fake_release)
    # The real create_session wraps its initial drain in wait_for(_, 5.0);
    # bypass it so tests don't spend 5s blocked on the never-empty fake
    # stdout. Same trick test_pool_leak_regression uses.
    monkeypatch.setattr(
        "portal_mcp_server.session_manager.asyncio.wait_for",
        lambda coro, timeout: coro,
    )
    return procs, create_kwargs


# ════════════════════════════════════════════════════════════════════════════
#  Layer 1: DEFAULT_DECODE_ERRORS is wired through to create_process
# ════════════════════════════════════════════════════════════════════════════

class TestDecodeErrorsKwarg:
    @pytest.mark.asyncio
    async def test_create_session_passes_errors_kwarg(self, monkeypatch):
        """create_session must forward errors='backslashreplace' to
        asyncssh so GBK / Latin-1 bytes don't kill the channel."""
        from portal_mcp_server import connection_manager, session_manager

        # The setup write loop tries to drain output; give it a single
        # empty chunk so the drain returns quickly.
        procs, create_kwargs = _install_fake_conn(
            monkeypatch, lambda: _FakeProc([""])
        )
        sm = session_manager.SessionManager()
        await sm.create_session("h")

        assert create_kwargs, "create_process was not called"
        assert create_kwargs[0].get("errors") == \
            connection_manager.DEFAULT_DECODE_ERRORS
        assert connection_manager.DEFAULT_DECODE_ERRORS == "backslashreplace"

    def test_shell_engine_passes_errors_kwarg(self):
        """Static check: shell_engine.ssh_exec passes errors= to conn.run."""
        import inspect
        from portal_mcp_server import shell_engine
        src = inspect.getsource(shell_engine.ssh_exec)
        assert "errors=DEFAULT_DECODE_ERRORS" in src, (
            "shell_engine.ssh_exec must pass errors=DEFAULT_DECODE_ERRORS to "
            "conn.run() to survive non-UTF-8 stdout from Windows hosts"
        )

    def test_remote_search_passes_errors_kwarg(self):
        """Static check: every conn.run in remote_search uses the kwarg."""
        import inspect
        from portal_mcp_server import remote_search
        src = inspect.getsource(remote_search)
        # Count the run() invocations and require errors=DEFAULT_DECODE_ERRORS
        # on each. The string "conn.run(" appears once per call site.
        run_calls = src.count("conn.run(")
        guarded = src.count("errors=DEFAULT_DECODE_ERRORS")
        assert run_calls > 0, "smoke check: remote_search should issue run()"
        assert guarded >= run_calls, (
            f"only {guarded}/{run_calls} conn.run() calls in remote_search "
            "pass errors=DEFAULT_DECODE_ERRORS"
        )


# ════════════════════════════════════════════════════════════════════════════
#  Layer 2: dead channel → SessionDead + registry eviction
# ════════════════════════════════════════════════════════════════════════════

class TestSessionDeath:
    @pytest.mark.asyncio
    async def test_stdin_write_failure_raises_session_dead(self, monkeypatch):
        """A broken stdin (channel closed by peer) must raise SessionDead
        and evict the session from the registry."""
        from portal_mcp_server import session_manager

        procs, _ = _install_fake_conn(monkeypatch, lambda: _FakeProc([""]))
        sm = session_manager.SessionManager()
        sid = await sm.create_session("h")

        # Arm the proc's stdin to fail on the next write.
        procs[-1].stdin.fail_with = BrokenPipeError("channel gone")

        with pytest.raises(session_manager.SessionDead) as excinfo:
            await sm.execute_in_session(sid, "echo hi")
        assert excinfo.value.session_id == sid
        assert isinstance(excinfo.value.original, BrokenPipeError)

        # Registry must have evicted the dead session so the next caller
        # gets a clean error instead of reusing a corpse.
        with pytest.raises(KeyError):
            sm._get(sid)

    @pytest.mark.asyncio
    async def test_decode_error_on_read_raises_session_dead(self, monkeypatch):
        """If a defense-in-depth UnicodeDecodeError still slips through
        (e.g. someone overrides DEFAULT_DECODE_ERRORS to 'strict'), the
        session must die cleanly instead of leaving a corpse in the
        registry."""
        from portal_mcp_server import session_manager

        # First create_session() reads one empty chunk to satisfy the drain.
        # Second use (execute_in_session) reads a chunk that raises.
        boom = UnicodeDecodeError("utf-8", b"\xd3\xd0", 0, 1, "invalid")
        # The first call to create_session triggers .read() once for drain.
        # We'll use a factory that returns a fresh proc per create_process call.
        call_count = {"n": 0}

        def factory():
            call_count["n"] += 1
            if call_count["n"] == 1:
                # The drain — empty chunk so drain returns
                return _FakeProc([""])
            return _FakeProc([])  # never used

        procs, _ = _install_fake_conn(monkeypatch, factory)
        sm = session_manager.SessionManager()
        sid = await sm.create_session("h")

        # Replace this session's stdout so the next read raises.
        procs[-1].stdout = _ScriptedStdout([None], exc=boom)

        with pytest.raises(session_manager.SessionDead) as excinfo:
            await sm.execute_in_session(sid, "echo hi", timeout=2.0)
        assert isinstance(excinfo.value.original, UnicodeDecodeError)
        with pytest.raises(KeyError):
            sm._get(sid)


# ════════════════════════════════════════════════════════════════════════════
#  Layer 3: remote_bash transparent auto-recovery on SessionDead
# ════════════════════════════════════════════════════════════════════════════

class TestRemoteBashAutoRecover:
    @pytest.mark.asyncio
    async def test_session_dead_triggers_silent_recreation(self, monkeypatch):
        """If the cached session dies mid-call, remote_bash should rebuild
        it transparently and return a non-error result on the retry —
        the agent should never see 'Channel not open for sending'."""
        from portal_mcp_server import remote_bash, session_manager

        # Clear the per-host caches so this test is hermetic.
        remote_bash._HOST_SESSIONS.clear()
        remote_bash._HOST_LOCKS.clear()

        calls = {"n": 0}

        async def fake_ensure(host):
            calls["n"] += 1
            return f"sid-{calls['n']}"

        async def fake_execute(self, sid, cmd, timeout=60.0):
            # Only the FIRST execute call dies — the recreated session
            # serves the retry happily.
            if sid == "sid-1":
                raise session_manager.SessionDead(
                    sid, BrokenPipeError("channel gone")
                )
            return f"OK from {sid}", 0

        monkeypatch.setattr(remote_bash, "_ensure_session", fake_ensure)
        monkeypatch.setattr(
            session_manager.SessionManager,
            "execute_in_session", fake_execute,
        )

        result = await remote_bash.remote_bash("h", "echo hi")
        assert result["output"] == "OK from sid-2"
        assert result["session_id"] == "sid-2"
        # Confirmed: _ensure_session called twice (once originally,
        # once after the death).
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_persistent_failure_propagates(self, monkeypatch):
        """If recreation *also* hits SessionDead (e.g. the host is just
        down), the second exception should propagate — we don't want an
        infinite retry loop."""
        from portal_mcp_server import remote_bash, session_manager

        remote_bash._HOST_SESSIONS.clear()
        remote_bash._HOST_LOCKS.clear()

        async def fake_ensure(host):
            return f"sid-{uuid.uuid4().hex[:4]}"

        async def fake_execute(self, sid, cmd, timeout=60.0):
            raise session_manager.SessionDead(
                sid, ConnectionResetError("network down")
            )

        monkeypatch.setattr(remote_bash, "_ensure_session", fake_ensure)
        monkeypatch.setattr(
            session_manager.SessionManager,
            "execute_in_session", fake_execute,
        )

        with pytest.raises(session_manager.SessionDead):
            await remote_bash.remote_bash("h", "echo hi")
