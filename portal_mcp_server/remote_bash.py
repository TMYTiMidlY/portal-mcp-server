"""remote_bash — single persistent shell session per host with auto-resume.

Wraps session_manager so the agent gets a "feels-local" shell on the remote
host: cwd and env survive across calls automatically (the underlying ``bash
-i`` / ``zsh -i`` process is the same).

Command completion + exit codes ride on **OSC 133 (FinalTerm) Shell
Integration** — the same事实标准 iTerm2 / VS Code's integrated terminal use. The
shell itself emits ``\\x1b]133;D;<exit>\\x07`` after every command via a
``PROMPT_COMMAND`` / ``precmd`` hook that this module injects once over stdin
(never written to disk). See ``session_manager`` for the protocol details.

Tools / entry points:
  - remote_bash(host, cmd, timeout?)          -> single-command result dict
  - remote_bash_many(host, cmds, …)           -> multi-step result dict
  - remote_bash_close(host)                    -> close the cached session
  - remote_bash_status()                       -> list cached sessions
  - remote_sudo_exec / remote_exec_with_env    -> one-shot credentialed paths
    (unchanged; these do NOT use the persistent session)

Concurrency note
----------------
The lock dict below is per-host, so independent hosts don't block each other
while the *first* call for a fresh host pays the session-startup cost. Within a
host, the session's own ``_read_lock`` serializes command execution on the
shared PTY channel; a multi-step batch holds that lock for its whole duration
so an interleaved single call can't desync its D-marker stream.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List

from .session_manager import (
    BashRequired,
    InteractivePromptBlocked,
    OSC133_INTEGRATION_SCRIPTS,
    SUPPORTED_SHELLS,
    SessionDead,
    get_session_manager,
)
from .safety import strip_ansi

logger = logging.getLogger("portal_mcp.remote_bash")

# Guidance returned (never raised) when a remote_shell command wedged on an
# interactive prompt. The persistent PTY has no input channel, so the fix for a
# sudo command is the one-shot sudo path, not a retry (which would wedge again).
# Unlike the old sentinel scheme the session is NOT destroyed: the command is
# Ctrl-C'd and the shell — with its cwd / env / functions — keeps running.
_INTERACTIVE_BLOCKED_GUIDANCE = (
    "This command wedged on an interactive prompt that reads from the terminal "
    "(most often sudo asking for a password, but also ssh's first-connect "
    "host-key/password prompt, mysql -p, read, or a gpg passphrase unlock). "
    "remote_shell's persistent PTY has no channel to feed user input, so the "
    "command was automatically Ctrl-C'd. The session and its cwd / env / shell "
    "functions are PRESERVED — the next non-interactive command can run straight "
    "away. To run a privileged command use "
    "remote_exec(host=..., command=..., use_sudo=True): it runs one-shot and "
    "feeds the stored sudo password to `sudo -S -k` on stdin. If no password is "
    "stored yet, ask the user to run `portal sudo set <host>` in a separate "
    "terminal first (never have them paste the password into the conversation)."
)

# host -> session_id mapping for the agent's "default" session per host
_HOST_SESSIONS: Dict[str, str] = {}
# Per-host async lock; created on first access. setdefault makes the lazy
# init race-safe under CPython.
_HOST_LOCKS: Dict[str, asyncio.Lock] = {}

# One-shot probe of the host's default shell + bash availability. Wrapped in
# ``sh -c`` so ``command -v`` is evaluated by a POSIX shell (a fish *login*
# shell would otherwise mishandle it); ``$SHELL`` is read from the inherited
# environment. Output: line 1 = $SHELL path, line 2 = bash path (or empty).
_SHELL_PROBE = (
    r"""sh -c 'printf "%s\n" "${SHELL:-}"; command -v bash 2>/dev/null || true'"""
)


def _lock_for(host: str) -> asyncio.Lock:
    return _HOST_LOCKS.setdefault(host, asyncio.Lock())


# ANSI / CSI / OSC stripping lives in safety.strip_ansi (single source of truth,
# shared with session_manager so the two passes can't drift apart). It is needed
# because the persistent session runs an interactive shell under a PTY which
# emits CSI/OSC + bracketed-paste markers even with `stty -echo`; the one-shot
# exec path uses conn.run WITHOUT a PTY and needs none of this.


def _clean(output: str) -> str:
    text = strip_ansi(output)
    # Drop leading/trailing blank lines.
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


async def _detect_remote_shell(host: str) -> str:
    """Probe the host's default shell. Returns a supported shell name
    ('bash'/'zsh'), or 'bash' as the fallback for any other shell that has bash
    available. Raises ``BashRequired`` only when the shell is unsupported AND
    bash is genuinely absent.
    """
    from .connection_manager import DEFAULT_DECODE_ERRORS, get_manager

    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        result = await conn.run(_SHELL_PROBE, check=False,
                                errors=DEFAULT_DECODE_ERRORS)
    finally:
        mgr.release_connection(host, conn)

    lines = (result.stdout or "").splitlines()
    shell_path = lines[0].strip() if len(lines) > 0 else ""
    bash_path = lines[1].strip() if len(lines) > 1 else ""
    basename = shell_path.rsplit("/", 1)[-1]
    if basename in SUPPORTED_SHELLS:
        return basename
    # Unsupported default shell (fish / dash / sh / …) → fall back to bash, but
    # only if bash actually exists; otherwise the persistent session can't run.
    if bash_path:
        return "bash"
    raise BashRequired(host)


async def _setup_session(host: str) -> str:
    """Create + bootstrap a persistent OSC 133 session on ``host``.

    Sniffs the remote shell, spawns the matching interactive shell with rc files
    disabled, injects the OSC 133 integration script over stdin, and blocks
    until the shell is ready (bootstrap readiness marker seen). The session is
    left at a clean prompt with cwd = $HOME and the agent's env.
    """
    smgr = get_session_manager()
    shell = await _detect_remote_shell(host)
    sid = await smgr.create_session(host, shell=shell)
    await smgr.bootstrap_osc133(sid, OSC133_INTEGRATION_SCRIPTS[shell])
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


def _interactive_blocked_result(host: str, cmd: str,
                                blocked: InteractivePromptBlocked,
                                sid: str, t0: float) -> Dict[str, object]:
    """Structured fast-fail result for a command that wedged on an interactive
    prompt and was soft-cancelled.

    Returned (not raised) so remote_shell hands the agent an exit_code + clear
    guidance immediately instead of a ``[timeout]`` after up to an hour. The
    session is PRESERVED (``session_preserved: true``): the wedged command was
    Ctrl-C'd and the shell verified back at a clean prompt, so cwd / env / shell
    functions all survive and the host->sid mapping is intentionally KEPT.
    """
    return {"host": host, "session_id": sid, "command": cmd,
            "exit_code": -1, "output": _clean(blocked.output),
            "duration_s": round(time.monotonic() - t0, 3),
            "error": "interactive_prompt_blocked",
            "session_preserved": True,
            "guidance": _INTERACTIVE_BLOCKED_GUIDANCE}


async def remote_bash(host: str, cmd: str, timeout: float = 3600.0) -> Dict[str, object]:
    """Run a single command in the persistent shell session for <host>.

    cwd and env vars are preserved across calls.

    Returns ``{host, session_id, command, exit_code, output, duration_s}`` (plus
    ``truncated: true`` if the output exceeded the cap). ``exit_code`` is the
    remote ``$?`` (``None`` only if the command timed out before completing;
    ``-2`` for a FinalTerm "aborted" marker). ``output`` is the combined
    stdout/stderr stream (a PTY merges them; use the one-shot exec path when you
    need them split).

    If the cached session's SSH channel has died (remote shell exited, TCP
    dropped, codec failure, …) this transparently recreates the session and
    retries the command **once** so the agent doesn't have to call
    ``remote_close`` and reissue.

    If the command wedged on an interactive prompt (sudo / ssh / passphrase /
    …) it is Ctrl-C'd — not retried — and the result carries ``exit_code: -1``,
    ``error: "interactive_prompt_blocked"``, ``session_preserved: true`` and a
    ``guidance`` field. The session and its cwd/env stay alive; the next
    non-interactive command can run straight away.
    """
    smgr = get_session_manager()
    sid = await _ensure_session(host)
    t0 = time.monotonic()
    try:
        raw, code, truncated = await smgr.execute_in_session(sid, cmd, timeout=timeout)
    except InteractivePromptBlocked as blocked:
        return _interactive_blocked_result(host, cmd, blocked, sid, t0)
    except SessionDead as dead:
        logger.warning(
            "remote_bash: session %s on host %s died (%s); recreating once",
            dead.session_id, host, dead.original,
        )
        # _invalidate already dropped the session from the registry; clear our
        # host->sid cache too so _ensure_session creates a fresh one.
        async with _lock_for(host):
            if _HOST_SESSIONS.get(host) == dead.session_id:
                _HOST_SESSIONS.pop(host, None)
        sid = await _ensure_session(host)
        try:
            raw, code, truncated = await smgr.execute_in_session(sid, cmd, timeout=timeout)
        except InteractivePromptBlocked as blocked:
            return _interactive_blocked_result(host, cmd, blocked, sid, t0)
    result: Dict[str, object] = {
        "host": host, "session_id": sid, "command": cmd,
        "exit_code": code, "output": _clean(raw),
        "duration_s": round(time.monotonic() - t0, 3),
    }
    if truncated:
        result["truncated"] = True
    return result


async def remote_bash_many(host: str, cmds: List[str], stop_on_error: bool = True,
                           timeout: float = 3600.0) -> Dict[str, object]:
    """Run a sequence of commands in ONE persistent session, in order, with
    cwd / env / shell functions carried across them.

    This is the multi-step counterpart to ``remote_bash``: ``cd`` /
    ``export`` / ``source venv/bin/activate`` in an earlier command are visible
    to later ones (unlike ``remote_exec``'s multi-command path, which opens a
    fresh channel + shell per step and keeps no state between them).

    The whole batch holds the session's ``_read_lock`` so a concurrent single
    call on the same host can't interleave a command and desync the D-marker
    stream. ``timeout`` is **per command**, matching single-step semantics.

    Returns ``{host, session_id, results: [...], duration_s}`` where each result
    is ``{command, exit_code, output[, truncated][, error, session_preserved]}``.
    With ``stop_on_error=True`` (default) the batch stops at the first command
    whose exit code is non-zero / unknown / interactive-blocked, and the result
    carries ``stopped_at: <that command>``.
    """
    smgr = get_session_manager()
    sid = await _ensure_session(host)
    try:
        session = smgr._get(sid)  # noqa: SLF001
    except KeyError:
        # Vanished between ensure and lookup — rebuild once.
        async with _lock_for(host):
            _HOST_SESSIONS.pop(host, None)
        sid = await _ensure_session(host)
        session = smgr._get(sid)  # noqa: SLF001

    t0 = time.monotonic()
    results: List[Dict[str, object]] = []
    stopped_at: "str | None" = None

    # Hold the lock for the WHOLE batch: releasing between commands would let an
    # interleaved single call steal a D and shift every subsequent command's
    # exit code by one.
    async with session._read_lock:  # noqa: SLF001
        for cmd in cmds:
            try:
                raw, code, truncated = await smgr._execute_locked(  # noqa: SLF001
                    session, cmd, timeout)
            except InteractivePromptBlocked as blocked:
                results.append({
                    "command": cmd, "exit_code": -1,
                    "output": _clean(blocked.output),
                    "error": "interactive_prompt_blocked",
                    "session_preserved": True,
                })
                if stop_on_error:
                    stopped_at = cmd
                    break
                continue
            except SessionDead as dead:
                # The session was invalidated under us; we can't continue the
                # batch on a dead channel (and recreating would lose the cwd/env
                # continuity that is the whole point of multi-step), so stop.
                results.append({
                    "command": cmd, "exit_code": -1,
                    "error": "session_dead",
                    "output": f"session died: {dead.original!r}",
                })
                async with _lock_for(host):
                    if _HOST_SESSIONS.get(host) == dead.session_id:
                        _HOST_SESSIONS.pop(host, None)
                stopped_at = cmd
                break
            entry: Dict[str, object] = {
                "command": cmd, "exit_code": code, "output": _clean(raw),
            }
            if truncated:
                entry["truncated"] = True
            results.append(entry)
            if stop_on_error and (code is None or code != 0):
                stopped_at = cmd
                break

    out: Dict[str, object] = {
        "host": host, "session_id": sid, "results": results,
        "duration_s": round(time.monotonic() - t0, 3),
    }
    if stopped_at is not None:
        out["stopped_at"] = stopped_at
    return out


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


async def _run_sudo_raw(host: str, command: str, password: str, *,
                        stdin_extra: str = "", encoding: str = "utf-8",
                        timeout: float = 3600.0):
    """Low-level one-shot sudo primitive shared by the exec and file-read paths.

    Runs ``sudo -S -k -p '' <command>`` on <host> with the sudo password fed on
    stdin, and returns the **raw** asyncssh completed process (``.returncode`` /
    ``.stdout`` / ``.stderr``) with NO output post-processing. The caller decides
    whether to strip trailing newlines:

    * command execution strips them (shell convention — ``$(cmd)`` does the same);
    * a byte-exact file read (``cat``) must **never** strip, or the content's
      SHA-256 won't match and ``remote_patch``'s hash precondition breaks.

    ``-p ''`` suppresses the prompt; ``-k`` forces fresh auth so a cached sudo
    ticket can't mask a wrong password. ``stdin_extra`` is appended after the
    password + newline (e.g. secret values read back inside the elevated shell).
    The password never lands on argv (stdin only) and is never logged. Raises
    :class:`asyncio.TimeoutError` on timeout (caller formats the result)."""
    from .connection_manager import DEFAULT_DECODE_ERRORS, get_manager

    mgr = get_manager()
    conn = await mgr.get_connection(host)
    wrapped = f"sudo -S -k -p '' {command}"
    stdin_data = password + "\n" + stdin_extra
    try:
        return await asyncio.wait_for(
            conn.run(wrapped, input=stdin_data, check=False, encoding=encoding,
                     errors=DEFAULT_DECODE_ERRORS),
            timeout=timeout,
        )
    finally:
        mgr.release_connection(host, conn)


async def remote_sudo_exec(host: str, command: str, password: str,
                           env: "dict | None" = None,
                           timeout: float = 3600.0) -> Dict[str, object]:
    """Run a command under ``sudo`` on <host>, feeding the password via stdin.

    Uses a one-shot ``conn.run`` (not the persistent session): ``sudo -S`` reads
    the password from stdin, which would collide with the persistent shell's
    boundary protocol. cwd/env from the persistent ``remote_shell`` session
    therefore do NOT apply here.

    ``env`` (already-resolved ``ENV_VAR_NAME -> value`` secrets) is injected
    WITHOUT relying on sudoers ``env_keep``: each value is fed on stdin right
    after the password and read back inside the elevated shell (see
    :func:`secrets_store.sudo_stdin_secret_script`), so it survives sudo's
    ``env_reset`` and never lands on argv.

    Returns ``{host, command, exit_code, stdout, stderr, elapsed_s}`` — the
    streams are split (sudo auth failures land on stderr). Output has its
    trailing newlines stripped (shell convention); do NOT reuse this for a
    byte-exact read — see :func:`_run_sudo_raw`.
    """
    from .safety import quote_shell
    from . import secrets_store

    env = env or {}
    names = list(env.keys())
    body = secrets_store.sudo_stdin_secret_script(command, names)
    wrapped = f"-- bash -c {quote_shell(body)}"
    stdin_extra = secrets_store.sudo_stdin_secret_values([env[n] for n in names])
    t0 = time.monotonic()
    try:
        result = await _run_sudo_raw(host, wrapped, password,
                                     stdin_extra=stdin_extra, timeout=timeout)
    except asyncio.TimeoutError:
        return {"host": host, "command": command, "exit_code": -1, "stdout": "",
                "stderr": f"[timeout] sudo command timed out after {timeout}s",
                "elapsed_s": round(time.monotonic() - t0, 3)}

    return {"host": host, "command": command, "exit_code": result.returncode,
            "stdout": (result.stdout or "").rstrip("\n"),
            "stderr": (result.stderr or "").rstrip("\n"),
            "elapsed_s": round(time.monotonic() - t0, 3)}


async def remote_exec_with_env(host: str, command: str, env: dict,
                               timeout: float = 3600.0) -> dict:
    """Run a command on <host> with named secrets injected as env vars.

    The secret values are fed to a one-shot ``bash -s`` on **stdin** as
    ``export VAR=<value>`` lines followed by the command, so the values never
    appear on argv (out of ``ps``) and never reach the audit log. Like
    :func:`remote_sudo_exec` this is a one-shot ``conn.run`` (not the persistent
    session): the command's own stdin is therefore already at EOF (fine for
    ``curl``/CLI tools that read flags, not stdin). cwd/env from prior
    ``remote_shell`` calls do NOT apply.

    ``env`` maps already-resolved ``ENV_VAR_NAME -> value``. Returns
    ``{host, command, exit_code, stdout, stderr, elapsed_s}``; the caller is
    responsible for redacting ``stdout`` / ``stderr``.
    """
    from .connection_manager import DEFAULT_DECODE_ERRORS, get_manager
    from .safety import quote_shell

    mgr = get_manager()
    conn = await mgr.get_connection(host)
    exports = "".join(
        f"export {name}={quote_shell(value)}\n" for name, value in env.items()
    )
    script = exports + command + "\n"
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            conn.run("bash -s", input=script, check=False,
                     errors=DEFAULT_DECODE_ERRORS),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"host": host, "command": command, "exit_code": -1, "stdout": "",
                "stderr": f"[timeout] command timed out after {timeout}s",
                "elapsed_s": round(time.monotonic() - t0, 3)}
    finally:
        mgr.release_connection(host, conn)

    return {"host": host, "command": command, "exit_code": result.returncode,
            "stdout": (result.stdout or "").rstrip("\n"),
            "stderr": (result.stderr or "").rstrip("\n"),
            "elapsed_s": round(time.monotonic() - t0, 3)}
