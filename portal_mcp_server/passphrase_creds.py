"""SSH private-key passphrase provisioning.

This is intentionally separate from :mod:`ssh_creds`: an SSH login password is
sent to the remote server during authentication, while a key passphrase unlocks
a local private key before authentication. They often have different rotation,
storage and risk profiles, so the interactive cache uses its own credential
agent kind populated by ``portal passphrase set <host>``.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import credential_agent
from .paths import credential_agent_socket_path

logger = logging.getLogger("portal_mcp.passphrase")

DEFAULT_TTL_SEC = 15 * 60

_cache_lock = threading.Lock()
_cache: dict[str, tuple[str, float]] = {}


def cache_passphrase(host: str, passphrase: str,
                     ttl: float = DEFAULT_TTL_SEC) -> None:
    with _cache_lock:
        _cache[host] = (passphrase, time.monotonic() + ttl)


def _get_cached(host: str) -> Optional[str]:
    with _cache_lock:
        item = _cache.get(host)
        if item is None:
            return None
        passphrase, expiry = item
        if time.monotonic() >= expiry:
            _cache.pop(host, None)
            return None
        return passphrase


def clear_passphrase(host: Optional[str] = None) -> None:
    with _cache_lock:
        if host is None:
            _cache.clear()
        else:
            _cache.pop(host, None)


def get_cached_passphrase(host: str) -> Optional[str]:
    """Return a locally cached key passphrase for ``host`` if still valid."""
    return _get_cached(host)


def control_socket_path():
    """Compatibility name for the per-user credential agent socket path."""
    return credential_agent_socket_path()


def fetch_passphrase_from_agent(host: str) -> Optional[str]:
    return credential_agent.fetch("passphrase", host)


def send_passphrase(host: str, passphrase: str,
                    ttl: float = DEFAULT_TTL_SEC) -> dict:
    """Client side of ``portal passphrase set``."""
    return credential_agent.store("passphrase", host, passphrase, ttl=ttl)
