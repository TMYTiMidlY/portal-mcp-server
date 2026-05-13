"""
Connection Manager — SSH connection pool, host registry, key-auth.
Manages persistent AsyncSSH connections to multiple remote hosts.
"""
import asyncio
import os
import logging
import subprocess
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

import asyncssh
import yaml

logger = logging.getLogger("portal_mcp.connections")

# Hard ceiling for password_command / passphrase_command execution. Long enough
# for an interactive `pass show` (which may unlock the GPG agent) but short
# enough that a misconfigured command does not silently hang the server.
SECRET_COMMAND_TIMEOUT_SEC = 10


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
    """

    def __init__(self, hosts_yaml: str | os.PathLike | None = None, pool_size: int = 5):
        from .paths import hosts_yaml_path
        self._registry: dict[str, HostConfig] = {}
        self._pool: dict[str, list[PooledConnection]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pool_size = pool_size
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
            if "password" in cfg:
                logger.error(
                    "Host '%s' has 'password' field in hosts.yaml — plaintext "
                    "password fields are not supported (would leak credentials "
                    "into config files and backups). Use 'auth: password' + "
                    "'password_command:' instead. The 'password' field is "
                    "being IGNORED.",
                    name,
                )
            auth = cfg.get("auth")
            password_command = cfg.get("password_command")
            passphrase_command = cfg.get("passphrase_command")
            if auth == "password" and not password_command:
                logger.error(
                    "Host '%s' declares 'auth: password' but has no "
                    "'password_command' — the host is loaded but connection "
                    "attempts will fail. Add a 'password_command:' that "
                    "prints the password to stdout (e.g. 'pass show ssh/%s' "
                    "or 'printf %%s \"$%s_PASSWORD\"').",
                    name, name, name.upper(),
                )
            if auth not in (None, "password"):
                logger.error(
                    "Host '%s' has unknown auth mode '%s'; expected None "
                    "(key-based) or 'password'. Treating as key-based.",
                    name, auth,
                )
                auth = None
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
        return [
            {"name": h.name, "host": h.host, "port": h.port,
             "user": h.user, "tags": h.tags}
            for h in self._registry.values()
        ]

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
                f"{kind} for host '{host}' timed out after "
                f"{SECRET_COMMAND_TIMEOUT_SEC}s"
            ) from None

        if result.returncode != 0:
            raise RuntimeError(
                f"{kind} for host '{host}' exited with code "
                f"{result.returncode}"
            )

        try:
            secret = result.stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            # Don't surface the offending bytes — they may contain the secret
            # on misconfigured commands (e.g. a binary key file printed by
            # mistake). Give the operator enough to fix the config.
            raise RuntimeError(
                f"{kind} for host '{host}' produced non-UTF-8 output. "
                "Ensure the command writes a plain-text secret to stdout."
            ) from None
        # Most secret stores append a single trailing newline. Strip exactly
        # one so passwords that legitimately end in whitespace survive.
        if secret.endswith("\r\n"):
            secret = secret[:-2]
        elif secret.endswith("\n"):
            secret = secret[:-1]
        if not secret:
            raise RuntimeError(
                f"{kind} for host '{host}' produced empty output"
            )
        return secret

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
        import re
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
            # Reuse alive, available connection
            for pc in pool:
                if pc.is_alive and pc.in_use < self._pool_size:
                    import time
                    pc.last_used = time.time()
                    pc.in_use += 1
                    return pc.conn
            # Prune dead connections
            self._pool[host_name] = [p for p in pool if p.is_alive]
            # Create new connection
            import time
            kwargs = await self._build_connect_kwargs(cfg)
            logger.info(f"Opening SSH connection to {host_name} ({cfg.user}@{cfg.host}:{cfg.port})")
            conn = await asyncssh.connect(**kwargs)
            pc = PooledConnection(
                host_name=host_name, conn=conn,
                created_at=time.time(), last_used=time.time(), in_use=1
            )
            self._pool.setdefault(host_name, []).append(pc)
            return conn

    def release_connection(self, host_name: str, conn: asyncssh.SSHClientConnection):
        """Decrement in-use counter for a connection."""
        for pc in self._pool.get(host_name, []):
            if pc.conn is conn:
                pc.in_use = max(0, pc.in_use - 1)
                return

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
        import time
        result = []
        for name, pool in self._pool.items():
            for pc in pool:
                result.append({
                    "host": name,
                    "alive": pc.is_alive,
                    "in_use": pc.in_use,
                    "age_s": round(time.time() - pc.created_at, 1),
                    "idle_s": round(time.time() - pc.last_used, 1),
                })
        return result


# Module-level singleton
_manager: Optional[ConnectionManager] = None

def get_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager
