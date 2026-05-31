"""sudo_creds — out-of-band sudo password provisioning.

The agent (LLM) must never see a sudo password: if a password were passed as
an MCP tool parameter it would land in the model's context / tool-call trace.
This module provides two password sources that both keep the secret out of the
model entirely, then feeds it to ``sudo -S`` on stdin (see remote_bash):

  1. **Live input (1b)** — ``portal-mcp-server sudo-login <host>`` prompts with
     :func:`getpass.getpass` (no echo) in a *separate* terminal and pushes the
     password into the *running* MCP server's memory over a local unix-domain
     socket (mode 0600, same-user only). Cached with a TTL (default 15 min).

  2. **Password manager (1a)** — ``hosts.yaml`` ``sudo_password_command`` (a
     shell command that prints the password, e.g. ``pass show sudo/web01`` or
     ``secret-tool lookup sudo web01``). Symmetric to the existing
     ``password_command`` for SSH login; executed via
     :meth:`ConnectionManager._run_secret_command`.

:func:`resolve_sudo_password` checks the in-memory cache first, then falls back
to ``sudo_password_command``. Nothing is ever written to disk by this module.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("portal_mcp.sudo")

# Default lifetime of a cached sudo password before it must be re-entered.
DEFAULT_TTL_SEC = 15 * 60

_cache_lock = threading.Lock()
# host -> (password, expiry_monotonic)
_cache: dict[str, tuple[str, float]] = {}


# ─────────────────────────────────────────────────────────────────────
# In-memory TTL cache (shared between the MCP event-loop thread and the
# control-socket thread; guarded by a plain threading.Lock).
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

    Order: in-memory cache (set via ``sudo-login``) → host's
    ``sudo_password_command``. Never raises for a missing/failed command —
    returns ``None`` so the caller can emit a friendly hint.
    """
    pw = _get_cached(host)
    if pw is not None:
        return pw
    try:
        from .connection_manager import get_manager
        return await get_manager().sudo_password_command_for(host)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("sudo_password_command failed for %s: %s", host, e)
        return None


# ─────────────────────────────────────────────────────────────────────
# Local control socket (the side-channel for `sudo-login`).
# ─────────────────────────────────────────────────────────────────────

def control_socket_path() -> Path:
    """Per-user path for the sudo control socket.

    Prefers ``$XDG_RUNTIME_DIR`` (a tmpfs, cleared on logout); falls back to
    a uid-scoped /tmp directory.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/portal-mcp-{os.getuid()}"
    return Path(base) / "portal-mcp-server" / "control.sock"


def _socket_is_live(path: Path) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(str(path))
        s.close()
        return True
    except OSError:
        return False


def start_control_server() -> Optional[threading.Thread]:
    """Start a daemon thread serving the sudo control socket.

    Best-effort: if another live server already owns the socket, or the socket
    cannot be created, returns ``None`` and the server runs without the live
    side-channel (the ``sudo_password_command`` path still works).
    """
    path = control_socket_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    except OSError as e:
        logger.warning("sudo control dir setup failed (%s); live sudo-login disabled", e)
        return None

    if path.exists():
        if _socket_is_live(path):
            logger.info("another portal-mcp-server owns %s; live sudo-login deferred to it", path)
            return None
        try:
            path.unlink()  # stale socket from a crashed instance
        except OSError:
            pass

    def _serve() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def handle(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter) -> None:
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=10)
                msg = json.loads(data.decode("utf-8"))
                host = msg.get("host")
                pw = msg.get("password")
                ttl = float(msg.get("ttl", DEFAULT_TTL_SEC))
                if host and pw:
                    cache_sudo_password(host, pw, ttl)
                    logger.info("sudo password cached for host '%s' (ttl=%ss)", host, int(ttl))
                    writer.write(b'{"status":"ok"}')
                else:
                    writer.write(b'{"status":"error","error":"host and password required"}')
                await writer.drain()
            except Exception as e:  # pragma: no cover - defensive
                try:
                    writer.write(json.dumps({"status": "error", "error": str(e)}).encode())
                    await writer.drain()
                except OSError:
                    pass
            finally:
                try:
                    writer.close()
                except OSError:
                    pass

        async def _main() -> None:
            server = await asyncio.start_unix_server(handle, path=str(path))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            logger.info("sudo control socket listening at %s", path)
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(_main())
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("sudo control server stopped: %s", e)

    t = threading.Thread(target=_serve, name="portal-sudo-control", daemon=True)
    t.start()
    return t


def send_sudo_password(host: str, password: str,
                       ttl: float = DEFAULT_TTL_SEC) -> dict:
    """Client side of ``sudo-login``: push a password to the running server."""
    path = control_socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(str(path))
        s.sendall(json.dumps({"host": host, "password": password, "ttl": ttl}).encode())
        s.shutdown(socket.SHUT_WR)
        resp = s.recv(4096)
    finally:
        s.close()
    return json.loads(resp.decode("utf-8")) if resp else {"status": "error", "error": "no response"}
