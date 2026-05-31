"""
Connection Manager — SSH connection pool, host registry, key-auth.
Manages persistent AsyncSSH connections to multiple remote hosts.
"""
import asyncio
import os
import logging
import re
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional
from dataclasses import dataclass, field

import asyncssh
import yaml

logger = logging.getLogger("portal_mcp.connections")

# Hard ceiling for password_command / passphrase_command execution. Long enough
# for an interactive `pass show` (which may unlock the GPG agent) but short
# enough that a misconfigured command does not silently hang the server.
SECRET_COMMAND_TIMEOUT_SEC = 10

# Default maximum concurrent channels (SFTP sessions, exec channels, etc.)
# multiplexed over a single SSH TCP connection.
DEFAULT_MAX_CHANNELS_PER_CONN = 5

# Default maximum idle time (seconds) before a connection with no active
# channels is eligible for pruning. 10 minutes matches OpenSSH ControlPersist.
DEFAULT_MAX_IDLE_TIME = 600.0

# Default maximum connection age (seconds). Connections older than this with
# no active channels are closed to avoid stale TCP/firewall state.
DEFAULT_MAX_CONN_AGE = 3600.0

# How asyncssh should handle bytes that aren't valid in the negotiated
# encoding (default 'utf-8'). The library default is 'strict', which raises
# UnicodeDecodeError on the first non-UTF-8 byte and tears the channel down
# — fatal when an SSH command happens to emit GBK / Latin-1 / ANSI bytes
# (e.g. ``powershell.exe`` on a Chinese Windows host whose codepage is 936
# returns ``netsh`` output as GBK). ``'backslashreplace'`` makes undecodable
# bytes survive as ``\xd3`` style escapes, so the agent still gets readable
# stdout AND the channel stays alive for the next command.
#
# Used by every ``conn.run(...)`` / ``conn.create_process(...)`` call in
# this codebase. Bumping it to ``'replace'`` would lose information; keep
# ``'backslashreplace'`` so the original bytes can still be reconstructed.
DEFAULT_DECODE_ERRORS = "backslashreplace"


async def _exec_secret_command(cmd: str, *, label: str) -> str:
    """Execute a user-supplied shell command and return its stdout as a secret.

    Shared by SSH/sudo password sources (:meth:`ConnectionManager._run_secret_command`)
    and the named-secret store (:mod:`secrets_store`). ``label`` is only used in
    error messages — it must NOT contain the secret. The command's stdout is the
    secret; stderr is captured but never logged or returned (it commonly contains
    the secret on misconfigured commands). One trailing newline is stripped.
    """
    loop = asyncio.get_running_loop()

    def _run() -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=SECRET_COMMAND_TIMEOUT_SEC,
            check=False,
            env=os.environ.copy(),
        )

    try:
        result = await loop.run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{label} timed out after {SECRET_COMMAND_TIMEOUT_SEC}s"
        ) from None

    if result.returncode != 0:
        raise RuntimeError(f"{label} exited with code {result.returncode}")

    try:
        secret = result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise RuntimeError(
            f"{label} produced non-UTF-8 output. Ensure the command writes a "
            "plain-text secret to stdout."
        ) from None
    # Most secret stores append a single trailing newline. Strip exactly one so
    # secrets that legitimately end in whitespace survive.
    if secret.endswith("\r\n"):
        secret = secret[:-2]
    elif secret.endswith("\n"):
        secret = secret[:-1]
    if not secret:
        raise RuntimeError(f"{label} produced empty output")
    return secret


@dataclass
class HostConfig:
    name: str
    host: str
    port: int = 22
    user: str = "root"
    key: Optional[str] = None
    connect_timeout: int = 30
    # Path to a known_hosts file. ``None`` means "let asyncssh use the
    # default" (which resolves to ``~/.ssh/known_hosts`` and behaves like
    # OpenSSH's strict mode). Set to the literal string ``"<disable>"`` to
    # opt out of host-key verification entirely (NOT recommended).
    known_hosts: Optional[str] = None
    strict_host_key_checking: bool = True
    tags: list = field(default_factory=list)
    # When True, defer everything to ~/.ssh/config (asyncssh.connect resolves
    # HostName/User/Port/IdentityFile/ProxyJump natively from `name`).
    use_ssh_config: bool = False
    # Authentication mode. ``None`` (default) means key-based auth (the
    # recommended path). ``"password"`` opts the host into password auth and
    # requires ``password_command`` to be set — see ``_build_connect_kwargs``.
    auth: Optional[str] = None
    # Shell command that prints the SSH login password to stdout. Executed
    # at connection time, never persisted, never logged, never exposed via
    # any MCP tool parameter. Borg's BORG_PASSCOMMAND / restic's
    # RESTIC_PASSWORD_COMMAND follow the same pattern.
    password_command: Optional[str] = None
    # Shell command that prints the passphrase for an encrypted private key.
    # Same execution model as ``password_command``. Prefer ssh-agent for
    # encrypted keys; this is the headless / CI fallback.
    passphrase_command: Optional[str] = None
    # Shell command that prints this host's *sudo* password to stdout. Same
    # execution model as ``password_command`` (run on demand, never persisted,
    # never logged, never exposed via any MCP tool parameter). Used by
    # ``portal_bash(use_sudo=True)`` to feed ``sudo -S`` on stdin. The
    # alternative source is the in-memory cache populated out-of-band by
    # ``portal-mcp-server sudo-login`` (see sudo_creds.py).
    sudo_password_command: Optional[str] = None


@dataclass
class PooledConnection:
    host_name: str
    conn: asyncssh.SSHClientConnection
    created_at: float
    last_used: float
    in_use: int = 0

    @property
    def is_alive(self) -> bool:
        try:
            return not self.conn.is_closed()
        except Exception:
            return False


class ConnectionManager:
    """
    Manages a pool of AsyncSSH connections keyed by host name.
    Supports host registry from YAML, dynamic registration, and connection reuse.

    Pool semantics
    --------------
    * **pool_size**: maximum number of TCP connections kept *per host*.
      When all connections are fully loaded and the cap is reached, the
      least-loaded connection is reused (with a warning log).
    * **max_channels_per_conn**: preferred ceiling for concurrent SSH channels
      (exec, SFTP, tunnel, …) multiplexed over one TCP connection.
    * **max_idle_time**: connections with ``in_use == 0`` that have been idle
      longer than this are closed on the next ``get_connection`` call.
    * **max_conn_age**: connections older than this with ``in_use == 0`` are
      closed regardless of recent activity (guards against stale
      TCP / NAT / firewall state).
    """

    def __init__(
        self,
        hosts_yaml: str | os.PathLike | None = None,
        pool_size: int = 5,
        max_channels_per_conn: int = DEFAULT_MAX_CHANNELS_PER_CONN,
        max_idle_time: float = DEFAULT_MAX_IDLE_TIME,
        max_conn_age: float = DEFAULT_MAX_CONN_AGE,
    ):
        from .paths import hosts_yaml_path
        self._registry: dict[str, HostConfig] = {}
        # host name -> human-readable config warnings collected at load time.
        # Surfaced to the agent via list_hosts() because a logger.error() on a
        # stdio MCP server's stderr is effectively invisible to the user.
        self._config_warnings: dict[str, list[str]] = {}
        self._pool: dict[str, list[PooledConnection]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pool_size = pool_size
        self._max_channels_per_conn = max_channels_per_conn
        self._max_idle_time = max_idle_time
        self._max_conn_age = max_conn_age
        self._hosts_yaml = str(hosts_yaml) if hosts_yaml else str(hosts_yaml_path())
        self._load_registry()

    def _load_registry(self):
        """Load host definitions from YAML file."""
        p = Path(self._hosts_yaml)
        if not p.exists():
            logger.warning(f"hosts.yaml not found at {p}, starting empty registry")
            return
        with open(p) as f:
            data = yaml.safe_load(f) or {}
        hosts = data.get("hosts", {})
        for name, cfg in hosts.items():
            warnings: list[str] = []
            if "password" in cfg:
                msg = (
                    "hosts.yaml has a plaintext 'password' field — it is being "
                    "IGNORED (plaintext credentials in config files/backups are "
                    "a leak risk). Remove it and rotate the password, then use "
                    "'auth: password' + 'password_command:' (a command that "
                    "prints the password to stdout) instead."
                )
                logger.error("Host '%s': %s", name, msg)
                warnings.append(msg)
            auth = cfg.get("auth")
            password_command = cfg.get("password_command")
            passphrase_command = cfg.get("passphrase_command")
            sudo_password_command = cfg.get("sudo_password_command")
            if auth == "password" and not password_command:
                msg = (
                    "declares 'auth: password' but has no 'password_command' — "
                    "the host is loaded but connection attempts will fail. Add a "
                    "'password_command:' that prints the password to stdout "
                    f"(e.g. 'pass show ssh/{name}' or 'printf %s \"${name.upper()}_PASSWORD\"')."
                )
                logger.error("Host '%s' %s", name, msg)
                warnings.append(msg)
            if auth not in (None, "password"):
                msg = (
                    f"unknown auth mode '{auth}'; expected None (key-based) or "
                    "'password'. Treating as key-based."
                )
                logger.error("Host '%s' has %s", name, msg)
                warnings.append(msg)
                auth = None
            if warnings:
                self._config_warnings[name] = warnings
            self._registry[name] = HostConfig(
                name=name,
                host=cfg["host"],
                port=int(cfg.get("port", 22)),
                user=cfg.get("user", "root"),
                key=self._resolve_path(cfg.get("key")),
                connect_timeout=int(cfg.get("connect_timeout", 30)),
                known_hosts=cfg.get("known_hosts"),
                strict_host_key_checking=bool(cfg.get(
                    "strict_host_key_checking", True
                )),
                tags=cfg.get("tags", []),
                auth=auth,
                password_command=password_command,
                passphrase_command=passphrase_command,
                sudo_password_command=sudo_password_command,
            )
        logger.info(f"Loaded {len(self._registry)} hosts from registry")

    def _resolve_path(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        return str(Path(path).expanduser())

    def register_host(self, name: str, host: str, user: str = "root",
                      port: int = 22, key: Optional[str] = None,
                      tags: list = None,
                      known_hosts: Optional[str] = None,
                      strict_host_key_checking: bool = True) -> str:
        """Dynamically register a new host into the registry.

        Password authentication is intentionally not supported; provide a
        key file via ``key`` or rely on default SSH key locations / agent.
        """
        self._registry[name] = HostConfig(
            name=name, host=host, port=port, user=user,
            key=self._resolve_path(key),
            tags=tags or [],
            known_hosts=known_hosts,
            strict_host_key_checking=strict_host_key_checking,
        )
        logger.info(f"Registered host: {name} ({user}@{host}:{port})")
        return f"Host '{name}' registered: {user}@{host}:{port}"

    def list_hosts(self) -> list[dict]:
        out = []
        for h in self._registry.values():
            entry = {"name": h.name, "host": h.host, "port": h.port,
                     "user": h.user, "tags": h.tags}
            warns = self._config_warnings.get(h.name)
            if warns:
                entry["warnings"] = warns
            out.append(entry)
        return out

    def config_warnings(self) -> dict[str, list[str]]:
        """host -> config warnings collected at registry load time."""
        return {k: list(v) for k, v in self._config_warnings.items()}

    def remove_host(self, name: str) -> str:
        if name not in self._registry:
            return f"Host '{name}' not found"
        del self._registry[name]
        # Drop the lazy lock (otherwise ``_locks`` grows indefinitely if hosts
        # churn) and close any pooled connections so we do not leak SSH
        # channels for an alias the caller has explicitly forgotten.
        self._locks.pop(name, None)
        pool = self._pool.pop(name, [])
        for pc in pool:
            try:
                pc.conn.close()
            except Exception:  # pragma: no cover
                pass
        return f"Host '{name}' removed from registry"

    async def _get_lock(self, name: str) -> asyncio.Lock:
        # ``setdefault`` is atomic under the GIL, so two concurrent tasks for
        # the same host name will always observe the same lock instance even
        # if both reach this method before the first creates the entry.
        return self._locks.setdefault(name, asyncio.Lock())

    def _known_hosts_arg(self, cfg: HostConfig):
        """Resolve the ``known_hosts`` value to pass to ``asyncssh.connect``.

        The asyncssh contract:
          * ``known_hosts=()``        -> use the default ``~/.ssh/known_hosts``
                                        and reject unknown hosts (strict).
          * ``known_hosts="path"``    -> load that specific file.
          * ``known_hosts=None``      -> disable verification entirely (UNSAFE).

        We map our policy onto that:
          * cfg.strict_host_key_checking == True (default):
              - cfg.known_hosts is a path -> pass the path
              - cfg.known_hosts is None   -> pass () so asyncssh defaults apply
          * cfg.strict_host_key_checking == False:
              - log a clear warning and pass None to disable verification
        """
        if not cfg.strict_host_key_checking:
            logger.warning(
                "Host '%s' has strict_host_key_checking=False — SSH host key "
                "verification is DISABLED for this host (MITM exposure).",
                cfg.name,
            )
            return None
        if cfg.known_hosts:
            return self._resolve_path(cfg.known_hosts)
        # Empty tuple = "use the default known_hosts file(s) and be strict",
        # which matches OpenSSH StrictHostKeyChecking=yes behaviour.
        return ()

    async def _run_secret_command(
        self,
        cmd: str,
        *,
        host: str,
        kind: str,
    ) -> str:
        """Execute a user-supplied shell command and return its stdout as a
        secret string.

        Modeled after Borg's BORG_PASSCOMMAND and restic's
        RESTIC_PASSWORD_COMMAND. The contract:

          * shell=True so users can write ``pass show ssh/web01``,
            ``printf '%s' "$WEB01_PASSWORD"``, ``op read op://...`` etc.
          * stdout is the secret. Trailing newline is stripped (``pass``,
            ``echo`` and friends almost always emit one).
          * stderr is captured but never logged or returned — it commonly
            contains the secret on misconfigured commands.
          * Hard timeout via ``SECRET_COMMAND_TIMEOUT_SEC`` so a hanging
            password manager cannot wedge the connection pool.
          * On failure, we raise ``RuntimeError`` with the host and exit
            code only — no command string, no output.
        """
        return await _exec_secret_command(
            cmd, label=f"{kind} for host '{host}'"
        )

    async def sudo_password_command_for(self, host_name: str) -> Optional[str]:
        """Run a host's ``sudo_password_command`` and return its output.

        Returns ``None`` when the host is unknown or has no
        ``sudo_password_command`` configured (so callers can fall back to the
        in-memory cache populated by ``sudo-login``). Raises only if the
        command itself fails, matching ``_run_secret_command`` semantics.
        """
        cfg = self._registry.get(host_name)
        if cfg is None or not cfg.sudo_password_command:
            return None
        return await self._run_secret_command(
            cfg.sudo_password_command, host=host_name, kind="sudo_password_command",
        )

    async def _build_connect_kwargs(self, cfg: HostConfig) -> dict:
        if cfg.use_ssh_config:
            # asyncssh resolves everything from ~/.ssh/config using the alias.
            # Defer host-key behaviour to the same policy as explicit hosts.
            return dict(
                host=cfg.name,
                connect_timeout=cfg.connect_timeout,
                known_hosts=self._known_hosts_arg(cfg),
            )
        kwargs = dict(
            host=cfg.host, port=cfg.port, username=cfg.user,
            connect_timeout=cfg.connect_timeout,
            known_hosts=self._known_hosts_arg(cfg),
        )

        # ── Password auth (opt-in via `auth: password` + `password_command`) ──
        # No key is loaded; asyncssh will negotiate `password` (or
        # keyboard-interactive that prompts for password) using the secret
        # produced by the user-supplied command.
        if cfg.auth == "password":
            if not cfg.password_command:
                raise RuntimeError(
                    f"Host '{cfg.name}' has 'auth: password' but no "
                    "'password_command' configured in hosts.yaml. Refusing "
                    "to attempt password auth without a password source."
                )
            password = await self._run_secret_command(
                cfg.password_command, host=cfg.name, kind="password_command",
            )
            kwargs["password"] = password
            # Disable client_keys so asyncssh does not silently fall back to
            # default key locations and, on success, mask a misconfigured
            # password_command.
            kwargs["client_keys"] = []
            return kwargs

        # ── Key-based auth (the default and recommended path) ──
        if cfg.key:
            kwargs["client_keys"] = [cfg.key]
        else:
            # Try default SSH agent / key locations
            default_keys = []
            for k in ["~/.ssh/id_ed25519", "~/.ssh/id_rsa", "~/.ssh/id_ecdsa"]:
                kp = Path(k).expanduser()
                if kp.exists():
                    default_keys.append(str(kp))
            if default_keys:
                kwargs["client_keys"] = default_keys

        # Encrypted private keys: if the user supplied a passphrase_command,
        # run it. Otherwise leave the slot empty so asyncssh can fall back to
        # ssh-agent (the recommended UX for encrypted keys).
        if cfg.passphrase_command:
            kwargs["passphrase"] = await self._run_secret_command(
                cfg.passphrase_command,
                host=cfg.name,
                kind="passphrase_command",
            )

        return kwargs

    def _try_load_from_ssh_config(self, host_name: str) -> Optional[HostConfig]:
        """Check whether host_name is defined as an alias in ~/.ssh/config.
        Returns a synthetic HostConfig (use_ssh_config=True) if found, else None.
        """
        ssh_config = Path("~/.ssh/config").expanduser()
        if not ssh_config.exists():
            return None
        try:
            with open(ssh_config) as f:
                content = f.read()
        except OSError:
            return None
        # Lightweight scan: look for a `Host <alias>` line that lists host_name
        # as one of the patterns (excluding wildcard-only entries).
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r"^Host\s+(.+)$", stripped, re.IGNORECASE)
            if not m:
                continue
            patterns = m.group(1).split()
            # Skip pure wildcard hosts like `Host *` to avoid false positives
            if all(p in ("*", "?") for p in patterns):
                continue
            if host_name in patterns:
                return HostConfig(
                    name=host_name,
                    host=host_name,  # placeholder; asyncssh ignores when use_ssh_config
                    use_ssh_config=True,
                    tags=["ssh-config"],
                )
        return None

    async def get_connection(self, host_name: str) -> asyncssh.SSHClientConnection:
        """Get or create a pooled connection to a host.
        Resolution order:
          1. Explicitly registered host (registry / hosts.yaml)
          2. Alias defined in ~/.ssh/config (auto-registered on first use)
        """
        if host_name not in self._registry:
            ssh_cfg_host = self._try_load_from_ssh_config(host_name)
            if ssh_cfg_host is not None:
                self._registry[host_name] = ssh_cfg_host
                logger.info(f"Auto-registered host '{host_name}' from ~/.ssh/config")
            else:
                raise ValueError(
                    f"Unknown host: '{host_name}'. "
                    "Register it explicitly, define it in hosts.yaml, "
                    "or add a Host alias to ~/.ssh/config."
                )
        cfg = self._registry[host_name]
        lock = await self._get_lock(host_name)

        async with lock:
            pool = self._pool.get(host_name, [])
            now = time.time()

            # ── Prune dead, idle, and aged connections ──
            alive: list[PooledConnection] = []
            for pc in pool:
                if not pc.is_alive:
                    self._close_pc(pc, reason="dead")
                elif pc.in_use == 0 and (now - pc.last_used) > self._max_idle_time:
                    self._close_pc(pc, reason=f"idle {now - pc.last_used:.0f}s")
                elif pc.in_use == 0 and (now - pc.created_at) > self._max_conn_age:
                    self._close_pc(pc, reason=f"aged {now - pc.created_at:.0f}s")
                else:
                    alive.append(pc)
            self._pool[host_name] = alive

            # ── Reuse an alive connection with channel capacity ──
            for pc in alive:
                if pc.in_use < self._max_channels_per_conn:
                    pc.last_used = now
                    pc.in_use += 1
                    return pc.conn

            # ── Pool at capacity: overload the least-busy connection ──
            if len(alive) >= self._pool_size:
                least = min(alive, key=lambda p: p.in_use)
                logger.warning(
                    "Pool for '%s' at capacity (%d conns, all ≥%d channels); "
                    "overloading connection (in_use=%d→%d)",
                    host_name, self._pool_size,
                    self._max_channels_per_conn,
                    least.in_use, least.in_use + 1,
                )
                least.last_used = now
                least.in_use += 1
                return least.conn

            # ── Create new connection ──
            kwargs = await self._build_connect_kwargs(cfg)
            logger.info(f"Opening SSH connection to {host_name} ({cfg.user}@{cfg.host}:{cfg.port})")
            conn = await asyncssh.connect(**kwargs)
            pc = PooledConnection(
                host_name=host_name, conn=conn,
                created_at=now, last_used=now, in_use=1,
            )
            self._pool[host_name].append(pc)
            return conn

    @asynccontextmanager
    async def connection(self, host_name: str) -> AsyncIterator[asyncssh.SSHClientConnection]:
        """Context manager that acquires a pooled connection and auto-releases it.

        Use for short-lived operations::

            async with mgr.connection("myhost") as conn:
                result = await conn.run("whoami")
        """
        conn = await self.get_connection(host_name)
        try:
            yield conn
        finally:
            self.release_connection(host_name, conn)

    def release_connection(self, host_name: str, conn: asyncssh.SSHClientConnection):
        """Decrement in-use counter for a connection."""
        pool = self._pool.get(host_name, [])
        for pc in pool:
            if pc.conn is conn:
                pc.in_use = max(0, pc.in_use - 1)
                pc.last_used = time.time()
                return

    def _close_pc(self, pc: PooledConnection, *, reason: str = "") -> None:
        """Close a pooled connection, swallowing errors."""
        if reason:
            logger.debug("Pruning connection to '%s' (%s)", pc.host_name, reason)
        try:
            pc.conn.close()
        except Exception:  # pragma: no cover
            pass

    async def close_all(self):
        """Close all pooled connections gracefully."""
        for name, pool in self._pool.items():
            for pc in pool:
                try:
                    pc.conn.close()
                    await pc.conn.wait_closed()
                except Exception:
                    pass
        self._pool.clear()
        logger.info("All SSH connections closed")

    def pool_status(self) -> list[dict]:
        now = time.time()
        result = []
        for name, pool in self._pool.items():
            for pc in pool:
                result.append({
                    "host": name,
                    "alive": pc.is_alive,
                    "in_use": pc.in_use,
                    "age_s": round(now - pc.created_at, 1),
                    "idle_s": round(now - pc.last_used, 1),
                })
        return result

    @property
    def pool_config(self) -> dict:
        """Return current pool configuration for diagnostics."""
        return {
            "pool_size": self._pool_size,
            "max_channels_per_conn": self._max_channels_per_conn,
            "max_idle_time": self._max_idle_time,
            "max_conn_age": self._max_conn_age,
        }


# Module-level singleton
_manager: Optional[ConnectionManager] = None

def get_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        def _int_env(key: str, default: int) -> int:
            val = os.environ.get(key, "")
            return int(val) if val.isdigit() else default

        def _float_env(key: str, default: float) -> float:
            val = os.environ.get(key, "")
            try:
                return float(val) if val else default
            except ValueError:
                return default

        _manager = ConnectionManager(
            pool_size=_int_env("PORTAL_SSH_POOL_SIZE", 5),
            max_channels_per_conn=_int_env("PORTAL_SSH_MAX_CHANNELS_PER_CONN", DEFAULT_MAX_CHANNELS_PER_CONN),
            max_idle_time=_float_env("PORTAL_SSH_MAX_IDLE_TIME", DEFAULT_MAX_IDLE_TIME),
            max_conn_age=_float_env("PORTAL_SSH_MAX_CONN_AGE", DEFAULT_MAX_CONN_AGE),
        )
    return _manager
