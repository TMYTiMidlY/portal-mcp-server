"""
Session Manager — persistent interactive shell sessions per host.
Each session maintains its own SSH channel, CWD, and env vars.
"""
import asyncio
import re
import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Optional

import asyncssh
from .connection_manager import DEFAULT_DECODE_ERRORS, get_manager
from .safety import quote_shell, strip_ansi, validate_env_dict, validate_env_key

logger = logging.getLogger("portal_mcp.sessions")
OUTPUT_BUFFER_LINES = 10000


class SessionDead(RuntimeError):
    """Raised by execute_in_session when the underlying SSH channel has died.

    The session has already been removed from the manager's registry by the
    time this is raised, so the caller (typically ``remote_bash``) can safely
    create a fresh session without first calling ``close_session``.
    """

    def __init__(self, session_id: str, original: BaseException):
        super().__init__(f"session {session_id} died: {original!r}")
        self.session_id = session_id
        self.original = original


@dataclass
class ShellSession:
    session_id: str
    host_name: str
    process: asyncssh.SSHClientProcess
    # The pooled SSH connection that backs ``process``. Stored here so
    # ``close_session`` can release the pool slot back to ConnectionManager;
    # without this reference we leak ``in_use`` counters and the pool grows
    # unboundedly under sustained portal_shell usage.
    conn: asyncssh.SSHClientConnection
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    output_buffer: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    # Serializes execute_in_session on this session's single shared PTY channel:
    # one ``bash -i`` cannot run two foreground commands at once, and concurrent
    # readers would otherwise split the byte stream and steal each other's
    # completion sentinel (wrong exit code / spurious timeout).
    _read_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self):
        self.last_used = time.time()


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, ShellSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, host_name: str, env: dict = None) -> str:
        """Spawn a persistent shell session on a remote host."""
        env = validate_env_dict(env)
        mgr = get_manager()
        conn = await mgr.get_connection(host_name)
        try:
            process = await conn.create_process(
                "bash -i", term_type="xterm-256color",
                env=env, request_pty=True,
                # See connection_manager.DEFAULT_DECODE_ERRORS — without this,
                # any non-UTF-8 byte on stdout (GBK from a Windows host,
                # Latin-1 from legacy tools, …) raises UnicodeDecodeError
                # inside asyncssh's stream reader and tears the channel down.
                errors=DEFAULT_DECODE_ERRORS,
            )
            session_id = str(uuid.uuid4())[:8]
            session = ShellSession(
                session_id=session_id,
                host_name=host_name,
                process=process,
                conn=conn,
                env=env,
            )
            # Drain initial prompt
            await asyncio.wait_for(self._drain(session), timeout=5.0)
        except BaseException:
            # Release the pool slot we just acquired before re-raising;
            # otherwise a failed session creation permanently consumes
            # one ``in_use`` counter and eventually exhausts the pool.
            mgr.release_connection(host_name, conn)
            raise
        async with self._lock:
            self._sessions[session_id] = session
        logger.info(f"Session {session_id} created on {host_name}")
        return session_id

    async def _drain(self, session: ShellSession, timeout: float = 0.5):
        """Read available output without blocking."""
        try:
            while True:
                chunk = await asyncio.wait_for(
                    session.process.stdout.read(4096), timeout=timeout
                )
                if not chunk:
                    break
                lines = chunk.splitlines()
                session.output_buffer.extend(lines)
                if len(session.output_buffer) > OUTPUT_BUFFER_LINES:
                    session.output_buffer = session.output_buffer[-OUTPUT_BUFFER_LINES:]
        except (asyncio.TimeoutError, asyncssh.ProcessError):
            pass

    async def execute_in_session(self, session_id: str, command: str,
                                  timeout: float = 30.0) -> "tuple[str, int | None]":
        """Execute a command inside a persistent shell session.

        Returns ``(output, exit_code)`` where ``output`` is the command's
        combined stdout/stderr (PTY merges the two streams, so they cannot
        be separated here — use the one-shot exec path when you need them
        split) and ``exit_code`` is the remote ``$?``. ``exit_code`` is
        ``None`` only when the command timed out before the sentinel
        arrived (``output`` then carries a trailing ``[timeout]`` marker).

        Raises:
            SessionDead: the underlying SSH channel died (write failed,
                EOF, codec error, etc.). The session is removed from the
                registry before this is raised, so callers can rebuild
                without first calling ``close_session``.
        """
        session = self._get(session_id)
        session.touch()
        # ADR — why a sentinel, not asyncssh's native exit status: a persistent
        # `bash -i` runs many commands over ONE channel, and SSH only reports an
        # exit status when the channel/process *closes*. asyncssh's conn.run()
        # returns a native exit code precisely because it opens a fresh channel
        # per command — that is the one-shot model (= portal_exec). To keep
        # cwd/env across calls we reuse the channel, so we recover each command's
        # $? by echoing a unique sentinel after it.
        # Sentinel uses the FULL 128-bit uuid so a command whose stdout
        # happens to contain the prefix string can never be mistaken for
        # completion. The previous 32-bit prefix had a 1-in-4-billion
        # collision per call, which is fine for an isolated test but a
        # latent corruption source under bursty multi-host workloads.
        sentinel = f"__DONE_{uuid.uuid4().hex}__"
        # ``echo {sentinel}:$?`` captures the command's exit status right
        # after it runs, so the agent learns whether the command succeeded
        # instead of guessing from stdout. The regex requires a newline AFTER
        # the digits, so a sentinel line split mid-number across read chunks
        # (e.g. ``:13`` arriving before the trailing ``0`` of ``130``) never
        # yields a truncated exit code — we wait for the terminator.
        full_cmd = f"{command}\necho {sentinel}:$?\n"
        sentinel_re = re.compile(re.escape(sentinel) + r":(\d+)(?=[\r\n])")
        # Hold the per-session lock for the whole write+read cycle: the channel
        # is shared, so two concurrent calls to the same session must not
        # interleave their sentinels (one would consume the other's and the
        # victim returns the wrong exit code or a spurious timeout).
        async with session._read_lock:
            try:
                session.process.stdin.write(full_cmd)
            except (BrokenPipeError, ConnectionResetError, OSError,
                    asyncssh.ChannelOpenError, asyncssh.ConnectionLost) as e:
                # stdin write failed — channel is gone. Drop the session
                # from the registry so the next call creates a fresh one,
                # and surface a typed error so the caller (remote_bash) can
                # transparently retry.
                await self._invalidate(session_id)
                raise SessionDead(session_id, e) from e
            buf = ""
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    chunk = await asyncio.wait_for(
                        session.process.stdout.read(4096), timeout=0.3
                    )
                except asyncio.TimeoutError:
                    continue
                except (asyncssh.ChannelOpenError, asyncssh.ConnectionLost,
                        UnicodeDecodeError, ConnectionResetError, OSError) as e:
                    # Channel-level failure during read. With
                    # DEFAULT_DECODE_ERRORS='backslashreplace' the UnicodeDecodeError
                    # branch shouldn't fire — keep it as defense-in-depth in case
                    # someone overrides the encoding to a stricter setting.
                    await self._invalidate(session_id)
                    raise SessionDead(session_id, e) from e
                if not chunk:
                    # EOF — bash exited or channel half-closed. Session is no
                    # longer usable.
                    await self._invalidate(session_id)
                    raise SessionDead(session_id, EOFError("stdout EOF"))
                buf += chunk
                m = sentinel_re.search(buf)
                if m:
                    exit_code = int(m.group(1))
                    output = strip_ansi(buf[:m.start()]).replace("\r\n", "\n")
                    return output.rstrip("\n"), exit_code
            return strip_ansi(buf).replace("\r\n", "\n").rstrip("\n") + "\n[timeout]", None

    async def _invalidate(self, session_id: str) -> None:
        """Drop a session from the registry and release its pool slot.

        Used when the underlying channel is detected to be dead; we don't
        try to ``exit`` cleanly (the channel is already gone) but we do
        need to decrement the pool's in-use counter or it leaks.
        """
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        try:
            session.process.close()
        except Exception:  # pragma: no cover
            pass
        try:
            get_manager().release_connection(session.host_name, session.conn)
        except Exception:  # pragma: no cover
            logger.debug("invalidate: release_connection failed", exc_info=True)
        logger.info(f"Session {session_id} invalidated (channel dead)")

    def _strip_ansi(self, text: str) -> str:
        # Back-compat shim: delegates to the shared stripper. Kept so any
        # external caller of this private method still works; new code should
        # call safety.strip_ansi directly.
        return strip_ansi(text)

    def read_buffer(self, session_id: str, lines: int = 100) -> str:
        """Read recent output from session buffer."""
        session = self._get(session_id)
        return "\n".join(session.output_buffer[-lines:])

    def set_env(self, session_id: str, key: str, value: str):
        """Set an environment variable in the session."""
        validate_env_key(key)
        if not isinstance(value, str):
            raise ValueError("value must be a string")
        if "\x00" in value:
            raise ValueError("value contains NUL byte")
        session = self._get(session_id)
        session.env[key] = value
        # Use shlex.quote on the value so an attacker cannot break out of
        # the export argument with embedded quotes / `$()` / backticks.
        # ``key`` has already been restricted to [A-Za-z_][A-Za-z0-9_]*.
        session.process.stdin.write(f"export {key}={quote_shell(value)}\n")

    async def close_session(self, session_id: str) -> str:
        """Terminate a persistent shell session."""
        session = self._get(session_id)
        try:
            session.process.stdin.write("exit\n")
            await asyncio.wait_for(session.process.wait(), timeout=3.0)
        except Exception:
            session.process.close()
        finally:
            # Release the pool slot regardless of how the bash process
            # terminated. Without this ``in_use`` keeps creeping up and
            # ``ConnectionManager`` opens a brand-new TCP connection for
            # every subsequent ``portal_shell`` call once the per-conn cap
            # is reached.
            get_manager().release_connection(session.host_name, session.conn)
        async with self._lock:
            del self._sessions[session_id]
        logger.info(f"Session {session_id} closed")
        return f"Session {session_id} closed"

    async def close_all(self) -> int:
        """Close every live session and release its pool slot. Returns the count.

        Called at server shutdown (FastMCP lifespan). Best-effort: a failure on
        one session never blocks the others. The channels would be reaped by the
        OS/sshd on process exit anyway, so this is a clean-shutdown nicety, not a
        correctness requirement.
        """
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            try:
                s.process.close()
            except Exception:  # pragma: no cover - best effort
                pass
            try:
                get_manager().release_connection(s.host_name, s.conn)
            except Exception:  # pragma: no cover - best effort
                pass
        if sessions:
            logger.info("Closed %d shell session(s) on shutdown", len(sessions))
        return len(sessions)

    def list_sessions(self) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "host": s.host_name,
                "age_s": round(time.time() - s.created_at, 1),
                "idle_s": round(time.time() - s.last_used, 1),
                "buffer_lines": len(s.output_buffer),
            }
            for s in self._sessions.values()
        ]

    def _get(self, session_id: str) -> ShellSession:
        if session_id not in self._sessions:
            raise KeyError(f"Session '{session_id}' not found")
        return self._sessions[session_id]


_session_mgr: Optional[SessionManager] = None

def get_session_manager() -> SessionManager:
    global _session_mgr
    if _session_mgr is None:
        _session_mgr = SessionManager()
    return _session_mgr
