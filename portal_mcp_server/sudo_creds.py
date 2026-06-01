"""sudo_creds — out-of-band sudo password provisioning.

The agent (LLM) must never see a sudo password: if a password were passed as
an MCP tool parameter it would land in the model's context / tool-call trace.
This module provides two password sources that both keep the secret out of the
model entirely, then feeds it to ``sudo -S`` on stdin (see remote_bash):

  1. **Live input (1b)** — ``portal sudo set <host>`` prompts with
     :func:`getpass.getpass` (no echo) in a *separate* terminal and pushes the
     password into the per-user systemd credential agent. Cached with a TTL
     (default 15 min).

  2. **Password manager (1a)** — ``hosts.yaml`` ``sudo_password_command`` (a
     shell command that prints the password, e.g. ``pass show sudo/web01`` or
     ``secret-tool lookup sudo web01``). Symmetric to the existing
     ``password_command`` for SSH login; executed via
     :meth:`ConnectionManager._run_secret_command`.

:func:`resolve_sudo_password` checks the local cache, then the per-user agent,
then falls back to ``sudo_password_command``. Nothing is ever written to disk by
this module.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from . import credential_agent
from .paths import credential_agent_socket_path

logger = logging.getLogger("portal_mcp.sudo")

# Default lifetime of a cached sudo password before it must be re-entered.
DEFAULT_TTL_SEC = 15 * 60

_cache_lock = threading.Lock()
# host -> (password, expiry_monotonic)
_cache: dict[str, tuple[str, float]] = {}


# ─────────────────────────────────────────────────────────────────────
# Local in-process TTL cache (mainly used by tests and direct embedding);
# normal live input is stored in the per-user credential agent.
# ─────────────────────────────────────────────────────────────────────

def cache_sudo_password(host: str, password: str,
                        ttl: float = DEFAULT_TTL_SEC) -> None:
    with _cache_lock:
        _cache[host] = (password, time.monotonic() + ttl)


def _get_cached(host: str) -> Optional[str]:
    with _cache_lock:
        item = _cache.get(host)
        if item is None:
            return None
        pw, expiry = item
        if time.monotonic() >= expiry:
            _cache.pop(host, None)
            return None
        return pw


def clear_sudo_password(host: Optional[str] = None) -> None:
    with _cache_lock:
        if host is None:
            _cache.clear()
        else:
            _cache.pop(host, None)


async def resolve_sudo_password(host: str) -> Optional[str]:
    """Return a sudo password for ``host`` or ``None`` if no source is set.

    Order: local in-memory cache → per-user credential agent → host's
    ``sudo_password_command``. Never raises for a missing/failed command —
    returns ``None`` so the caller can emit a friendly hint.
    """
    pw = _get_cached(host)
    if pw is not None:
        return pw
    pw = await asyncio.to_thread(credential_agent.fetch, "sudo", host)
    if pw is not None:
        return pw
    try:
        from .connection_manager import get_manager
        return await get_manager().sudo_password_command_for(host)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("sudo_password_command failed for %s: %s", host, e)
        return None


# ─────────────────────────────────────────────────────────────────────
# Per-user agent socket (the side-channel for `portal sudo set`).
# ─────────────────────────────────────────────────────────────────────

def control_socket_path():
    """Compatibility name for the per-user credential agent socket path."""
    return credential_agent_socket_path()


def send_sudo_password(host: str, password: str,
                       ttl: float = DEFAULT_TTL_SEC) -> dict:
    """Client side of ``portal sudo set``: push a password to the per-user agent."""
    return credential_agent.store("sudo", host, password, ttl=ttl)
