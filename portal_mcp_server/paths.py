"""Default filesystem locations for portal-mcp-server.

Resolution order for each path:
1. Environment variable override (e.g. SSH_HOSTS_YAML).
2. Legacy `./config/<file>` relative to the current working directory
   (preserves the original developer-checkout layout).
3. XDG-style user directory (works for `uvx`/`pipx` installs where the
   package source is in an isolated tool cache and not writable):
     ``$XDG_CONFIG_HOME/portal-mcp-server/`` (default ``~/.config/portal-mcp-server/``)
     ``$XDG_STATE_HOME/portal-mcp-server/`` (default ``~/.local/state/portal-mcp-server/``)
"""
from __future__ import annotations

import os
from pathlib import Path

_APP = "portal-mcp-server"


def _xdg_dir(env_var: str, fallback: str) -> Path:
    base = os.environ.get(env_var)
    return (Path(base).expanduser() if base else Path.home() / fallback) / _APP


def xdg_config_home() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def xdg_state_home() -> Path:
    return _xdg_dir("XDG_STATE_HOME", ".local/state")


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
