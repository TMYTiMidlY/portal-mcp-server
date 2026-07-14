"""
Session Manager — persistent interactive shell sessions per host.
Each session maintains its own SSH channel, CWD, and env vars.

Command boundary protocol — OSC 133 (FinalTerm) Shell Integration
-----------------------------------------------------------------
A persistent ``bash -i`` (or ``zsh -i``) runs many commands over ONE SSH
channel, and SSH only reports an exit status when the channel/process *closes*.
To recover each command's ``$?`` without tearing the channel down we used to
append ``echo <sentinel>:$?`` after every command and scan stdout for the
sentinel — an *in-band* signalling scheme that was fundamentally fragile:

  * ``sudo`` (or any program that grabs stdin) swallowed the sentinel as a bogus
    password and the command hung until ``timeout`` (3600 s by default);
  * large output bloated an unbounded buffer;
  * a command whose stdout literally contained the sentinel string could be
    mistaken for completion.

We now let the *shell itself* emit the command boundary, exactly like iTerm2 /
VS Code's integrated terminal / Kitty / WezTerm. The integration script
(injected once over stdin, never written to disk — see
``OSC133_INTEGRATION_SCRIPTS``) registers a ``PROMPT_COMMAND`` / ``precmd`` hook
that prints

    ESC ] 133 ; D ; <exit> BEL          (\x1b]133;D;<digits>\x07)

after every command. We are now just a *parser*: we scan the raw byte stream for
that escape sequence (which requires an ESC byte, so ordinary text — even text
that literally spells ``]133;D;0`` — can never forge it) and read ``$?`` straight
out of it. The whole class of sentinel fragilities disappears at the root, and a
command that wedges on an interactive prompt becomes *recoverable*: we Ctrl-C it,
verify the shell came back (next D arrived) and keep the session — cwd / env /
shell functions intact.

The one-shot exec path (``remote_exec``) is unaffected: it opens a fresh channel
per command and gets a native exit code from asyncssh, so it never needed any of
this.
"""
import asyncio
import os
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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# Largest command output we keep in memory before truncating the *front* (the
# OSC 133 D marker always arrives at the very end, so dropping the head never
# loses it). The SSH-programming literature flags unbounded output capture as a
# classic OOM/DoS sink; this guard applies regardless of the boundary protocol.
MAX_OUTPUT_BYTES = _env_int("PORTAL_SHELL_MAX_OUTPUT", 8 * 1024 * 1024)
# How long bootstrap waits, overall, for the readiness marker + its D.
BOOT_TIMEOUT = _env_float("PORTAL_SHELL_BOOT_TIMEOUT", 10.0)
# Quiet window with no new output that marks the integration script as fully
# sourced (incl. `stty -echo`). Only THEN is the readiness marker sent — as a
# separate write the shell won't echo, so its text appears exactly once. Sending
# the marker inside the script payload instead lets the PTY echo the marker
# command line before `stty -echo` applies (notably under zsh), which duplicates
# the marker text and desyncs every later command by one. See bootstrap_osc133.
BOOT_QUIET = _env_float("PORTAL_SHELL_BOOT_QUIET", 0.6)
# After an interactive prompt is first seen, how long we wait (still scanning
# for a D, which wins) before concluding the command is genuinely wedged.
INTERACTIVE_GRACE_SEC = _env_float("PORTAL_SHELL_INTERACTIVE_GRACE", 1.0)
# After Ctrl-C'ing a wedged command, how long we wait for the next D (proof the
# shell returned to a clean prompt) before giving up and killing the session.
SOFT_CANCEL_TIMEOUT = _env_float("PORTAL_SHELL_SOFT_CANCEL_TIMEOUT", 3.0)

# Per-read poll window and chunk size for the streaming read loop.
_READ_POLL = 0.3
_READ_CHUNK = 65536
# When scanning only freshly-appended bytes, overlap back this far so a marker
# split across two reads is still matched whole.
_MARKER_OVERLAP = 64

# OSC 133 ; D [; <exit>] ST  — FTCS_COMMAND_FINISHED. ST is BEL (\x07): every
# terminal that speaks OSC 133 accepts the BEL-terminated form. The exit group
# is optional: FinalTerm allows a bare ``D`` meaning "the command was aborted".
OSC133_D = re.compile(rb"\x1b\]133;D(?:;(\d+))?\x07")

# Conservative whitelist of interactive prompts that read from the PTY and would
# otherwise wedge the command until ``timeout``. Bytes, matched on raw output.
# The zh_CN sudo prompt "的密码" is its UTF-8 bytes. Kept deliberately small:
# a false positive only costs the grace delay, but we'd rather under-match and
# let a rare uncommon prompt fall through to the timeout than over-match and
# disrupt ordinary commands.
INTERACTIVE_PROMPT_RE = re.compile(
    rb"\[sudo\][^\n]*(?:password for |\xe7\x9a\x84\xe5\xaf\x86\xe7\xa0\x81)"
    rb"|Password:"
    rb"|Enter passphrase for "
    rb"|Are you sure you want to continue connecting"
)

# Command line used to spawn each supported shell. ``--noprofile --norc`` /
# ``--no-rcs`` is mandatory: a user's own rc could clobber PROMPT_COMMAND /
# precmd or reset the DEBUG trap and silently break the boundary protocol — the
# injected integration script must be the *only* rc in effect.
SHELL_COMMAND_LINES = {
    "bash": "bash --noprofile --norc -i",
    "zsh": "zsh --no-rcs -i",
}

# OSC 133 integration scripts, injected once over stdin per session (never
# written to disk). Both validated on real hardware (bash 5.2 on a remote VPS,
# zsh 5.9 on macOS). Only the D sequence is used; A/B/C are emitted by the bash
# hook for parity with the FinalTerm spec but are stripped as ordinary OSC noise.
#
# zsh note: ``unsetopt zle`` is essential and was NOT in the original drafted
# zsh script — a real-machine spike (zsh 5.9) surfaced the bug. zsh's line
# editor (ZLE) echoes the typed command line back regardless of ``stty -echo``,
# so without disabling ZLE the command text leaks into every command's captured
# output. The bash hook needs no equivalent because bash's readline honours
# ``stty -echo``; this is why bash was clean from the start and only zsh tripped.
#
# fish is intentionally absent: its ``fish_postexec`` hook is the documented
# equivalent, but no fish host was reachable to validate it, and shipping an
# unverified interactive-shell integration risks silent desync. fish hosts fall
# back to bash (see remote_bash._detect_remote_shell); add a "fish" entry here
# once it has been spiked on real hardware.
OSC133_INTEGRATION_SCRIPTS = {
    "bash": (
        "__p133_pre()  { printf '\\033]133;A\\007'; }\n"
        "__p133_cmd()  { printf '\\033]133;B\\007'; }\n"
        "__p133_exec() { printf '\\033]133;C\\007'; }\n"
        "__p133_done() { printf '\\033]133;D;%d\\007' \"$?\"; }\n"
        "trap '__p133_exec' DEBUG\n"
        "PROMPT_COMMAND='__p133_done; __p133_pre; __p133_cmd'\n"
        "PS1='' ; PS2=''\n"
        "stty -echo 2>/dev/null\n"
        "bind 'set enable-bracketed-paste off' 2>/dev/null\n"
    ),
    "zsh": (
        "__p133_done() { printf '\\033]133;D;%d\\007' \"$?\"; }\n"
        "precmd_functions+=(__p133_done)\n"
        "PS1='' ; PS2='' ; PROMPT='' ; RPROMPT=''\n"
        "unsetopt zle\n"
        "setopt no_prompt_cr no_prompt_sp\n"
        "stty -echo 2>/dev/null\n"
    ),
}

# Shells with a validated, activated integration script. Anything else a host
# might report as its default shell falls back to bash.
SUPPORTED_SHELLS = tuple(SHELL_COMMAND_LINES.keys())


def _wrap_compound(command: str) -> str:
    """Wrap a multi-line command so an interactive shell runs it as ONE
    compound command, emitting a single OSC 133 ``D`` boundary marker.

    Interactive bash / zsh fire ``PROMPT_COMMAND`` / ``precmd`` — and therefore
    the ``D`` completion marker this module keys on — after *every* top-level
    input line. A ``command`` string with embedded newlines would thus emit one
    ``D`` per line, and the reader (which returns at the *first* ``D``) would run
    only the first line, leave the rest queued in the PTY, and desync every later
    call by one marker. Enclosing the whole string in a brace group ``{ … }``
    keeps the shell in PS2 continuation until the matching ``}``, so the prompt —
    and the marker — fire exactly once for the entire command.

    A brace group (unlike a ``( … )`` subshell) executes in the *current* shell,
    so ``cd`` / ``export`` and other state still persist across calls — the whole
    point of a persistent session. The ``$?`` reported in the ``D`` marker is the
    group's last command's exit status, matching what a one-shot exec of the same
    script returns.

    Single-line commands (no embedded newline) are returned unchanged: they
    already produce exactly one marker, so the common path stays byte-for-byte
    identical and well-exercised ``a; b; c`` joins are unaffected.
    """
    if "\n" not in command:
        return command
    # Trailing newlines are dropped so the closing brace sits on its own line
    # right after the final command; the leading "{\n" opens the group.
    return "{\n" + command.rstrip("\n") + "\n}"



class InteractivePromptBlocked(RuntimeError):
    """Raised by execute_in_session when a command wedged on an interactive
    prompt that reads from the PTY — the most common is ``sudo`` asking for a
    password, but it also covers ``ssh`` host-key/password prompts, ``mysql
    -p``, ``read``-with-prompt, gpg passphrase unlock, etc.

    remote_shell's persistent PTY has no channel to feed such input, so the
    command would otherwise hang until ``timeout``. Unlike the old
    ``SudoPromptBlocked`` we do **not** destroy the session: the wedged command
    is Ctrl-C'd, we verify the shell returned to a clean prompt (the next OSC
    133 D arrived), and the session — with its cwd / env / shell functions —
    stays alive for the next, non-interactive command. ``output`` carries the
    decoded text captured before the block.
    """

    def __init__(self, session_id: str, output: str = ""):
        super().__init__(
            f"session {session_id}: blocked on an interactive prompt"
        )
        self.session_id = session_id
        self.output = output


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


class BashRequired(RuntimeError):
    """Raised when a host has no usable bash for the OSC 133 persistent session.

    The integration hooks need bash or zsh; any other default shell falls back
    to bash, but if bash itself is absent we cannot run a persistent session at
    all and the caller should redirect the agent to ``remote_exec`` (one-shot,
    which runs fine on plain ``sh``).
    """

    def __init__(self, host_name: str):
        super().__init__(
            f"host {host_name!r} has no bash available for remote_shell"
        )
        self.host_name = host_name


@dataclass
class ShellSession:
    session_id: str
    host_name: str
    process: asyncssh.SSHClientProcess
    # The pooled SSH connection that backs ``process``. Stored here so
    # ``close_session`` can release the pool slot back to ConnectionManager;
    # without this reference we leak ``in_use`` counters and the pool grows
    # unboundedly under sustained remote_shell usage.
    conn: asyncssh.SSHClientConnection
    # Which shell (and thus which integration script) this session runs.
    shell: str = "bash"
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    output_buffer: list[str] = field(default_factory=list)
    env: dict = field(default_factory=dict)
    # Serializes execute_in_session on this session's single shared PTY channel:
    # one ``bash -i`` cannot run two foreground commands at once, and concurrent
    # readers would otherwise split the byte stream and steal each other's D
    # marker (wrong exit code / spurious timeout). A multi-step batch holds this
    # for its *whole* duration so an interleaved single call can't desync it.
    _read_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def touch(self):
        self.last_used = time.time()


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, ShellSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(self, host_name: str, env: dict = None,
                             shell: str = "bash") -> str:
        """Spawn a persistent shell session on a remote host.

        ``shell`` selects the spawn command line (and, downstream, the OSC 133
        integration script the caller injects). The integration script itself
        is NOT injected here — see ``bootstrap_osc133`` — so this stays a thin
        spawn + light initial drain.
        """
        env = validate_env_dict(env)
        if shell not in SHELL_COMMAND_LINES:
            shell = "bash"
        mgr = get_manager()
        conn = await mgr.get_connection(host_name)
        try:
            process = await conn.create_process(
                SHELL_COMMAND_LINES[shell], term_type="xterm-256color",
                env=env, request_pty=True,
                # encoding=None puts the channel in *bytes* mode: OSC sequences
                # are matched at the byte level (ESC = 0x1b) before any decode,
                # so a non-UTF-8 byte can never shift a marker boundary or get
                # lost to a codec replacement. We decode the real output
                # ourselves afterwards with DEFAULT_DECODE_ERRORS. ``errors`` is
                # unused in bytes mode but kept as the documented decode policy.
                encoding=None,
                errors=DEFAULT_DECODE_ERRORS,
            )
            session_id = str(uuid.uuid4())[:8]
            session = ShellSession(
                session_id=session_id,
                host_name=host_name,
                process=process,
                conn=conn,
                shell=shell,
                env=env,
            )
            # Drain the spawn banner so it doesn't bleed into bootstrap.
            await asyncio.wait_for(self._drain(session), timeout=5.0)
        except BaseException:
            # Release the pool slot we just acquired before re-raising;
            # otherwise a failed session creation permanently consumes
            # one ``in_use`` counter and eventually exhausts the pool.
            mgr.release_connection(host_name, conn)
            raise
        async with self._lock:
            self._sessions[session_id] = session
        logger.info(f"Session {session_id} created on {host_name} (shell={shell})")
        return session_id

    async def _drain(self, session: ShellSession, timeout: float = 0.5):
        """Read available output without blocking (bytes-safe)."""
        try:
            while True:
                chunk = await asyncio.wait_for(
                    session.process.stdout.read(_READ_CHUNK), timeout=timeout
                )
                if not chunk:
                    break
                if isinstance(chunk, (bytes, bytearray)):
                    chunk = bytes(chunk).decode("utf-8", errors=DEFAULT_DECODE_ERRORS)
                lines = chunk.splitlines()
                session.output_buffer.extend(lines)
                if len(session.output_buffer) > OUTPUT_BUFFER_LINES:
                    session.output_buffer = session.output_buffer[-OUTPUT_BUFFER_LINES:]
        except (asyncio.TimeoutError, asyncssh.ProcessError):
            pass

    async def bootstrap_osc133(self, session_id: str, integration_script: str) -> None:
        """Inject the OSC 133 integration script and block until the shell is
        ready at a clean prompt.

        Three steps, in order — the ordering is load-bearing:

          1. Inject the integration script (whose last line, ``stty -echo``,
             stops the TTY echoing input).
          2. Drain its bootstrap output to quiescence (a ``BOOT_QUIET`` window
             with no new bytes). This both consumes the D's the prompt redraw
             fires while sourcing AND guarantees ``stty -echo`` has taken effect.
          3. ONLY THEN send the readiness marker as a *separate* write and read
             until the marker text is followed by a D.

        Why not append the marker to the script payload (simpler)? Because the
        whole payload arrives in one burst and the PTY echoes the marker command
        line *before* ``stty -echo`` applies — notably under zsh, whose ZLE has
        already echoed earlier lines — so the marker text appears twice and we'd
        lock onto the echoed copy, leaving a stray D that shifts every later
        command's exit code by one. Sending the marker only after the shell is
        quiet (echo off) makes its text appear exactly once, and any late
        bootstrap D still lands *before* the marker, so the marker re-syncs us.
        bash never exhibited this (readline suppresses the echo); the desync
        only showed up on a real zsh host, which is why the spike was worth it.
        """
        session = self._get(session_id)
        try:
            payload = integration_script
            if not payload.endswith("\n"):
                payload += "\n"
            session.process.stdin.write(payload.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError,
                asyncssh.ChannelOpenError, asyncssh.ConnectionLost) as e:
            await self._invalidate(session_id)
            raise SessionDead(session_id, e) from e

        deadline = time.monotonic() + BOOT_TIMEOUT
        # Step 2: drain to quiescence.
        await self._drain_until_quiet(session, BOOT_QUIET, deadline)

        # Step 3: readiness marker (separate write; echo is now off).
        marker = f"__P133_READY_{uuid.uuid4().hex}__"
        try:
            session.process.stdin.write(f"printf '%s\\n' {marker}\n".encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError,
                asyncssh.ChannelOpenError, asyncssh.ConnectionLost) as e:
            await self._invalidate(session_id)
            raise SessionDead(session_id, e) from e

        marker_b = marker.encode("utf-8")
        buf = bytearray()
        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    session.process.stdout.read(_READ_CHUNK), timeout=_READ_POLL
                )
            except asyncio.TimeoutError:
                continue
            except (asyncssh.ChannelOpenError, asyncssh.ConnectionLost,
                    UnicodeDecodeError, ConnectionResetError, OSError) as e:
                await self._invalidate(session_id)
                raise SessionDead(session_id, e) from e
            if not chunk:
                await self._invalidate(session_id)
                raise SessionDead(session_id, EOFError("stdout EOF during bootstrap"))
            buf += chunk
            idx = buf.find(marker_b)
            if idx >= 0 and OSC133_D.search(buf, idx + len(marker_b)):
                return
        await self._invalidate(session_id)
        raise RuntimeError(
            f"OSC 133 bootstrap failed on host {session.host_name!r} "
            f"(no readiness marker within {BOOT_TIMEOUT}s)"
        )

    async def _drain_until_quiet(self, session: ShellSession, quiet: float,
                                 deadline: float) -> None:
        """Read and discard output until a ``quiet``-second window passes with
        no new bytes (or the overall ``deadline`` is hit). Used during bootstrap
        to consume script-sourcing noise and confirm the shell went idle."""
        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    session.process.stdout.read(_READ_CHUNK), timeout=quiet
                )
            except asyncio.TimeoutError:
                return  # quiet window reached — drained
            except (asyncssh.ChannelOpenError, asyncssh.ConnectionLost,
                    UnicodeDecodeError, ConnectionResetError, OSError) as e:
                await self._invalidate(session.session_id)
                raise SessionDead(session.session_id, e) from e
            if not chunk:
                await self._invalidate(session.session_id)
                raise SessionDead(session.session_id,
                                  EOFError("stdout EOF during bootstrap drain"))

    async def execute_in_session(self, session_id: str, command: str,
                                  timeout: float = 30.0) -> "tuple[str, int | None, bool]":
        """Execute a command inside a persistent shell session.

        Returns ``(output, exit_code, truncated)`` where ``output`` is the
        command's combined stdout/stderr (a PTY merges the two streams — use the
        one-shot exec path when you need them split), ``exit_code`` is the
        remote ``$?`` (``None`` only when the command timed out before its OSC
        133 D arrived; ``output`` then carries a trailing ``[timeout]`` marker;
        ``-2`` for a FinalTerm "aborted" D with no exit digits), and
        ``truncated`` is ``True`` when the output exceeded ``MAX_OUTPUT_BYTES``
        and the head was dropped.

        Raises:
            SessionDead: the underlying SSH channel died (write failed, EOF,
                codec error, …). The session is removed from the registry
                before this is raised, so callers can rebuild without first
                calling ``close_session``.
            InteractivePromptBlocked: the command wedged on an interactive
                prompt the PTY can't answer; the command is Ctrl-C'd, the
                session is verified alive and KEPT (cwd/env survive), and this
                is raised so the caller fails fast with guidance.
        """
        session = self._get(session_id)
        session.touch()
        async with session._read_lock:
            return await self._execute_locked(session, command, timeout)

    async def _execute_locked(self, session: ShellSession, command: str,
                              timeout: float) -> "tuple[str, int | None, bool]":
        """Write + stream-read one command. Assumes ``session._read_lock`` is
        already held — the public ``execute_in_session`` acquires it, while a
        multi-step batch holds it across the whole sequence so its commands
        can't be desynced by an interleaved single call on the same channel.
        """
        session_id = session.session_id
        try:
            payload = _wrap_compound(command)
            session.process.stdin.write((payload + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError,
                asyncssh.ChannelOpenError, asyncssh.ConnectionLost) as e:
            await self._invalidate(session_id)
            raise SessionDead(session_id, e) from e

        buf = bytearray()
        deadline = time.monotonic() + timeout
        # Set once an interactive prompt is first seen; if it elapses with no D
        # the command is wedged and we soft-cancel.
        prompt_deadline: "float | None" = None
        truncated = False
        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    session.process.stdout.read(_READ_CHUNK), timeout=_READ_POLL
                )
            except asyncio.TimeoutError:
                # No new bytes. If a prompt is pending and its grace elapsed,
                # the command is wedged on input we can't supply — soft-cancel.
                if (prompt_deadline is not None
                        and time.monotonic() >= prompt_deadline):
                    await self._soft_cancel(session, buf)
                continue
            except (asyncssh.ChannelOpenError, asyncssh.ConnectionLost,
                    UnicodeDecodeError, ConnectionResetError, OSError) as e:
                await self._invalidate(session_id)
                raise SessionDead(session_id, e) from e
            if not chunk:
                await self._invalidate(session_id)
                raise SessionDead(session_id, EOFError("stdout EOF"))

            prev_len = len(buf)
            buf += chunk
            if len(buf) > MAX_OUTPUT_BYTES:
                drop = len(buf) - MAX_OUTPUT_BYTES
                del buf[:drop]
                prev_len = max(0, prev_len - drop)
                truncated = True
            # Only freshly-appended bytes (plus a small overlap, so a marker
            # split across reads is still seen whole) can hold a new marker.
            scan_from = max(0, prev_len - _MARKER_OVERLAP)

            # D must be checked FIRST: a command that merely echoes a prompt
            # string and then finishes emits its D in (nearly) the same chunk —
            # the D has to win so the literal echo isn't misread as a wedge.
            m = OSC133_D.search(buf, scan_from)
            if m:
                exit_code = int(m.group(1)) if m.group(1) is not None else -2
                output = self._decode_output(buf[:m.start()])
                return output, exit_code, truncated

            if prompt_deadline is None and INTERACTIVE_PROMPT_RE.search(buf, scan_from):
                prompt_deadline = time.monotonic() + INTERACTIVE_GRACE_SEC
            if (prompt_deadline is not None
                    and time.monotonic() >= prompt_deadline):
                await self._soft_cancel(session, buf)

        # Timed out. The remote command is still running on this session's
        # shared PTY channel; if we just returned, its late output and OSC-133
        # D would bleed into the NEXT command and desync the session. Send
        # Ctrl-C (SIGINT to the PTY foreground process) and wait briefly for a
        # clean prompt: if it returns, the shell — and cwd/env — survive and the
        # session is safe to reuse; if not, the session is desynced, so drop it
        # and let the next call rebuild a fresh one.
        output = self._decode_output(buf)
        tail = "\n[timeout]" if output else "[timeout]"
        try:
            session.process.stdin.write(b"\x03")
        except (BrokenPipeError, ConnectionResetError, OSError,
                asyncssh.ChannelOpenError, asyncssh.ConnectionLost):
            await self._invalidate(session_id)
            return output + tail, None, truncated
        if await self._await_next_done(session, SOFT_CANCEL_TIMEOUT) is None:
            await self._invalidate(session_id)
        return output + tail, None, truncated

    async def _soft_cancel(self, session: ShellSession,
                           captured: "bytearray") -> None:
        """Ctrl-C a wedged command, verify the shell recovered, keep the session.

        Sends ``\\x03`` to interrupt whatever grabbed stdin (sudo, ssh, …) and
        waits for the next OSC 133 D — its arrival proves the shell returned to
        a clean prompt with cwd/env intact. On success raises
        ``InteractivePromptBlocked`` (session preserved); if no D arrives the
        session is no longer trustworthy, so it is invalidated and ``SessionDead``
        is raised. Always raises — never returns normally.
        """
        session_id = session.session_id
        snapshot = self._decode_output(captured)
        try:
            session.process.stdin.write(b"\x03")
        except (BrokenPipeError, ConnectionResetError, OSError,
                asyncssh.ChannelOpenError, asyncssh.ConnectionLost) as e:
            await self._invalidate(session_id)
            raise SessionDead(session_id, e) from e
        recovered = await self._await_next_done(session, SOFT_CANCEL_TIMEOUT)
        if recovered is None:
            await self._invalidate(session_id)
            raise SessionDead(
                session_id,
                RuntimeError("soft-cancel: no prompt after Ctrl-C"),
            )
        raise InteractivePromptBlocked(session_id, snapshot)

    async def _await_next_done(self, session: ShellSession,
                               timeout: float) -> "int | None":
        """Read until the next OSC 133 D arrives; return its exit code (``-2``
        for an abort-D with no digits) or ``None`` on timeout / dead channel."""
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(
                    session.process.stdout.read(_READ_CHUNK), timeout=_READ_POLL
                )
            except asyncio.TimeoutError:
                continue
            except (asyncssh.ChannelOpenError, asyncssh.ConnectionLost,
                    UnicodeDecodeError, ConnectionResetError, OSError):
                return None
            if not chunk:
                return None
            buf += chunk
            m = OSC133_D.search(buf)
            if m:
                return int(m.group(1)) if m.group(1) is not None else -2
        return None

    def _decode_output(self, raw: "bytes | bytearray") -> str:
        """Decode captured bytes to clean text: backslash-replace undecodable
        bytes, strip residual ANSI/OSC (colour, the bracketed-paste markers, and
        the A/B/C boundary markers bash emits), normalize CRLF, trim."""
        text = bytes(raw).decode("utf-8", errors=DEFAULT_DECODE_ERRORS)
        return strip_ansi(text).replace("\r\n", "\n").rstrip("\n")

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
        # The channel is in bytes mode, so the line is encoded before writing.
        session.process.stdin.write(
            f"export {key}={quote_shell(value)}\n".encode("utf-8")
        )

    async def close_session(self, session_id: str) -> str:
        """Terminate a persistent shell session."""
        session = self._get(session_id)
        try:
            session.process.stdin.write(b"exit\n")
            await asyncio.wait_for(session.process.wait(), timeout=3.0)
        except Exception:
            session.process.close()
        finally:
            # Release the pool slot regardless of how the bash process
            # terminated. Without this ``in_use`` keeps creeping up and
            # ``ConnectionManager`` opens a brand-new TCP connection for
            # every subsequent ``remote_shell`` call once the per-conn cap
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
