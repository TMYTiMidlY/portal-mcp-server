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
from asyncssh.config import SSHClientConfig
import yaml

from .safety import normalize_host_name

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


class _SSHConfigAliasHarvester(SSHClientConfig):
    """Collect every explicitly-defined ``Host`` alias from OpenSSH client
    config, reusing asyncssh's own parser so ``Include`` directives, the
    tokenizer, and glob expansion all behave exactly like a real connection.

    asyncssh resolves options for a *single* host and discards the ``Host``
    patterns, so it exposes no "enumerate all hosts" API. The option dispatch
    table (``_handlers``) captures the base ``_match_host`` *function* at
    class-definition time, so overriding the method alone is ignored — we must
    re-point ``_handlers['host']`` at our collector. ``Include`` is added to
    ``_conditionals`` so included files are always followed regardless of the
    (sentinel-driven, irrelevant) match state. ``_default_path`` is left at
    asyncssh's ``~/.ssh`` default, which matches OpenSSH's relative-``Include``
    base for user configs (verified against openssh-portable ``readconf.c``).
    """

    _conditionals = SSHClientConfig._conditionals | {"include"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.harvested: list[str] = []

    def _collect_host(self, option, args):
        # ``args`` holds the raw Host patterns; capture them before the base
        # implementation clears the list, then defer to the real matching logic.
        self.harvested.extend(args)
        SSHClientConfig._match_host(self, option, args)

    _handlers = {**SSHClientConfig._handlers, "host": ("Host", _collect_host)}


def _is_concrete_alias(token: str) -> bool:
    """True for a real Host alias (not a ``*``/``?`` wildcard or ``!`` negation)."""
    return bool(token) and not (set("*?") & set(token)) and not token.startswith("!")


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
    # ``remote_exec(use_sudo=True)`` to feed ``sudo -S`` on stdin. The
    # alternative source is the per-user credential agent populated
    # out-of-band by ``portal sudo set`` (see sudo_creds.py).
    sudo_password_command: Optional[str] = None
    # Explicit operator opt-in: when true, ``portal ssh set <host>`` also
    # caches that same login password under the sudo credential kind. Default
    # stays false so sudo normally requires its own prompt/source.
    sudo_password_same_as_ssh: bool = False
    # ── Selected ssh_config-style connection options (forwarded to asyncssh
    # in ``_build_connect_kwargs`` for explicit, non-use_ssh_config hosts) ──
    # ProxyJump: "user@jump:port" or a bare alias -> asyncssh ``tunnel``.
    # Value semantics: unset -> defer to ssh_config ProxyJump (merge mode);
    # ``none`` -> force a DIRECT connection (overrides a config ProxyJump);
    # an empty/blank value is ambiguous and is refused at connection time.
    proxy_jump: Optional[str] = None
    # ServerAliveInterval equivalent (seconds). -> ``keepalive_interval``.
    keepalive_interval: Optional[int] = None
    # ForwardAgent. -> ``agent_forwarding``.
    forward_agent: Optional[bool] = None
    # ssh-agent usage for key auth. ``None`` = auto (asyncssh consults
    # SSH_AUTH_SOCK on its own, alongside key files); ``True`` = pure agent
    # (omit client_keys, authenticate with the keys the agent holds); ``False``
    # = hard-disable the agent (key files only). See ``_build_connect_kwargs``.
    use_ssh_agent: Optional[bool] = None
    # Run one-shot remote_exec commands in a LOGIN shell (``bash -lc``) so the
    # user's ``~/.profile`` / ``~/.bashrc`` environment (PATH additions for
    # conda / nvm / pyenv, …) is loaded. ``None`` = defer to PORTAL_LOGIN_SHELL
    # (on by default); ``True`` / ``False`` = per-host override. Ignored on
    # hosts without bash (the wrap degrades to a plain exec) and by remote_shell
    # (its persistent session is deliberately ``--norc`` for the OSC 133
    # boundary protocol).
    login_shell: Optional[bool] = None
    # Where this entry was declared, surfaced by ``list_hosts``: "hosts.yaml"
    # (the config file), "runtime" (a ``hosts`` register call), or
    # "ssh-config" (auto-resolved from the OpenSSH client config). Combined with
    # ``use_ssh_config`` to produce the list's ``source`` label.
    source: str = "hosts.yaml"
    # Names of the fields the user EXPLICITLY set in this hosts.yaml entry (the
    # raw yaml keys). The use_ssh_config merge uses this to decide which fields
    # override the ssh_config alias vs. defer to it — a HostConfig DEFAULT (e.g.
    # user="root", port=22) must NOT clobber the alias's User/Port.
    specified_fields: frozenset = field(default_factory=frozenset)


@dataclass
class PooledConnection:
    host_name: str
    conn: asyncssh.SSHClientConnection
    created_at: float
    last_used: float
    in_use: int = 0
    # Set when the host is reconfigured/removed while this connection is still
    # in use: it is never handed out again and is closed on release.
    stale: bool = False

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
        # sudo_password_command for the MCP server's OWN machine, read from a
        # TOP-LEVEL ``<local>:`` section in hosts.yaml (a reserved key, NOT a host
        # under ``hosts:``, so it never enters the host namespace). Consumed by
        # ``resolve_sudo_password("<local>")`` for ``local_exec(use_sudo=True)``.
        self._local_sudo_password_command: Optional[str] = None
        self._pool: dict[str, list[PooledConnection]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Bumped whenever a host is (re)registered or removed. get_connection
        # captures it before its connect await and discards a connection built
        # against a since-superseded config (see _invalidate_pool).
        self._generation: dict[str, int] = {}
        # Clamp to >=1: pool_size 0 would make the overload branch call
        # min() on an empty list and crash get_connection with an opaque
        # ValueError (PORTAL_SSH_POOL_SIZE="0" is otherwise accepted).
        self._pool_size = max(1, pool_size)
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
            name = normalize_host_name(name)
            warnings: list[str] = []
            use_ssh_config = bool(cfg.get("use_ssh_config", False))
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
            sudo_password_same_as_ssh = bool(
                cfg.get("sudo_password_same_as_ssh", False))
            if auth == "password" and not password_command:
                # NOT an error: 'auth: password' without 'password_command' is a
                # fully supported setup — the password is supplied out-of-band via
                # `portal ssh set <host>` (per-user credential agent). Record a
                # soft advisory only (surfaced by list_hosts / policy_check, NOT
                # logged to stderr) so it does not spam `portal ssh set` and the
                # other credential-CLI commands that load the registry. The
                # genuine "no password source" failure is still raised precisely
                # at connection time (see _build_connect_kwargs).
                warnings.append(
                    "uses 'auth: password' without a 'password_command' — "
                    f"connections rely on `portal ssh set {name}` having cached "
                    "a password in the per-user credential agent. For unattended "
                    "use, add a 'password_command:' that prints the password to "
                    f"stdout (e.g. 'pass show ssh/{name}')."
                )
            if auth not in (None, "password"):
                msg = (
                    f"unknown auth mode '{auth}'; expected None (key-based) or "
                    "'password'. Treating as key-based."
                )
                logger.error("Host '%s' has %s", name, msg)
                warnings.append(msg)
                auth = None
            proxy_jump = cfg.get("proxy_jump")
            if "proxy_jump" in cfg and not (proxy_jump or "").strip():
                # Present but empty/null -> ambiguous (force-direct vs. inherit
                # ssh_config). Normalise to a "" sentinel; the connection is
                # refused at connect time (see _build_connect_kwargs) and a config
                # advisory is surfaced now via list_hosts / policy_check.
                proxy_jump = ""
                warnings.append(
                    "has an empty 'proxy_jump:' — an empty value is ambiguous. "
                    "Connecting will be REFUSED until fixed: use 'proxy_jump: "
                    "none' to force a direct connection (overriding any ssh_config "
                    "ProxyJump), or remove the key to defer to ssh_config."
                )
            warnings.extend(self._overlay_warnings(name, use_ssh_config))
            if use_ssh_config and cfg.get("host"):
                try:
                    resolved_hn = self._resolve_ssh_config_fields(name).get("host")
                except Exception:
                    resolved_hn = None
                if resolved_hn and cfg.get("host") != resolved_hn:
                    warnings.append(
                        f"use_ssh_config is on and host: {cfg.get('host')!r} "
                        f"disagrees with the ssh_config alias's resolved HostName "
                        f"{resolved_hn!r}. Connecting to this host will be "
                        "REFUSED until they agree — set 'HostName' in the ssh "
                        "config stanza, or drop host: from this hosts.yaml entry "
                        "so it is inherited.")
            if warnings:
                self._config_warnings[name] = warnings
            self._registry[name] = HostConfig(
                name=name,
                host=cfg.get("host") or name,
                port=int(cfg.get("port", 22)),
                user=cfg.get("user", "root"),
                key=self._resolve_path(cfg.get("key")),
                connect_timeout=int(cfg.get("connect_timeout", 30)),
                known_hosts=cfg.get("known_hosts"),
                strict_host_key_checking=bool(cfg.get(
                    "strict_host_key_checking", True
                )),
                tags=cfg.get("tags", []),
                use_ssh_config=use_ssh_config,
                auth=auth,
                password_command=password_command,
                passphrase_command=passphrase_command,
                sudo_password_command=sudo_password_command,
                sudo_password_same_as_ssh=sudo_password_same_as_ssh,
                proxy_jump=proxy_jump,
                keepalive_interval=(int(cfg["keepalive_interval"])
                                    if cfg.get("keepalive_interval") is not None
                                    else None),
                forward_agent=(bool(cfg["forward_agent"])
                               if cfg.get("forward_agent") is not None
                               else None),
                use_ssh_agent=(bool(cfg["use_ssh_agent"])
                               if cfg.get("use_ssh_agent") is not None
                               else None),
                login_shell=(bool(cfg["login_shell"])
                             if cfg.get("login_shell") is not None
                             else None),
                specified_fields=frozenset(cfg.keys()),
                source="hosts.yaml",
            )
        # Top-level ``<local>:`` section (a reserved key sibling of ``hosts:``,
        # NOT a host under it) carries the sudo_password_command for THIS machine's
        # local_exec sudo. It is not a connectable host, so it stays out of the
        # registry. The reserved key matches the ``<local>`` credential identity.
        local_cfg = data.get("<local>")
        if isinstance(local_cfg, dict):
            self._local_sudo_password_command = (
                local_cfg.get("sudo_password_command") or None)
        logger.info(f"Loaded {len(self._registry)} hosts from registry")

    def _resolve_path(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        return str(Path(path).expanduser())

    # Options that mark a name as an *explicitly configured* Host (vs one merely
    # caught by a `Host *` wildcard). Compared against a never-defined sentinel
    # so wildcard-applied values cancel out and only explicit stanzas remain.
    _SSH_CONFIG_MARKER_OPTS = ("Hostname", "User", "Port", "IdentityFile",
                               "ProxyJump", "ProxyCommand")
    _SSH_CONFIG_SENTINEL = "zz-portal-mcp-sentinel-9d3f-no-such-host"

    def _ssh_config_files(self) -> list[Path]:
        """OpenSSH client config files to consult, in precedence order.

        Delegates to :func:`paths.ssh_config_files`: ``PORTAL_SSH_CONFIG`` acts
        as the ``ssh -F`` analogue, else user ``~/.ssh/config`` + system-wide
        ``/etc/ssh/ssh_config`` fallback. Only existing, readable files are
        returned so asyncssh's loader never trips over a missing path.
        """
        from .paths import ssh_config_files
        return ssh_config_files()

    def _ssh_config_label(self) -> str:
        """User-facing name of the active OpenSSH client config source (honours
        ``PORTAL_SSH_CONFIG``), for warnings/messages — see
        :func:`paths.ssh_config_source_label`."""
        from .paths import ssh_config_source_label
        return ssh_config_source_label()

    def _parse_ssh_config(self, config: SSHClientConfig,
                          files: list[Path]) -> None:
        """Parse ``files`` into ``config`` in order, each with its own directory
        as the relative-``Include`` base.

        Mirrors ``SSHClientConfig.load`` but sets ``_default_path`` per file so a
        relative ``Include`` resolves against that file's directory — matching
        OpenSSH, which anchors user-config includes at ``~/.ssh`` and
        system-config includes at ``/etc/ssh`` (openssh-portable ``readconf.c``).
        """
        for path in files:
            config._default_path = path.parent
            config.parse(path)
        config.loaded = True

    def _load_ssh_config(self, files: list[Path], name: str) -> SSHClientConfig:
        """asyncssh ``SSHClientConfig`` resolved for ``name`` across ``files``.

        Parse-only — unlike the full connection path it does NOT load
        IdentityFile keys, so an absent/encrypted key can't make detection throw.
        ``user``/``port`` are passed as ``()`` (asyncssh's "unspecified") so the
        config's own User/Port directives resolve.
        """
        import getpass
        cfg = SSHClientConfig(None, False, False, False,
                              getpass.getuser(), (), name, ())
        self._parse_ssh_config(cfg, files)
        return cfg

    def _ssh_config_signature(self, files: list[Path], name: str) -> tuple:
        """Resolve the marker options for ``name`` via asyncssh's own parser.

        ``Include`` directives are followed natively and ``files`` are parsed in
        OpenSSH precedence order (user before system).
        """
        cfg = self._load_ssh_config(files, name)
        out = []
        for key in self._SSH_CONFIG_MARKER_OPTS:
            try:
                out.append(repr(cfg.get(key)))
            except Exception:  # pragma: no cover - unknown option key
                out.append(None)
        return tuple(out)

    def has_ssh_config_alias(self, name: str) -> bool:
        """True if ``name`` is an explicitly configured ``Host`` in the OpenSSH
        client config (user config + system fallback; ``PORTAL_SSH_CONFIG`` aware).

        Uses asyncssh's config parser so ``Include`` directives are followed (the
        old hand-rolled line scan missed them). A name counts as explicit when
        its resolved marker options differ from a never-defined sentinel host —
        i.e. something more than a ``Host *`` wildcard matched it. Falls back to a
        regex line scan (no Include) only if asyncssh can't parse the files.
        """
        files = self._ssh_config_files()
        if not files:
            return False
        try:
            sentinel = self._ssh_config_signature(files, self._SSH_CONFIG_SENTINEL)
            candidate = self._ssh_config_signature(files, name)
        except Exception:
            logger.debug("asyncssh ssh-config parse failed; regex fallback",
                         exc_info=True)
            return self._regex_ssh_config_alias(name, files)
        return candidate != sentinel

    def enumerate_ssh_config_aliases(
            self, files: Optional[list[Path]] = None) -> list[str]:
        """Every explicitly-defined ``Host`` alias across the OpenSSH client config.

        Reuses asyncssh's parser via :class:`_SSHConfigAliasHarvester` (so
        ``Include`` is followed and tokenizing/globbing match a real connection),
        excluding wildcard/negated patterns. Order is preserved and de-duped.
        Falls back to a regex scan (no Include) if asyncssh can't parse a file.
        """
        if files is None:
            files = self._ssh_config_files()
        if not files:
            return []
        import getpass
        try:
            harvester = _SSHConfigAliasHarvester(
                None, False, False, False, getpass.getuser(), (),
                self._SSH_CONFIG_SENTINEL, ())
            self._parse_ssh_config(harvester, files)
            raw: list[str] = harvester.harvested
        except Exception:
            logger.debug("asyncssh ssh-config harvest failed; regex fallback",
                         exc_info=True)
            raw = self._regex_harvest_aliases(files)
        names: list[str] = []
        seen: set[str] = set()
        for tok in raw:
            if _is_concrete_alias(tok) and tok not in seen:
                seen.add(tok)
                names.append(tok)
        return names

    def _resolve_ssh_config_fields(self, name: str,
                                   files: Optional[list[Path]] = None) -> dict:
        """Resolve real HostName/User/Port for an ssh-config alias via asyncssh.

        Returns connection params as a real ssh connection would see them:
        ``host`` falls back to the alias name (OpenSSH uses the alias as the
        HostName when none is set), ``user`` to the local login user, ``port`` to
        22. Resilient: any parse failure yields the alias-name defaults.
        """
        import getpass
        if files is None:
            files = self._ssh_config_files()
        host: str = name
        user: Optional[str] = None
        port: object = 22
        if files:
            try:
                cfg = self._load_ssh_config(files, name)
                host = cfg.get("Hostname") or name
                user = cfg.get("User")
                port = cfg.get("Port") or 22
            except Exception:
                logger.debug("ssh-config field resolve failed for %r", name,
                             exc_info=True)
        if not user:
            try:
                user = getpass.getuser()
            except Exception:
                user = "root"
        return {"host": host, "user": user, "port": int(port)}

    def _regex_ssh_config_alias(self, name: str,
                                files: Optional[list[Path]] = None) -> bool:
        """Degraded fallback (does NOT follow Include); used only when asyncssh
        cannot parse the config files."""
        if files is None:
            files = self._ssh_config_files()
        return name in set(self._regex_harvest_aliases(files))

    def _regex_harvest_aliases(self, files: list[Path]) -> list[str]:
        """Degraded enumeration (no Include) for when asyncssh can't parse a file.

        Returns concrete alias tokens (wildcards/negations excluded) across all
        files, in order.
        """
        out: list[str] = []
        for path in files:
            try:
                content = path.read_text()
            except OSError:
                continue
            for line in content.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                m = re.match(r"^Host\s+(.+)$", s, re.IGNORECASE)
                if m:
                    out.extend(p for p in m.group(1).split()
                               if _is_concrete_alias(p))
        return out

    def _overlay_warnings(self, name: str, use_ssh_config: bool) -> list[str]:
        """Warnings about hosts.yaml <-> ~/.ssh/config interactions.

        Two footguns we surface (never block):
          * use_ssh_config: true but no matching alias -> asyncssh falls back
            to default DNS+user+key for ``name`` (probably not intended).
          * a hosts.yaml host that ALSO exists in ssh config -> hosts.yaml
            silently wins (ssh config's IdentityFile/ProxyJump/User are
            ignored); the fix is the use_ssh_config overlay recipe.
        """
        exists = self.has_ssh_config_alias(name)
        label = self._ssh_config_label()
        if use_ssh_config and not exists:
            return [(f"host '{name}' sets use_ssh_config: true but {label} "
                     "has no matching Host alias; asyncssh will fall back to a "
                     f"default connection (DNS lookup of '{name}', default user "
                     "and key), which is probably not what you intended. Add a "
                     f"`Host {name}` stanza to {label} or set host/user/"
                     "port explicitly in hosts.yaml.")]
        if (not use_ssh_config) and exists:
            return [(f"host '{name}' is defined in BOTH hosts.yaml and "
                     f"{label}; hosts.yaml wins and ssh config is IGNORED for it "
                     "(no merge unless you opt in). To MERGE — inherit the ssh "
                     "config alias's HostName / IdentityAgent / ProxyJump / … "
                     "with hosts.yaml fields overriding on top — set "
                     "`use_ssh_config: true` on this host.")]
        return []

    def login_shell_for(self, name: str) -> Optional[bool]:
        """Per-host login-shell override (hosts.yaml ``login_shell``), or None
        if the host isn't registered / didn't set it. ``None`` means the caller
        should fall back to the PORTAL_LOGIN_SHELL default."""
        cfg = self._registry.get(normalize_host_name(name))
        return cfg.login_shell if cfg is not None else None

    def register_host(self, name: str, host: str = "", user: str = "root",
                      port: int = 22, key: Optional[str] = None,
                      tags: list = None,
                      known_hosts: Optional[str] = None,
                      strict_host_key_checking: bool = True,
                      use_ssh_config: bool = False) -> str:
        """Dynamically register a new host into the registry.

        Password authentication is intentionally not supported; provide a
        key file via ``key`` or rely on default SSH key locations / agent.
        With ``use_ssh_config=True`` the connection params come from
        ~/.ssh/config and ``host`` may be omitted (defaults to ``name``).
        """
        name = normalize_host_name(name)
        self._registry[name] = HostConfig(
            name=name, host=host or name, port=port, user=user,
            key=self._resolve_path(key),
            tags=tags or [],
            known_hosts=known_hosts,
            strict_host_key_checking=strict_host_key_checking,
            use_ssh_config=use_ssh_config,
            source="runtime",
        )
        warns = self._overlay_warnings(name, use_ssh_config)
        if warns:
            self._config_warnings[name] = warns
            for w in warns:
                logger.warning("Host '%s': %s", name, w)
        elif name in self._config_warnings:
            # Re-registering cleanly clears a stale overlay warning.
            self._config_warnings.pop(name, None)
        # Re-registration may change the dial address / user / auth, so any
        # pooled connection is now to a superseded target: bump the generation
        # and invalidate the pool so later ops don't reuse the old connection.
        self._generation[name] = self._generation.get(name, 0) + 1
        self._invalidate_pool(name)
        if use_ssh_config:
            label = self._ssh_config_label()
            logger.info(f"Registered host: {name} (via {label})")
            return f"Host '{name}' registered (connection via {label})"
        logger.info(f"Registered host: {name} ({user}@{host}:{port})")
        return f"Host '{name}' registered: {user}@{host}:{port}"

    @staticmethod
    def _display_source(h: HostConfig) -> str:
        """``list_hosts`` source label combining declaration origin and whether
        the connection params are resolved from ssh config.

        ``hosts.yaml`` / ``runtime`` / ``ssh-config`` for the plain cases; the
        ``+ssh-config`` suffix (e.g. ``hosts.yaml+ssh-config``) marks a
        ``use_ssh_config`` overlay whose metadata lives in the declared origin
        but whose host/user/port come from the OpenSSH client config.
        """
        origin = h.source or "hosts.yaml"
        if origin == "ssh-config":
            return "ssh-config"
        if h.use_ssh_config:
            return f"{origin}+ssh-config"
        return origin

    def list_hosts(self) -> list[dict]:
        """All known hosts: the explicit registry (hosts.yaml + runtime) plus
        every alias discoverable in the OpenSSH client config.

        Each entry carries a ``source`` field. For ``use_ssh_config`` overlays
        and ssh-config-only aliases the host/user/port are resolved from the ssh
        config (the same files a real connection reads) rather than shown as the
        placeholder alias name.
        """
        files = self._ssh_config_files()
        out = []
        seen = set()
        for h in self._registry.values():
            seen.add(h.name)
            if h.use_ssh_config:
                fields = self._resolve_ssh_config_fields(h.name, files)
                host, user, port = fields["host"], fields["user"], fields["port"]
            else:
                host, user, port = h.host, h.user, h.port
            entry = {"name": h.name, "host": host, "port": port,
                     "user": user, "tags": h.tags,
                     "source": self._display_source(h)}
            warns = self._config_warnings.get(h.name)
            if warns:
                entry["warnings"] = warns
            out.append(entry)
        # ssh-config aliases not already shadowed by a registry/hosts.yaml entry.
        for name in self.enumerate_ssh_config_aliases(files):
            if name in seen:
                continue
            seen.add(name)
            fields = self._resolve_ssh_config_fields(name, files)
            out.append({"name": name, "host": fields["host"],
                        "port": fields["port"], "user": fields["user"],
                        "tags": ["ssh-config"], "source": "ssh-config"})
        return out

    def should_cache_ssh_password_as_sudo(self, host_name: str) -> bool:
        """Whether ``portal ssh set`` should also seed the sudo cache.

        This is deliberately config-only. Unknown hosts and ssh-config-only
        aliases default to false unless they have an explicit hosts.yaml entry.
        """
        cfg = self._registry.get(normalize_host_name(host_name))
        return bool(cfg and cfg.sudo_password_same_as_ssh)

    def config_warnings(self) -> dict[str, list[str]]:
        """host -> config warnings collected at registry load time."""
        return {k: list(v) for k, v in self._config_warnings.items()}

    def remove_host(self, name: str) -> str:
        name = normalize_host_name(name)
        if name not in self._registry:
            return f"Host '{name}' not found"
        del self._registry[name]
        # Drop the config warnings (re-registering the same alias cleanly later
        # must not resurrect stale diagnostics), bump the generation, and
        # invalidate pooled connections so we don't leak SSH channels for an
        # alias the caller has explicitly forgotten. Idle connections close now;
        # in-use ones are marked stale and close on release (so we never yank a
        # connection out from under an in-flight command).
        self._config_warnings.pop(name, None)
        self._generation[name] = self._generation.get(name, 0) + 1
        self._invalidate_pool(name)
        # Drop the lazy lock last (otherwise ``_locks`` grows indefinitely if
        # hosts churn); a concurrent waiter already holds the same lock object.
        self._locks.pop(name, None)
        return f"Host '{name}' removed from registry"

    def _invalidate_pool(self, name: str) -> None:
        """Retire pooled connections for a host whose config changed/was removed.

        Idle connections are closed immediately; connections still in use are
        marked ``stale`` (kept so :meth:`release_connection` can close them when
        the in-flight op finishes) and are never handed out again."""
        pool = self._pool.get(name)
        if not pool:
            self._pool.pop(name, None)
            return
        keep: list[PooledConnection] = []
        for pc in pool:
            if pc.in_use == 0:
                self._close_pc(pc, reason="host reconfigured/removed")
            else:
                pc.stale = True
                keep.append(pc)
        if keep:
            self._pool[name] = keep
        else:
            self._pool.pop(name, None)

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
        credential agent populated by ``portal sudo set``). Raises only if the
        command itself fails, matching ``_run_secret_command`` semantics.

        The reserved local identity ``<local>`` resolves to the top-level
        ``local:`` section's ``sudo_password_command`` instead of the host
        registry (used by ``local_exec(use_sudo=True)``).
        """
        from .sudo_creds import LOCAL_SUDO_KEY
        host_name = normalize_host_name(host_name)
        if host_name == LOCAL_SUDO_KEY:
            cmd = self._local_sudo_password_command
            if not cmd:
                return None
            return await self._run_secret_command(
                cmd, host=host_name, kind="sudo_password_command",
            )
        cfg = self._registry.get(host_name)
        if cfg is None or not cfg.sudo_password_command:
            return None
        return await self._run_secret_command(
            cfg.sudo_password_command, host=host_name, kind="sudo_password_command",
        )

    async def _resolve_ssh_password(self, cfg: HostConfig) -> Optional[str]:
        """SSH-login password chain: local cache → credential agent → host's
        ``password_command``. Returns ``None`` only when neither source is
        available; a configured ``password_command`` that fails raises
        ``RuntimeError`` (host-named, exit-code only; never the secret).

        Takes a ``HostConfig`` (not just a host name) so callers exercising a
        fresh, un-registered ``HostConfig`` — including the unit tests — keep
        working without going through the module-level singleton.
        """
        from .ssh_creds import get_cached_password, fetch_ssh_password_from_agent
        pw = get_cached_password(cfg.name)
        if pw is not None:
            return pw
        pw = await asyncio.to_thread(fetch_ssh_password_from_agent, cfg.name)
        if pw is not None:
            return pw
        if cfg.password_command:
            return await self._run_secret_command(
                cfg.password_command, host=cfg.name, kind="password_command",
            )
        return None

    async def _resolve_ssh_passphrase(self, cfg: HostConfig) -> Optional[str]:
        """Key-passphrase chain.

        This intentionally uses a different credential-agent kind from the SSH
        login password: a passphrase unlocks a local private key, while a login
        password is sent to the remote SSH server. Order: local cache →
        credential-agent cache populated by ``portal passphrase set <host>`` →
        ``passphrase_command`` in hosts.yaml. Returns ``None`` when neither is
        set (asyncssh then relies on the ssh-agent or an unencrypted key).
        """
        from .passphrase_creds import (
            get_cached_passphrase, fetch_passphrase_from_agent)
        pp = get_cached_passphrase(cfg.name)
        if pp is not None:
            return pp
        pp = await asyncio.to_thread(fetch_passphrase_from_agent, cfg.name)
        if pp is not None:
            return pp
        if cfg.passphrase_command:
            return await self._run_secret_command(
                cfg.passphrase_command, host=cfg.name, kind="passphrase_command",
            )
        return None

    def _guard_hostname_merge(self, cfg: "HostConfig") -> None:
        """Merge (use_ssh_config) HostName guard: hosts.yaml MAY omit host: to
        inherit the alias's HostName, but if it sets host: it MUST agree with the
        ssh_config alias's resolved HostName — otherwise refuse rather than
        silently pick one. Raised at connection time (execution/connection tools
        hard-fail); ``list`` / ``check`` surface the same conflict as a warning."""
        if "host" not in cfg.specified_fields:
            return
        resolved = self._resolve_ssh_config_fields(cfg.name).get("host")
        if resolved and cfg.host != resolved:
            raise RuntimeError(
                f"Host '{cfg.name}': use_ssh_config is on and hosts.yaml sets "
                f"host: {cfg.host!r}, but the ssh_config alias resolves HostName "
                f"to {resolved!r}. For a merged host the two must agree — either "
                f"set 'HostName {cfg.host}' under 'Host {cfg.name}' in your ssh "
                "config, or drop host: from this hosts.yaml entry so it is "
                "inherited from ssh_config.")

    async def _build_connect_kwargs(self, cfg: HostConfig) -> dict:
        if cfg.use_ssh_config:
            # MERGE (opt-in): connect with host=<alias> + the ssh config files so
            # asyncssh matches `Host <alias>` and inherits its options (HostName /
            # User / Port / IdentityFile / IdentityAgent / ProxyJump / …).
            # hosts.yaml fields layer ON TOP as explicit kwargs (asyncssh
            # precedence: explicit kwarg > config), but ONLY the fields the user
            # actually set — an unset field defers to ssh_config instead of
            # clobbering it with a HostConfig default. An empty file list
            # (PORTAL_SSH_CONFIG=none / no config) is passed as-is so asyncssh
            # reads NOTHING rather than re-triggering its ~/.ssh/config default.
            self._guard_hostname_merge(cfg)
            kwargs = dict(
                host=cfg.name,
                connect_timeout=cfg.connect_timeout,
                known_hosts=self._known_hosts_arg(cfg),
                config=[str(p) for p in self._ssh_config_files()],
            )
            spec = cfg.specified_fields
            if "user" in spec:
                kwargs["username"] = cfg.user
            if "port" in spec:
                kwargs["port"] = cfg.port
        else:
            kwargs = dict(
                host=cfg.host, port=cfg.port, username=cfg.user,
                connect_timeout=cfg.connect_timeout,
                known_hosts=self._known_hosts_arg(cfg),
            )

        # ── Optional ssh_config-style connection options ──
        # ProxyJump -> tunnel, ServerAliveInterval -> keepalive_interval,
        # ForwardAgent -> agent_forwarding. Applied to every auth mode below.
        #
        # ProxyJump uses VALUE semantics (not truthiness), mirroring the
        # `is not None` gating of keepalive_interval / forward_agent below, so an
        # explicit "none" can force-disable a config-inherited ProxyJump:
        #   • None   -> not set: don't pass `tunnel`, so asyncssh keeps its `()`
        #               default and (merge mode) DEFERS to the ssh_config ProxyJump.
        #   • "none" -> force a DIRECT connection: pass `tunnel=None`, which
        #               asyncssh treats as "no jump" (tunnel != () short-circuits
        #               config.get('ProxyJump')), OVERRIDING any config ProxyJump.
        #   • ""     -> ambiguous empty value (normalised at load): REFUSE.
        #   • str    -> use it as the jump host (overrides any config ProxyJump).
        #
        # KNOWN LIMITATION (string jump only): asyncssh opens the jump with its
        # DEFAULT auth (default key files / ssh-agent) and reuses the SAME
        # passphrase resolved for THIS target — it does NOT read a jump-specific
        # IdentityFile from ssh_config, and applying the target's passphrase to a
        # differently-encrypted jump key fails with "Incorrect passphrase". For a
        # jump that needs its own key/passphrase, set `use_ssh_config: true` (then
        # asyncssh reads the jump's own `Host` block) and load the jump key into
        # ssh-agent (agent auth needs no passphrase). See README "ProxyJump 凭据".
        if cfg.proxy_jump is not None:
            pj = cfg.proxy_jump.strip()
            if pj == "":
                raise RuntimeError(
                    f"Host '{cfg.name}' has an empty 'proxy_jump:'; refusing to "
                    "connect. Use 'proxy_jump: none' to force a direct connection "
                    "(overriding any ssh_config ProxyJump), or remove the key to "
                    "defer to ssh_config.")
            if pj.lower() == "none":
                kwargs["tunnel"] = None
            else:
                kwargs["tunnel"] = cfg.proxy_jump
        if cfg.keepalive_interval is not None:
            kwargs["keepalive_interval"] = cfg.keepalive_interval
        if cfg.forward_agent is not None:
            kwargs["agent_forwarding"] = cfg.forward_agent

        # ── Password auth (opt-in via `auth: password`) ────────────────
        # The side-channel password chain: credential agent (populated by
        # `portal ssh set <host>` in a separate terminal) →
        # hosts.yaml `password_command`. Either alone is enough; if both are
        # absent, refuse rather than silently falling back to key auth.
        if cfg.auth == "password":
            pw = await self._resolve_ssh_password(cfg)
            if pw is None:
                raise RuntimeError(
                    f"Host '{cfg.name}' has 'auth: password' but no password "
                    "source is available; the connection was NOT made. Ask the "
                    "user to provide it out-of-band — never have them paste the "
                    "password into this conversation. Two ways:\n"
                    f"  • Temporary (no-echo, TTL-cached): run `portal ssh set "
                    f"{cfg.name}` in a separate terminal, type the password at "
                    "the hidden prompt, then retry.\n"
                    "  • Permanent: add a 'password_command:' to hosts.yaml (a "
                    "shell command that prints the password to stdout).\n"
                    "Prefer an interactive input tool (e.g. ask_user) to ask the "
                    "user to run the first command and confirm when done; if you "
                    "have no such tool, tell them what to run and end your turn "
                    "to wait."
                )
            kwargs["password"] = pw
            # Disable client_keys so asyncssh does not silently fall back to
            # default key locations and, on success, mask a misconfigured
            # password source.
            kwargs["client_keys"] = []
            return kwargs

        # ── Key-based auth (the default and recommended path) ──
        # ssh-agent control (use_ssh_agent): None = auto, True = pure agent,
        # False = hard-disable the agent.
        if cfg.use_ssh_agent is False:
            # asyncssh normalises ``None`` → ``''`` for agent_path but still
            # constructs an ``SSHAgentClient('')`` at auth time; that client's
            # ``open_agent('')`` then fails with ``OSError(ENOENT)`` which gets
            # wrapped to ``ValueError`` and silently swallowed by the
            # auth loop. Net effect: no agent keys are ever offered. Setting
            # ``agent_path`` to anything truthy would re-enable it.
            kwargs["agent_path"] = None

        if cfg.use_ssh_agent is True:
            # Pure ssh-agent: omit client_keys so asyncssh authenticates with
            # the keys the agent holds via SSH_AUTH_SOCK (the user ran
            # `ssh-add`). The key never leaves the agent.
            pass
        elif cfg.key:
            kwargs["client_keys"] = [cfg.key]
        elif not cfg.use_ssh_config:
            # No explicit key file (and not a merge host): enumerate the usual
            # default keys. NOTE: asyncssh ALSO consults the ssh-agent
            # (SSH_AUTH_SOCK) by default, so an agent-held key authenticates here
            # too even though this loop only lists files. A merge host is skipped
            # on purpose so asyncssh reads IdentityFile / IdentityAgent from the
            # ssh config instead of being pinned to the default key files.
            default_keys = []
            for k in ["~/.ssh/id_ed25519", "~/.ssh/id_rsa", "~/.ssh/id_ecdsa"]:
                kp = Path(k).expanduser()
                if kp.exists():
                    default_keys.append(str(kp))
            if default_keys:
                kwargs["client_keys"] = default_keys

        # Encrypted private keys: resolve a passphrase through its own side
        # channel (agent cache → passphrase_command).
        # Skipped for the pure-agent path (the agent already holds the
        # decrypted key, so no passphrase is needed here).
        if cfg.use_ssh_agent is not True:
            passphrase = await self._resolve_ssh_passphrase(cfg)
            if passphrase is not None:
                kwargs["passphrase"] = passphrase

        return kwargs

    def _try_load_from_ssh_config(self, host_name: str) -> Optional[HostConfig]:
        """Check whether host_name is an explicit alias in ~/.ssh/config.
        Returns a synthetic HostConfig (use_ssh_config=True) if so, else None.

        Detection is delegated to has_ssh_config_alias, which uses asyncssh's
        own parser (Include-aware). The actual connection params are resolved by
        asyncssh at connect time (use_ssh_config -> host=alias).
        """
        if not self.has_ssh_config_alias(host_name):
            return None
        return HostConfig(
            name=host_name,
            host=host_name,  # placeholder; asyncssh ignores when use_ssh_config
            use_ssh_config=True,
            tags=["ssh-config"],
            source="ssh-config",
        )

    async def get_connection(self, host_name: str) -> asyncssh.SSHClientConnection:
        """Get or create a pooled connection to a host.
        Resolution order:
          1. Explicitly registered host (registry / hosts.yaml)
          2. Alias defined in ~/.ssh/config (auto-registered on first use)
        """
        host_name = normalize_host_name(host_name)
        if host_name not in self._registry:
            ssh_cfg_host = self._try_load_from_ssh_config(host_name)
            if ssh_cfg_host is not None:
                self._registry[host_name] = ssh_cfg_host
                logger.info(f"Auto-registered host '{host_name}' from "
                            f"{self._ssh_config_label()}")
            else:
                raise ValueError(
                    f"Unknown host: '{host_name}'. "
                    "Register it explicitly, define it in hosts.yaml, "
                    f"or add a Host alias to {self._ssh_config_label()}."
                )
        cfg = self._registry[host_name]
        gen = self._generation.get(host_name, 0)
        lock = await self._get_lock(host_name)

        superseded_conn = None
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
            # Stale connections (host reconfigured mid-use) are kept for release
            # bookkeeping but never handed out again or counted toward capacity.
            usable = [pc for pc in alive if not pc.stale]

            # ── Reuse an alive connection with channel capacity ──
            for pc in usable:
                if pc.in_use < self._max_channels_per_conn:
                    pc.last_used = now
                    pc.in_use += 1
                    return pc.conn

            # ── Pool at capacity: overload the least-busy connection ──
            if usable and len(usable) >= self._pool_size:
                least = min(usable, key=lambda p: p.in_use)
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
            try:
                conn = await asyncssh.connect(**kwargs)
            except asyncssh.PermissionDenied:
                # Key auth refused. If this is a key-mode host (or a
                # ~/.ssh/config alias) AND we have a side-channel password
                # (`portal ssh set` cache or hosts.yaml password_command),
                # retry once with password auth. `auth: password` hosts
                # already ran the password chain inside _build_connect_kwargs, so
                # skip the retry to avoid masking a wrong password.
                if cfg.auth == "password":
                    raise
                pw = await self._resolve_ssh_password(cfg)
                if pw is None:
                    raise
                logger.info(
                    "Key auth refused by '%s'; retrying with side-channel "
                    "password (`portal ssh set` cache or password_command).",
                    host_name,
                )
                pw_kwargs = dict(kwargs)
                pw_kwargs["password"] = pw
                pw_kwargs["client_keys"] = []
                pw_kwargs.pop("passphrase", None)
                conn = await asyncssh.connect(**pw_kwargs)
            # The host may have been reconfigured/removed during the connect
            # await (register_host / remove_host bump the generation). If so this
            # connection is to a now-superseded target — discard and retry
            # against the current config instead of pooling a stale connection.
            if self._generation.get(host_name, 0) == gen:
                pc = PooledConnection(
                    host_name=host_name, conn=conn,
                    created_at=now, last_used=now, in_use=1,
                )
                self._pool.setdefault(host_name, []).append(pc)
                return conn
            superseded_conn = conn

        # (lock released) generation changed mid-connect → drop this connection
        # and rebuild against the current config.
        if superseded_conn is not None:
            try:
                superseded_conn.close()
            except Exception:  # pragma: no cover
                pass
        logger.info("Config for '%s' changed during connect; reconnecting",
                    host_name)
        return await self.get_connection(host_name)

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
                # A connection retired while in use (host reconfigured/removed)
                # is closed once its last channel is released; the next prune
                # drops it from the pool.
                if pc.stale and pc.in_use == 0:
                    self._close_pc(pc, reason="stale released")
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
        # Snapshot and clear under no await first: an in-flight get_connection
        # for a new host during shutdown would otherwise mutate self._pool mid
        # iteration ("dictionary changed size during iteration"), aborting
        # cleanup early.
        pools = list(self._pool.values())
        self._pool.clear()
        for pool in pools:
            for pc in pool:
                try:
                    pc.conn.close()
                    await pc.conn.wait_closed()
                except Exception:
                    pass
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
