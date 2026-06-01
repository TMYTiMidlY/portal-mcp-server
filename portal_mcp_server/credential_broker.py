"""Per-user credential broker for live portal credentials.

The broker is intended to be started by a systemd --user socket unit. systemd
owns the filesystem socket and activation lifecycle; this process only keeps a
TTL memory cache and serves same-uid JSON requests over the activated socket.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from ._peer_creds import is_same_uid_peer, peer_uid
from .paths import (
    CredentialBrokerNotConfigured,
    credential_broker_config_path,
    credential_broker_socket_path,
    default_systemd_credential_broker_socket_path,
    systemd_user_unit_dir,
)

logger = logging.getLogger("portal_mcp.credential_broker")

UNIT_BASENAME = "portal-mcp-credential-broker"
SOCKET_UNIT = f"{UNIT_BASENAME}.socket"
SERVICE_UNIT = f"{UNIT_BASENAME}.service"

DEFAULT_TTL_SEC = 15 * 60
_LISTEN_FDS_START = 3
_VALID_KINDS = {"secret", "sudo", "ssh"}


class CredentialBroker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None and not is_same_uid_peer(sock):
                logger.warning(
                    "credential broker: rejecting peer uid %r (broker uid %d)",
                    peer_uid(sock), os.getuid(),
                )
                return

            data = await asyncio.wait_for(reader.read(65536), timeout=10)
            msg = json.loads(data.decode("utf-8"))
            resp = await self.dispatch(msg)
            writer.write(json.dumps(resp).encode("utf-8"))
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

    async def dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg.get("kind")
        op = msg.get("op")
        if kind not in _VALID_KINDS:
            return {"status": "error", "error": "invalid credential kind"}
        if op == "set":
            return await self._set(kind, msg)
        if op == "get":
            return await self._get(kind, msg)
        if op == "clear":
            return await self._clear(kind, msg)
        return {"status": "error", "error": "invalid credential operation"}

    def _key_from_msg(self, kind: str, msg: dict[str, Any]) -> Optional[str]:
        field = "name" if kind == "secret" else "host"
        value = msg.get(field)
        return value if isinstance(value, str) and value else None

    async def _set(self, kind: str, msg: dict[str, Any]) -> dict[str, Any]:
        key = self._key_from_msg(kind, msg)
        value_field = "value" if kind == "secret" else "password"
        value = msg.get(value_field)
        if not key or not isinstance(value, str) or not value:
            return {"status": "error", "error": f"{kind} key and value required"}
        ttl = float(msg.get("ttl", DEFAULT_TTL_SEC))
        async with self._lock:
            self._cache[(kind, key)] = (value, time.monotonic() + ttl)
        logger.info("%s credential cached for %r (ttl=%ss)", kind, key, int(ttl))
        return {"status": "ok"}

    async def _get(self, kind: str, msg: dict[str, Any]) -> dict[str, Any]:
        key = self._key_from_msg(kind, msg)
        if not key:
            return {"status": "error", "error": f"{kind} key required"}
        async with self._lock:
            item = self._cache.get((kind, key))
            if item is None:
                return {"status": "missing"}
            value, expiry = item
            if time.monotonic() >= expiry:
                self._cache.pop((kind, key), None)
                return {"status": "missing"}
        field = "value" if kind == "secret" else "password"
        return {"status": "ok", field: value}

    async def _clear(self, kind: str, msg: dict[str, Any]) -> dict[str, Any]:
        key = self._key_from_msg(kind, msg)
        async with self._lock:
            if key:
                self._cache.pop((kind, key), None)
            else:
                for cache_key in list(self._cache):
                    if cache_key[0] == kind:
                        self._cache.pop(cache_key, None)
        return {"status": "ok"}


def _systemd_activated_sockets() -> list[socket.socket]:
    try:
        listen_pid = int(os.environ.get("LISTEN_PID", "0"))
        listen_fds = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return []
    if listen_pid != os.getpid() or listen_fds <= 0:
        return []

    sockets: list[socket.socket] = []
    for fd in range(_LISTEN_FDS_START, _LISTEN_FDS_START + listen_fds):
        try:
            s = socket.socket(fileno=fd)
            s.setblocking(False)
            sockets.append(s)
        except OSError:
            logger.warning("ignoring invalid systemd socket fd %d", fd)

    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    return sockets


def _bind_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
            raise RuntimeError(f"credential broker socket already live at {path}")
        except OSError:
            pass
        finally:
            probe.close()
        path.unlink()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    os.chmod(path, 0o600)
    s.listen(socket.SOMAXCONN)
    s.setblocking(False)
    return s


async def serve_async(sock: socket.socket) -> None:
    broker = CredentialBroker()
    server = await asyncio.start_unix_server(broker.handle, sock=sock)
    async with server:
        await server.serve_forever()


def serve_forever(socket_path: Path | None = None) -> None:
    sockets = _systemd_activated_sockets()
    if len(sockets) > 1:
        logger.warning("received %d activated sockets; using the first", len(sockets))
    sock = sockets[0] if sockets else _bind_socket(socket_path or credential_broker_socket_path())
    asyncio.run(serve_async(sock))


def request(msg: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
    path = credential_broker_socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        if not is_same_uid_peer(s):
            raise RuntimeError(
                f"credential broker socket {path} peer uid {peer_uid(s)!r} "
                f"does not match our uid {os.getuid()}; refusing to send"
            )
        s.sendall(json.dumps(msg).encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        resp = s.recv(65536)
    finally:
        s.close()
    return json.loads(resp.decode("utf-8")) if resp else {"status": "error", "error": "no response"}


def fetch(kind: str, key: str) -> Optional[str]:
    key_field = "name" if kind == "secret" else "host"
    value_field = "value" if kind == "secret" else "password"
    try:
        resp = request({"kind": kind, "op": "get", key_field: key})
    except (CredentialBrokerNotConfigured, OSError):
        return None
    if resp.get("status") != "ok":
        return None
    value = resp.get(value_field)
    return value if isinstance(value, str) else None


def store(kind: str, key: str, value: str, ttl: float = DEFAULT_TTL_SEC) -> dict[str, Any]:
    key_field = "name" if kind == "secret" else "host"
    value_field = "value" if kind == "secret" else "password"
    return request({
        "kind": kind,
        "op": "set",
        key_field: key,
        value_field: value,
        "ttl": ttl,
    })


def _unit_texts(listen_stream: str, exec_argv: list[str]) -> tuple[str, str]:
    exec_start = " ".join(shlex.quote(part) for part in exec_argv)
    socket_unit = f"""[Unit]
Description=portal-mcp-server credential broker socket

[Socket]
ListenStream={listen_stream}
SocketMode=0600
DirectoryMode=0700
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
"""
    service_unit = f"""[Unit]
Description=portal-mcp-server credential broker
Documentation=https://github.com/TMYTiMidlY/portal-mcp-server

[Service]
Type=simple
ExecStart={exec_start}
NoNewPrivileges=yes
"""
    return socket_unit, service_unit


def install_user_units(*, socket_path: Path | None = None,
                       enable_now: bool = False) -> dict[str, str]:
    if socket_path is None:
        resolved_socket_path = default_systemd_credential_broker_socket_path()
        listen_stream = "%t/portal-mcp-server/credentials.sock"
    else:
        resolved_socket_path = socket_path
        listen_stream = str(socket_path)
    exec_argv = [sys.executable, "-m", "portal_mcp_server", "broker"]
    socket_text, service_text = _unit_texts(listen_stream, exec_argv)

    unit_dir = systemd_user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    socket_unit_path = unit_dir / SOCKET_UNIT
    service_unit_path = unit_dir / SERVICE_UNIT
    socket_unit_path.write_text(socket_text)
    service_unit_path.write_text(service_text)

    config_path = credential_broker_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({
        "socket_path": str(resolved_socket_path),
    }, indent=2) + "\n")

    if enable_now:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", SOCKET_UNIT], check=True)

    return {
        "socket_unit": str(socket_unit_path),
        "service_unit": str(service_unit_path),
        "socket_path": str(resolved_socket_path),
        "config_path": str(config_path),
    }


def _run_systemctl(args: list[str]) -> str | None:
    try:
        subprocess.run(["systemctl", "--user", *args], check=False)
    except OSError as e:
        return str(e)
    return None


def uninstall_user_units(*, stop_now: bool = True,
                         remove_config: bool = True) -> dict[str, Any]:
    """Remove the per-user broker systemd units and optional client config.

    This is intentionally idempotent: missing units/files are not an error.
    """
    errors: list[str] = []
    if stop_now:
        for args in (
            ["disable", "--now", SOCKET_UNIT],
            ["stop", SERVICE_UNIT],
        ):
            err = _run_systemctl(args)
            if err:
                errors.append(err)

    unit_dir = systemd_user_unit_dir()
    paths = [
        unit_dir / SOCKET_UNIT,
        unit_dir / SERVICE_UNIT,
    ]
    if remove_config:
        paths.append(credential_broker_config_path())

    removed: list[str] = []
    for path in paths:
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            pass

    if stop_now:
        err = _run_systemctl(["daemon-reload"])
        if err:
            errors.append(err)

    return {
        "removed": removed,
        "config_removed": remove_config,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="portal-mcp-server broker")
    p.add_argument("--socket", type=Path, default=None,
                   help="manual socket path for non-systemd debugging/tests")
    args = p.parse_args(argv)
    serve_forever(args.socket)
