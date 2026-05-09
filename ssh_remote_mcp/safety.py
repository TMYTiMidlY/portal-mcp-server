"""Centralized input validation & quoting helpers.

Every value that ultimately reaches one of:
  * a shell command line on the remote host (``conn.run("...")``)
  * an SFTP API call (``sftp.put`` / ``sftp.open`` / ...)
  * a generated remote artifact path (``/tmp/<x>``)

MUST be funnelled through one of the validators below.

Why these helpers exist
-----------------------
The MCP server takes its parameters from an LLM tool-call. The model is not
adversarial in the classical sense, but it *will* happily synthesize values
that contain shell metacharacters, embedded NULs, traversal sequences, etc.
Letting those flow unchecked into ``f"cd {cwd} && {command}"`` is a textbook
command-injection sink — see ``tests/test_command_injection.py`` for concrete
exploit strings that used to succeed.

Design choices
--------------
* Validators raise :class:`ValueError`; callers convert that into a structured
  MCP error string. We never silently sanitize — silent fixups create
  surprise sinks downstream.
* Path validators are *intentionally* lenient about absolute vs relative:
  this is an SSH server, the operator already owns whatever the SSH user can
  reach. We only block the things that have no legitimate use (NUL bytes,
  control chars, empty strings).
* ``quote_shell`` is a thin wrapper around :func:`shlex.quote` so the rest of
  the codebase has a single name to import and so we can swap the
  implementation later without churn.
"""
from __future__ import annotations

import re
import shlex
from typing import Iterable, Optional

# ─── Regexes ────────────────────────────────────────────────────────────────

# POSIX 3.231: a name shall consist solely of underscores, digits, and
# alphabetics from the portable character set, and shall not begin with a digit.
# NOTE: ``re.fullmatch`` is used at call sites — ``$`` would let a trailing
# newline through, which an attacker can use to inject ``export FOO=bar\necho
# pwned`` if the value gets shell-interpolated.
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# tmux session names may contain letters, digits, and a small punctuation set.
# We deliberately reject `:` and `.` (tmux uses them as window/pane separators)
# and anything shell-special.
_TMUX_NAME_RE = re.compile(r"[A-Za-z0-9_\-]+")

# Unix signal names accepted by `kill -<NAME>`. We don't accept numeric signals
# here because the agent never has a reason to use them and rejecting them
# closes a small fingerprint-injection avenue.
_ALLOWED_SIGNALS = frozenset({
    "TERM", "KILL", "HUP", "INT", "QUIT", "USR1", "USR2",
    "STOP", "CONT", "ABRT", "ALRM",
})

# Interpreters we will hand a temp script to. Anything else has no business
# being invoked by an LLM.
_ALLOWED_INTERPRETERS = frozenset({
    "bash", "sh", "zsh", "ash", "dash", "ksh",
    "python", "python2", "python3",
    "perl", "ruby", "node",
})


# ─── Validators ─────────────────────────────────────────────────────────────

def validate_remote_path(path: str, *, allow_empty: bool = False) -> str:
    """Validate a path destined for SFTP or shell expansion on the remote host.

    Rejects:
      * non-string values
      * the empty string (unless ``allow_empty=True``)
      * embedded NUL bytes (``\\x00``) — POSIX paths cannot contain them and
        many shells truncate at the first NUL, which is exactly the trick
        used by smuggling attacks
      * any other ASCII control character (``\\x01-\\x1f``, ``\\x7f``) — these
        cannot appear in a legitimate path and are commonly used to confuse
        log scrapers or terminal rendering

    Returns the path unchanged on success.
    """
    if not isinstance(path, str):
        raise ValueError(f"path must be a string, got {type(path).__name__}")
    if not path and not allow_empty:
        raise ValueError("path must not be empty")
    if "\x00" in path:
        raise ValueError("path contains NUL byte")
    for ch in path:
        if ch < " " and ch not in ("\t",):  # tab is borderline-legitimate
            raise ValueError(f"path contains control character {ch!r}")
        if ch == "\x7f":
            raise ValueError("path contains DEL character")
    return path


def quote_shell(value: str) -> str:
    """Single-source wrapper for :func:`shlex.quote`.

    Use this whenever a value is interpolated into a remote shell command
    string, e.g. ``f"cd {quote_shell(cwd)} && ..."``. ``shlex.quote`` produces
    output that is safe under POSIX shell word-splitting and quote-removal
    rules.
    """
    if not isinstance(value, str):
        raise ValueError(f"quote_shell requires str, got {type(value).__name__}")
    if "\x00" in value:
        raise ValueError("value contains NUL byte")
    return shlex.quote(value)


def validate_env_key(key: str) -> str:
    """Validate a POSIX environment-variable name.

    The shell itself silently ignores ``export FOO=BAR; echo $FOO`` if the
    name violates this regex, so a malformed key is at best dead code and at
    worst (e.g. ``"FOO; rm -rf /"``) an injection sink. Reject up-front.
    """
    if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
        raise ValueError(
            f"invalid env-var name {key!r}: must match [A-Za-z_][A-Za-z0-9_]*"
        )
    return key


def validate_env_dict(env: Optional[dict]) -> dict:
    """Validate every key in an env dict; coerce values to strings.

    Returns a shallow copy. Values are stringified because asyncssh / OpenSSH
    require ``Dict[str, str]`` and will silently drop non-strings.
    """
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ValueError(f"env must be a dict, got {type(env).__name__}")
    out: dict[str, str] = {}
    for k, v in env.items():
        validate_env_key(k)
        if v is None:
            continue
        if "\x00" in str(v):
            raise ValueError(f"env value for {k!r} contains NUL byte")
        out[k] = str(v)
    return out


def validate_signal(sig: str) -> str:
    """Validate a Unix signal name (``TERM``, ``HUP``, ...). Returns upper-cased."""
    if not isinstance(sig, str):
        raise ValueError(f"signal must be a string, got {type(sig).__name__}")
    s = sig.upper().lstrip("SIG")
    if s not in _ALLOWED_SIGNALS:
        raise ValueError(
            f"signal {sig!r} not in allowlist: {sorted(_ALLOWED_SIGNALS)}"
        )
    return s


def validate_interpreter(name: str) -> str:
    """Validate the interpreter name passed to ``ssh_exec_script``.

    We restrict to a known-good list: the value is interpolated into a shell
    command, and accepting arbitrary strings reintroduces the injection sink
    that ``shlex.quote`` would otherwise close.
    """
    if not isinstance(name, str):
        raise ValueError(f"interpreter must be a string, got {type(name).__name__}")
    if name not in _ALLOWED_INTERPRETERS:
        raise ValueError(
            f"interpreter {name!r} not in allowlist: {sorted(_ALLOWED_INTERPRETERS)}"
        )
    return name


def validate_tmux_name(name: str) -> str:
    """Validate a tmux session/window name (alphanumerics, ``_`` and ``-``)."""
    if not isinstance(name, str) or not _TMUX_NAME_RE.fullmatch(name):
        raise ValueError(
            f"tmux name {name!r} invalid: must match [A-Za-z0-9_-]+"
        )
    return name


def validate_pid(pid: int) -> int:
    """Validate a Unix PID: positive int, fits in pid_t (2^22 on Linux ≥ 4.x)."""
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise ValueError(f"pid must be int, got {type(pid).__name__}")
    if pid <= 0:
        raise ValueError(f"pid must be > 0, got {pid}")
    if pid > 4_194_304:
        raise ValueError(f"pid out of range: {pid}")
    return pid


# ─── Convenience: build a safe `cd <dir> && <cmd>` prefix ──────────────────

def build_cwd_prefix(cwd: Optional[str], command: str) -> str:
    """If ``cwd`` is set, return ``"cd <quoted> && <command>"``; else ``command``.

    The single source of truth for the (previously injection-prone)
    ``f"cd {cwd} && {command}"`` pattern.
    """
    if not cwd:
        return command
    validate_remote_path(cwd)
    return f"cd {quote_shell(cwd)} && {command}"
