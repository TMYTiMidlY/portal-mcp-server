"""Default filesystem locations for portal-mcp-server.

Resolution order for each path:
1. Environment variable override (e.g. SSH_HOSTS_YAML).
2. Legacy `./config/<file>` relative to the current working directory
   (preserves the original developer-checkout layout).
3. XDG-style user directory (works for `uvx`/`pipx` installs where the
   package source is in an isolated tool cache and not writable).

XDG namespace migration
-----------------------
The XDG namespace was renamed from ``ssh-remote-mcp`` to
``portal-mcp-server`` in v0.3.0. To avoid losing existing user config,
the resolver still honours legacy locations:

    ``~/.config/ssh-remote-mcp/``        (read-only fallback)
    ``~/.local/state/ssh-remote-mcp/``   (read-only fallback)

If the new ``portal-mcp-server`` directory does not exist but the
legacy one does, the legacy path is used and a one-time WARNING is
logged suggesting ``mv`` to the new location.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

_APP = "portal-mcp-server"
_LEGACY_APP = "ssh-remote-mcp"

_logger = logging.getLogger("ssh_mcp.paths")
_warned_legacy: set[str] = set()


def _xdg_dir(env_var: str, fallback: str, app: str = _APP) -> Path:
    base = os.environ.get(env_var)
    return (Path(base).expanduser() if base else Path.home() / fallback) / app


def _xdg_with_fallback(env_var: str, fallback: str) -> Path:
    """Return the new XDG dir, but if it doesn't exist and the legacy
    `ssh-remote-mcp` dir does, return the legacy dir (one-time WARN)."""
    new = _xdg_dir(env_var, fallback, _APP)
    if new.exists():
        return new
    legacy = _xdg_dir(env_var, fallback, _LEGACY_APP)
    if legacy.exists():
        if env_var not in _warned_legacy:
            _warned_legacy.add(env_var)
            _logger.warning(
                "Using legacy XDG dir %s; please `mv %s %s` (or set %s).",
                legacy, legacy, new, env_var,
            )
        return legacy
    return new


def xdg_config_home() -> Path:
    return _xdg_with_fallback("XDG_CONFIG_HOME", ".config")


def xdg_state_home() -> Path:
    return _xdg_with_fallback("XDG_STATE_HOME", ".local/state")


def _resolve(env_var: str, legacy: str, xdg_default: Path) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    legacy_path = Path(legacy)
    if legacy_path.exists():
        return legacy_path
    return xdg_default


def hosts_yaml_path() -> Path:
    return _resolve(
        "SSH_HOSTS_YAML",
        "config/hosts.yaml",
        xdg_config_home() / "hosts.yaml",
    )


def policies_yaml_path() -> Path:
    return _resolve(
        "SSH_POLICIES_YAML",
        "config/policies.yaml",
        xdg_config_home() / "policies.yaml",
    )


def default_log_dir() -> Path:
    override = os.environ.get("SSH_MCP_LOG_DIR")
    if override:
        return Path(override).expanduser()
    legacy = Path("logs")
    if legacy.exists():
        return legacy
    return xdg_state_home() / "logs"
