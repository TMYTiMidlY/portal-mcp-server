"""Default filesystem locations for portal-mcp-server.

Resolution order for each path:

1. ``PORTAL_*`` environment variable override (e.g. ``PORTAL_HOSTS_YAML``).
   **Must be absolute** — relative values are warned and ignored, matching
   the XDG-spec policy applied to ``XDG_CONFIG_HOME`` / ``XDG_STATE_HOME``.
   ``portal-mcp-server`` is a long-lived daemon, so a cwd-relative override
   would silently let the launch directory poison the resolved path.
2. Platform-native user directory resolved via :mod:`platformdirs`:

   - Linux:   ``~/.config/portal-mcp-server/``,
              ``~/.local/state/portal-mcp-server/``,
              ``~/.local/state/portal-mcp-server/log/``
              (honours absolute ``$XDG_CONFIG_HOME`` / ``$XDG_STATE_HOME``).
   - macOS:   ``~/Library/Application Support/portal-mcp-server/`` (config + state),
              ``~/Library/Logs/portal-mcp-server/`` (logs).
   - Windows: ``%LOCALAPPDATA%\\portal-mcp-server\\`` (config + state),
              ``%LOCALAPPDATA%\\portal-mcp-server\\Logs\\`` (logs).

This intentionally does **not** look at the current working directory.
``portal-mcp-server`` is a long-lived user-level daemon, not a project
tool: a cwd-relative ``./config/<file>`` lookup would let any directory
the server happens to be launched from silently override the user's
real config. No mainstream user-level CLI (``ssh``, ``git`` outside its
own ``.git/``, ``gh``, ``docker``, ``kubectl``, ``rclone``, ``borg``,
``mpv``, ``age``) does this. Set ``PORTAL_*`` env vars explicitly
(e.g. via ``direnv``) if you need a per-checkout config.

The runtime / credential-socket paths (``systemd_user_runtime_dir``,
``credential_agent_socket_path``, ``systemd_user_unit_dir``) are
Linux/systemd specific and intentionally bypass :mod:`platformdirs`
— they encode a contract with systemd user units, not a portable
"where do user files live" question.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

from platformdirs import PlatformDirs

_APP = "portal-mcp-server"
_DIRS = PlatformDirs(appname=_APP, appauthor=False)

logger = logging.getLogger(__name__)


class CredentialAgentNotConfigured(RuntimeError):
    pass


@lru_cache(maxsize=None)
def _warn_relative_xdg(env_var: str, raw: str) -> None:
    logger.warning(
        "Ignoring non-absolute %s=%r per XDG Base Directory spec; "
        "falling back to platform default.", env_var, raw,
    )


def _sanitize_xdg_env() -> None:
    """Drop relative ``XDG_*`` values before :mod:`platformdirs` consults them.

    The XDG Base Directory spec mandates that implementations ignore
    non-absolute values, but :mod:`platformdirs` accepts them verbatim.
    Pre-sanitize so a stray ``XDG_CONFIG_HOME=./local`` can't poison the
    resolved path. Idempotent and cheap; called once per public lookup.
    """
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        raw = os.environ.get(var)
        if raw and not Path(raw).expanduser().is_absolute():
            _warn_relative_xdg(var, raw)
            del os.environ[var]


def xdg_config_home() -> Path:
    """User config directory (legacy name kept for call-site compatibility)."""
    _sanitize_xdg_env()
    return Path(_DIRS.user_config_dir)


def xdg_state_home() -> Path:
    """User state directory (legacy name kept for call-site compatibility)."""
    _sanitize_xdg_env()
    return Path(_DIRS.user_state_dir)


def _resolve(env_var: str, default: Path) -> Path:
    """Honour an absolute ``PORTAL_*`` override; otherwise return ``default``.

    Relative override values are rejected with a warning and ignored. This
    matches the XDG-spec policy applied to ``XDG_CONFIG_HOME`` /
    ``XDG_STATE_HOME``: ``portal-mcp-server`` is a long-lived daemon and
    accepting a cwd-relative value here would silently let the directory
    the server was launched from poison the resolved path.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return default
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate
    logger.warning(
        "Ignoring non-absolute %s=%r; only absolute paths are accepted. "
        "Falling back to default %r.", env_var, raw, str(default),
    )
    return default


def hosts_yaml_path() -> Path:
    return _resolve("PORTAL_HOSTS_YAML", xdg_config_home() / "hosts.yaml")


def policies_yaml_path() -> Path:
    return _resolve("PORTAL_POLICIES_YAML", xdg_config_home() / "policies.yaml")


def secrets_yaml_path() -> Path:
    return _resolve("PORTAL_SECRETS_YAML", xdg_config_home() / "secrets.yaml")


def default_log_dir() -> Path:
    """Platform-native log directory (overridden by ``PORTAL_LOG_DIR``).

    Linux: ``~/.local/state/portal-mcp-server/log/`` (XDG state per spec).
    macOS: ``~/Library/Logs/portal-mcp-server/`` (Apple's documented home).
    Windows: ``%LOCALAPPDATA%\\portal-mcp-server\\Logs\\``.
    """
    _sanitize_xdg_env()
    return _resolve("PORTAL_LOG_DIR", Path(_DIRS.user_log_dir))


def systemd_user_runtime_dir() -> Path:
    """Return the runtime dir provided by the systemd user session."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base).expanduser()
    raise CredentialAgentNotConfigured(
        "XDG_RUNTIME_DIR is not set. Run `portal agent install` from a "
        "systemd user session, or pass --socket explicitly."
    )


def credential_agent_config_path() -> Path:
    return xdg_config_home() / "agent.json"


def _configured_agent_socket_path() -> Path | None:
    path = credential_agent_config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    value = data.get("socket_path")
    return Path(value).expanduser() if isinstance(value, str) and value else None


def credential_agent_socket_path() -> Path:
    override = os.environ.get("PORTAL_CREDENTIAL_AGENT_SOCKET")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate
        logger.warning(
            "Ignoring non-absolute PORTAL_CREDENTIAL_AGENT_SOCKET=%r; "
            "only absolute paths are accepted. Falling back to the "
            "configured agent.json value.", override,
        )
    configured = _configured_agent_socket_path()
    if configured is not None:
        return configured
    raise CredentialAgentNotConfigured(
        "Portal credential agent socket is not configured. Run "
        "`portal agent install --now` first."
    )


def default_systemd_credential_agent_socket_path() -> Path:
    return systemd_user_runtime_dir() / _APP / "credentials.sock"


def default_launchd_credential_agent_socket_path() -> Path:
    """macOS default socket path for the credential agent.

    macOS has no ``XDG_RUNTIME_DIR``; per-user ``$TMPDIR`` (e.g.
    ``/var/folders/.../T/``) is the Apple-blessed per-user-writable runtime
    location. We place a 0700 subdir under it for the 0600 socket.
    """
    base = os.environ.get("TMPDIR") or "/tmp"
    return Path(base).expanduser() / _APP / "credentials.sock"


def default_namedpipe_credential_agent_address() -> str:
    """Windows default credential-agent address — a per-user named pipe.

    Named pipes live in the single machine-global ``\\\\.\\pipe\\`` namespace
    (NOT per-session), and the name embeds the (non-secret, predictable)
    username, so the name alone is not an access control. Same-user enforcement
    comes from the pipe's ACL plus the best-effort peer-SID check in
    :mod:`._peer_creds` (``is_same_user_named_pipe_peer``), mirroring the
    SO_PEERCRED defence-in-depth on Unix.
    """
    import getpass
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    safe = "".join(c for c in user if c.isalnum()) or "user"
    return rf"\\.\pipe\{_APP}-credentials-{safe}"


def default_scheduled_task_name() -> str:
    """Windows Task Scheduler task name for the credential agent (per-user).

    Scheduled-task names share a per-user namespace under the calling account,
    so a fixed name is fine; we keep it stable so install/uninstall agree.
    """
    return f"{_APP}-credential-agent"


def credential_agent_platform() -> str:
    """Which credential-agent *install* backend fits this OS.

    ``"systemd"`` (Linux user units) / ``"launchd"`` (macOS LaunchAgent) /
    ``"schtasks"`` (Windows per-user logon scheduled task) / ``"unsupported"``
    (everything else — use the command-source credentials, or run the agent
    manually).

    All three supported backends auto-start a **per-user** agent that runs as
    the logged-in user: systemd ``--user`` and launchd LaunchAgent run in the
    user session, and the Windows scheduled task uses an *interactive-token*
    logon trigger (runs as you, only while you're logged on — never as SYSTEM,
    and with no stored password). The IPC transport is chosen separately by
    :data:`sys.platform` (Unix domain socket on Linux/macOS, named pipe on
    Windows).
    """
    if sys.platform.startswith("linux"):
        return "systemd"
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "win32":
        return "schtasks"
    return "unsupported"


def credential_agent_unsupported_hint() -> str:
    """Actionable message for platforms without an automated agent install.

    The credential *agent* (the no-echo `portal {ssh,sudo,secret} set` path)
    needs an OS service manager to auto-start. Where we don't have one wired up,
    the agent's purpose — keeping a value out of the LLM — is still fully
    achievable via the command-source credentials, which this message points at.
    """
    return (
        f"The interactive credential agent has no automated install on this "
        f"platform ({sys.platform}). The MCP server and every remote portal_* "
        f"tool still work — only the no-echo `portal {{ssh,sudo,secret}} set` "
        f"caching path needs an OS service manager (systemd on Linux, launchd "
        f"on macOS, a logon scheduled task on Windows). Instead, drive "
        f"credentials from command sources: `password_command` / "
        f"`passphrase_command` / `sudo_password_command` in hosts.yaml, and a "
        f"`command:` in secrets.yaml — each reads from your password manager "
        f"(Keychain, pass, 1Password CLI, ...) on demand and never enters the "
        f"model context."
    )


def systemd_user_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"
