"""
portal-mcp-server — Agent-feels-local SSH orchestration MCP server.
Exposes 14 portal_* tools covering: read/patch/grep/glob/
shell(+close_shell)/exec/job core + local_exec + host/transfer/tunnel/audit/
check. (remote_patch sweeps its own orphan tmp files — no separate tool.)
"""
import asyncio
import hmac
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Literal

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError
from .paths import default_log_dir
from .connection_manager import get_manager
from .shell_engine import ssh_exec
from .file_ops import (ssh_upload_file, ssh_download_file, ssh_sync_directory,
                       ssh_mirror_directory, ssh_upload_list, ssh_download_list)
from .network_tools import get_tunnel_manager
from .job_manager import get_job_manager
from .audit import audit_log, get_history, get_audit_stats
from .security import get_policy
from .safety import normalize_host_name
from .server_info import server_info, set_transport as _set_server_transport
from .remote_text_editor import (
    remote_read as _re_read,
    remote_patch as _re_patch,
)
from .remote_search import remote_grep as _re_grep, remote_glob as _re_glob
from .remote_bash import (
    remote_bash as _re_bash,
    remote_bash_many as _re_bash_many,
    remote_bash_close as _re_bash_close,
    remote_bash_status as _re_bash_status,
    remote_sudo_exec as _re_sudo_exec,
    remote_exec_with_env as _re_exec_env,
)
from .local_exec import local_exec_with_env as _local_exec_env
from .local_exec import local_sudo_exec_with_env as _local_sudo_exec_env

_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
try:
    _log_dir = default_log_dir()
    _log_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_log_dir, 0o700)  # owner-only: server.log records host/cmd context
    except OSError:
        pass
    _server_log = _log_dir / "server.log"
    _fh = logging.FileHandler(_server_log, encoding="utf-8")
    try:
        os.chmod(_server_log, 0o600)
    except OSError:
        pass
    _log_handlers.append(_fh)
except Exception as _log_err:
    print(f"[portal-mcp-server] WARNING: could not open log file: {_log_err}", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("portal_mcp")


@asynccontextmanager
async def _server_lifespan(_server: "FastMCP"):
    """Graceful shutdown hook for all transports (stdio / streamable_http).

    On shutdown, close live shell sessions and pooled SSH connections. The
    OS/sshd reap the channels on process exit regardless, so this is a
    clean-shutdown + observability win (sessions closed and logged), not a
    correctness fix. Failures are swallowed so shutdown can't hang.
    """
    try:
        yield
    finally:
        try:
            from .session_manager import get_session_manager
            await get_session_manager().close_all()
        except Exception:  # pragma: no cover - best effort
            logger.debug("session close_all on shutdown failed", exc_info=True)
        try:
            await get_manager().close_all()
        except Exception:  # pragma: no cover - best effort
            logger.debug("pool close_all on shutdown failed", exc_info=True)


mcp = FastMCP("portal-mcp-server", lifespan=_server_lifespan)

# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

async def _gate(host: str, command: str = "", *,
                commit_rate_limit: bool = True) -> str | None:
    """Returns error string if blocked, None if allowed.

    ``commit_rate_limit=False`` runs the host/command checks without consuming
    a rate-limit token (for the ``policy_check`` dry-run).

    Async because the underlying ``SecurityPolicy.enforce`` may invoke the
    optional ``safety_net`` subprocess gate; running it synchronously would
    block the MCP server's event loop for the whole ``safety_net.timeout_s``.
    """
    return await get_policy().enforce(
        host, command, commit_rate_limit=commit_rate_limit)


async def _resolve_secrets(names: "list[str]"):
    """Resolve secret names to an injectable env dict + the raw values (for
    redaction). Returns ``(env, values, error)``: on the first unresolved name,
    ``env``/``values`` are partial and ``error`` is a friendly JSON-able string
    naming the missing secret (never the value). The agent only ever passes
    NAMES; values are fetched from secrets.yaml or the `portal secret set` cache.
    """
    from . import secrets_store

    env: dict[str, str] = {}
    values: list[str] = []
    seen: dict[str, str] = {}  # ENV_VAR_NAME -> originating secret name
    for name in names:
        ev = secrets_store.env_var_name(name)
        if ev in seen and seen[ev] != name:
            return env, values, (
                f"secret names '{seen[ev]}' and '{name}' both normalize to the "
                f"same environment variable ${ev}, so one would silently "
                f"clobber the other. Rename one so each injected secret gets a "
                f"distinct variable. The command was NOT run."
            )
        try:
            value = await secrets_store.resolve_secret(name)
        except Exception as e:  # command configured but failed (value-free msg)
            return env, values, f"secret '{name}' could not be resolved: {e}"
        if value is None:
            return env, values, (
                f"secret '{name}' is not available and the command was NOT run. "
                f"Ask the user to provide it out-of-band: prefer an interactive "
                f"input/choice tool (e.g. ask_user) to request that they run "
                f"`portal secret set {name}` in a separate terminal and "
                f"confirm when done, then retry this call. If you have no such tool, "
                f"tell the user what to run and end your turn to wait for their next "
                f"message. (Alternatively an operator can add a "
                f"'{name}: {{command: ...}}' entry to secrets.yaml.) Never ask the "
                f"user to paste the secret value into this conversation."
            )
        env[ev] = value
        seen[ev] = name
        values.append(value)
    return env, values, None


def _make_progress_cb(ctx: "Context | None"):
    """Build a throttled (done, total) callback that emits MCP progress.

    The progress notification doubles as a keepalive: clients reset their
    idle/timeout window on each one, which is what unblocks large transfers.
    Calls are throttled to ~2/s (plus a final 100% tick) to avoid flooding.
    The async ``ctx.report_progress`` is fire-and-forget scheduled onto the
    running loop, since asyncssh invokes the handler from a sync context.
    """
    if ctx is None:
        return None
    state = {"last": 0.0}
    # Strong refs to in-flight report tasks: the event loop only holds a weak
    # reference to a bare create_task(), so a suspended progress tick could be
    # GC'd before it sends, silently dropping a keepalive.
    pending: set = set()

    def cb(done: int, total: int) -> None:
        if not total:
            return
        now = time.monotonic()
        if done < total and (now - state["last"]) < 0.5:
            return
        state["last"] = now
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop, nothing to notify
            return
        task = loop.create_task(_safe_report(ctx, done, total))
        pending.add(task)
        task.add_done_callback(pending.discard)

    return cb


async def _safe_report(ctx: "Context", done: int, total: "int | None" = None) -> None:
    try:
        await ctx.report_progress(done, total)
    except Exception:  # pragma: no cover - progress is best-effort
        pass


def _heartbeat_interval() -> float:
    """Seconds between remote_shell/remote_exec keepalive pings (env-overridable)."""
    raw = os.environ.get("PORTAL_BASH_HEARTBEAT_INTERVAL", "")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 5.0
    return v if v > 0 else 5.0


# Hard ceiling (seconds) on a FOREGROUND blocking exec call — remote_exec /
# remote_shell / local_exec. `timeout` is a REQUIRED argument on those
# tools (no default: the agent must consciously choose a cut-off), and a value
# ABOVE this ceiling is refused. A genuinely long task must be backgrounded with
# remote_job (no ceiling) so the agent isn't pinned on one blocking call and can
# inspect / intervene between steps. Operator-tunable via PORTAL_MAX_TIMEOUT
# (seconds); defaults to 5 minutes.
_DEFAULT_MAX_TIMEOUT = 300.0


def _max_command_timeout() -> float:
    """Resolve the foreground per-command timeout CEILING (seconds).

    Read from PORTAL_MAX_TIMEOUT in the MCP server's ``env`` block; falls back
    to the built-in 5-minute default when unset, non-numeric, or non-positive.
    This is a safety ceiling, NOT a default the agent inherits — the exec tools
    have no default `timeout` at all; the agent must always pass one, and any
    value above this ceiling is refused with guidance to use remote_job.
    """
    raw = os.environ.get("PORTAL_MAX_TIMEOUT", "")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TIMEOUT
    return v if v > 0 else _DEFAULT_MAX_TIMEOUT


def _enforce_timeout_cap(timeout: float, *, job_hint: bool) -> None:
    """Validate a foreground exec `timeout` (raises ToolError, else returns).

    Rejects non-positive values and any value above the PORTAL_MAX_TIMEOUT
    ceiling. ``job_hint`` steers the over-ceiling guidance: remote exec / shell
    can offload a long task to remote_job; local_exec has no local
    background runner, so it is told to split the work into shorter steps.
    """
    if timeout is None or timeout <= 0:
        raise ToolError("timeout (seconds) must be a positive number.")
    cap = _max_command_timeout()
    if timeout > cap:
        if job_hint:
            raise ToolError(
                f"timeout {timeout:g}s exceeds the foreground ceiling {cap:g}s "
                "(PORTAL_MAX_TIMEOUT). Don't pin a long task on a blocking call "
                "— background it with remote_job (submit → poll → cancel; no "
                "ceiling), so you can keep working and step in while it runs.")
        raise ToolError(
            f"timeout {timeout:g}s exceeds the foreground ceiling {cap:g}s "
            "(PORTAL_MAX_TIMEOUT). local_exec has no background runner — "
            "split the work into shorter steps under the ceiling.")


def _login_shell_default() -> bool:
    """Global default for whether exec runs in a login shell (``bash -lc``).

    Controlled by PORTAL_LOGIN_SHELL in the server env; ON by default. Only an
    explicit false-y value ("0" / "false" / "no" / "off") turns it off — unset
    or anything else is on.
    """
    raw = os.environ.get("PORTAL_LOGIN_SHELL", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _resolve_login(login: "bool | None", host: str) -> bool:
    """Effective login-shell decision for <host>: an explicit per-call ``login``
    wins; else the host's hosts.yaml ``login_shell``; else PORTAL_LOGIN_SHELL
    (on by default)."""
    if login is not None:
        return bool(login)
    try:
        per_host = get_manager().login_shell_for(host)
    except Exception:
        per_host = None
    if per_host is not None:
        return per_host
    return _login_shell_default()


async def _await_with_heartbeat(coro, ctx: "Context | None",
                                interval: "float | None" = None):
    """Await ``coro`` while emitting periodic MCP progress notifications.

    Unlike remote_transfer, a remote command produces no output until it
    finishes, so a slow command would leave the client hearing nothing. Many
    clients abort a silent request after a fixed idle window even though the
    server-side ``timeout`` is far
    higher — and the remote command keeps running, so the result is simply
    lost. Each progress notification resets that window; the value is just a
    monotonic liveness tick (``total`` left indeterminate). No-op when ``ctx``
    is None or the client supplied no progressToken.

    ADR — why hand-driven: FastMCP exposes the progress primitive
    (``ctx.report_progress``) but does not auto-emit keepalives during a
    blocking tool call, so we drive the periodic tick ourselves. The
    notification transport itself is FastMCP-native.
    """
    task = asyncio.ensure_future(coro)
    if ctx is None:
        return await task
    if interval is None:
        interval = _heartbeat_interval()
    tick = 0
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if task in done:
                break
            tick += 1
            await _safe_report(ctx, tick, None)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    return task.result()


async def _gate_exec(hosts: list[str], commands: list[str]) -> str | None:
    """Multi-host / multi-command policy gate for remote_exec.

    Two-phase to avoid burning rate-limit quota on hosts that pass when a
    later host fails: first run all *non-mutating* checks (every command
    against the blocklist, then every host against the allowlist), and only
    commit per-host rate-limit consumption ONCE per host after everything
    validated. Returns the first error found, or None if all checks pass.

    Async because ``check_command`` may invoke the ``safety_net`` subprocess
    gate (one subprocess per command); we ``await`` it instead of blocking
    the MCP server's event loop.
    """
    pol = get_policy()
    # Phase 1: command blocklist for every command (no mutation).
    for c in commands:
        err = await pol.check_command(c)
        if err:
            return f"command {c[:60]!r}: {err}"
    # Phase 2: validate every host (no mutation).
    for h in hosts:
        err = pol.check_host(h)
        if err:
            return f"{h}: {err}"
    # Phase 3: commit rate-limit only once per host after every host validated.
    for h in hosts:
        err = pol.check_rate_limit(h)
        if err:
            return f"{h}: {err}"
    return None


async def _gate_many(hosts: list[str], command: str = "") -> str | None:
    """Single-command multi-host gate — thin wrapper over :func:`_gate_exec`.

    Kept for the direct unit tests that pin the two-phase rate-limit behaviour.
    """
    return await _gate_exec(hosts, [command] if command else [])


def _sudo_missing_message(host: str) -> str:
    """Friendly error when no sudo password is available for ``host``.

    Names BOTH ways to provide one so the agent can guide the user precisely.
    """
    return (
        f"No sudo password available for host '{host}'; the command was NOT "
        "run. Ask the user to provide it out-of-band — never have them paste a "
        "password into this conversation. Two ways:\n"
        f"  • Temporary (no-echo, cached with a TTL): run `portal sudo set "
        f"{host}` in a separate terminal, type the password at the hidden "
        "prompt, then retry this call.\n"
        f"  • Permanent (from a password manager): set `sudo_password_command` "
        f"for '{host}' in hosts.yaml to a command that prints the password "
        f"(e.g. `pass show sudo/{host}`).\n"
        "Prefer an interactive input tool (e.g. ask_user) to ask the user to "
        "run the first command and confirm when done; if you have no such "
        "tool, tell them what to run and end your turn to wait."
    )


def _local_sudo_missing_message() -> str:
    """Friendly error when no sudo password is available for the LOCAL machine.

    Names BOTH ways to provide one so the agent can guide the user precisely.
    The local identity is the reserved ``<local>`` — distinct from any SSH host
    named ``local`` / ``localhost``.
    """
    return (
        "No sudo password available for THIS machine (local_exec); the command "
        "was NOT run. Ask the user to provide it out-of-band — never have them "
        "paste a password into this conversation. Two ways:\n"
        "  • Temporary (no-echo, cached with a TTL): run `portal sudo set-local` "
        "in a separate terminal, type the password at the hidden prompt, then "
        "retry this call.\n"
        "  • Permanent (from a password manager): add a top-level `<local>:` "
        "section to hosts.yaml with a `sudo_password_command` that prints the "
        "password (e.g. `pass show sudo/this-box`).\n"
        "Prefer an interactive input tool (e.g. ask_user) to ask the user to "
        "run the first command and confirm when done; if you have no such "
        "tool, tell them what to run and end your turn to wait."
    )


# Substrings sudo itself prints when it needs a password but has no tty and no
# `-S`/askpass (i.e. a plain remote_exec command containing a bare `sudo`).
# The command fails fast (good) but with a cryptic message — we attach a hint
# pointing at the right tool so the agent doesn't have to guess.
_SUDO_TTY_SIGNS = (
    "a terminal is required to read the password",
    "no tty present and no askpass program",
    "sudo: a password is required",
)


def _maybe_hint_sudo_tty(res: dict) -> None:
    """If a plain exec result shows sudo failing for lack of a tty, attach a
    `hint` telling the agent to use use_sudo=True (in place, idempotent)."""
    if not isinstance(res, dict) or "hint" in res:
        return
    text = (res.get("stderr") or "") + "\n" + (res.get("stdout") or "")
    if any(sign in text for sign in _SUDO_TTY_SIGNS):
        res["hint"] = (
            "sudo needs a password but had no tty to prompt on. Re-run this as "
            "remote_exec(host=..., command=..., use_sudo=True): it feeds the "
            "stored sudo password to `sudo -S` over stdin. If none is stored "
            "yet, ask the user to run `portal sudo set <host>` in a separate "
            "terminal first — never have them paste the password into the chat."
        )


def _result_failed(r: dict) -> bool:
    """True if a per-host result (single dict or {host, results:[...]}) carries
    any failing exit code — non-zero OR unknown (``None``). Used to halt a
    serialized fan-out, so a command whose status can't be determined still
    stops the rollout (matching the docstring's "non-zero / unknown")."""
    def _failed(x: dict) -> bool:
        # Missing key (no command ran) is not a failure; an explicit None
        # (channel closed without an exit status) IS, like any non-zero code.
        return "exit_code" in x and x["exit_code"] != 0
    if "results" in r:
        return any(_failed(x) for x in r["results"])
    return _failed(r)


def _resolve_group(group_tag: str) -> list[str]:
    """Resolve a group tag to the list of registered host names carrying it."""
    mgr = get_manager()
    return [h.name for h in mgr._registry.values() if group_tag in h.tags]


# ═══════════════════════════════════════════════════════════════════
# 1. HOST REGISTRY  (hosts)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def hosts(action: Literal["list", "register", "remove"], name: str = "",
                       host: str = "", user: str = "root", port: int = 22,
                       key_path: str = "", tags: str = "") -> str:
    """Manage the SSH host registry.

    ## Modes
    - action="list": list all known hosts. Includes hosts.yaml / runtime-
        registered entries AND every `Host` alias discoverable in the OpenSSH
        client config (`~/.ssh/config` + system-wide fallback; or only
        `$PORTAL_SSH_CONFIG` when it is an absolute path; or none of them when
        it is "none" — the `ssh -F none` analogue), resolving real
        HostName/User/Port. Each entry has a `source` field: "hosts.yaml",
        "runtime", "ssh-config", or a "…+ssh-config" overlay (metadata from the
        declared origin, connection params from ssh config).
        Example: hosts(action="list")
    - action="register": add a host to the registry. Pass `host` (ip/hostname),
        or just `name` if ~/.ssh/config has a matching Host alias (registers a
        use_ssh_config overlay). Optional: user (default root), port (default
        22), key_path (else asyncssh falls back to ~/.ssh/id_* or ssh-agent),
        tags (comma-separated, used by remote_exec's group_tag).
        Example: hosts(action="register", name="web01", host="10.0.0.1",
                              user="ubuntu", tags="web,prod")
    - action="remove": remove a host from the registry.
        Required: name.
        Example: hosts(action="remove", name="web01")

    Hosts already defined in ~/.ssh/config are auto-resolved on first use; explicit
    registration is only needed for tag-based grouping. This MCP tool only
    accepts key-based hosts — password auth is intentionally not exposed
    here so credentials cannot leak into LLM tool-call traces. To use
    password auth, declare the host in hosts.yaml with `auth: password` and
    a `password_command:`. See README §Authentication.

    action="list" may include a per-host `warnings` array (e.g. a plaintext
    `password:` field in hosts.yaml that is being ignored). When present,
    relay these warnings to the user — they flag misconfigurations the user
    must fix, and server-side logs are not visible to them.
    """
    mgr = get_manager()
    if action == "list":
        hosts = mgr.list_hosts()
        return json.dumps(hosts, indent=2) if hosts else "No hosts registered."
    if action == "register":
        if not name:
            raise ToolError('action="register" requires `name`.')
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if not host:
            # (c) Only a name was given. If ~/.ssh/config has a matching Host
            # alias, register an overlay that takes its connection params from
            # there; otherwise we have no target, so ask for `host`.
            if not mgr.has_ssh_config_alias(name):
                from .paths import ssh_config_source_label
                label = ssh_config_source_label()
                raise ToolError(
                    f'action="register" needs `host` — no Host alias {name!r} '
                    f'found in {label}. Either pass host=<ip/hostname> or '
                    f'add a `Host {name}` stanza to {label} first.')
            # Gate on the alias name (the actual target lives in ssh config and
            # is not visible here; tools gate on the alias name anyway).
            err = await _gate(name)
            if err:
                raise ToolError(f"BLOCKED: {err}")
            result = mgr.register_host(name=name, use_ssh_config=True,
                                       tags=tag_list)
            audit_log(name, "register:ssh-config", "ok",
                      operation="host_register")
            return result
        # Gate against the *target* (the actual host/IP that traffic will
        # reach), not the alias the agent picked. Otherwise an agent can
        # register an arbitrary alias pointing at a non-allowlisted host
        # and then operate on it freely — host_allowlist would only ever
        # see the alias.
        err = await _gate(host)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        result = mgr.register_host(name=name, host=host, user=user, port=port,
                                    key=key_path or None, tags=tag_list)
        audit_log(name, f"register:{user}@{host}:{port}", "ok",
                  operation="host_register")
        return result
    if action == "remove":
        if not name:
            raise ToolError('action="remove" requires `name`.')
        # Gate the alias being removed — same surface as any other
        # state-changing op against that alias.
        err = await _gate(name)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        result = mgr.remove_host(name)
        audit_log(name, "remove_host", "ok", operation="host_remove")
        return result
    raise ToolError(f'unknown action {action!r}. Valid: list, register, remove.')


# ═══════════════════════════════════════════════════════════════════
# 2. FILE TRANSFER  (remote_transfer — SFTP-based, binary safe)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def remote_transfer(
        direction: Literal["upload", "download", "sync", "mirror",
                           "upload-list", "download-list"],
        host: str, local_path: str, remote_path: str,
        ctx: Context, checksum: bool = False,
        paths_json: str = "", resume: bool = True) -> str:
    """Transfer files between local and remote via SFTP (binary-safe, atomic).

    ## Modes
    - direction="upload": local_path → remote_path (single file).
        Example: remote_transfer(direction="upload", host="web01",
                                  local_path="/tmp/app.jar",
                                  remote_path="/opt/app/app.jar")
    - direction="download": remote_path → local_path (single file).
        Example: remote_transfer(direction="download", host="web01",
                                  remote_path="/var/log/syslog",
                                  local_path="/tmp/syslog")
    - direction="sync": recursively sync local_path directory → remote_path
        directory (upload). Files already present with a matching size+mtime
        (or sha256 when checksum=True) are skipped.
    - direction="mirror": recursively mirror remote_path directory → local_path
        directory (download); the remote→local counterpart of "sync".
        Example: remote_transfer(direction="mirror", host="web01",
                                  remote_path="/srv/www/", local_path="./www/")
    - direction="upload-list" / "download-list": transfer an explicit list of
        file pairs given in paths_json (an arbitrary local→remote mapping, not a
        whole directory). Each pair is skipped when already present with a
        matching size+mtime (or sha256 when checksum=True), so re-runs only move
        the changed files; a single pair's failure is collected in failed[]
        without aborting the batch. local_path / remote_path are ignored in
        these modes.
        Example: remote_transfer(direction="upload-list", host="web01",
            paths_json='[{"local":"/tmp/a.conf","remote":"/etc/app/a.conf"},
                         {"local":"/tmp/b.conf","remote":"/etc/app/b.conf"}]')

    Args:
        checksum: for the incremental modes (sync/mirror/upload-list/
            download-list), compare files by sha256 instead of size+mtime
            (slower, requires `sha256sum` on the remote; missing remote files or
            an unavailable sha256sum force a re-transfer).
        paths_json: JSON array of {"local": ..., "remote": ...} objects, required
            by the upload-list / download-list modes (ignored otherwise).
        resume: (upload only) if a SMALLER partial already exists at remote_path
            from an interrupted transfer, append just the missing tail instead
            of restarting, then verify the whole file's sha256 and re-upload
            fresh once on mismatch. Default on; set False to force a fresh
            overwrite. The result carries resumed=true when the tail path ran.

    Progress is reported to the MCP client during transfers.

    Returns a JSON status dict. Single-file: {status, direction, host, bytes,
    duration_s, ...}. sync/mirror/upload-list/download-list: {status,
    uploaded|downloaded, skipped, failed[], bytes_total, bytes_transferred,
    duration_s}.

    For text-only edits prefer remote_patch (hash-protected). Use remote_transfer
    when SFTP semantics are needed: binary files, large files, whole directory
    trees. Note: directory modes copy *files* only — symlinks and special files
    are skipped, and empty directories are not created on their own.
    """
    err = await _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    progress_cb = _make_progress_cb(ctx)
    if direction == "upload":
        result = await ssh_upload_file(host, local_path, remote_path,
                                       progress_cb=progress_cb, resume=resume)
    elif direction == "download":
        result = await ssh_download_file(host, remote_path, local_path,
                                         progress_cb=progress_cb)
    elif direction == "sync":
        result = await ssh_sync_directory(host, local_path, remote_path,
                                          checksum=checksum, progress_cb=progress_cb)
    elif direction == "mirror":
        result = await ssh_mirror_directory(host, remote_path, local_path,
                                            checksum=checksum, progress_cb=progress_cb)
    elif direction in ("upload-list", "download-list"):
        try:
            entries = json.loads(paths_json or "[]")
        except (json.JSONDecodeError, TypeError) as e:
            raise ToolError(f"paths_json is not valid JSON: {e}")
        if not isinstance(entries, list) or not entries:
            raise ToolError('paths_json must be a non-empty JSON array of '
                            '{"local": ..., "remote": ...} objects.')
        pairs = []
        for i, item in enumerate(entries):
            if not isinstance(item, dict) or "local" not in item or "remote" not in item:
                raise ToolError(f'paths_json[{i}] must be an object with "local" '
                                'and "remote" string keys.')
            pairs.append((str(item["local"]), str(item["remote"])))
        if direction == "upload-list":
            result = await ssh_upload_list(host, pairs, checksum=checksum,
                                           progress_cb=progress_cb)
        else:
            dl_pairs = [(remote, local) for local, remote in pairs]
            result = await ssh_download_list(host, dl_pairs, checksum=checksum,
                                             progress_cb=progress_cb)
        audit_log(host, f"{direction}:{len(pairs)} files",
                  result.get("status", "?"), operation=f"file_{direction}")
        return json.dumps(result, indent=2, ensure_ascii=False)
    else:
        raise ToolError(f'unknown direction {direction!r}. Valid: upload, '
                        'download, sync, mirror, upload-list, download-list.')
    audit_log(host, f"{direction}:{local_path}<->{remote_path}",
              result.get("status", "?"), operation=f"file_{direction}")
    return json.dumps(result, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════
# 3. SSH TUNNELS  (remote_tunnel — action=open|close|list)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def remote_tunnel(action: Literal["open", "close", "list"],
                        kind: Literal["local", "reverse", "socks"] = "local",
                        host: str = "", tunnel_id: str = "",
                        local_port: int = 0, local_bind: str = "127.0.0.1",
                        remote_host: str = "", remote_port: int = 0) -> str:
    """Manage SSH tunnels — a single entry point (like hosts) where
    `action` selects the operation and the other args parameterise it.

    ## Actions
    - action="open": open a tunnel through `host`. `kind` picks the type:
        - kind="local"  : forward localhost:local_port → remote_host:remote_port
            via host. Required: remote_host, remote_port (local_port 0 = auto).
            Example: remote_tunnel(action="open", kind="local", host="bastion",
                                   local_port=5432, remote_host="db.internal",
                                   remote_port=5432)
        - kind="reverse": expose local_bind:local_port to host as host:remote_port.
            Required: remote_port, local_bind, local_port.
            Example: remote_tunnel(action="open", kind="reverse", host="bastion",
                                   remote_port=8080, local_bind="127.0.0.1",
                                   local_port=3000)
        - kind="socks"  : SOCKS5 proxy on localhost:local_port via host.
            Required: local_port (default 1080).
            Example: remote_tunnel(action="open", kind="socks", host="bastion",
                                   local_port=1080)
      Returns {tunnel_id, type, host, local, remote}.
    - action="close": close a live tunnel. Required: tunnel_id (from open).
        Example: remote_tunnel(action="close", tunnel_id="ab12cd34")
    - action="list": list all active tunnels (JSON array).

    Tunnels are a resource you manage explicitly (open → close), so `list`
    lives here rather than in inspect.
    """
    tm = get_tunnel_manager()

    if action == "list":
        tunnels = tm.list_tunnels()
        return json.dumps(tunnels, indent=2) if tunnels else "No active tunnels."

    if action == "open":
        if not host:
            raise ToolError('action="open" requires `host`.')
        err = await _gate(host)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        if kind == "local":
            result = await tm.open_local_forward(host, local_port,
                                                 remote_host, remote_port, local_bind)
        elif kind == "reverse":
            result = await tm.open_remote_forward(host, remote_port,
                                                  local_bind, local_port)
        else:  # socks (Literal guarantees one of the three)
            result = await tm.open_dynamic_proxy(host, local_port or 1080, local_bind)
        if isinstance(result, dict) and result.get("error"):
            # open_* returns {"error": ...} on failure; audit the real outcome
            # instead of unconditionally recording "ok", and surface it.
            audit_log(host, f"tunnel:{kind}", f"ERROR:{result['error']}",
                      operation="tunnel_open")
            raise ToolError(f"tunnel open failed on {host!r}: {result['error']}")
        audit_log(host, f"tunnel:{kind}", "ok", operation="tunnel_open")
        return json.dumps(result, indent=2)

    if action == "close":
        if not tunnel_id:
            raise ToolError('action="close" requires `tunnel_id`.')
        # Look up the originating host so we can run it through the security
        # gate (consistent with action="open"). Without this gate an agent
        # that lost host access could still dismantle live tunnels.
        owner = next((t["host"] for t in tm.list_tunnels()
                      if t["tunnel_id"] == tunnel_id), None)
        if owner is None:
            raise ToolError(f"Tunnel '{tunnel_id}' not found")
        err = await _gate(owner)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        result = await tm.close_tunnel(tunnel_id)
        audit_log("tunnel", f"close:{tunnel_id}", "ok", operation="tunnel_close")
        return result

    raise ToolError(f'unknown action {action!r}. Valid: open, close, list.')


# ═══════════════════════════════════════════════════════════════════
# 5. POLICY DRY-RUN  (policy_check)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def policy_check(host: str, command: str = "") -> str:
    """Dry-run a host (and optional command) through the security policy.

    - command="" : check whether the host is accessible at all.
        Example: policy_check(host="web01")
    - command="rm -rf /" : check whether this command would be allowed on this host.
        Example: policy_check(host="web01", command="systemctl stop nginx")

    Returns "ALLOWED" or "BLOCKED: <reason>". Does not execute anything.
    Use this before risky multi-host operations to surface policy errors early.
    Being a dry-run, it does NOT consume a rate-limit token (a pre-flight check
    never throttles the real operation it is checking for).

    ⚠️  Default policy is PERMISSIVE — out of the box `policies.yaml` has an
    empty host_allowlist (any host), empty command_blocklist / allowlist
    (any command), and only a per-host rate limit. So `policy_check` will
    return ALLOWED for almost anything until you populate
    `$XDG_CONFIG_HOME/portal-mcp-server/policies.yaml` (default
    `~/.config/portal-mcp-server/policies.yaml`)
    with explicit rules. Use `inspect(view="policy")` to inspect what
    the server actually has loaded. ALLOWED therefore means "no rule
    currently blocks this", not "this is safe to run".
    """
    err = await _gate(host, command, commit_rate_limit=False)
    if err:
        return f"BLOCKED: {err}"
    if not command:
        reg = get_manager()._registry
        if host not in reg:
            return f"ALLOWED by policy but host {host!r} is not registered."
    return f"ALLOWED: {host!r} passes all security checks."


# ═══════════════════════════════════════════════════════════════════
# 7. AUDIT & STATE  (inspect)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def inspect(view: Literal["snapshot", "server", "sessions",
                               "history", "stats", "policy"] = "snapshot",
                  limit: int = 50, host_filter: str = "") -> str:
    """Inspect MCP server internal state and the audit log — the read-only
    introspection hub for *plumbing* (the connection pool, persistent bash
    sessions) and *history* (audit log, stats, policy).

    Note the "resource vs plumbing" split: things the agent manages explicitly
    (registered hosts, open tunnels) are listed by their own resource tools —
    hosts(action="list") and remote_tunnel(action="list") — NOT here.
    inspect only surfaces server-internal plumbing the agent never
    explicitly creates.

    ## Views
    - view="snapshot" (default): server metadata + connection pool + bash
        sessions + audit stats + security policy summary. Use this for an
        all-at-once diagnostic. (Hosts and tunnels are intentionally absent —
        see the note above.)
    - view="server": just the server-level metadata (version, python_version,
        pid, started_at, uptime_s, transport, resolved config paths). Cheap;
        use this when you only need to know "which version am I talking to?"
        without pulling the full snapshot.
    - view="sessions": the `host → session_id` map of cached persistent bash
        sessions (what remote_shell reuses per host). This is plumbing
        diagnostics — the sessions are implicit, which is why they live in
        inspect rather than carrying their own list like tunnels/hosts.
    - view="history": last `limit` audit log entries (default 50). Optional `host_filter`.
        Example: inspect(view="history", limit=20, host_filter="web01")
    - view="stats": aggregate audit stats (counts by operation, error rate, etc.).
    - view="policy": current security policy details (host allowlist, command
        blocklist, allowlist, rate limit).

    Read-only. Used to introspect what the MCP server has been doing and what
    limits are in place.
    """
    if view == "server":
        return json.dumps(server_info(), indent=2)
    if view == "sessions":
        return json.dumps(_re_bash_status(), indent=2)
    if view == "history":
        history = get_history(limit=limit, host_filter=host_filter)
        return json.dumps(history, indent=2) if history \
                else "No operations recorded yet."
    if view == "stats":
        return json.dumps(get_audit_stats(), indent=2)
    if view == "policy":
        pol = get_policy()
        return json.dumps({
            "host_allowlist": pol.host_allowlist or ["* (all hosts permitted)"],
            "command_blocklist": pol.command_blocklist,
            "command_allowlist": pol.command_allowlist or ["* (all commands permitted)"],
            "rate_limit_rps": pol.rate_limit_rps,
            "safety_net": {
                "enabled": pol.safety_net.enabled,
                "command": pol.safety_net.command,
                "fail_closed": pol.safety_net.fail_closed,
            },
        }, indent=2)
    if view == "snapshot":
        mgr = get_manager()
        # Resource lists (hosts, tunnels) live in their own tools
        # (hosts / remote_tunnel action="list"); the snapshot carries
        # only server-internal plumbing + audit/policy state.
        snap = {
            "server": server_info(),
            "connection_pool": mgr.pool_status(),
            "bash_sessions": _re_bash_status(),
            "audit_stats": get_audit_stats(),
            "security": {
                "host_allowlist_count": len(get_policy().host_allowlist),
                "command_blocklist_count": len(get_policy().command_blocklist),
                "rate_limit_rps": get_policy().rate_limit_rps,
                "safety_net_enabled": get_policy().safety_net.enabled,
            },
        }
        return json.dumps(snap, indent=2)
    raise ToolError(f'unknown view {view!r}. Valid: snapshot, server, sessions, history, stats, policy.')


# ═══════════════════════════════════════════════════════════════════
# 12. PORTAL CORE — agent-feels-local tools
# ═══════════════════════════════════════════════════════════════════
# These wrap server.remote_* modules. Designed to be the *primary* tools an
# AI agent uses when working on a remote host. They share one SSH connection
# per host (via the connection pool) and provide:
#   - remote_read / remote_patch :  hash-protected concurrent-safe edits
#   - remote_grep / remote_glob  :  remote ripgrep / find with structured output
#   - remote_shell               :  single persistent bash session (cwd + env survive)
#   - remote_exec                :  stateless one-shot exec (1 host; +sudo/secrets)

@mcp.tool()
async def remote_read(host: str, path: str, start: int = 1,
                      end: int | None = None, limit: int | None = None,
                      encoding: str = "utf-8", use_sudo: bool = False) -> str:
    """Read a file (or a 1-based line range) from a remote host with SHA-256 hashes.

    Returns JSON with: content, file_hash, range_hash, start, end, total_lines,
    truncated. The file_hash MUST be supplied to remote_patch; if the file
    changed in between, remote_patch will refuse to overwrite.

    The read is **paged** so a large file never has to come back as one
    oversized blob: a single call returns at most ``limit`` lines (default
    PORTAL_READ_MAX_LINES=2000) and at most PORTAL_READ_MAX_BYTES (default
    16384) of content, whichever binds first — but always at least one line.
    When the page stops before the requested end, ``truncated`` is true and
    ``next_start`` gives the line to continue from (pass it back as ``start``).
    Pages are cut on line boundaries, so ``range_hash`` stays valid for patching.

    Usage:
        * Whole file (auto-paged): call ``remote_read(host, path)``; if the result
          has ``truncated: true``, call again with ``start=<next_start>`` and repeat
          until ``truncated`` is false, concatenating each page's ``content`` in order.
        * A specific range: ``remote_read(host, path, start=40, end=80)`` (still
          paged if that range is huge).
        * Just the first N lines: ``remote_read(host, path, limit=N)`` — handy to
          peek the head of a big file without pulling it all.

    Args:
        host: SSH host alias (from ~/.ssh/config) or registered host name
        path: Absolute remote path
        start: 1-based starting line (default 1)
        end: 1-based ending line of the requested range, inclusive
            (default: end of file)
        limit: max number of lines to return in this page (default: the
            PORTAL_READ_MAX_LINES cap). The byte cap may shorten the page further.
        encoding: Text encoding (default utf-8)
        use_sudo: read via `sudo cat` (root) for a file the SSH user can't read
            (root-owned / mode 600). The sudo password is resolved out-of-band
            per host (never a parameter) and the result is flagged high_risk.
            Exact bytes are preserved (no newline stripping) so file_hash stays
            valid for remote_patch.
    """
    err = await _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    res = await _re_read(host, path, start=start, end=end, limit=limit,
                         encoding=encoding, use_sudo=use_sudo)
    if use_sudo and isinstance(res, dict):
        res["high_risk"] = True
        res["high_risk_note"] = (
            f"Read {path!r} on {host!r} via sudo (root) using the user's "
            "stored/cached sudo password. Briefly tell the user you did this, "
            "or only do so with their explicit permission.")
        audit_log(host, f"read:{path}", "ok", operation="file_read_sudo")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_patch(host: str, path: str, file_hash: str,
                       patches_json: str, encoding: str = "utf-8",
                       auto_newline: bool = False,
                       use_sudo: bool = False) -> str:
    """Apply patches to a remote file with hash-based conflict detection.

    Workflow:
      1. Call remote_read to obtain content + file_hash + range_hash for each region.
      2. Call remote_patch with the SAME file_hash and per-patch range_hash.
      3. If the file was modified by anyone else in between, this call returns
         {"result": "error", "reason": "Content hash mismatch ...", "current_file_hash": ...}
         — re-read and try again. The remote file is untouched.

    patches_json must decode to a list of patch objects:
      [{"start": int, "end": int|null, "contents": str, "range_hash": str}, ...]

    Notes:
      - Patches are applied bottom-to-top so line numbers stay valid.
      - Overlapping patches are rejected.
      - Writes are atomic (tmp file + rename) and re-hashed after write.
      - When auto_newline is true, missing trailing newlines on patch
        contents are auto-appended *only* if the slice they replace ended
        with one. The result includes a "warnings" list either way.
      - use_sudo: read+write the file as root, for a root-owned file the SSH
        user can't touch. Keeps the SAME hash safety + atomicity as the normal
        path: content is read via `sudo cat`, staged over SFTP into the user's
        private home, then placed with one sudo step (cp → temp beside the
        target → restore owner/group/mode → atomic rename). The target must
        already exist; the sudo password is resolved out-of-band per host and
        the result is flagged high_risk. (Implementation note in the README.)
    """
    err = await _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    try:
        patches = json.loads(patches_json)
    except Exception as e:
        raise ToolError(f"invalid patches_json: {e}")
    res = await _re_patch(host, path, file_hash=file_hash, patches=patches,
                          encoding=encoding, auto_newline=auto_newline,
                          use_sudo=use_sudo)
    if use_sudo and isinstance(res, dict) and res.get("result") == "ok":
        res["high_risk"] = True
        res["high_risk_note"] = (
            f"Patched {path!r} on {host!r} via sudo (root) using the user's "
            "stored/cached sudo password. Briefly tell the user you did this, "
            "or only do so with their explicit permission.")
    audit_log(host, f"patch:{path}",
              res.get("result", "?") if isinstance(res, dict) else "?",
              operation="file_patch_sudo" if use_sudo else "file_patch")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_grep(host: str, pattern: str, path: str = ".",
                      glob: str = "", file_type: str = "",
                      output_mode: Literal["files_with_matches", "content",
                                           "count"] = "files_with_matches",
                      ignore_case: bool = False,
                      before_context: int = 0, after_context: int = 0,
                      context: int = 0, head_limit: int = 250,
                      offset: int = 0, multiline: bool = False) -> str:
    """Search file contents with a regex on a remote host (ripgrep, fallback
    grep). **Prefer this over running raw `rg`/`grep` through remote_exec** —
    it returns structured JSON and caps output so a broad search can't blow up
    your context. Pair it with remote_glob to *find files by name*.

    Args:
        host: SSH host alias / registered name.
        pattern: the regex to search for (rg/PCRE-ish syntax).
        path: file or directory to search under (default: cwd "."). Result
            paths are returned relative to it.
        glob: filter files by a glob, e.g. "*.py" or "!*.test.ts".
        file_type: rg --type filter, e.g. "py", "rust", "js".
        output_mode:
            - "files_with_matches" (default): just the matching file paths,
              NEWEST FIRST. Cheapest; use it to locate, then re-grep with
              output_mode="content" on the file you care about.
            - "content": matching lines as {file, line, text} (context lines
              carry "context": true). `head_limit` caps the TOTAL lines
              returned and `offset` pages through them.
            - "count": per-file match counts + a grand total.
        ignore_case: case-insensitive match.
        before_context / after_context / context: lines of context to include
            around each match in "content" mode (context = both sides).
        head_limit: cap on results (files / content lines / count rows). Default
            250; a `truncated` flag in the result tells you more were dropped.
        offset: skip the first `offset` results (pagination).
        multiline: let `.` and the pattern span line boundaries.

    Respects `.gitignore` (ripgrep's default). Returns JSON whose shape depends
    on output_mode; every shape includes a `truncated` flag.
    """
    err = await _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    res = await _re_grep(
        host, pattern, path,
        glob=glob or None,
        file_type=file_type or None,
        output_mode=output_mode,
        ignore_case=ignore_case,
        before_context=before_context,
        after_context=after_context,
        context=context,
        head_limit=head_limit,
        offset=offset,
        multiline=multiline,
    )
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_glob(host: str, pattern: str, path: str = ".") -> str:
    """Find files by a glob pattern on a remote host, **newest first**.
    **Prefer this over running raw `find`/`ls` through remote_exec** — it
    returns structured JSON, sorts by modification time, and hard-caps at 100
    files. Use remote_grep when you need to match file *contents* instead.

    Args:
        host: SSH host alias / registered name.
        pattern: a glob, e.g. "**/*.py", "src/**/*.{ts,tsx}", "*.toml".
        path: directory to search under (default: cwd "."). Returned filenames
            are relative to it.

    Returns {filenames:[…newest first], num_files, truncated, duration_ms}.
    Unlike remote_grep this does NOT respect `.gitignore` (matches CC Glob).
    """
    err = await _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    res = await _re_glob(host, pattern, path=path)
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_shell(host: str, command: str = "",
                       commands: "list[str] | None" = None,
                       stop_on_error: bool = True, *,
                       timeout: float,
                       ctx: "Context | None" = None) -> str:
    """Run a command (or a sequence) on ONE remote host in a **persistent shell
    session**: cwd and environment (cd / export / venv activation) survive
    across calls. Use it only when you need that continuity — otherwise
    remote_exec is faster (no session setup) and can target many hosts; for a
    long task to background and poll, use remote_job.

    Pick one:
      - command="pytest"  → {host, session_id, command, exit_code, output,
        duration_s}. ONE command per call is the preferred shape: the session
        PERSISTS across separate calls, so cwd / exports / venv from an earlier
        call still apply to the next — you keep step-by-step continuity AND get
        to read each step's real output before deciding the next.
      - commands=["cd /app","source .venv/bin/activate","pytest"]  → runs the
        sequence in order in ONE call (same session). Reach for it ONLY for a
        fixed sequence you do NOT need to inspect between steps; you can't judge
        one step's output before the next runs. Returns {host, session_id,
        results:[…]}; stop_on_error=True (default) halts at the first failure and
        adds stopped_at.

    Behavior:
      - bash or zsh; an unknown login shell falls back to bash (if bash is
        missing the call is refused — use remote_exec).
      - Output is the COMBINED stdout+stderr stream (use remote_exec when you
        need them split). Oversize output is capped and flagged truncated=true.
      - A command that wedges on an interactive prompt (sudo password, ssh
        first-connect, passphrase, …) is auto-aborted, returning exit_code -1 +
        error:"interactive_prompt_blocked" + session_preserved:true — cwd/env
        survive, so the next command runs straight away.

    ★ No sudo or secret injection here (both are one-shot by nature): to run as
    root use remote_exec(use_sudo=True); for a command needing a secret use
    remote_exec(secrets=[…]).

    timeout (per command, seconds; REQUIRED — no default): the call is held open
    until the command exits or timeout elapses. Keepalive pings stop the client
    aborting a hung call, so timeout is your real cut-off — pass a SMALL one for
    an exploratory / re-runnable command (e.g. 10–30) to fail fast, raising it
    only for a genuinely slow but bounded command. Hard ceiling
    (PORTAL_MAX_TIMEOUT, default 300s): a request above it is refused — use
    remote_job for a long task.

    ⚠️ By convention, write operations should target /tmp/ on the remote unless
    the user approved another path (not enforced here).
    """
    _enforce_timeout_cap(timeout, job_hint=True)
    host = normalize_host_name(host)

    # command vs commands: exactly one, mirroring remote_exec.
    if commands is not None:
        if command:
            raise ToolError("pass either command or commands, not both.")
        if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
            raise ToolError("commands must be a list of strings.")
        if not commands:
            raise ToolError("commands list is empty.")
        for c in commands:
            err = await _gate(host, c)
            if err:
                raise ToolError(f"BLOCKED: {err}")
        res = await _await_with_heartbeat(
            _re_bash_many(host, commands, stop_on_error=stop_on_error,
                          timeout=timeout), ctx)
        for entry in res.get("results", []):
            audit_log(host, entry.get("command", "?"),
                      entry.get("exit_code", "?"), operation="shell")
        return json.dumps(res, indent=2, ensure_ascii=False)

    if not command:
        raise ToolError("provide command (str) or commands (list of str).")
    err = await _gate(host, command)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    res = await _await_with_heartbeat(
        _re_bash(host, command, timeout=timeout), ctx)
    audit_log(host, command, res.get("exit_code", "?"), operation="shell")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_exec(host: "str | list[str]" = "", command: str = "",
                      commands: "list[str] | None" = None,
                      group_tag: str = "", *,
                      timeout: float,
                      login: "bool | None" = None,
                      use_sudo: bool = False,
                      secrets: "list[str] | None" = None,
                      serialize: bool = False, delay_s: float = 0.0,
                      stop_on_error: bool = True,
                      ctx: "Context | None" = None) -> str:
    """Run a shell command on one or more remote hosts over SSH and get the
    result immediately (exit code + SEPARATE stdout/stderr). This is the default
    way to execute ANYTHING on a remote machine — reach for it instead of
    hand-rolling `ssh user@host …`; it reuses a warm connection pool and fans
    out across hosts. Stateless: need cwd/exports to persist → remote_shell;
    a long task to background and poll → remote_job.

    ★ sudo: to run a command as root, call this with use_sudo=True — NEVER put a
    bare `sudo …` in the command or in remote_shell (there is no TTY and the
    password can't be fed). The password is supplied out-of-band; you never pass
    it. (For root on the server's OWN machine, sudo is not available — see
    local_exec.)

    ★ credentials: when a command needs a secret (API token, deploy key, …), do
    NOT have the user paste it into the chat. They run
    `portal secret set <name>` in their own terminal; you pass
    secrets=["<name>"] and reference it as the uppercased env var ($NAME).

    Targets (pick one): host="web01" | host=["web01","web02"] |
    group_tag="prod" (all registered hosts carrying that tag).

    Commands (pick one): command="uptime" — ONE command per call is the DEFAULT
    and preferred shape. Run it, read the real output, check it against what you
    expected (an exit-0 step can still be wrong), THEN decide the next call —
    that keeps you in the loop and able to course-correct. commands=["a","b"]
    batches a sequence into a SINGLE call (each with its own exit code; stops at
    the first failure when stop_on_error=True, and with use_sudo each entry runs
    as its own sudo command) — reach for it ONLY for a fixed, independent
    sequence you do NOT need to inspect between steps. Don't bury a long or
    branching pipeline in one `a && b && c` string: you lose per-step visibility
    and can't intervene. A multi-line command string runs as one bash script.

    Multi-host fan-out is parallel by default; serialize=True (+ delay_s,
    stop_on_error) does a rolling, stop-on-first-failure rollout.

    timeout (seconds, REQUIRED — no default; consciously choose a cut-off): the
    call is held open until the command finishes or timeout elapses. Keepalive
    pings stop the client aborting a hung call, so timeout is your real cut-off —
    pass a SMALL one for an exploratory / re-runnable command (e.g. 10–30) to
    fail fast, raising it only for a genuinely slow but bounded command. Hard
    ceiling (PORTAL_MAX_TIMEOUT, default 300s): a request above it is REFUSED —
    background a long task with remote_job instead of pinning it on a blocking
    call.

    login (default on): run the command in a LOGIN shell (bash -lc) so the
    remote user's ~/.profile / ~/.bashrc PATH + env (conda / nvm / pyenv,
    ~/.local/bin, …) is loaded; pass login=False for a bare non-login shell.
    Silently degrades to a plain exec on a host without bash. Affects only this
    plain path (not sudo / secrets); operators set the default via
    PORTAL_LOGIN_SHELL or a per-host `login_shell:` in hosts.yaml.

    use_sudo: run via `sudo -S`, with the password resolved out-of-band per host
        (the per-user credential agent from `portal sudo set <host>`, or the
        host's sudo_password_command) — never from you. Refused with guidance if
        no source is set. Consumes the command's stdin (tools that read their
        own stdin aren't supported under sudo). May be combined with secrets
        (their values ride the same stdin channel, after the password).

    secrets: a list of NAMES, not values (e.g. ["github_token"]); each is
        resolved server-side, fed over SSH stdin (never on argv/audit), exported
        as its uppercased env var ($GITHUB_TOKEN), and redacted to *** in the
        output. May be combined with use_sudo (values are read+exported inside
        the sudo'd shell, so sudo's env_reset can't strip them).

    Returns one dict {host, command, exit_code, stdout, stderr, elapsed_s} for a
    single host + command, else a JSON list (a multi-command host carries {host,
    results:[…]}).

    ★ When use_sudo or secrets is used the result is flagged "high_risk": briefly
    tell the user you ran a privileged / credentialed command with their stored
    sudo password / secret, or only do so with their explicit prior permission.
    """
    _enforce_timeout_cap(timeout, job_hint=True)

    # ── Resolve target hosts (host str | host list | group_tag) ──
    if group_tag:
        if host:
            raise ToolError("pass either host or group_tag, not both.")
        hosts = _resolve_group(group_tag)
        if not hosts:
            return json.dumps([{"error": f"No hosts found with tag {group_tag!r}"}],
                              indent=2)
        multi_host = True
    elif isinstance(host, list):
        hosts = [str(h) for h in host]
        if not hosts:
            raise ToolError("host list is empty.")
        multi_host = True
    elif host:
        hosts = [host]
        multi_host = False
    else:
        raise ToolError("provide a target: host (str or list) or group_tag.")

    hosts = [normalize_host_name(h) for h in hosts]
    if any(not h for h in hosts):
        raise ToolError("empty host name (after trimming whitespace).")

    # ── Resolve command(s) ──
    if commands:
        if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
            raise ToolError("commands must be a list of strings.")
        cmd_list = list(commands)
        multi_cmd = True
    elif command:
        cmd_list = [command]
        multi_cmd = False
    else:
        raise ToolError("provide command (str) or commands (list of str).")

    # ── Gate every (host, command) pair up front ──
    err = await _gate_exec(hosts, cmd_list)
    if err:
        raise ToolError(f"BLOCKED: {err}")

    # ── Resolve secrets once (same env injected on every host) ──
    secret_env: "dict | None" = None
    secret_values: list[str] = []
    if secrets:
        secret_env, secret_values, serr = await _resolve_secrets(secrets)
        if serr:
            raise ToolError(serr)
    secret_label = f"  [secrets: {','.join(secrets)}]" if secrets else ""

    from . import secrets_store
    from .sudo_creds import resolve_sudo_password

    async def run_one(h: str, cmd: str) -> dict:
        if use_sudo:
            password = await resolve_sudo_password(h)
            if password is None:
                if multi_host:
                    return {"host": h, "command": cmd, "exit_code": -1,
                            "stdout": "", "stderr": f"no sudo password for {h!r}",
                            "error": "no sudo password available"}
                raise ToolError(_sudo_missing_message(h))
            res = await _re_sudo_exec(h, cmd, password,
                                      env=secret_env or {}, timeout=timeout)
            if secret_values:
                res["stdout"] = secrets_store.redact(res.get("stdout", ""), secret_values)
                res["stderr"] = secrets_store.redact(res.get("stderr", ""), secret_values)
            res["high_risk"] = True
            note = (f"Ran a privileged sudo command on {h!r} using the user's "
                    "stored/cached sudo password")
            if secrets:
                note += f", with stored secret(s) [{','.join(secrets)}] injected"
            note += (". Briefly tell the user you did this (or only do so with "
                     "their explicit permission).")
            res["high_risk_note"] = note
            audit_log(h, "sudo: " + cmd + secret_label, res.get("exit_code", "?"),
                      operation="exec_sudo")
            return res
        if secret_env is not None:
            res = await _re_exec_env(h, cmd, secret_env, timeout=timeout)
            res["stdout"] = secrets_store.redact(res.get("stdout", ""), secret_values)
            res["stderr"] = secrets_store.redact(res.get("stderr", ""), secret_values)
            res["high_risk"] = True
            res["high_risk_note"] = (
                f"Injected stored/cached secret(s) [{','.join(secrets)}] into a "
                f"command on {h!r}. Briefly tell the user you ran a command with "
                "their configured credential (or only do so with their explicit "
                "permission)."
            )
            audit_log(h, cmd + secret_label, res.get("exit_code", "?"),
                      operation="exec_secrets")
            return res
        # Plain one-shot path: ssh_exec runs over the pool and audits as "exec".
        # Pass timeout as-is (float): int() would floor a sub-second value like
        # 0.9 to 0, turning a short deadline into "expire immediately".
        res = await ssh_exec(h, cmd, timeout=timeout,
                             login=_resolve_login(login, h))
        _maybe_hint_sudo_tty(res)
        return res

    async def run_host(h: str):
        if not multi_cmd:
            return await run_one(h, cmd_list[0])
        results = []
        for cmd in cmd_list:
            r = await run_one(h, cmd)
            results.append(r)
            if stop_on_error and r.get("exit_code", 0) not in (0, None):
                results.append({"info": f"stopped at {cmd!r} (exit {r.get('exit_code')})"})
                break
        return {"host": h, "results": results}

    async def run_all():
        if not multi_host:
            return await run_host(hosts[0])
        if serialize:
            out = []
            for i, h in enumerate(hosts):
                r = await run_host(h)
                out.append(r)
                if stop_on_error and _result_failed(r):
                    out.append({"info": f"serialized run stopped at host {h!r}"})
                    break
                if i < len(hosts) - 1 and delay_s > 0:
                    await asyncio.sleep(delay_s)
            return out
        raw = await asyncio.gather(*[run_host(h) for h in hosts],
                                   return_exceptions=True)
        out = []
        for h, r in zip(hosts, raw):
            if isinstance(r, Exception):
                out.append({"host": h, "error": str(r), "exit_code": -1})
            else:
                out.append(r)
        return out

    result = await _await_with_heartbeat(run_all(), ctx)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def local_exec(command: str, secrets: "list[str] | None" = None,
                            use_sudo: bool = False, *,
                            timeout: float,
                            ctx: "Context | None" = None) -> str:
    """Run a command on the **MCP SERVER's OWN machine** (local), NOT over SSH —
    for anything on a remote host use remote_exec instead. This departs from the
    project's core goal (driving *remote* hosts as if local); it's a useful but
    off-target derivative, so it is OFF by default: the operator must explicitly
    set PORTAL_ALLOW_LOCAL_EXEC=1 in the server process's env to enable it.

    ★ credentials: if the command needs a secret, don't have the user paste it
    into the chat — they run `portal secret set <name>` in their own terminal;
    you pass secrets=["<name>"] and reference it as the uppercased env var
    ($NAME), injected into the local child's env and redacted from output. This
    lets a local script use an API token without the value ever entering this
    conversation.

    ★ use_sudo: run the command under sudo on THIS machine (local_exec). The
    sudo password is resolved out-of-band (NEVER an argument): the per-user
    credential agent populated by `portal sudo set-local`, or a top-level
    `<local>:` section's `sudo_password_command` in hosts.yaml. Fed to `sudo -S -k`
    on stdin (never on argv/audit). May be combined with secrets (their values
    ride the same stdin channel, read+exported inside the sudo'd shell). This is
    the LOCAL counterpart of remote_exec(use_sudo=True); the reserved identity
    `<local>` is distinct from any SSH host named `local` / `localhost`.

    timeout (seconds, REQUIRED — no default): held open until the command exits
    or timeout elapses. Keepalive pings stop the client aborting a hung call, so
    pass a SMALL one for an exploratory / re-runnable command (e.g. 10–30) to
    fail fast, raising it only for a genuinely slow but bounded command. Hard
    ceiling (PORTAL_MAX_TIMEOUT, default 300s): a request above it is refused —
    local_exec has no background runner, so split the work into shorter steps.

    ★ secrets flags the result "high_risk": briefly tell the user you ran a local
    command with their stored credential, or only do so with prior permission.
    """
    if os.environ.get("PORTAL_ALLOW_LOCAL_EXEC", "").lower() not in (
        "1", "true", "yes", "on",
    ):
        raise ToolError(
            "local_exec is disabled. Running commands on the MCP "
            "server's OWN machine is off by default. To enable it, set the "
            "environment variable PORTAL_ALLOW_LOCAL_EXEC=1 in the MCP server "
            "PROCESS's environment — this is a process-env switch, NOT a "
            "policies.yaml/hosts.yaml setting (it is deliberately kept out of "
            "the config files so the agent can't self-enable local execution by "
            "editing a file it can otherwise write). The usual place is the env "
            "block of the portal server entry in the MCP client config that "
            'launches it (e.g. .mcp.json): add  "env": '
            '{"PORTAL_ALLOW_LOCAL_EXEC": "1"}  to that entry, then restart the '
            "session so the server is relaunched with it. (If you start the "
            "server another way, export the variable in its shell or set "
            "Environment=PORTAL_ALLOW_LOCAL_EXEC=1 in its systemd unit instead.)")
    _enforce_timeout_cap(timeout, job_hint=False)
    err = await _gate("<local>", command)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    if use_sudo:
        from .sudo_creds import LOCAL_SUDO_KEY, resolve_sudo_password
        from . import secrets_store
        password = await resolve_sudo_password(LOCAL_SUDO_KEY)
        if password is None:
            raise ToolError(_local_sudo_missing_message())
        sudo_env: dict[str, str] = {}
        sudo_values: list[str] = []
        if secrets:
            sudo_env, sudo_values, serr = await _resolve_secrets(secrets)
            if serr:
                raise ToolError(serr)
        res = await _await_with_heartbeat(
            _local_sudo_exec_env(command, password, sudo_env, timeout=timeout), ctx)
        res["output"] = secrets_store.redact(res.get("output", ""), sudo_values)
        res["high_risk"] = True
        note = (
            "Ran a privileged sudo command on the MCP server's OWN machine "
            "(local_exec) using the user's stored/cached local sudo password"
        )
        if secrets:
            note += f", with stored secret(s) [{','.join(secrets)}] injected"
        note += (". Briefly tell the user you did this (or only do so with their "
                 "explicit permission).")
        res["high_risk_note"] = note
        label = f"  [secrets: {','.join(secrets)}]" if secrets else ""
        audit_log("<local>", "sudo: " + command + label, res.get("exit_code", "?"),
                  operation="local_exec_sudo")
        return json.dumps(res, indent=2, ensure_ascii=False)

    env: dict[str, str] = {}
    values: list[str] = []
    if secrets:
        env, values, serr = await _resolve_secrets(secrets)
        if serr:
            raise ToolError(serr)
    from . import secrets_store
    res = await _await_with_heartbeat(
        _local_exec_env(command, env, timeout=timeout), ctx)
    res["output"] = secrets_store.redact(res.get("output", ""), values)
    if secrets:
        res["high_risk"] = True
        res["high_risk_note"] = (
            f"Ran a LOCAL command on the server using stored/cached secret(s) "
            f"[{','.join(secrets)}]. Briefly tell the user you did this (or only "
            "do so with their explicit permission)."
        )
    suffix = f"  [secrets: {','.join(secrets)}]" if secrets else ""
    audit_log("<local>", command + suffix, res.get("exit_code", "?"),
              operation="local_exec")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_close(host: str) -> str:
    """Close the cached persistent bash session for <host> (the next
    remote_shell call reopens a fresh one).

    Rarely needed: the session is created/reused/auto-recreated implicitly by
    remote_shell — you don't manage its lifecycle. Use this only to reset a
    session whose state has gotten dirty."""
    err = await _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    result = await _re_bash_close(host)
    audit_log(host, "close", "ok", operation="bash_close")
    return result


# ═══════════════════════════════════════════════════════════════════
# BACKGROUND JOBS  (remote_job — action=submit|poll|cancel|list)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def remote_job(action: Literal["submit", "poll", "cancel", "list"],
                     host: str = "", command: str = "", job_id: str = "",
                     since: int = 0, tail: int = 0, max_bytes: int = 65536,
                     signal: Literal["TERM", "KILL"] = "TERM",
                     use_sudo: bool = False,
                     secrets: "list[str] | None" = None,
                     login: "bool | None" = None) -> str:
    """Run a command in the **background** and get a job_id back immediately, so
    you can keep thinking while it runs, poll for incremental output, and cancel
    it. Use this for long tasks; for a command that finishes quickly just use
    remote_exec (it waits and returns the result).

    ## Actions
    - action="submit": start `command` on `host` in the background — in a LOGIN
        shell (bash -lc, loading ~/.profile/.bashrc PATH+env; login=False to opt
        out) via nohup + remote tmp files — returning {job_id, host, remote_pid,
        started_at,
        status} right away. The job keeps running even if the SSH connection
        drops. NOTE: sudo / secret injection are NOT supported in the
        background and passing use_sudo=True or secrets=[...] here is rejected
        with guidance (sudo -S needs a stdin the nohup process detaches;
        secrets would land on argv / `ps` for the whole job) — run those with
        remote_exec (one-shot) or remote_shell instead.
    - action="poll": fetch this job's status + new output **on demand, not all
        at once**. Required: job_id. Pass since=<new_offset from the previous
        poll> to get only the bytes produced since then; each poll returns at
        most `max_bytes` (default 64 KiB) so a big backlog doesn't dump in one
        shot. Keep polling with since=new_offset while the returned `more` is
        true to drain the rest. Or pass tail=N to just peek the last N lines.
        Returns {status: running|done|failed|cancelled|
        unknown, exit_code?, output_chunk, new_offset, more, finished_at?}.
    - action="cancel": signal the job. Required: job_id. signal=TERM (default)
        or KILL. Best-effort — `kill` doesn't guarantee instant death; poll to
        confirm. Returns {job_id, signal_sent, status_after}.
    - action="list": list all known jobs {job_id, host, status, started_at,
        age_s, exit_code?}.

    Limits (L1): job_ids are **best-effort persisted** across a server restart
    (the table reloads from <state>/jobs.json and a poll re-probes the remote
    PID); set PORTAL_JOB_PERSIST=0 to disable. It's not a durable queue — a
    crash mid-write loses the view, but the remote process keeps running and is
    recoverable via `ps`. Finished jobs are swept after a TTL (default 1h) and
    their tmp files removed. There is a cap on concurrent live jobs (default 50).

    Manual fallback (no remote_job): you can always background a command
    yourself with remote_exec(command="nohup mycmd >/tmp/x.log 2>&1 & echo $!")
    and poll the log with remote_exec(command="tail /tmp/x.log").
    """
    jm = get_job_manager()

    if action == "list":
        return json.dumps(await jm.list_jobs(), indent=2, ensure_ascii=False)

    if action == "submit":
        if not host or not command:
            raise ToolError('action="submit" requires `host` and `command`.')
        if use_sudo or secrets:
            raise ToolError(
                "remote_job is background-only and can't inject sudo passwords "
                "or secrets: the nohup process detaches stdin (nothing to feed "
                "`sudo -S`), and an exported secret would sit on the process "
                "argv (visible in `ps`) for the whole job lifetime. Use "
                "remote_exec(use_sudo=True / secrets=[...]) — it's synchronous, "
                "one-shot, and feeds them over stdin — or remote_shell for an "
                "interactive session. If the job is genuinely long AND needs "
                "elevation, configure NOPASSWD on the remote, or pre-stage the "
                "secret into a 0600 file with a one-shot remote_exec and read "
                "it inside the job."
            )
        err = await _gate(host, command)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        try:
            res = await jm.submit(host, command, login=_resolve_login(login, host))
        except RuntimeError as e:
            raise ToolError(str(e))
        audit_log(host, f"job-submit: {command}", res.get("job_id", "?"),
                  operation="job_submit")
        return json.dumps(res, indent=2, ensure_ascii=False)

    if action == "poll":
        if not job_id:
            raise ToolError('action="poll" requires `job_id`.')
        res = await jm.poll(job_id, since=since, tail=tail, max_bytes=max_bytes)
        return json.dumps(res, indent=2, ensure_ascii=False)

    if action == "cancel":
        if not job_id:
            raise ToolError('action="cancel" requires `job_id`.')
        # Gate against the job's host so an agent that lost host access can't
        # still kill its jobs (consistent with remote_tunnel close).
        jobs = await jm.list_jobs()
        owner = next((j["host"] for j in jobs if j["job_id"] == job_id), None)
        if owner is not None:
            err = await _gate(owner)
            if err:
                raise ToolError(f"BLOCKED: {err}")
        res = await jm.cancel(job_id, signal=signal)
        audit_log(owner or "?", f"job-cancel:{job_id}",
                  res.get("status_after", "?"), operation="job_cancel")
        return json.dumps(res, indent=2, ensure_ascii=False)

    raise ToolError(f'unknown action {action!r}. Valid: submit, poll, cancel, list.')


# ═══════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════
#
# Credential agent CLI design — single source of truth
# ----------------------------------------------------
# The CLI exposes four namespaces for credential management. The conceptual
# name (in docs, code, systemd units) is "credential agent"; the CLI uses a
# shorter top-level form for daily ergonomics.
#
#   portal agent install [--now]          install systemd --user units
#   portal agent uninstall                tear them down
#   portal agent run [--socket PATH]      daemon entry (used by systemd
#                                         ExecStart); not for humans
#   portal agent status                   ping the agent + count entries
#   portal agent clear                    clear the entire cache
#
#   portal ssh        set HOST     [--ttl N]  cache SSH login password
#   portal passphrase set HOST     [--ttl N]  cache SSH key passphrase
#   portal ssh        confirm HOST [--ttl N]  prompt twice, compare, then cache
#   portal ssh        show HOST               fingerprint + TTL (NO plaintext)
#   portal ssh        clear HOST              drop one entry
#   portal ssh        list                    list keys + fingerprint + TTL
#
#   portal sudo       {set,confirm,show,clear,list}   same shape, sudo password
#   portal secret     {set,confirm,show,clear,list}   same shape, named secret
#
# Design principle — *plaintext never leaves the agent's memory*. There is no
# `show plaintext` command, no `dump` command. Every human-facing verb
# returns either a fingerprint (sha256[:16]) + TTL or the plaintext is fed
# directly to a same-uid client process (the SSH connect loop, key unlock,
# sudo stdin, $env injection). Same posture as ssh-agent / gpg-agent / vault agent /
# polkit-agent: any echo to a TTY is one screenshot / scrollback / asciinema
# / OBS overlay away from a leak, so the agent simply refuses to do it.
# Sanity-check a stored credential with `confirm` (re-type and compare) or
# `show` (compare fingerprints); export with a command source from your
# password manager instead of asking the agent to echo.

_CREDENTIAL_SUBCOMMANDS = ("agent", "ssh", "passphrase", "sudo", "secret")
_CREDENTIAL_KINDS = ("ssh", "passphrase", "sudo", "secret")


def _agent_missing_message(path=None) -> str:
    first = (
        f"No portal credential agent socket at {path}."
        if path is not None
        else "Portal credential agent socket is not configured."
    )
    return (
        f"{first}\n"
        "Install/start the per-user agent first:\n"
        "  portal agent install --now\n"
        "Then retry this command."
    )


def _agent_path_or_exit(path_func):
    try:
        path = path_func()
    except RuntimeError as e:
        print(f"{e}\n\n{_agent_missing_message()}", file=sys.stderr)
        sys.exit(1)
    if path.exists():
        return path
    print(_agent_missing_message(path), file=sys.stderr)
    sys.exit(1)


def _ensure_agent_for_write(path_func):
    """Resolve the agent socket for a credential WRITE (set / confirm).

    Unlike _agent_path_or_exit (which bails), if the agent isn't installed/
    running yet this AUTO-INSTALLS it (equivalent to `portal agent install
    --now`), prints what it did (the full install output), and continues — so
    the user only ever runs `portal sudo set <host>` once. The password prompt
    happens after this returns.
    """
    try:
        path = path_func()
    except RuntimeError as e:
        # Can't even resolve a socket path (e.g. unsupported platform).
        print(f"{e}\n\n{_agent_missing_message()}", file=sys.stderr)
        sys.exit(1)
    if path.exists():
        return path
    print("Credential agent is not running yet — installing and starting it "
          "now (this is the same as `portal agent install --now`):\n")
    from .credential_agent import install_agent
    from .paths import credential_agent_platform
    try:
        res = install_agent(socket_path=None, enable_now=True)
    except RuntimeError as e:        # unsupported platform
        print(f"Auto-install not possible: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Auto-install failed: {e}\n\n{_agent_missing_message()}",
              file=sys.stderr)
        sys.exit(1)
    _print_install_result(credential_agent_platform(), res, enable_now=True)
    path = path_func()
    if not path.exists():
        print("\nNote: the agent was installed but its socket isn't visible "
              "yet; if the next step fails, retry in a moment.", file=sys.stderr)
    print()  # blank line before the (hidden) password prompt
    return path


def _kind_key_noun(kind: str) -> str:
    """User-facing label for the credential kind's key argument."""
    return {
        "ssh": "host",
        "passphrase": "host",
        "sudo": "host",
        "secret": "name",
    }[kind]


def _kind_prompt(kind: str, key: str) -> str:
    """getpass prompt for a kind/key."""
    if kind == "sudo" and key == "<local>":
        return "sudo password for THIS machine (local_exec): "
    return {
        "ssh": f"SSH login password for host '{key}': ",
        "passphrase": f"SSH key passphrase for host '{key}': ",
        "sudo": f"sudo password for host '{key}': ",
        "secret": f"value for secret '{key}': ",
    }[kind]


def _kind_label(kind: str) -> str:
    """Human label for the credential kind (singular)."""
    return {
        "ssh": "SSH login password",
        "passphrase": "SSH key passphrase",
        "sudo": "sudo password",
        "secret": "secret",
    }[kind]


def _store_credential_value(credential_agent, kind: str, key: str,
                            value: str, ttl: int) -> str | None:
    try:
        resp = credential_agent.store(kind, key, value, ttl=ttl)
    except (OSError, RuntimeError) as e:
        return f"Failed to reach credential agent: {e}"
    if resp.get("status") != "ok":
        return f"Error: {resp.get('error', 'unknown')}"
    return None


def _should_cache_ssh_password_as_sudo(host: str) -> bool:
    try:
        return get_manager().should_cache_ssh_password_as_sudo(host)
    except Exception:
        logger.debug("could not inspect host sudo/ssh password policy for %s",
                     host, exc_info=True)
        return False


def _maybe_cache_ssh_password_as_sudo(args, credential_agent,
                                      value: str) -> int:
    if args.kind != "ssh" or not _should_cache_ssh_password_as_sudo(args.key):
        return 0
    error = _store_credential_value(
        credential_agent, "sudo", args.key, value, args.ttl)
    if error:
        print(f"Failed to also cache sudo password for '{args.key}': {error}",
              file=sys.stderr)
        return 1
    print(f"sudo password also cached for '{args.key}' "
          f"(same as SSH login password, expires in {_format_ttl(args.ttl)}).")
    return 0


def _format_ttl(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


# ── portal agent ─────────────────────────────────────────────────────

def _print_install_result(backend: str, res: dict, *, enable_now: bool) -> None:
    """Print what ``install_agent`` did. Shared by ``portal agent install`` and
    the auto-install triggered on the first ``portal <kind> set``."""
    from .credential_agent import SOCKET_UNIT, LAUNCHD_LABEL
    if backend == "systemd":
        print("Installed portal credential agent user units:")
        print(f"  socket unit:   {res['socket_unit']}")
        print(f"  service unit:  {res['service_unit']}")
        print(f"  config:        {res['config_path']}")
        print(f"  recorded path: {res['socket_path']}")
        if not enable_now:
            print(f"Enable it with: systemctl --user enable --now {SOCKET_UNIT}")
    elif backend == "launchd":
        print("Installed portal credential agent LaunchAgent:")
        print(f"  plist:         {res['plist']}")
        print(f"  config:        {res['config_path']}")
        print(f"  recorded path: {res['socket_path']}")
        if not enable_now:
            print(f"Load it with: launchctl load -w "
                  f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
    else:  # schtasks (Windows per-user logon task)
        print("Installed portal credential agent scheduled task (per-user):")
        print(f"  task name:     {res['task_name']}")
        print(f"  task xml:      {res['task_xml']}")
        print(f"  config:        {res['config_path']}")
        print(f"  recorded pipe: {res['socket_path']}")
        print("  (runs as you, in your session, at logon — never as SYSTEM)")
        if not enable_now:
            print(f"Start it now with: schtasks /Run /TN {res['task_name']}"
                  f"  (or just log out and back in)")


def _agent_install_cli(args) -> int:
    from .credential_agent import install_agent
    from .paths import credential_agent_platform
    backend = credential_agent_platform()
    try:
        res = install_agent(socket_path=args.socket, enable_now=args.now)
    except RuntimeError as e:
        # Unsupported platform: print the actionable hint, not a stack trace.
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Failed to install credential agent: {e}", file=sys.stderr)
        return 1
    _print_install_result(backend, res, enable_now=args.now)
    return 0


def _agent_uninstall_cli(args) -> int:
    from .credential_agent import uninstall_agent
    try:
        res = uninstall_agent(
            stop_now=not args.no_stop,
            remove_config=not args.keep_config,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("Uninstalled portal credential agent.")
    if res["removed"]:
        print("Removed:")
        for path in res["removed"]:
            print(f"  {path}")
    if res["errors"]:
        print("Warnings:")
        for err in res["errors"]:
            print(f"  service manager: {err}")
    return 0


def _agent_run_cli(args) -> int:
    from .credential_agent import serve_forever
    serve_forever(args.socket)
    return 0


def _agent_status_cli(_args) -> int:
    from . import credential_agent
    from .paths import credential_agent_socket_path
    try:
        path = credential_agent_socket_path()
    except RuntimeError as e:
        print(f"{e}", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"agent socket: {path} (does not exist)")
        print("agent: not running (run `portal agent install --now`).")
        return 1
    try:
        resp = credential_agent.status()
    except (OSError, RuntimeError) as e:
        print(f"agent socket: {path}")
        print(f"agent: unreachable — {e}", file=sys.stderr)
        return 1
    if resp.get("status") != "ok":
        print(f"agent socket: {path}")
        print(f"agent: error — {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    counts = resp.get("counts", {})
    print(f"agent socket: {path}")
    print("agent: running")
    total = sum(counts.values())
    print(f"cached entries: {total} total")
    for kind in _CREDENTIAL_KINDS:
        print(f"  {kind:<7s} {counts.get(kind, 0)}")
    return 0


def _agent_clear_cli(_args) -> int:
    from . import credential_agent
    errors: list[str] = []
    for kind in _CREDENTIAL_KINDS:
        try:
            resp = credential_agent.clear(kind)
        except (OSError, RuntimeError) as e:
            errors.append(f"{kind}: {e}")
            continue
        if resp.get("status") != "ok":
            errors.append(f"{kind}: {resp.get('error', 'unknown')}")
    if errors:
        print("Some kinds could not be cleared:")
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    print("Cleared all cached credentials (ssh / sudo / secret).")
    return 0


def _build_agent_subparser(sub):
    import argparse
    from pathlib import Path
    from .credential_agent import SOCKET_UNIT

    p = sub.add_parser(
        "agent",
        help="Manage the per-user credential agent (systemd / launchd / "
             "scheduled task).",
        description="Install/run/inspect the per-user credential agent. The "
                    "agent is a long-lived process that holds TTL-cached "
                    "ssh/sudo/secret values in memory and is reached over a "
                    "Unix socket (Linux/macOS) or named pipe (Windows) scoped "
                    "to your user. `install` picks the right per-user service "
                    "manager for the platform: systemd --user (Linux, "
                    "socket-activated on first request), launchd (macOS), or a "
                    "scheduled task (Windows).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    asub = p.add_subparsers(dest="verb", required=True, metavar="<verb>")

    p_install = asub.add_parser(
        "install", help="Install systemd --user .socket + .service units.")
    p_install.add_argument(
        "--socket", type=Path, default=None,
        help="override ListenStream path; default unit uses systemd %%t")
    p_install.add_argument(
        "--now", action="store_true",
        help=f"run systemctl --user enable --now {SOCKET_UNIT}")
    p_install.set_defaults(func=_agent_install_cli)

    p_uninstall = asub.add_parser(
        "uninstall", help="Stop/disable and remove the agent's user units.")
    p_uninstall.add_argument(
        "--keep-config", action="store_true",
        help="keep ~/.config/portal-mcp-server/agent.json")
    p_uninstall.add_argument(
        "--no-stop", action="store_true",
        help="remove files only; do not call systemctl --user")
    p_uninstall.set_defaults(func=_agent_uninstall_cli)

    p_run = asub.add_parser(
        "run",
        help="Daemon entry — used by the systemd service ExecStart; not "
             "for interactive use.")
    p_run.add_argument(
        "--socket", type=Path, default=None,
        help="manual socket path for non-systemd debugging/tests")
    p_run.set_defaults(func=_agent_run_cli)

    p_status = asub.add_parser(
        "status",
        help="Ping the agent and print cached-entry counts per kind.")
    p_status.set_defaults(func=_agent_status_cli)

    p_clear = asub.add_parser(
        "clear",
        help="Clear ALL cached credentials (ssh, passphrase, sudo, secret).")
    p_clear.set_defaults(func=_agent_clear_cli)


# ── portal {ssh,passphrase,sudo,secret} ──────────────────────────────

def _kind_set_cli(args) -> int:
    import getpass
    from . import credential_agent
    from .paths import credential_agent_socket_path
    _ensure_agent_for_write(credential_agent_socket_path)
    prompt = _kind_prompt(args.kind, args.key)
    value = getpass.getpass(prompt)
    if not value:
        print("Empty value, aborted.", file=sys.stderr)
        return 1
    error = _store_credential_value(
        credential_agent, args.kind, args.key, value, args.ttl)
    if error:
        print(error, file=sys.stderr)
        return 1
    print(f"{_kind_label(args.kind)} cached for "
          f"'{args.key}' (expires in {_format_ttl(args.ttl)}).")
    return _maybe_cache_ssh_password_as_sudo(args, credential_agent, value)


def _kind_confirm_cli(args) -> int:
    import getpass
    from . import credential_agent
    from .paths import credential_agent_socket_path
    _ensure_agent_for_write(credential_agent_socket_path)
    prompt = _kind_prompt(args.kind, args.key)
    first = getpass.getpass(prompt)
    if not first:
        print("Empty value, aborted.", file=sys.stderr)
        return 1
    again = getpass.getpass(f"confirm: {prompt}")
    if first != again:
        print("Values differ; nothing cached.", file=sys.stderr)
        return 1
    error = _store_credential_value(
        credential_agent, args.kind, args.key, first, args.ttl)
    if error:
        print(error, file=sys.stderr)
        return 1
    print(f"{_kind_label(args.kind)} cached for "
          f"'{args.key}' (entries matched, expires in {_format_ttl(args.ttl)}).")
    return _maybe_cache_ssh_password_as_sudo(args, credential_agent, first)


def _kind_show_cli(args) -> int:
    from . import credential_agent
    from .paths import credential_agent_socket_path
    _agent_path_or_exit(credential_agent_socket_path)
    try:
        resp = credential_agent.fingerprint(args.kind, args.key)
    except (OSError, RuntimeError) as e:
        print(f"Failed to reach credential agent: {e}", file=sys.stderr)
        return 1
    status = resp.get("status")
    if status == "missing":
        print(f"No cached {_kind_label(args.kind)} for '{args.key}'.")
        return 1
    if status != "ok":
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    fp = resp.get("fingerprint", "?")
    ttl = int(resp.get("ttl_remaining", 0))
    noun = _kind_key_noun(args.kind)
    print(f"{_kind_label(args.kind)} for {noun} '{args.key}':")
    print(f"  fingerprint: sha256:{fp}  (plaintext is NOT shown by design)")
    print(f"  expires in:  {_format_ttl(ttl)}")
    return 0


def _kind_clear_cli(args) -> int:
    from . import credential_agent
    from .paths import credential_agent_socket_path
    _agent_path_or_exit(credential_agent_socket_path)
    try:
        resp = credential_agent.clear(args.kind, args.key)
    except (OSError, RuntimeError) as e:
        print(f"Failed to reach credential agent: {e}", file=sys.stderr)
        return 1
    if resp.get("status") != "ok":
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    noun = _kind_key_noun(args.kind)
    print(f"Cleared {_kind_label(args.kind)} for {noun} '{args.key}'.")
    return 0


def _kind_list_cli(args) -> int:
    from . import credential_agent
    from .paths import credential_agent_socket_path
    _agent_path_or_exit(credential_agent_socket_path)
    try:
        resp = credential_agent.list_entries(args.kind)
    except (OSError, RuntimeError) as e:
        print(f"Failed to reach credential agent: {e}", file=sys.stderr)
        return 1
    if resp.get("status") != "ok":
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    entries = resp.get("entries", [])
    if not entries:
        print(f"No cached {_kind_label(args.kind)} entries.")
        return 0
    noun = _kind_key_noun(args.kind)
    width = max(len(noun), max(len(e["key"]) for e in entries))
    print(f"{noun:<{width}s}  fingerprint        expires in")
    for e in entries:
        fp = e.get("fingerprint", "?")
        ttl = int(e.get("ttl_remaining", 0))
        print(f"{e['key']:<{width}s}  sha256:{fp}  {_format_ttl(ttl)}")
    return 0


def _build_kind_subparser(sub, kind: str):
    import argparse
    from .credential_agent import DEFAULT_TTL_SEC

    noun = _kind_key_noun(kind)
    descriptions = {
        "ssh": "Manage the cached SSH login password for a host — used for "
               "`auth: password` hosts, or as the explicit password fallback "
               "after key authentication is refused. Cached in the per-user "
               "credential agent's memory only.",
        "passphrase": "Manage cached SSH private-key passphrases for a host. "
                      "This unlocks a local encrypted key file and is separate "
                      "from the SSH login password. Cached in the per-user "
                      "credential agent's memory only.",
        "sudo": "Manage cached sudo passwords. Fed to `sudo -S` on stdin "
                "when an MCP call sets use_sudo=True. Cached in the "
                "per-user credential agent's memory only.",
        "secret": "Manage cached named secrets (API tokens). Injected as "
                  "$NAME env vars when an MCP call lists the secret in "
                  "its `secrets` parameter. Cached in the per-user "
                  "credential agent's memory only.",
    }
    p = sub.add_parser(
        kind,
        help=descriptions[kind].split(".")[0] + ".",
        description=descriptions[kind] + "\n\n"
                    "Design principle: there is no `show plaintext` verb. "
                    "Use `confirm` to sanity-check (re-type and compare), "
                    "`show` to view sha256 fingerprint + TTL, or `list` "
                    "for an overview. The plaintext is only ever handed to "
                    "the same-uid SSH/key-unlock/sudo/$env consumer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ksub = p.add_subparsers(dest="verb", required=True, metavar="<verb>")

    p_set = ksub.add_parser(
        "set",
        help=f"Prompt (no echo) for a {_kind_label(kind)} for the given "
             f"{noun} and cache it.")
    p_set.add_argument(noun, help=f"{noun} the value is for")
    p_set.add_argument(
        "--ttl", type=int, default=DEFAULT_TTL_SEC,
        help=f"seconds before the cached value expires (default {DEFAULT_TTL_SEC})")
    p_set.set_defaults(kind=kind, key=None, func=_kind_set_cli)

    p_confirm = ksub.add_parser(
        "confirm",
        help="Prompt twice (no echo) and cache only if the two entries match.")
    p_confirm.add_argument(noun, help=f"{noun} the value is for")
    p_confirm.add_argument(
        "--ttl", type=int, default=DEFAULT_TTL_SEC,
        help=f"seconds before the cached value expires (default {DEFAULT_TTL_SEC})")
    p_confirm.set_defaults(kind=kind, key=None, func=_kind_confirm_cli)

    p_show = ksub.add_parser(
        "show",
        help="Print sha256 fingerprint + remaining TTL (NO plaintext).")
    p_show.add_argument(noun, help=f"{noun} to look up")
    p_show.set_defaults(kind=kind, key=None, func=_kind_show_cli)

    p_clear = ksub.add_parser(
        "clear", help=f"Drop the cached {_kind_label(kind)} for one {noun}.")
    p_clear.add_argument(noun, help=f"{noun} to clear")
    p_clear.set_defaults(kind=kind, key=None, func=_kind_clear_cli)

    p_list = ksub.add_parser(
        "list",
        help=f"List every cached {_kind_label(kind)} ({noun}, fingerprint, TTL).")
    p_list.set_defaults(kind=kind, key=None, func=_kind_list_cli)

    if kind == "sudo":
        # Local-machine sudo for local_exec(use_sudo=True). The reserved
        # identity is `<local>` (illegal as a hostname, so it never collides with
        # a remote named `local`/`localhost`). A dedicated verb means the user
        # never has to type the angle-bracketed key or quote it for the shell.
        p_setlocal = ksub.add_parser(
            "set-local",
            help="Prompt (no echo) for THIS machine's sudo password (used by "
                 "local_exec(use_sudo=True)) and cache it.")
        p_setlocal.add_argument(
            "--ttl", type=int, default=DEFAULT_TTL_SEC,
            help=f"seconds before the cached value expires (default {DEFAULT_TTL_SEC})")
        p_setlocal.set_defaults(kind="sudo", host="<local>", func=_kind_set_cli)


def _credential_main(argv: list[str]) -> int:
    """Subcommand dispatcher for credential-agent CLI verbs.

    argv[0] is the subcommand name (one of _CREDENTIAL_SUBCOMMANDS).
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="portal",
        description="Portal credential agent CLI — manage cached SSH login "
                    "passwords, key passphrases, sudo passwords and secrets "
                    "held by the per-user agent process.",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True, metavar="<subcmd>")
    _build_agent_subparser(sub)
    for kind in _CREDENTIAL_KINDS:
        _build_kind_subparser(sub, kind)

    # Argparse copies dest values from sub-sub-parsers onto the Namespace,
    # so args.kind / args.key are populated by the per-verb set_defaults.
    # `key` defaults to None and is overwritten by the positional argument
    # via dest aliasing below.
    args = parser.parse_args(argv)
    # For kind subparsers, the positional was named after the noun
    # ("host" / "name"); copy it onto the conventional `key` field so the
    # verb handlers don't have to special-case.
    if args.subcmd in _CREDENTIAL_KINDS:
        noun = _kind_key_noun(args.subcmd)
        raw_key = getattr(args, noun, None)
        args.key = normalize_host_name(raw_key) if raw_key is not None else None
    return args.func(args)


def _is_loopback_host(host: str) -> bool:
    """True if an HTTP bind address is loopback-only (safe without auth).

    ``0.0.0.0`` / ``::`` (all interfaces) and any concrete non-127 address are
    reachable off-box and therefore NOT loopback."""
    h = (host or "").strip().lower()
    return h in ("localhost", "127.0.0.1", "::1") or h.startswith("127.")


def main() -> None:
    """CLI entrypoint registered as `portal-mcp-server` / `portal`.

    Dispatch:
      * `portal <cred-subcmd> ...` — credential agent CLI
        (cred-subcmd ∈ {agent, ssh, passphrase, sudo, secret})
      * everything else — start the MCP server (default stdio transport).
    """
    import argparse
    if len(sys.argv) >= 2 and sys.argv[1] in _CREDENTIAL_SUBCOMMANDS:
        sys.exit(_credential_main(sys.argv[1:]) or 0)

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    PORTAL_AUTH_TOKEN = os.environ.get("PORTAL_AUTH_TOKEN", "")

    class TokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if not PORTAL_AUTH_TOKEN:
                return await call_next(request)
            auth = request.headers.get("Authorization", "")
            # Constant-time compare so a timing side channel can't be used to
            # recover the token byte-by-byte. Encode to bytes to avoid
            # compare_digest's TypeError on a non-ASCII token.
            if not hmac.compare_digest(
                    auth.encode("utf-8"), f"Bearer {PORTAL_AUTH_TOKEN}".encode("utf-8")):
                return Response("Unauthorized", status_code=401)
            return await call_next(request)

    parser = argparse.ArgumentParser(
        prog="portal-mcp-server",
        description="portal-mcp-server — Agent-feels-local SSH orchestration MCP server.\n\n"
                    "Without any subcommand this starts the MCP server. The credential\n"
                    "agent CLI lives under these subcommands (use `<subcmd> --help`):\n"
                    "  portal agent      install / uninstall / run / status / clear\n"
                    "  portal ssh        set / confirm / show / clear / list  <host>\n"
                    "  portal passphrase set / confirm / show / clear / list  <host>\n"
                    "  portal sudo       set / confirm / show / clear / list  <host>\n"
                    "  portal secret     set / confirm / show / clear / list  <name>",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    from . import server_info as _si
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {_si._VERSION}")
    parser.add_argument("--transport", choices=["stdio", "streamable_http"], default="stdio",
                        help="MCP transport (default: stdio)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind address (default: 127.0.0.1, loopback "
                             "only; a non-loopback bind requires PORTAL_AUTH_TOKEN)")
    args = parser.parse_args()

    _set_server_transport(args.transport)
    logger.info(
        f"portal-mcp-server v{_si._VERSION} starting | transport={args.transport}")

    if args.transport == "streamable_http":
        import uvicorn
        loopback = _is_loopback_host(args.host)
        if not loopback and not PORTAL_AUTH_TOKEN:
            # Every MCP tool here can drive remote hosts / read-write local
            # files; exposing that unauthenticated on a reachable interface is
            # a footgun. Refuse rather than silently bind wide open.
            raise SystemExit(
                f"refusing to start HTTP transport on non-loopback address "
                f"{args.host!r} without authentication. Set PORTAL_AUTH_TOKEN "
                f"(and terminate TLS in front, e.g. a reverse proxy), or bind "
                f"--host 127.0.0.1 for local-only access.")
        app = mcp.streamable_http_app()
        if PORTAL_AUTH_TOKEN:
            app.add_middleware(TokenAuthMiddleware)
            logger.info("Bearer token auth enabled")
        logger.info(f"HTTP transport on {args.host}:{args.port}/mcp")
        if not loopback:
            logger.warning(
                "HTTP transport bound to non-loopback %s — portal serves "
                "plaintext HTTP, so terminate TLS in front (reverse proxy).",
                args.host)
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        logger.info("stdio transport active")
        mcp.run()


if __name__ == "__main__":
    main()
