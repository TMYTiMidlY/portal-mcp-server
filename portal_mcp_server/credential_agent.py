"""Per-user credential agent for live portal credentials.

The agent is intended to be started by a systemd --user socket unit. systemd
owns the filesystem socket and activation lifecycle; this process only keeps a
TTL memory cache and serves same-uid JSON requests over the activated socket.

The wire protocol is line-oriented JSON over a Unix stream socket. Every
request has a ``kind`` ∈ ``{"secret", "sudo", "ssh"}`` and an ``op``:

  set        store ``value`` (or ``password``) for ``key``; resets TTL.
  get        return plaintext value for ``key`` or ``{status: missing}``.
  clear      drop one ``key`` (or all of ``kind`` when ``key`` is absent).
  fingerprint   return sha256[:16] of stored value + remaining TTL seconds
                (intentionally NO plaintext — see "design principle" below).
  list       return [{key, fingerprint, ttl_remaining}, ...] for ``kind``.
  status     return ``{counts: {kind: cached_entry_count}}`` (server health).

**Design principle — plaintext never leaves the agent.** The agent will hand
the plaintext value back to a same-uid peer ONLY via ``get`` — the path used by
the SSH connect loop and the ``$SECRET`` env injection. Human-facing CLI verbs
(``portal ssh show`` / ``list`` / ``confirm``) use ``fingerprint`` / ``list``
and never carry plaintext back to a TTY, so terminal scrollback, screenshots
and recordings cannot leak a stored credential. Same rule as ssh-agent,
gpg-agent, vault agent, polkit-agent.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
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

from ._peer_creds import (
    is_same_uid_peer, is_same_user_named_pipe_peer, peer_uid)
from .paths import (
    CredentialAgentNotConfigured,
    credential_agent_config_path,
    credential_agent_platform,
    credential_agent_socket_path,
    credential_agent_unsupported_hint,
    default_launchd_credential_agent_socket_path,
    default_namedpipe_credential_agent_address,
    default_scheduled_task_name,
    default_systemd_credential_agent_socket_path,
    systemd_user_unit_dir,
)

logger = logging.getLogger("portal_mcp.credential_agent")

UNIT_BASENAME = "portal-credential-agent"
SOCKET_UNIT = f"{UNIT_BASENAME}.socket"
SERVICE_UNIT = f"{UNIT_BASENAME}.service"
# macOS LaunchAgent label (reverse-DNS, Apple convention).
LAUNCHD_LABEL = "com.tmytimidly.portal-credential-agent"

DEFAULT_TTL_SEC = 15 * 60
_LISTEN_FDS_START = 3
_VALID_KINDS = {"secret", "sudo", "ssh"}


def _fingerprint(value: str) -> str:
    """Stable, short fingerprint for human sanity-checking that doesn't leak
    plaintext. sha256 truncated to 16 hex chars (64 bits) — plenty to
    distinguish 'is this the password I just set' from 'is this last
    Tuesday's leftover'.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class CredentialAgent:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None and not is_same_uid_peer(sock):
                logger.warning(
                    "credential agent: rejecting peer uid %r (agent uid %d)",
                    peer_uid(sock), os.getuid(),
                )
                return
            # Windows named pipe (no SO_PEERCRED): verify the client's user SID
            # matches ours. get_extra_info("socket") is None for a pipe; the
            # pipe HANDLE is under "pipe". Fails open inside the check.
            if sock is None and sys.platform == "win32":
                pipe = writer.get_extra_info("pipe")
                ph = _pipe_handle_int(pipe)
                if ph is not None and not is_same_user_named_pipe_peer(
                        ph, role="server"):
                    logger.warning(
                        "credential agent: rejecting named-pipe client "
                        "(different Windows user)")
                    return

            # Newline-delimited single-line JSON framing (transport-agnostic:
            # works over a Unix socket AND a Windows named pipe, neither of
            # which can rely on a half-close to delimit the request).
            data = await asyncio.wait_for(reader.readline(), timeout=10)
            msg = json.loads(data.decode("utf-8"))
            resp = await self.dispatch(msg)
            writer.write(json.dumps(resp).encode("utf-8") + b"\n")
            await writer.drain()
        except Exception as e:  # pragma: no cover - defensive
            try:
                writer.write(json.dumps(
                    {"status": "error", "error": str(e)}).encode() + b"\n")
                await writer.drain()
            except OSError:
                pass
        finally:
            try:
                writer.close()
            except OSError:
                pass

    async def dispatch(self, msg: dict[str, Any]) -> dict[str, Any]:
        op = msg.get("op")
        if op == "status":
            return await self._status()
        kind = msg.get("kind")
        if kind not in _VALID_KINDS:
            return {"status": "error", "error": "invalid credential kind"}
        if op == "set":
            return await self._set(kind, msg)
        if op == "get":
            return await self._get(kind, msg)
        if op == "clear":
            return await self._clear(kind, msg)
        if op == "fingerprint":
            return await self._fingerprint(kind, msg)
        if op == "list":
            return await self._list(kind)
        return {"status": "error", "error": "invalid credential operation"}

    def _key_from_msg(self, kind: str, msg: dict[str, Any]) -> Optional[str]:
        field = "name" if kind == "secret" else "host"
        value = msg.get(field)
        return value if isinstance(value, str) and value else None

    def _ttl_remaining(self, expiry: float) -> int:
        return max(0, int(expiry - time.monotonic()))

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

    async def _fingerprint(self, kind: str, msg: dict[str, Any]) -> dict[str, Any]:
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
            ttl_remaining = self._ttl_remaining(expiry)
        return {
            "status": "ok",
            "fingerprint": _fingerprint(value),
            "ttl_remaining": ttl_remaining,
        }

    async def _list(self, kind: str) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        now = time.monotonic()
        expired: list[tuple[str, str]] = []
        async with self._lock:
            for (k, key), (value, expiry) in self._cache.items():
                if k != kind:
                    continue
                if now >= expiry:
                    expired.append((k, key))
                    continue
                entries.append({
                    "key": key,
                    "fingerprint": _fingerprint(value),
                    "ttl_remaining": max(0, int(expiry - now)),
                })
            for cache_key in expired:
                self._cache.pop(cache_key, None)
        entries.sort(key=lambda e: e["key"])
        return {"status": "ok", "entries": entries}

    async def _status(self) -> dict[str, Any]:
        counts = {kind: 0 for kind in _VALID_KINDS}
        now = time.monotonic()
        expired: list[tuple[str, str]] = []
        async with self._lock:
            for (kind, key), (_value, expiry) in self._cache.items():
                if now >= expiry:
                    expired.append((kind, key))
                    continue
                counts[kind] = counts.get(kind, 0) + 1
            for cache_key in expired:
                self._cache.pop(cache_key, None)
        return {"status": "ok", "counts": counts}


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
    parent = path.parent
    parent_existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    if path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(path))
            raise RuntimeError(f"credential agent socket already live at {path}")
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
    agent = CredentialAgent()
    server = await asyncio.start_unix_server(agent.handle, sock=sock)
    async with server:
        await server.serve_forever()


async def serve_async_pipe(pipe_name: str) -> None:
    """Windows named-pipe server.

    asyncio's named-pipe support is protocol-based (ProactorEventLoop only), so
    we bridge it to the same stream ``handle(reader, writer)`` used on Unix via
    a ``StreamReaderProtocol``. ``start_serving_pipe`` manages creating fresh
    pipe instances per client connection.
    """
    agent = CredentialAgent()
    loop = asyncio.get_running_loop()

    def factory() -> asyncio.StreamReaderProtocol:
        reader = asyncio.StreamReader()
        return asyncio.StreamReaderProtocol(reader, agent.handle)

    servers = await loop.start_serving_pipe(factory, pipe_name)
    try:
        await asyncio.Event().wait()  # serve until the process is stopped
    finally:
        for srv in servers:
            srv.close()


def _resolve_windows_pipe_name(socket_path: Path | None) -> str:
    """Pipe address for the Windows agent: explicit arg > configured > default.

    Unlike the Unix path (which *requires* configuration so a missing install is
    a hard error), Windows has no auto-install yet, so we fall back to a stable
    per-user default pipe name to keep manual ``portal agent run`` ergonomic.
    """
    if socket_path is not None:
        return str(socket_path)
    try:
        return str(credential_agent_socket_path())
    except CredentialAgentNotConfigured:
        return default_namedpipe_credential_agent_address()


def serve_forever(socket_path: Path | None = None) -> None:
    if sys.platform == "win32":
        # ProactorEventLoop is the Windows default (3.8+) and is required for
        # start_serving_pipe; set it explicitly for robustness.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        asyncio.run(serve_async_pipe(_resolve_windows_pipe_name(socket_path)))
        return
    sockets = _systemd_activated_sockets()
    if len(sockets) > 1:
        logger.warning("received %d activated sockets; using the first", len(sockets))
    sock = sockets[0] if sockets else _bind_socket(socket_path or credential_agent_socket_path())
    asyncio.run(serve_async(sock))


def _recv_line(sock: socket.socket, timeout: float) -> bytes:
    """Read one newline-delimited frame from a connected stream socket."""
    sock.settimeout(timeout)
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0]


def _request_unix_socket(path: Path, payload: bytes, timeout: float) -> bytes:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        if not is_same_uid_peer(s):
            raise RuntimeError(
                f"credential agent socket {path} peer uid {peer_uid(s)!r} "
                f"does not match our uid {os.getuid()}; refusing to send"
            )
        s.sendall(payload + b"\n")
        return _recv_line(s, timeout)
    finally:
        s.close()


def _pipe_handle_int(pipe) -> Optional[int]:
    """Best-effort extraction of an OS pipe HANDLE (int) from asyncio's pipe
    transport ``get_extra_info("pipe")`` object, for the Windows peer check.
    Returns ``None`` if it can't (the peer check then degrades to allow)."""
    if pipe is None:
        return None
    for attr in ("handle", "fileno"):
        try:
            v = getattr(pipe, attr)
            v = v() if callable(v) else v
            if isinstance(v, int):
                return v
        except Exception:
            continue
    try:
        return int(pipe)
    except Exception:
        return None


def _request_named_pipe(pipe_name: str, payload: bytes, timeout: float) -> bytes:
    """Windows transport: a named pipe is openable as a file. Retry briefly if
    the server is between pipe instances (ERROR_PIPE_BUSY). After connecting we
    verify the pipe SERVER runs as the current user (peer-SID check, fails open)
    before sending — the named-pipe analogue of the Unix same-uid client check;
    the pipe's name embeds the username but is not itself an access control."""
    deadline = time.monotonic() + timeout
    f = None
    while f is None:
        try:
            f = open(pipe_name, "r+b", buffering=0)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    try:
        try:
            import msvcrt
            ph = msvcrt.get_osfhandle(f.fileno())
        except Exception:
            ph = None
        if ph is not None and not is_same_user_named_pipe_peer(ph, role="client"):
            raise RuntimeError(
                f"credential agent named pipe {pipe_name} is served by a "
                f"different Windows user; refusing to send")
        f.write(payload + b"\n")
        f.flush()
        buf = b""
        while b"\n" not in buf:
            chunk = f.read(65536)
            if not chunk:
                break
            buf += chunk
        return buf.split(b"\n", 1)[0]
    finally:
        f.close()


def request(msg: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
    """Send one request to the credential agent and return its JSON response.

    Transport-agnostic newline-delimited framing over a Unix socket
    (Linux/macOS) or a Windows named pipe; the address comes from
    :func:`credential_agent_socket_path`.
    """
    path = credential_agent_socket_path()
    payload = json.dumps(msg).encode("utf-8")
    if sys.platform == "win32":
        resp = _request_named_pipe(str(path), payload, timeout)
    else:
        resp = _request_unix_socket(path, payload, timeout)
    return json.loads(resp.decode("utf-8")) if resp else {
        "status": "error", "error": "no response"}


def fetch(kind: str, key: str) -> Optional[str]:
    key_field = "name" if kind == "secret" else "host"
    value_field = "value" if kind == "secret" else "password"
    try:
        resp = request({"kind": kind, "op": "get", key_field: key})
    except (CredentialAgentNotConfigured, OSError):
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


def clear(kind: str, key: Optional[str] = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"kind": kind, "op": "clear"}
    if key is not None:
        key_field = "name" if kind == "secret" else "host"
        msg[key_field] = key
    return request(msg)


def fingerprint(kind: str, key: str) -> dict[str, Any]:
    key_field = "name" if kind == "secret" else "host"
    return request({"kind": kind, "op": "fingerprint", key_field: key})


def list_entries(kind: str) -> dict[str, Any]:
    return request({"kind": kind, "op": "list"})


def status() -> dict[str, Any]:
    return request({"op": "status"})


def _unit_texts(listen_stream: str, exec_argv: list[str]) -> tuple[str, str]:
    exec_start = " ".join(shlex.quote(part) for part in exec_argv)
    socket_unit = f"""[Unit]
Description=portal-mcp-server credential agent socket

[Socket]
ListenStream={listen_stream}
SocketMode=0600
DirectoryMode=0700
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
"""
    service_unit = f"""[Unit]
Description=portal-mcp-server credential agent
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
        resolved_socket_path = default_systemd_credential_agent_socket_path()
        listen_stream = "%t/portal-mcp-server/credentials.sock"
    else:
        resolved_socket_path = socket_path
        listen_stream = str(socket_path)
    exec_argv = [sys.executable, "-m", "portal_mcp_server", "agent", "run"]
    socket_text, service_text = _unit_texts(listen_stream, exec_argv)

    unit_dir = systemd_user_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    socket_unit_path = unit_dir / SOCKET_UNIT
    service_unit_path = unit_dir / SERVICE_UNIT
    socket_unit_path.write_text(socket_text)
    service_unit_path.write_text(service_text)

    config_path = credential_agent_config_path()
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
    """Remove the per-user agent systemd units and optional client config.

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
        paths.append(credential_agent_config_path())

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


# ── macOS launchd LaunchAgent ────────────────────────────────────────────────
#
# Rather than launchd *socket activation* (which needs the C
# ``launch_activate_socket`` API via ctypes — fiddly and easy to get wrong),
# we install a plain run-and-keepalive LaunchAgent: launchd keeps
# ``portal agent run --socket <path>`` alive and the agent binds the socket
# itself (``_bind_socket`` works on macOS — it has AF_UNIX). The same-uid peer
# check degrades to filesystem permissions on non-Linux (see _peer_creds), and
# the 0700 dir / 0600 socket still gate access.


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_plist_text(socket_path: Path, exec_argv: list[str]) -> str:
    args_xml = "\n".join(
        f"        <string>{_xml_escape(a)}</string>" for a in exec_argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PORTAL_CREDENTIAL_AGENT_SOCKET</key>
        <string>{_xml_escape(str(socket_path))}</string>
    </dict>
</dict>
</plist>
"""


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def install_launchd_agent(*, socket_path: Path | None = None,
                          enable_now: bool = False) -> dict[str, str]:
    resolved_socket_path = (socket_path
                            or default_launchd_credential_agent_socket_path())
    exec_argv = [sys.executable, "-m", "portal_mcp_server", "agent", "run",
                 "--socket", str(resolved_socket_path)]
    plist_text = _launchd_plist_text(resolved_socket_path, exec_argv)

    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist_text)

    config_path = credential_agent_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({
        "socket_path": str(resolved_socket_path),
    }, indent=2) + "\n")

    if enable_now:
        # `launchctl load -w` registers + starts the agent (RunAtLoad).
        subprocess.run(["launchctl", "load", "-w", str(plist_path)], check=True)

    return {
        "plist": str(plist_path),
        "socket_path": str(resolved_socket_path),
        "config_path": str(config_path),
    }


def uninstall_launchd_agent(*, stop_now: bool = True,
                            remove_config: bool = True) -> dict[str, Any]:
    errors: list[str] = []
    plist_path = launchd_plist_path()
    if stop_now and plist_path.exists():
        try:
            subprocess.run(["launchctl", "unload", "-w", str(plist_path)],
                           check=False)
        except OSError as e:
            errors.append(str(e))

    removed: list[str] = []
    targets = [plist_path]
    if remove_config:
        targets.append(credential_agent_config_path())
    for path in targets:
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            pass

    return {"removed": removed, "config_removed": remove_config,
            "errors": errors}


# ── Windows per-user logon scheduled task ────────────────────────────────────
#
# Windows Services run in session 0 and default to the LocalSystem identity —
# the wrong trust boundary for a per-user secret cache (any admin/SYSTEM could
# read it). The correct per-user analog of `systemd --user` / a launchd
# LaunchAgent is a *scheduled task* with a **logon trigger** and an
# **interactive-token principal**: it runs as the logged-in user, in their
# session, only while they are logged on, and stores no password. We register it
# from a Task Scheduler XML because the `schtasks` command line cannot set
# `ExecutionTimeLimit` (so a plain task would be killed after the 72h default)
# nor a `RestartOnFailure` keepalive — both of which the XML provides.

TASK_SCHEDULER_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _current_windows_user_id() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME")
    if domain and user:
        return f"{domain}\\{user}"
    if user:
        return user
    import getpass
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - getuser only fails w/o any user env
        return "user"


def _pythonw_executable() -> str:
    """Prefer pythonw.exe (no flashing console window) next to the interpreter."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def scheduled_task_xml_path() -> Path:
    return credential_agent_config_path().with_name("credential-agent-task.xml")


def _scheduled_task_xml_text(exec_path: str, exec_args: str, *,
                             user_id: str, task_name: str) -> str:
    esc = _xml_escape
    uri = "\\" + task_name
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{TASK_SCHEDULER_NS}">
  <RegistrationInfo>
    <Description>portal-mcp-server credential agent (per-user)</Description>
    <URI>{esc(uri)}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{esc(user_id)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{esc(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{esc(exec_path)}</Command>
      <Arguments>{esc(exec_args)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def install_scheduled_task(*, socket_path: Path | None = None,
                           enable_now: bool = False) -> dict[str, str]:
    task_name = default_scheduled_task_name()
    socket_addr = (str(socket_path) if socket_path
                   else default_namedpipe_credential_agent_address())
    exec_args = f"-m portal_mcp_server agent run --socket {socket_addr}"
    xml_text = _scheduled_task_xml_text(
        _pythonw_executable(), exec_args,
        user_id=_current_windows_user_id(), task_name=task_name)

    xml_path = scheduled_task_xml_path()
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    # schtasks expects a Unicode XML; UTF-16 (with BOM) is the most-compatible.
    xml_path.write_text(xml_text, encoding="utf-16")

    config_path = credential_agent_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"socket_path": socket_addr}, indent=2) + "\n")

    subprocess.run(["schtasks", "/Create", "/TN", task_name, "/XML",
                    str(xml_path), "/F"], check=True)
    if enable_now:
        subprocess.run(["schtasks", "/Run", "/TN", task_name], check=True)

    return {
        "task_name": task_name,
        "task_xml": str(xml_path),
        "socket_path": socket_addr,
        "config_path": str(config_path),
    }


def uninstall_scheduled_task(*, stop_now: bool = True,
                             remove_config: bool = True) -> dict[str, Any]:
    task_name = default_scheduled_task_name()
    errors: list[str] = []
    if stop_now:
        try:
            subprocess.run(["schtasks", "/End", "/TN", task_name], check=False)
        except OSError as e:
            errors.append(str(e))
    try:
        # /F suppresses the confirm prompt; a missing task just exits non-zero.
        subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"], check=False)
    except OSError as e:
        errors.append(str(e))

    removed: list[str] = []
    targets = [scheduled_task_xml_path()]
    if remove_config:
        targets.append(credential_agent_config_path())
    for path in targets:
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            pass

    return {"removed": removed, "config_removed": remove_config,
            "errors": errors}


# ── OS-dispatching install entry points ──────────────────────────────────────


def install_agent(*, socket_path: Path | None = None,
                  enable_now: bool = False) -> dict[str, Any]:
    """Install the per-user credential agent for the current OS.

    Linux -> systemd user units; macOS -> launchd LaunchAgent; Windows ->
    per-user logon scheduled task (named-pipe transport). Raises ``RuntimeError``
    with an actionable hint on platforms without an automated install — use
    command-source credentials instead.
    """
    backend = credential_agent_platform()
    if backend == "systemd":
        res = install_user_units(socket_path=socket_path, enable_now=enable_now)
    elif backend == "launchd":
        res = install_launchd_agent(socket_path=socket_path, enable_now=enable_now)
    elif backend == "schtasks":
        res = install_scheduled_task(socket_path=socket_path, enable_now=enable_now)
    else:
        raise RuntimeError(credential_agent_unsupported_hint())
    res["backend"] = backend
    return res


def uninstall_agent(*, stop_now: bool = True,
                    remove_config: bool = True) -> dict[str, Any]:
    backend = credential_agent_platform()
    if backend == "systemd":
        res = uninstall_user_units(stop_now=stop_now, remove_config=remove_config)
    elif backend == "launchd":
        res = uninstall_launchd_agent(stop_now=stop_now, remove_config=remove_config)
    elif backend == "schtasks":
        res = uninstall_scheduled_task(stop_now=stop_now, remove_config=remove_config)
    else:
        raise RuntimeError(credential_agent_unsupported_hint())
    res["backend"] = backend
    return res


def main(argv: list[str] | None = None) -> None:
    """Direct daemon entry — equivalent to ``portal agent run``.

    Kept as a top-level :func:`main` so the module can be invoked via
    ``python -m portal_mcp_server.credential_agent`` for tests and
    debugging that bypass the CLI dispatcher.
    """
    p = argparse.ArgumentParser(prog="portal-mcp-server agent run")
    p.add_argument("--socket", type=Path, default=None,
                   help="manual socket path for non-systemd debugging/tests")
    args = p.parse_args(argv)
    serve_forever(args.socket)
