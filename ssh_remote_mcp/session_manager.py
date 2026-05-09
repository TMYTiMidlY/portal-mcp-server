"""
Session Manager — persistent interactive shell sessions per host.
Each session maintains its own SSH channel, CWD, and env vars.
"""
import asyncio
import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Optional

import asyncssh
from .connection_manager import get_manager

logger = logging.getLogger("ssh_mcp.sessions")
OUTPUT_BUFFER_LINES = 10000


@dataclass
class ShellSession:
    session_id: str
    host_name: str
    process: asyncssh.SSHClientProcess
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    output_buffer: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: str = "~"
    _read_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self):
        self.last_used = time.time()


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, ShellSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, host_name: str, env: dict = None) -> str:
        """Spawn a persistent shell session on a remote host."""
        mgr = get_manager()
        conn = await mgr.get_connection(host_name)
        env = env or {}
        process = await conn.create_process(
            "bash -i", term_type="xterm-256color",
            env=env, request_pty=True,
        )
        session_id = str(uuid.uuid4())[:8]
        session = ShellSession(
            session_id=session_id,
            host_name=host_name,
            process=process,
            env=env,
        )
        # Drain initial prompt
        await asyncio.wait_for(self._drain(session), timeout=5.0)
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
                                  timeout: float = 30.0) -> str:
        """Execute a command inside a persistent shell session."""
        session = self._get(session_id)
        session.touch()
        # Use a sentinel to detect command completion
        sentinel = f"__DONE_{uuid.uuid4().hex[:8]}__"
        full_cmd = f"{command}\necho {sentinel}\n"
        session.process.stdin.write(full_cmd)
        output_lines = []
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                try:
                    chunk = await asyncio.wait_for(
                        session.process.stdout.read(4096), timeout=0.3
                    )
                    if not chunk:
                        break
                    lines = chunk.splitlines()
                    for line in lines:
                        clean = self._strip_ansi(line)
                        if sentinel in clean:
                            return "\n".join(output_lines)
                        output_lines.append(clean)
                except asyncio.TimeoutError:
                    continue
        except Exception as e:
            return f"Error: {e}"
        return "\n".join(output_lines) + "\n[timeout]"

    def _strip_ansi(self, text: str) -> str:
        import re
        return re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', text)

    def read_buffer(self, session_id: str, lines: int = 100) -> str:
        """Read recent output from session buffer."""
        session = self._get(session_id)
        return "\n".join(session.output_buffer[-lines:])

    def set_env(self, session_id: str, key: str, value: str):
        """Set an environment variable in the session."""
        session = self._get(session_id)
        session.env[key] = value
        session.process.stdin.write(f"export {key}={value!r}\n")

    async def close_session(self, session_id: str) -> str:
        """Terminate a persistent shell session."""
        session = self._get(session_id)
        try:
            session.process.stdin.write("exit\n")
            await asyncio.wait_for(session.process.wait(), timeout=3.0)
        except Exception:
            session.process.close()
        async with self._lock:
            del self._sessions[session_id]
        logger.info(f"Session {session_id} closed")
        return f"Session {session_id} closed"

    def list_sessions(self) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "host": s.host_name,
                "cwd": s.cwd,
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
