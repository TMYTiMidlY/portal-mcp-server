"""Default filesystem locations for portal-mcp-server.

Resolution order for each path:
1. Environment variable override (e.g. ``PORTAL_HOSTS_YAML``).
2. XDG-style user directory:
     ``$XDG_CONFIG_HOME/portal-mcp-server/`` (default ``~/.config/portal-mcp-server/``)
     ``$XDG_STATE_HOME/portal-mcp-server/`` (default ``~/.local/state/portal-mcp-server/``)

This intentionally does **not** look at the current working directory.
``portal-mcp-server`` is a long-lived user-level daemon, not a project
tool: a cwd-relative ``./config/<file>`` lookup would let any directory
the server happens to be launched from silently override the user's
real config. No mainstream user-level CLI (``ssh``, ``git`` outside its
own ``.git/``, ``gh``, ``docker``, ``kubectl``, ``rclone``, ``borg``,
``mpv``, ``age``) does this. Set ``PORTAL_*`` env vars explicitly
(e.g. via ``direnv``) if you need a per-checkout config.
"""
from __future__ import annotations

import logging
import os
import json
from functools import lru_cache
from pathlib import Path

_APP = "portal-mcp-server"

logger = logging.getLogger(__name__)


class CredentialAgentNotConfigured(RuntimeError):
    pass


@lru_cache(maxsize=None)
def _warn_relative_xdg(env_var: str, raw: str) -> None:
    logger.warning(
        "Ignoring non-absolute %s=%r per XDG Base Directory spec; "
        "falling back to user-home default.", env_var, raw,
    )


def _xdg_dir(env_var: str, fallback: str) -> Path:
    raw = os.environ.get(env_var)
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate / _APP
        _warn_relative_xdg(env_var, raw)
    return Path.home() / fallback / _APP


def xdg_config_home() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def xdg_state_home() -> Path:
    return _xdg_dir("XDG_STATE_HOME", ".local/state")


def _resolve(env_var: str, xdg_default: Path) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    return xdg_default


def hosts_yaml_path() -> Path:
    return _resolve("PORTAL_HOSTS_YAML", xdg_config_home() / "hosts.yaml")


def policies_yaml_path() -> Path:
    return _resolve("PORTAL_POLICIES_YAML", xdg_config_home() / "policies.yaml")


def secrets_yaml_path() -> Path:
    return _resolve("PORTAL_SECRETS_YAML", xdg_config_home() / "secrets.yaml")


def default_log_dir() -> Path:
    return _resolve("PORTAL_LOG_DIR", xdg_state_home() / "logs")


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
        return Path(override).expanduser()
    configured = _configured_agent_socket_path()
    if configured is not None:
        return configured
    raise CredentialAgentNotConfigured(
        "Portal credential agent socket is not configured. Run "
        "`portal agent install --now` first."
    )


def default_systemd_credential_agent_socket_path() -> Path:
    return systemd_user_runtime_dir() / _APP / "credentials.sock"


def systemd_user_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"
