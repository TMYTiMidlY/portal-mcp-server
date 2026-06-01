"""ssh_creds — out-of-band SSH login password provisioning.

Symmetric to :mod:`sudo_creds` but for the *SSH login* password (the one
asyncssh feeds during connection setup), not the sudo password (the one fed to
``sudo -S`` on stdin after a session is open). Same threat model: the agent
(LLM) must never see the value, so the password reaches asyncssh through a
side channel and is never an MCP tool parameter.

Two sources, both keeping the value out of the model:

  1. **Live input (A·getpass)** — ``portal ssh set <host>`` prompts with
     :func:`getpass.getpass` (no echo) in a *separate* terminal and pushes
     the password into the per-user systemd credential agent. Cached with a
     TTL (default 15 min) keyed by host name.

  2. **Password manager (A·command)** — ``hosts.yaml`` ``password_command``
     (a shell command that prints the password, e.g. ``pass show ssh/web01``).
     Same execution model as the existing :attr:`HostConfig.password_command`;
     fetched on demand via :meth:`ConnectionManager.password_command_for`.

:func:`resolve_ssh_password` checks local cache, then the per-user agent, then
falls back to ``password_command``. Nothing is ever written to disk by this
module.

Routing into the connection path
--------------------------------
The connection manager calls :func:`resolve_ssh_password` in two situations:

* host has ``auth: password`` in hosts.yaml — the side channel is the *only*
  password source (key auth was opted out of); cache → command → friendly error.
* host is key-based but asyncssh raised ``PermissionDenied`` — the manager
  tries this fallback once before re-raising. Pure key hosts where neither
  source is set fall straight through (we return ``None``) so the original
  ``PermissionDenied`` surfaces unchanged.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import credential_agent
from .paths import credential_agent_socket_path

logger = logging.getLogger("portal_mcp.ssh_creds")

# Default lifetime of a cached SSH password before it must be re-entered.
DEFAULT_TTL_SEC = 15 * 60

_cache_lock = threading.Lock()
# host -> (password, expiry_monotonic)
_cache: dict[str, tuple[str, float]] = {}


# ─────────────────────────────────────────────────────────────────────
# Local in-process TTL cache (mainly used by tests and direct embedding);
# normal live input is stored in the per-user credential agent.
# ─────────────────────────────────────────────────────────────────────

def cache_ssh_password(host: str, password: str,
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


def clear_ssh_password(host: Optional[str] = None) -> None:
    with _cache_lock:
        if host is None:
            _cache.clear()
        else:
            _cache.pop(host, None)


def get_cached_password(host: str) -> Optional[str]:
    """Read the in-memory SSH-login password cache. Returns ``None`` if no
    valid entry exists for ``host`` (missing or TTL-expired). Sync because the
    underlying lookup is a constant-time guarded dict access; the connection
    manager wraps this in its own async chain. Callers MUST treat the
    returned string as a secret (do not log it, do not echo it).
    """
    return _get_cached(host)


# ─────────────────────────────────────────────────────────────────────
# Per-user agent socket (the side-channel for `portal ssh set`).
# ─────────────────────────────────────────────────────────────────────

def control_socket_path():
    """Compatibility name for the per-user credential agent socket path."""
    return credential_agent_socket_path()


def fetch_ssh_password_from_agent(host: str) -> Optional[str]:
    return credential_agent.fetch("ssh", host)


def send_ssh_password(host: str, password: str,
                      ttl: float = DEFAULT_TTL_SEC) -> dict:
    """Client side of ``portal ssh set``: push a password to the per-user agent."""
    return credential_agent.store("ssh", host, password, ttl=ttl)
