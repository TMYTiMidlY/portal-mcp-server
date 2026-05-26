"""remote_bash — single persistent bash session per host with auto-resume.

Wraps server.session_manager so the agent gets a "feels-local" shell on the
remote host: cwd and env survive across calls automatically (the underlying
``bash -i`` process is the same).

Quirks handled:
  - PTY echo is disabled on session creation, otherwise the upstream
    sentinel-based completion detection matches the *echo* of the sentinel
    rather than the actual output.
  - PS1 / PS2 / PROMPT_COMMAND / bracketed-paste are silenced to keep
    stdout clean. PS2 in particular leaks `> ` markers into output when
    the agent uses heredocs or unclosed multi-line constructs.
  - Output is post-processed to strip residual ANSI escapes and the two
    bracketed-paste markers (``\\x1b[?2004l`` / ``\\x1b[?2004h``) that bash
    emits even with `stty -echo`.

Tools:
  - remote_bash(host, cmd, timeout?) -> {host, session_id, output}
  - remote_bash_close(host) -> close the cached session
  - remote_bash_status() -> list cached sessions

Concurrency note
----------------
The pre-fix implementation used a single global ``asyncio.Lock`` to guard
session lookup. That serialized every ``remote_bash`` call across every
host: ``remote_bash("a", ...)`` and ``remote_bash("b", ...)`` could not
proceed concurrently. The lock dict below is per-host, so independent hosts
no longer block each other while the *first* call for a fresh host pays the
session-startup cost.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict

from .session_manager import SessionDead, get_session_manager

logger = logging.getLogger("portal_mcp.remote_bash")

# host -> session_id mapping for the agent's "default" session per host
_HOST_SESSIONS: Dict[str, str] = {}
# Per-host async lock; created on first access. setdefault makes the lazy
# init race-safe under CPython.
_HOST_LOCKS: Dict[str, asyncio.Lock] = {}


def _lock_for(host: str) -> asyncio.Lock:
    return _HOST_LOCKS.setdefault(host, asyncio.Lock())

# Match all CSI escape sequences (covers bracketed-paste ?2004l/h, colors, cursor moves)
_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")


def _clean(output: str) -> str:
    text = _ANSI_OSC.sub("", _ANSI_CSI.sub("", output))
    # Drop leading/trailing blank lines; collapse runs of >1 blank line in the middle
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


async def _setup_session(host: str) -> str:
    smgr = get_session_manager()
    sid = await smgr.create_session(host)
    s = smgr._get(sid)  # noqa: SLF001 - intentional access
    # Silence echo and prompts so the sentinel-based completion detector in
    # SessionManager.execute_in_session works correctly.
    s.process.stdin.write(
        "stty -echo 2>/dev/null; "
        "export PS1=''; "
        "export PS2=''; "
        "export PROMPT_COMMAND=''; "
        "bind 'set enable-bracketed-paste off' 2>/dev/null\n"
    )
    await asyncio.sleep(0.3)
    await smgr._drain(s, timeout=0.5)  # noqa: SLF001
    return sid


async def _ensure_session(host: str) -> str:
    """Return a live session_id for the host, creating one if needed."""
    smgr = get_session_manager()
    async with _lock_for(host):
        sid = _HOST_SESSIONS.get(host)
        if sid is not None:
            try:
                smgr._get(sid)  # noqa: SLF001
                return sid
            except KeyError:
                _HOST_SESSIONS.pop(host, None)
        new_sid = await _setup_session(host)
        _HOST_SESSIONS[host] = new_sid
        logger.info(f"remote_bash: created default session {new_sid} for host {host}")
        return new_sid


async def remote_bash(host: str, cmd: str, timeout: float = 60.0) -> Dict[str, str]:
    """Run a command in the persistent bash session for <host>.

    cwd and env vars are preserved across calls.

    If the cached session's SSH channel has died (e.g. the remote shell
    exited, the TCP connection dropped, or an earlier command produced
    output the codec couldn't decode), this transparently recreates the
    session and retries the command **once** so the agent doesn't have
    to call ``portal_bash_close`` and reissue every time.
    """
    smgr = get_session_manager()
    sid = await _ensure_session(host)
    try:
        raw = await smgr.execute_in_session(sid, cmd, timeout=timeout)
    except SessionDead as dead:
        logger.warning(
            "remote_bash: session %s on host %s died (%s); recreating once",
            dead.session_id, host, dead.original,
        )
        # _invalidate already dropped the session from the registry; clear
        # our host->sid cache too so _ensure_session creates a fresh one.
        async with _lock_for(host):
            if _HOST_SESSIONS.get(host) == dead.session_id:
                _HOST_SESSIONS.pop(host, None)
        sid = await _ensure_session(host)
        raw = await smgr.execute_in_session(sid, cmd, timeout=timeout)
    return {"host": host, "session_id": sid, "output": _clean(raw)}


async def remote_bash_close(host: str) -> str:
    """Close the cached default session for a host (next call will reopen)."""
    smgr = get_session_manager()
    async with _lock_for(host):
        sid = _HOST_SESSIONS.pop(host, None)
    if sid is None:
        return f"No cached session for {host}"
    return await smgr.close_session(sid)


def remote_bash_status() -> Dict[str, str]:
    """Map of host -> cached session_id."""
    return dict(_HOST_SESSIONS)

