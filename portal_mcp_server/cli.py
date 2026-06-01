"""
portal-mcp-server — Agent-feels-local SSH orchestration MCP server.
Exposes 19 portal_* tools covering: read/patch/cleanup_tmps/grep/glob/
bash(+close,status) core + local_exec + host/transfer/tunnel(open,close,
list)/multi_exec/playbook/ping/audit/check.
"""
import asyncio
import json
import logging
import os
import sys
import time

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError
from .paths import default_log_dir
from .connection_manager import get_manager
from .shell_engine import ssh_exec
from .file_ops import (ssh_upload_file, ssh_download_file, ssh_sync_directory,
                       ssh_mirror_directory, ssh_upload_list, ssh_download_list)
from .network_tools import get_tunnel_manager
from .orchestrator import (ssh_parallel_exec, ssh_rolling_exec,
                            ssh_broadcast, run_playbook, run_playbook_on_group)
from .audit import audit_log, get_history, get_audit_stats
from .security import get_policy
from .remote_text_editor import (
    remote_read as _re_read,
    remote_patch as _re_patch,
    cleanup_orphan_tmps as _re_cleanup_tmps,
)
from .remote_search import remote_grep as _re_grep, remote_glob as _re_glob
from .remote_bash import (
    remote_bash as _re_bash,
    remote_bash_close as _re_bash_close,
    remote_bash_status as _re_bash_status,
    remote_sudo_exec as _re_sudo_exec,
    remote_exec_with_env as _re_exec_env,
)
from .local_exec import local_exec_with_env as _local_exec_env

_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
try:
    _log_dir = default_log_dir()
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(logging.FileHandler(_log_dir / "server.log", encoding="utf-8"))
except Exception as _log_err:
    print(f"[portal-mcp-server] WARNING: could not open log file: {_log_err}", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("portal_mcp")

mcp = FastMCP("portal-mcp-server")

# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _gate(host: str, command: str = "") -> str | None:
    """Returns error string if blocked, None if allowed."""
    return get_policy().enforce(host, command)


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
    for name in names:
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
        env[secrets_store.env_var_name(name)] = value
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
        loop.create_task(_safe_report(ctx, done, total))

    return cb


async def _safe_report(ctx: "Context", done: int, total: int) -> None:
    try:
        await ctx.report_progress(done, total)
    except Exception:  # pragma: no cover - progress is best-effort
        pass


def _gate_many(hosts: list[str], command: str = "") -> str | None:
    """Multi-host policy gate.

    Two-phase to avoid burning rate-limit quota on hosts that pass when a
    later host fails: first run all *non-mutating* checks (command +
    host allowlist) over every host, only commit per-host rate-limit
    consumption once every host has passed. Returns the first error
    found, or None if all checks pass.

    Used by multi-host orchestration tools (ssh_rolling, ssh_group_exec,
    ssh_broadcast_batch, ssh_playbook_on_group) so they cannot bypass the
    policy gate that ssh_run / ssh_exec_with_env etc. enforce.
    """
    pol = get_policy()
    if command:
        err = pol.check_command(command)
        if err:
            return err
    # Phase 1: validate every host (no mutation).
    for h in hosts:
        err = pol.check_host(h)
        if err:
            return f"{h}: {err}"
    # Phase 2: commit rate-limit only after every host validated.
    for h in hosts:
        err = pol.check_rate_limit(h)
        if err:
            return f"{h}: {err}"
    return None


def _resolve_group(group_tag: str) -> list[str]:
    """Resolve a group tag to the list of registered host names carrying it."""
    mgr = get_manager()
    return [h.name for h in mgr._registry.values() if group_tag in h.tags]


def _gate_playbook(hosts: list[str], playbook: dict) -> str | None:
    """Policy gate for playbooks: check every step's command against the
    blocklist/allowlist on every target host. Rate limit is checked per host
    once (not per step) to avoid burning the rate-limit budget before the
    playbook even runs. Same two-phase shape as ``_gate_many`` so a single
    bad host can't burn rate-limit quota on the others.
    """
    pol = get_policy()
    steps = playbook.get("steps", []) or []
    # Phase 0: every step must be a string we can actually execute.
    for step in steps:
        if not isinstance(step, str):
            return (f"step {step!r}: invalid type {type(step).__name__}; "
                    "playbook steps must be shell-command strings")
    # Phase 1: command blocklist for every step (no mutation).
    for step in steps:
        err = pol.check_command(step)
        if err:
            return f"step {step[:60]!r}: {err}"
    # Phase 2: validate every host (no mutation).
    for h in hosts:
        err = pol.check_host(h)
        if err:
            return f"{h}: {err}"
    # Phase 3: commit rate-limit only after every host validated.
    for h in hosts:
        err = pol.check_rate_limit(h)
        if err:
            return f"{h}: {err}"
    return None


def _parse_env(env_json: str) -> dict:
    """Parse a JSON env string into a dict. Returns empty dict on any error."""
    try:
        return json.loads(env_json) if env_json.strip() not in ("", "{}") else {}
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════════════
# 1. HOST REGISTRY  (portal_host)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def portal_host(action: str, name: str = "", host: str = "",
                 user: str = "root", port: int = 22,
                 key_path: str = "", tags: str = "") -> str:
    """Manage the SSH host registry.

    ## Modes
    - action="list": list all registered hosts.
        Example: portal_host(action="list")
    - action="register": add a host to the registry.
        Required: name, host. Optional: user (default root), port (default 22),
        key_path (else asyncssh falls back to ~/.ssh/id_* or ssh-agent),
        tags (comma-separated, used by portal_multi_exec mode="group").
        Example: portal_host(action="register", name="web01", host="10.0.0.1",
                              user="ubuntu", tags="web,prod")
    - action="remove": remove a host from the registry.
        Required: name.
        Example: portal_host(action="remove", name="web01")

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
        if not name or not host:
            raise ToolError('action="register" requires both `name` and `host`.')
        # Gate against the *target* (the actual host/IP that traffic will
        # reach), not the alias the agent picked. Otherwise an agent can
        # register an arbitrary alias pointing at a non-allowlisted host
        # and then operate on it freely — host_allowlist would only ever
        # see the alias.
        err = _gate(host)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
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
        err = _gate(name)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        result = mgr.remove_host(name)
        audit_log(name, "remove_host", "ok", operation="host_remove")
        return result
    raise ToolError(f'unknown action {action!r}. Valid: list, register, remove.')


# ═══════════════════════════════════════════════════════════════════
# 2. FILE TRANSFER  (portal_transfer — SFTP-based, binary safe)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def portal_transfer(direction: str, host: str,
                           local_path: str, remote_path: str,
                           ctx: Context, checksum: bool = False,
                           paths_json: str = "") -> str:
    """Transfer files between local and remote via SFTP (binary-safe, atomic).

    ## Modes
    - direction="upload": local_path → remote_path (single file).
        Example: portal_transfer(direction="upload", host="web01",
                                  local_path="/tmp/app.jar",
                                  remote_path="/opt/app/app.jar")
    - direction="download": remote_path → local_path (single file).
        Example: portal_transfer(direction="download", host="web01",
                                  remote_path="/var/log/syslog",
                                  local_path="/tmp/syslog")
    - direction="sync": recursively sync local_path directory → remote_path
        directory (upload). Files already present with a matching size+mtime
        (or sha256 when checksum=True) are skipped.
    - direction="mirror": recursively mirror remote_path directory → local_path
        directory (download); the remote→local counterpart of "sync".
        Example: portal_transfer(direction="mirror", host="web01",
                                  remote_path="/srv/www/", local_path="./www/")
    - direction="upload-list" / "download-list": transfer an explicit list of
        file pairs given in paths_json (an arbitrary local→remote mapping, not a
        whole directory). Each pair is skipped when already present with a
        matching size+mtime (or sha256 when checksum=True), so re-runs only move
        the changed files; a single pair's failure is collected in failed[]
        without aborting the batch. local_path / remote_path are ignored in
        these modes.
        Example: portal_transfer(direction="upload-list", host="web01",
            paths_json='[{"local":"/tmp/a.conf","remote":"/etc/app/a.conf"},
                         {"local":"/tmp/b.conf","remote":"/etc/app/b.conf"}]')

    Args:
        checksum: for the incremental modes (sync/mirror/upload-list/
            download-list), compare files by sha256 instead of size+mtime
            (slower, requires `sha256sum` on the remote; missing remote files or
            an unavailable sha256sum force a re-transfer).
        paths_json: JSON array of {"local": ..., "remote": ...} objects, required
            by the upload-list / download-list modes (ignored otherwise).

    Progress is reported to the MCP client during transfers, which also keeps
    the connection alive so large files don't trip client idle timeouts.

    Returns a JSON status dict. Single-file: {status, direction, host, bytes,
    duration_s, ...}. sync/mirror/upload-list/download-list: {status,
    uploaded|downloaded, skipped, failed[], bytes_total, bytes_transferred,
    duration_s}.

    For text-only edits prefer portal_patch (hash-protected). Use portal_transfer
    when SFTP semantics are needed: binary files, large files, whole directory
    trees. Note: directory modes copy *files* only — symlinks and special files
    are skipped, and empty directories are not created on their own.
    """
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    progress_cb = _make_progress_cb(ctx)
    if direction == "upload":
        result = await ssh_upload_file(host, local_path, remote_path,
                                       progress_cb=progress_cb)
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
# 3. SSH TUNNELS  (portal_tunnel_open / close / list)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def portal_tunnel_open(mode: str, host: str,
                              local_port: int = 0, local_bind: str = "127.0.0.1",
                              remote_host: str = "", remote_port: int = 0) -> str:
    """Open an SSH tunnel through `host`.

    ## Modes
    - mode="local": forward localhost:local_port → remote_host:remote_port via host.
        Required: local_port (0 = auto-assign), remote_host, remote_port.
        Example: portal_tunnel_open(mode="local", host="bastion",
                                     local_port=5432, remote_host="db.internal",
                                     remote_port=5432)
    - mode="reverse": expose local_bind:local_port to host as host:remote_port.
        Required: remote_port, local_bind, local_port.
        Example: portal_tunnel_open(mode="reverse", host="bastion",
                                     remote_port=8080, local_bind="127.0.0.1",
                                     local_port=3000)
    - mode="socks": SOCKS5 proxy on localhost:local_port via host.
        Required: local_port (default 1080).
        Example: portal_tunnel_open(mode="socks", host="bastion", local_port=1080)

    Returns: {tunnel_id, type, host, local, remote}. Use portal_tunnel_close
    with the tunnel_id to close. portal_tunnel_list shows all active tunnels.
    """
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    tm = get_tunnel_manager()
    if mode == "local":
        result = await tm.open_local_forward(host, local_port,
                                              remote_host, remote_port, local_bind)
    elif mode == "reverse":
        result = await tm.open_remote_forward(host, remote_port,
                                               local_bind, local_port)
    elif mode == "socks":
        result = await tm.open_dynamic_proxy(host,
                                              local_port or 1080, local_bind)
    else:
        raise ToolError(f'unknown mode {mode!r}. Valid: local, reverse, socks.')
    audit_log(host, f"tunnel:{mode}", "ok", operation="tunnel_open")
    return json.dumps(result, indent=2)


@mcp.tool()
async def portal_tunnel_close(tunnel_id: str) -> str:
    """Close an active SSH tunnel by ID.

    Args:
        tunnel_id: ID returned by portal_tunnel_open.
    """
    tm = get_tunnel_manager()
    # Look up the originating host so we can run it through the security
    # gate (consistent with portal_tunnel_open). Without this gate an
    # agent that lost host access could still dismantle live tunnels.
    host = next((t["host"] for t in tm.list_tunnels()
                 if t["tunnel_id"] == tunnel_id), None)
    if host is None:
        raise ToolError(f"Tunnel '{tunnel_id}' not found")
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    result = await tm.close_tunnel(tunnel_id)
    audit_log("tunnel", f"close:{tunnel_id}", "ok", operation="tunnel_close")
    return result


@mcp.tool()
def portal_tunnel_list() -> str:
    """List all currently active SSH tunnels (returns JSON array)."""
    tunnels = get_tunnel_manager().list_tunnels()
    return json.dumps(tunnels, indent=2) if tunnels else "No active tunnels."


# ═══════════════════════════════════════════════════════════════════
# 4. MULTI-HOST EXECUTION  (portal_multi_exec, portal_playbook)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def portal_multi_exec(mode: str, command: str = "",
                             commands_json: str = "",
                             hosts_json: str = "", group_tag: str = "",
                             timeout: int = 3600, delay_s: float = 2.0,
                             stop_on_error: bool = True) -> str:
    """Execute commands across multiple hosts.

    ## Modes
    - mode="parallel": run `command` on all hosts simultaneously.
        Required: command + (hosts_json OR group_tag).
        Example: portal_multi_exec(mode="parallel", command="uptime",
                                    hosts_json='["web01","web02"]')
    - mode="rolling": run `command` sequentially with delay between hosts
        (zero-downtime restart pattern).
        Required: command + hosts_json. Optional: delay_s (default 2.0),
        stop_on_error (default True).
        Example: portal_multi_exec(mode="rolling",
                                    command="systemctl restart nginx",
                                    hosts_json='["web01","web02","web03"]',
                                    delay_s=5)
    - mode="broadcast": run a SEQUENCE of commands on all hosts in parallel
        (each host runs the full sequence).
        Required: commands_json (JSON array) + hosts_json.
        Example: portal_multi_exec(mode="broadcast",
                                    commands_json='["apt update","apt install -y nginx"]',
                                    hosts_json='["web01","web02"]')

    For single-host commands use portal_bash. To select hosts by tag instead of
    explicit list, pass group_tag="<tag>" instead of hosts_json (parallel mode only).
    """
    if group_tag:
        hosts = _resolve_group(group_tag)
        if not hosts:
            return json.dumps([{"error": f"No hosts found with tag {group_tag!r}"}], indent=2)
    else:
        try:
            hosts = json.loads(hosts_json) if hosts_json else []
        except Exception as e:
            raise ToolError(f"Invalid hosts_json: {e}")
        if not hosts:
            raise ToolError('must provide either hosts_json or group_tag.')

    if mode == "parallel":
        if not command:
            raise ToolError('mode="parallel" requires `command`.')
        err = _gate_many(hosts, command)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        results = await ssh_parallel_exec(hosts, command, timeout=timeout)
        return json.dumps(results, indent=2)
    if mode == "rolling":
        if not command:
            raise ToolError('mode="rolling" requires `command`.')
        err = _gate_many(hosts, command)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        audit_log(",".join(hosts), command, "rolling", operation="multi_rolling")
        results = await ssh_rolling_exec(hosts, command, delay_s=delay_s,
                                          stop_on_error=stop_on_error,
                                          timeout=timeout)
        return json.dumps(results, indent=2)
    if mode == "broadcast":
        if not commands_json:
            raise ToolError('mode="broadcast" requires `commands_json`.')
        try:
            commands = json.loads(commands_json)
        except Exception as e:
            raise ToolError(f"Invalid commands_json: {e}")
        if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
            raise ToolError('Invalid commands_json: must be a JSON array of strings.')
        for cmd in commands:
            err = _gate_many(hosts, cmd)
            if err:
                raise ToolError(f"BLOCKED on command {cmd[:60]!r}: {err}")
        audit_log(",".join(hosts), f"{len(commands)} cmds",
                  "broadcast", operation="multi_broadcast")
        results = await ssh_broadcast(hosts, commands, timeout=timeout)
        return json.dumps(results, indent=2)
    raise ToolError(f'unknown mode {mode!r}. Valid: parallel, rolling, broadcast.')


@mcp.tool()
async def portal_playbook(playbook_json: str, host: str = "",
                           group_tag: str = "") -> str:
    """Execute an infrastructure playbook on a host or group.

    Specify exactly one target:
    - host="web01"        : run on a single host.
    - group_tag="prod"    : run on all hosts with this tag.

    Playbook JSON format:
        {
          "name": "deploy_docker_stack",
          "on_error": "stop",
          "steps": ["apt update", "apt install docker.io -y",
                    "systemctl enable --now docker"]
        }

    Each `steps` entry is gate-checked against the security policy before
    execution begins.
    """
    if bool(host) == bool(group_tag):
        raise ToolError('specify exactly one of `host` or `group_tag`.')
    try:
        playbook = json.loads(playbook_json)
    except Exception as e:
        raise ToolError(f"Invalid playbook_json: {e}")
    if host:
        err = _gate_playbook([host], playbook)
        if err:
            raise ToolError(f"BLOCKED: {err}")
        result = await run_playbook(host, playbook)
        return json.dumps(result, indent=2)
    hosts = _resolve_group(group_tag)
    if not hosts:
        return json.dumps([{"error": f"No hosts with tag {group_tag!r}"}], indent=2)
    err = _gate_playbook(hosts, playbook)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    audit_log(",".join(hosts),
              f"playbook:{playbook.get('name','unnamed')}",
              f"group:{group_tag}", operation="playbook_group")
    results = await run_playbook_on_group(group_tag, playbook)
    return json.dumps(results, indent=2)


# ═══════════════════════════════════════════════════════════════════
# 5. HEALTH CHECK  (portal_ping)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def portal_ping(hosts_json: str = "") -> str:
    """Test SSH connectivity to one or more hosts.

    - hosts_json="" or "[]" (default): ping all registered hosts in parallel.
    - hosts_json='["web01"]': ping a specific host or set.

    Returns: {"online": N, "total": M, "hosts": [{host, reachable, latency_s, ...}, ...]}
    """
    mgr = get_manager()
    try:
        hosts = json.loads(hosts_json) if hosts_json.strip() not in ("", "[]") \
                else list(mgr._registry.keys())
    except Exception:
        hosts = list(mgr._registry.keys())
    if not hosts:
        return json.dumps({"online": 0, "total": 0, "hosts": [],
                           "message": "No hosts to ping (registry empty "
                                      "and no hosts_json provided)."},
                          indent=2)

    async def _ping(h: str) -> dict:
        err = _gate(h)
        if err:
            return {"host": h, "reachable": False, "error": f"BLOCKED: {err}"}
        t0 = time.time()
        try:
            res = await asyncio.wait_for(
                ssh_exec(h, "echo pong", timeout=10), timeout=12)
            return {"host": h,
                    "reachable": res.get("stdout", "").strip() == "pong",
                    "latency_s": round(time.time() - t0, 3),
                    "exit_code": res.get("exit_code")}
        except Exception as e:
            return {"host": h, "reachable": False, "error": str(e)}

    results = await asyncio.gather(*[_ping(h) for h in hosts])
    online = sum(1 for r in results if r.get("reachable"))
    return json.dumps({"online": online, "total": len(hosts), "hosts": results},
                      indent=2)


# ═══════════════════════════════════════════════════════════════════
# 6. POLICY DRY-RUN  (portal_check)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def portal_check(host: str, command: str = "") -> str:
    """Dry-run a host (and optional command) through the security policy.

    - command="" : check whether the host is accessible at all.
        Example: portal_check(host="web01")
    - command="rm -rf /" : check whether this command would be allowed on this host.
        Example: portal_check(host="web01", command="systemctl stop nginx")

    Returns "ALLOWED" or "BLOCKED: <reason>". Does not execute anything.
    Use this before risky multi-host operations to surface policy errors early.

    ⚠️  Default policy is PERMISSIVE — out of the box `policies.yaml` has an
    empty host_allowlist (any host), empty command_blocklist / allowlist
    (any command), and only a per-host rate limit. So `portal_check` will
    return ALLOWED for almost anything until you populate
    `$XDG_CONFIG_HOME/portal-mcp-server/policies.yaml` (default
    `~/.config/portal-mcp-server/policies.yaml`)
    with explicit rules. Use `portal_audit(view="policy")` to inspect what
    the server actually has loaded. ALLOWED therefore means "no rule
    currently blocks this", not "this is safe to run".
    """
    err = _gate(host, command)
    if err:
        return f"BLOCKED: {err}"
    if not command:
        reg = get_manager()._registry
        if host not in reg:
            return f"ALLOWED by policy but host {host!r} is not registered."
    return f"ALLOWED: {host!r} passes all security checks."


# ═══════════════════════════════════════════════════════════════════
# 7. AUDIT & STATE  (portal_audit)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def portal_audit(view: str = "snapshot", limit: int = 50,
                  host_filter: str = "") -> str:
    """Inspect MCP server internal state and audit log.

    ## Views
    - view="snapshot" (default): full state — registered hosts, connection pool,
        active sessions, active tunnels, audit aggregate stats, security policy summary.
    - view="history": last `limit` audit log entries (default 50). Optional `host_filter`.
        Example: portal_audit(view="history", limit=20, host_filter="web01")
    - view="stats": aggregate audit stats (counts by operation, error rate, etc.).
    - view="policy": current security policy details (host allowlist, command
        blocklist, allowlist, rate limit).

    Read-only. Used to introspect what the MCP server has been doing and what
    limits are in place.
    """
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
        }, indent=2)
    if view == "snapshot":
        mgr = get_manager()
        snap = {
            "registered_hosts": len(mgr._registry),
            "hosts": mgr.list_hosts(),
            "connection_pool": mgr.pool_status(),
            "active_tunnels": get_tunnel_manager().list_tunnels(),
            "audit_stats": get_audit_stats(),
            "security": {
                "host_allowlist_count": len(get_policy().host_allowlist),
                "command_blocklist_count": len(get_policy().command_blocklist),
                "rate_limit_rps": get_policy().rate_limit_rps,
            },
        }
        return json.dumps(snap, indent=2)
    raise ToolError(f'unknown view {view!r}. Valid: snapshot, history, stats, policy.')


# ═══════════════════════════════════════════════════════════════════
# 12. PORTAL CORE — agent-feels-local tools
# ═══════════════════════════════════════════════════════════════════
# These wrap server.remote_* modules. Designed to be the *primary* tools an
# AI agent uses when working on a remote host. They share one SSH connection
# per host (via the connection pool) and provide:
#   - portal_read / portal_patch :  hash-protected concurrent-safe edits
#   - portal_grep / portal_glob  :  remote ripgrep / find with structured output
#   - portal_bash                :  single persistent bash session (cwd + env survive)

@mcp.tool()
async def portal_read(host: str, path: str, start: int = 1,
                      end: int | None = None, encoding: str = "utf-8") -> str:
    """Read a file (or a 1-based line range) from a remote host with SHA-256 hashes.

    Returns JSON with: content, file_hash, range_hash, start, end, total_lines.
    The file_hash MUST be supplied to portal_patch; if the file changed in
    between, portal_patch will refuse to overwrite.

    Args:
        host: SSH host alias (from ~/.ssh/config) or registered host name
        path: Absolute remote path
        start: 1-based starting line (default 1)
        end: 1-based ending line, inclusive (default: end of file)
        encoding: Text encoding (default utf-8)
    """
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    res = await _re_read(host, path, start=start, end=end, encoding=encoding)
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_patch(host: str, path: str, file_hash: str,
                       patches_json: str, encoding: str = "utf-8",
                       auto_newline: bool = False) -> str:
    """Apply patches to a remote file with hash-based conflict detection.

    Workflow:
      1. Call portal_read to obtain content + file_hash + range_hash for each region.
      2. Call portal_patch with the SAME file_hash and per-patch range_hash.
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
    """
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    try:
        patches = json.loads(patches_json)
    except Exception as e:
        raise ToolError(f"invalid patches_json: {e}")
    res = await _re_patch(host, path, file_hash=file_hash, patches=patches,
                           encoding=encoding, auto_newline=auto_newline)
    audit_log(host, f"patch:{path}",
              res.get("result", "?") if isinstance(res, dict) else "?",
              operation="file_patch")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_cleanup_tmps(host: str, directory: str,
                              max_age_s: int = 3600) -> str:
    """Remove orphan tmp files left by interrupted portal_patch writes.

    portal_patch writes through ``<path>.mcp_tmp.<12hex>`` and renames into
    place atomically. If the SSH connection dies after the tmp file is
    created but before the rename, the tmp file is left on disk. This tool
    finds and removes those orphans.

    Args:
        host:      registered host alias
        directory: absolute remote directory to scan (non-recursive)
        max_age_s: only remove files older than this many seconds (default
                   3600). Pass 0 to remove every match unconditionally —
                   useful in tests, dangerous in production.

    Returns JSON: {"scanned": int, "removed": [str], "skipped": [[str, str]]}.
    """
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    res = await _re_cleanup_tmps(host, directory, max_age_s=max_age_s)
    removed = res.get("removed", []) if isinstance(res, dict) else []
    audit_log(host, f"cleanup_tmps:{directory}",
              f"removed:{len(removed)}", operation="file_cleanup")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_grep(host: str, path: str, pattern: str,
                      glob: str = "", file_type: str = "",
                      ignore_case: bool = False, max_count: int = 0) -> str:
    """Search for a regex pattern under a path on a remote host.

    Uses ripgrep when available (fast, structured), else falls back to grep -rn.

    Args:
        host: SSH host alias
        path: Absolute remote path to search under
        pattern: Pattern to find (regex by default)
        glob: Optional glob filter (e.g. "*.py")
        file_type: Optional rg --type value (e.g. "py", "rust")
        ignore_case: Case-insensitive match
        max_count: Stop after N matches (0 = no limit)
    """
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    res = await _re_grep(
        host, path, pattern,
        glob=glob or None,
        type=file_type or None,
        ignore_case=ignore_case,
        max_count=max_count if max_count > 0 else None,
    )
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_glob(host: str, pattern: str, path: str = ".") -> str:
    """List files matching a glob pattern on a remote host.

    Args:
        host: SSH host alias
        pattern: Glob (e.g. "**/*.py", "*.toml")
        path: Directory to search under (default: cwd)
    """
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    res = await _re_glob(host, pattern, path=path)
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_bash(host: str, command: str, timeout: float = 3600.0,
                      use_sudo: bool = False,
                      secrets: "list[str] | None" = None) -> str:
    """Run a command in the persistent bash session for <host>.

    Behavior:
      - First call for a host auto-creates a `bash -i` session via SSH.
      - Subsequent calls reuse the same shell, so cwd and exported env vars survive.
        Example: `cd /tmp` in one call, `pwd` in the next prints `/tmp`.
      - Output is sentinel-bounded; PTY echo is disabled so output is clean.

    ⚠️ Safety: by default, write operations should target /tmp/ on the remote
       unless the user has explicitly approved a different path. This tool does
       NOT enforce that — it's a convention for the agent's skill prompt.

    use_sudo: run the command via `sudo -S`, feeding a password obtained
        out-of-band (NEVER passed by the agent). The password comes from the
        per-user credential agent populated by `portal sudo set <host>`, or
        from the host's `sudo_password_command` in hosts.yaml. Because sudo
        reads stdin, this runs as a ONE-SHOT command (not the persistent
        session): cwd/env from prior portal_bash calls do not apply.

    secrets: a list of named secrets (e.g. ["github_token"]) to inject as
        environment variables for THIS command only. You pass the NAME, never
        the value: the server resolves each from secrets.yaml or the
        `portal secret set` cache and exports it as the uppercased env var (github_token
        → $GITHUB_TOKEN). Reference it in `command` as `$GITHUB_TOKEN`. The value
        is fed over SSH stdin (never on argv/audit) and is redacted to *** in the
        returned output. Like use_sudo this is a ONE-SHOT command (cwd/env from
        prior calls do not apply). Cannot be combined with use_sudo.
    """
    err = _gate(host, command)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    if secrets:
        if use_sudo:
            raise ToolError("secrets and use_sudo cannot be combined in one call.")
        env, values, serr = await _resolve_secrets(secrets)
        if serr:
            raise ToolError(serr)
        from . import secrets_store
        res = await _re_exec_env(host, command, env, timeout=timeout)
        res["output"] = secrets_store.redact(res.get("output", ""), values)
        audit_log(host, command + f"  [secrets: {','.join(secrets)}]",
                  res.get("exit_code", "?"), operation="remote_exec_secrets")
        return json.dumps(res, indent=2, ensure_ascii=False)
    if use_sudo:
        from .sudo_creds import resolve_sudo_password
        password = await resolve_sudo_password(host)
        if password is None:
            raise ToolError(
                "No sudo password available for this host; the command "
                "was NOT run. Ask the user to provide it out-of-band: "
                "prefer an interactive input/choice tool (e.g. ask_user) "
                "to request that they run "
                f"`portal sudo set {host}` in a separate "
                "terminal and confirm when done, then retry this call. If "
                "you have no such tool, tell the user what to run and end "
                "your turn to wait for their next message. (Alternatively "
                "an operator can set `sudo_password_command` for the host "
                "in hosts.yaml.) Never ask the user to paste the password "
                "into this conversation."
            )
        res = await _re_sudo_exec(host, command, password, timeout=timeout)
        audit_log(host, "sudo: " + command, res.get("exit_code", "?"),
                  operation="remote_sudo")
        return json.dumps(res, indent=2, ensure_ascii=False)
    res = await _re_bash(host, command, timeout=timeout)
    audit_log(host, command, "ok", operation="remote_bash")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_local_exec(command: str, secrets: "list[str] | None" = None,
                            timeout: float = 600.0) -> str:
    """Run a ONE-SHOT command on the MCP server host (LOCAL), optionally with
    named secrets injected as environment variables.

    Unlike every other portal_* tool (which runs over SSH on a remote host),
    this executes locally — a larger threat surface — so it is DISABLED unless
    the operator sets `PORTAL_ALLOW_LOCAL_EXEC=1` for the server process.

    secrets: a list of named secrets (e.g. ["github_token"]). You pass the NAME,
        never the value: the server resolves each from secrets.yaml or the
        `portal secret set` cache and exports it as the uppercased env var (github_token
        → $GITHUB_TOKEN) into the child process environment (never on argv/audit).
        Reference it in `command` as `$GITHUB_TOKEN`. Any echo of the value in the
        output is redacted to ***.

    Use this to run a local command/script that needs an API token without the
    token ever entering this conversation or being sent to the model backend.
    """
    if os.environ.get("PORTAL_ALLOW_LOCAL_EXEC", "").lower() not in (
        "1", "true", "yes", "on",
    ):
        raise ToolError("portal_local_exec is disabled. The operator must set "
                        "PORTAL_ALLOW_LOCAL_EXEC=1 for the MCP server process to "
                        "allow running commands on the local host.")
    err = _gate("<local>", command)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    env: dict[str, str] = {}
    values: list[str] = []
    if secrets:
        env, values, serr = await _resolve_secrets(secrets)
        if serr:
            raise ToolError(serr)
    from . import secrets_store
    res = await _local_exec_env(command, env, timeout=timeout)
    res["output"] = secrets_store.redact(res.get("output", ""), values)
    suffix = f"  [secrets: {','.join(secrets)}]" if secrets else ""
    audit_log("<local>", command + suffix, res.get("exit_code", "?"),
              operation="local_exec")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_bash_close(host: str) -> str:
    """Close the cached default bash session for <host> (next call will reopen)."""
    err = _gate(host)
    if err:
        raise ToolError(f"BLOCKED: {err}")
    return await _re_bash_close(host)


@mcp.tool()
async def portal_bash_status() -> str:
    """List host -> session_id mappings for cached default bash sessions."""
    return json.dumps(_re_bash_status(), indent=2)


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
#   portal ssh    set HOST     [--ttl N]  prompt (no echo) and cache SSH login pw
#   portal ssh    confirm HOST [--ttl N]  prompt twice, compare, then cache
#   portal ssh    show HOST               fingerprint + TTL (NO plaintext)
#   portal ssh    clear HOST              drop one entry
#   portal ssh    list                    list keys + fingerprint + TTL
#
#   portal sudo   {set,confirm,show,clear,list}   same shape, sudo password
#   portal secret {set,confirm,show,clear,list}   same shape, named secret
#
# Design principle — *plaintext never leaves the agent's memory*. There is no
# `show plaintext` command, no `dump` command. Every human-facing verb
# returns either a fingerprint (sha256[:16]) + TTL or the plaintext is fed
# directly to a same-uid client process (the SSH connect loop, sudo stdin,
# $env injection). Same posture as ssh-agent / gpg-agent / vault agent /
# polkit-agent: any echo to a TTY is one screenshot / scrollback / asciinema
# / OBS overlay away from a leak, so the agent simply refuses to do it.
# Sanity-check a stored credential with `confirm` (re-type and compare) or
# `show` (compare fingerprints); export with a `password_command` from your
# password manager instead of asking the agent to echo.

_CREDENTIAL_SUBCOMMANDS = ("agent", "ssh", "sudo", "secret")
_CREDENTIAL_KINDS = ("ssh", "sudo", "secret")


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


def _kind_key_noun(kind: str) -> str:
    """User-facing label for the credential kind's key argument."""
    return {"ssh": "host", "sudo": "host", "secret": "name"}[kind]


def _kind_prompt(kind: str, key: str) -> str:
    """getpass prompt for a kind/key."""
    return {
        "ssh": f"SSH password for host '{key}': ",
        "sudo": f"sudo password for host '{key}': ",
        "secret": f"value for secret '{key}': ",
    }[kind]


def _kind_label(kind: str) -> str:
    """Human label for the credential kind (singular)."""
    return {"ssh": "SSH password",
            "sudo": "sudo password",
            "secret": "secret"}[kind]


def _format_ttl(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


# ── portal agent ─────────────────────────────────────────────────────

def _agent_install_cli(args) -> int:
    from .credential_agent import install_user_units, SOCKET_UNIT
    try:
        res = install_user_units(socket_path=args.socket, enable_now=args.now)
    except Exception as e:
        print(f"Failed to install credential agent units: {e}", file=sys.stderr)
        return 1
    print("Installed portal credential agent user units:")
    print(f"  socket unit:   {res['socket_unit']}")
    print(f"  service unit:  {res['service_unit']}")
    print(f"  config:        {res['config_path']}")
    print(f"  recorded path: {res['socket_path']}")
    if not args.now:
        print(f"Enable it with: systemctl --user enable --now {SOCKET_UNIT}")
    return 0


def _agent_uninstall_cli(args) -> int:
    from .credential_agent import uninstall_user_units
    res = uninstall_user_units(
        stop_now=not args.no_stop,
        remove_config=not args.keep_config,
    )
    print("Uninstalled portal credential agent user units.")
    if res["removed"]:
        print("Removed:")
        for path in res["removed"]:
            print(f"  {path}")
    if res["errors"]:
        print("Warnings:")
        for err in res["errors"]:
            print(f"  systemctl --user: {err}")
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
        help="Manage the per-user credential agent (systemd --user only).",
        description="Install/run/inspect the per-user credential agent. The "
                    "agent is a long-lived process that holds TTL-cached "
                    "ssh/sudo/secret values in memory and is reached over a "
                    "Unix socket owned by your uid. Install via systemd "
                    "--user; the agent is socket-activated on first request.",
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
        help="Clear ALL cached credentials (ssh, sudo, secret).")
    p_clear.set_defaults(func=_agent_clear_cli)


# ── portal {ssh,sudo,secret} ─────────────────────────────────────────

def _kind_set_cli(args) -> int:
    import getpass
    from . import credential_agent
    from .paths import credential_agent_socket_path
    _agent_path_or_exit(credential_agent_socket_path)
    prompt = _kind_prompt(args.kind, args.key)
    value = getpass.getpass(prompt)
    if not value:
        print("Empty value, aborted.", file=sys.stderr)
        return 1
    try:
        resp = credential_agent.store(args.kind, args.key, value, ttl=args.ttl)
    except (OSError, RuntimeError) as e:
        print(f"Failed to reach credential agent: {e}", file=sys.stderr)
        return 1
    if resp.get("status") != "ok":
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    print(f"{_kind_label(args.kind)} cached for "
          f"'{args.key}' (expires in {_format_ttl(args.ttl)}).")
    return 0


def _kind_confirm_cli(args) -> int:
    import getpass
    from . import credential_agent
    from .paths import credential_agent_socket_path
    _agent_path_or_exit(credential_agent_socket_path)
    prompt = _kind_prompt(args.kind, args.key)
    first = getpass.getpass(prompt)
    if not first:
        print("Empty value, aborted.", file=sys.stderr)
        return 1
    again = getpass.getpass(f"confirm: {prompt}")
    if first != again:
        print("Values differ; nothing cached.", file=sys.stderr)
        return 1
    try:
        resp = credential_agent.store(args.kind, args.key, first, ttl=args.ttl)
    except (OSError, RuntimeError) as e:
        print(f"Failed to reach credential agent: {e}", file=sys.stderr)
        return 1
    if resp.get("status") != "ok":
        print(f"Error: {resp.get('error', 'unknown')}", file=sys.stderr)
        return 1
    print(f"{_kind_label(args.kind)} cached for "
          f"'{args.key}' (entries matched, expires in {_format_ttl(args.ttl)}).")
    return 0


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
        "ssh": "Manage cached SSH login passwords (side-channel for "
               "`auth: password` hosts and the key-auth fallback). "
               "Cached in the per-user credential agent's memory only.",
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
                    "the same-uid SSH/sudo/$env consumer.",
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
        help=f"Prompt twice (no echo) and cache only if the two entries match.")
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


def _credential_main(argv: list[str]) -> int:
    """Subcommand dispatcher for `portal agent / ssh / sudo / secret`.

    argv[0] is the subcommand name (one of _CREDENTIAL_SUBCOMMANDS).
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog="portal",
        description="Portal credential agent CLI — manage cached SSH / sudo / "
                    "secret values held by the per-user agent process.",
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
        args.key = getattr(args, noun, None)
    return args.func(args)


def main() -> None:
    """CLI entrypoint registered as `portal-mcp-server` / `portal`.

    Dispatch:
      * `portal <cred-subcmd> ...` — credential agent CLI
        (cred-subcmd ∈ {agent, ssh, sudo, secret})
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
            if auth != f"Bearer {PORTAL_AUTH_TOKEN}":
                return Response("Unauthorized", status_code=401)
            return await call_next(request)

    parser = argparse.ArgumentParser(
        prog="portal-mcp-server",
        description="portal-mcp-server — Agent-feels-local SSH orchestration MCP server.\n\n"
                    "Without any subcommand this starts the MCP server. The credential\n"
                    "agent CLI lives under these subcommands (use `<subcmd> --help`):\n"
                    "  portal agent  install / uninstall / run / status / clear\n"
                    "  portal ssh    set / confirm / show / clear / list  <host>\n"
                    "  portal sudo   set / confirm / show / clear / list  <host>\n"
                    "  portal secret set / confirm / show / clear / list  <name>",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        _ver = _pkg_version("portal-mcp-server")
    except PackageNotFoundError:
        _ver = "unknown"
    parser.add_argument("--version", action="version", version=f"%(prog)s {_ver}")
    parser.add_argument("--transport", choices=["stdio", "streamable_http"], default="stdio",
                        help="MCP transport (default: stdio)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    args = parser.parse_args()

    logger.info(f"portal-mcp-server starting | transport={args.transport}")

    if args.transport == "streamable_http":
        import uvicorn
        app = mcp.streamable_http_app()
        if PORTAL_AUTH_TOKEN:
            app.add_middleware(TokenAuthMiddleware)
            logger.info("Bearer token auth enabled")
        logger.info(f"HTTP transport on {args.host}:{args.port}/mcp")
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        logger.info("stdio transport active")
        mcp.run()


if __name__ == "__main__":
    main()
