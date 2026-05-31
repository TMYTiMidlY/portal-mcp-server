"""secrets_store — named API-token injection the agent (LLM) never sees.

The agent often needs a command to use a secret (an API token, a deploy key)
WITHOUT the value entering the model's context / tool-call trace and WITHOUT it
being sent to the third-party LLM backend. This is the same threat model as the
sudo password (see :mod:`sudo_creds`): the value must reach the executed command
through a side channel, never as an MCP tool parameter.

A secret is referenced by **name** (e.g. ``github_token``). The agent passes the
name; the server resolves the value from one of two sources and injects it as an
**environment variable** (``github_token`` → ``$GITHUB_TOKEN``) into a one-shot
command — locally (:func:`local_exec.local_exec_with_env`) or remotely
(:func:`remote_bash.remote_exec_with_env`). The value goes in via the process
environment / SSH stdin, never on argv (so it stays out of ``ps`` and the audit
log), and any echo of it in the command output is redacted to ``***`` before the
result is returned to the agent.

Two sources, both keeping the value out of the model:

  1. **Secret manager (secrets.yaml)** — ``secrets: {github_token: {command:
     "pass show api/github"}}``. Symmetric to ``password_command``; executed via
     :func:`connection_manager._exec_secret_command`.

  2. **Live input (secret-set)** — ``portal-mcp-server secret-set <name>``
     prompts with :func:`getpass.getpass` (no echo) in a *separate* terminal and
     pushes the value into the *running* server's memory over a local
     unix-domain socket (mode 0600, same-user only), cached with a TTL.

Nothing in this module is ever written to disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Optional

import yaml

from .paths import secrets_yaml_path

logger = logging.getLogger("portal_mcp.secrets")

# Default lifetime of a cached secret before it must be re-entered.
DEFAULT_TTL_SEC = 15 * 60

_cache_lock = threading.Lock()
# name -> (value, expiry_monotonic)
_cache: dict[str, tuple[str, float]] = {}

# ── secrets.yaml registry (lazy-loaded module singleton) ──────────────
_registry_lock = threading.Lock()
_registry: dict[str, str] = {}            # name -> command
_registry_warnings: dict[str, list[str]] = {}
_registry_loaded = False
_registry_path: Optional[str] = None

_VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


# ─────────────────────────────────────────────────────────────────────
# name → ENV_VAR_NAME mapping
# ─────────────────────────────────────────────────────────────────────

def env_var_name(name: str) -> str:
    """Map a secret name to the environment variable it is injected as.

    ``github_token`` → ``GITHUB_TOKEN``; non-``[A-Za-z0-9_]`` characters become
    ``_``; a leading digit is prefixed with ``_`` so the result is a valid shell
    identifier.
    """
    ev = re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
    if ev and ev[0].isdigit():
        ev = "_" + ev
    return ev


# ─────────────────────────────────────────────────────────────────────
# secrets.yaml registry
# ─────────────────────────────────────────────────────────────────────

def _load_registry() -> None:
    global _registry_loaded, _registry_path
    with _registry_lock:
        if _registry_loaded:
            return
        _registry.clear()
        _registry_warnings.clear()
        p = secrets_yaml_path()
        _registry_path = str(p)
        _registry_loaded = True
        if not p.exists():
            logger.info("secrets.yaml not found at %s; named-secret manager source disabled", p)
            return
        try:
            data = yaml.safe_load(p.read_text()) or {}
        except Exception as e:  # pragma: no cover - defensive
            logger.error("failed to parse secrets.yaml at %s: %s", p, e)
            return
        secrets = data.get("secrets", {})
        if not isinstance(secrets, dict):
            logger.error("secrets.yaml 'secrets' must be a mapping; ignoring")
            return
        for name, spec in secrets.items():
            warnings: list[str] = []
            if not _VALID_NAME.match(str(name)):
                warnings.append(
                    f"secret name '{name}' is not a valid identifier "
                    "([A-Za-z_][A-Za-z0-9_.-]*); it is ignored."
                )
                logger.error("secrets.yaml: %s", warnings[-1])
                _registry_warnings[name] = warnings
                continue
            if isinstance(spec, str):
                command = spec
            elif isinstance(spec, dict):
                command = spec.get("command")
                if "value" in spec:
                    warnings.append(
                        f"secret '{name}' has a plaintext 'value' field — it is "
                        "IGNORED (plaintext secrets in config/backups are a leak "
                        "risk). Use 'command:' (prints the secret to stdout) or "
                        f"the out-of-band `secret-set {name}`."
                    )
                    logger.error("secrets.yaml: %s", warnings[-1])
            else:
                command = None
            if not command:
                warnings.append(
                    f"secret '{name}' has no 'command' — it is loaded but cannot "
                    "be resolved from secrets.yaml (the secret-set cache may still "
                    "provide it)."
                )
                logger.error("secrets.yaml: %s", warnings[-1])
            else:
                _registry[name] = command
            if warnings:
                _registry_warnings[name] = warnings


def reload_registry() -> None:
    """Force a re-read of secrets.yaml on the next access (used by tests)."""
    global _registry_loaded
    with _registry_lock:
        _registry_loaded = False


def secret_command_for(name: str) -> Optional[str]:
    _load_registry()
    with _registry_lock:
        return _registry.get(name)


def registry_warnings() -> dict[str, list[str]]:
    _load_registry()
    with _registry_lock:
        return {k: list(v) for k, v in _registry_warnings.items()}


# ─────────────────────────────────────────────────────────────────────
# In-memory TTL cache (shared between the MCP event-loop thread and the
# control-socket thread; guarded by a plain threading.Lock).
# ─────────────────────────────────────────────────────────────────────

def cache_secret(name: str, value: str, ttl: float = DEFAULT_TTL_SEC) -> None:
    with _cache_lock:
        _cache[name] = (value, time.monotonic() + ttl)


def _get_cached(name: str) -> Optional[str]:
    with _cache_lock:
        item = _cache.get(name)
        if item is None:
            return None
        value, expiry = item
        if time.monotonic() >= expiry:
            _cache.pop(name, None)
            return None
        return value


def clear_secret(name: Optional[str] = None) -> None:
    with _cache_lock:
        if name is None:
            _cache.clear()
        else:
            _cache.pop(name, None)


async def resolve_secret(name: str) -> Optional[str]:
    """Return the value for ``name`` or ``None`` if no source is configured.

    Order: in-memory cache (set via ``secret-set``) → secrets.yaml ``command``.
    Returns ``None`` only when *neither* source exists, so the caller can emit a
    friendly hint. If a secrets.yaml command IS configured but fails, the
    underlying :class:`RuntimeError` propagates (its message is value-free) so
    the agent sees a real error instead of a misleading "not configured".
    """
    value = _get_cached(name)
    if value is not None:
        return value
    command = secret_command_for(name)
    if not command:
        return None
    from .connection_manager import _exec_secret_command
    return await _exec_secret_command(command, label=f"secret_command for '{name}'")


# ─────────────────────────────────────────────────────────────────────
# Output redaction
# ─────────────────────────────────────────────────────────────────────

def redact(text: str, values) -> str:
    """Replace every occurrence of each secret value in ``text`` with ``***``.

    Applied to stdout/stderr before the result is returned to the agent (and
    before it reaches the audit log), so a command that echoes its token cannot
    leak the value back into the session history / third-party LLM.
    """
    if not text:
        return text
    # Longest first so a value that is a substring of another is masked fully.
    for v in sorted({v for v in values if v}, key=len, reverse=True):
        text = text.replace(v, "***")
    return text


# ─────────────────────────────────────────────────────────────────────
# Local control socket (the side-channel for `secret-set`).
# ─────────────────────────────────────────────────────────────────────

def control_secrets_socket_path() -> Path:
    """Per-user path for the named-secret control socket.

    Kept distinct from the sudo control socket so the two side-channels stay
    isolated. Prefers ``$XDG_RUNTIME_DIR`` (tmpfs, cleared on logout); falls
    back to a uid-scoped /tmp directory.
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/portal-mcp-{os.getuid()}"
    return Path(base) / "portal-mcp-server" / "control-secrets.sock"


def _socket_is_live(path: Path) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(str(path))
        s.close()
        return True
    except OSError:
        return False


def start_secrets_control_server() -> Optional[threading.Thread]:
    """Start a daemon thread serving the named-secret control socket.

    Best-effort: if another live server already owns the socket, or the socket
    cannot be created, returns ``None`` and the server runs without the live
    side-channel (the secrets.yaml ``command`` source still works).
    """
    path = control_secrets_socket_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
    except OSError as e:
        logger.warning("secrets control dir setup failed (%s); live secret-set disabled", e)
        return None

    if path.exists():
        if _socket_is_live(path):
            logger.info("another portal-mcp-server owns %s; live secret-set deferred to it", path)
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
                name = msg.get("name")
                value = msg.get("value")
                ttl = float(msg.get("ttl", DEFAULT_TTL_SEC))
                if name and value:
                    cache_secret(name, value, ttl)
                    logger.info("secret cached for name '%s' (ttl=%ss)", name, int(ttl))
                    writer.write(b'{"status":"ok"}')
                else:
                    writer.write(b'{"status":"error","error":"name and value required"}')
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
            logger.info("secrets control socket listening at %s", path)
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(_main())
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("secrets control server stopped: %s", e)

    t = threading.Thread(target=_serve, name="portal-secrets-control", daemon=True)
    t.start()
    return t


def send_secret(name: str, value: str, ttl: float = DEFAULT_TTL_SEC) -> dict:
    """Client side of ``secret-set``: push a value to the running server."""
    path = control_secrets_socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(str(path))
        s.sendall(json.dumps({"name": name, "value": value, "ttl": ttl}).encode())
        s.shutdown(socket.SHUT_WR)
        resp = s.recv(4096)
    finally:
        s.close()
    return json.loads(resp.decode("utf-8")) if resp else {"status": "error", "error": "no response"}
