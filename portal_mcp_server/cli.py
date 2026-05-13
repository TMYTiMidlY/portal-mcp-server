"""
portal-mcp-server — Agent-feels-local SSH orchestration MCP server.
Exposes 18 portal_* tools covering: read/patch/grep/glob/bash core +
host/transfer/tunnel/multi_exec/playbook/ping/audit/check.
"""
import asyncio
import json
import logging
import os
import sys
import time

from mcp.server.fastmcp import FastMCP
from .paths import default_log_dir
from .connection_manager import get_manager
from .shell_engine import ssh_exec
from .file_ops import ssh_upload_file, ssh_download_file, ssh_sync_directory
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
)

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
    """
    mgr = get_manager()
    if action == "list":
        hosts = mgr.list_hosts()
        return json.dumps(hosts, indent=2) if hosts else "No hosts registered."
    if action == "register":
        if not name or not host:
            return 'Error: action="register" requires both `name` and `host`.'
        # Gate against the *target* (the actual host/IP that traffic will
        # reach), not the alias the agent picked. Otherwise an agent can
        # register an arbitrary alias pointing at a non-allowlisted host
        # and then operate on it freely — host_allowlist would only ever
        # see the alias.
        err = _gate(host)
        if err:
            return f"BLOCKED: {err}"
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        result = mgr.register_host(name=name, host=host, user=user, port=port,
                                    key=key_path or None, tags=tag_list)
        audit_log(name, f"register:{user}@{host}:{port}", "ok",
                  operation="host_register")
        return result
    if action == "remove":
        if not name:
            return 'Error: action="remove" requires `name`.'
        # Gate the alias being removed — same surface as any other
        # state-changing op against that alias.
        err = _gate(name)
        if err:
            return f"BLOCKED: {err}"
        result = mgr.remove_host(name)
        audit_log(name, "remove_host", "ok", operation="host_remove")
        return result
    return f'Error: unknown action {action!r}. Valid: list, register, remove.'


# ═══════════════════════════════════════════════════════════════════
# 2. FILE TRANSFER  (portal_transfer — SFTP-based, binary safe)
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def portal_transfer(direction: str, host: str,
                           local_path: str, remote_path: str) -> str:
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
    - direction="sync": recursively sync local_path directory → remote_path directory.
        Example: portal_transfer(direction="sync", host="web01",
                                  local_path="./build/", remote_path="/srv/www/")

    For text-only edits prefer portal_patch (hash-protected). Use portal_transfer
    when SFTP semantics are needed: binary files, large files, whole directory trees.
    """
    err = _gate(host)
    if err:
        return f"BLOCKED: {err}"
    if direction == "upload":
        result = await ssh_upload_file(host, local_path, remote_path)
        audit_log(host, f"upload:{local_path}→{remote_path}", "ok",
                  operation="file_upload")
        return result
    if direction == "download":
        result = await ssh_download_file(host, remote_path, local_path)
        audit_log(host, f"download:{remote_path}", "ok",
                  operation="file_download")
        return result
    if direction == "sync":
        result = await ssh_sync_directory(host, local_path, remote_path)
        audit_log(host, f"sync:{local_path}→{remote_path}", "ok",
                  operation="file_sync")
        return result
    return f'Error: unknown direction {direction!r}. Valid: upload, download, sync.'


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
        return f"BLOCKED: {err}"
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
        return f'Error: unknown mode {mode!r}. Valid: local, reverse, socks.'
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
        return f"Tunnel '{tunnel_id}' not found"
    err = _gate(host)
    if err:
        return f"BLOCKED: {err}"
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
                             timeout: int = 60, delay_s: float = 2.0,
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
            return f"Invalid hosts_json: {e}"
        if not hosts:
            return 'Error: must provide either hosts_json or group_tag.'

    if mode == "parallel":
        if not command:
            return 'Error: mode="parallel" requires `command`.'
        err = _gate_many(hosts, command)
        if err:
            return f"BLOCKED: {err}"
        results = await ssh_parallel_exec(hosts, command, timeout=timeout)
        return json.dumps(results, indent=2)
    if mode == "rolling":
        if not command:
            return 'Error: mode="rolling" requires `command`.'
        err = _gate_many(hosts, command)
        if err:
            return f"BLOCKED: {err}"
        audit_log(",".join(hosts), command, "rolling", operation="multi_rolling")
        results = await ssh_rolling_exec(hosts, command, delay_s=delay_s,
                                          stop_on_error=stop_on_error,
                                          timeout=timeout)
        return json.dumps(results, indent=2)
    if mode == "broadcast":
        if not commands_json:
            return 'Error: mode="broadcast" requires `commands_json`.'
        try:
            commands = json.loads(commands_json)
        except Exception as e:
            return f"Invalid commands_json: {e}"
        if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
            return 'Invalid commands_json: must be a JSON array of strings.'
        for cmd in commands:
            err = _gate_many(hosts, cmd)
            if err:
                return f"BLOCKED on command {cmd[:60]!r}: {err}"
        audit_log(",".join(hosts), f"{len(commands)} cmds",
                  "broadcast", operation="multi_broadcast")
        results = await ssh_broadcast(hosts, commands, timeout=timeout)
        return json.dumps(results, indent=2)
    return f'Error: unknown mode {mode!r}. Valid: parallel, rolling, broadcast.'


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
        return 'Error: specify exactly one of `host` or `group_tag`.'
    try:
        playbook = json.loads(playbook_json)
    except Exception as e:
        return f"Invalid playbook_json: {e}"
    if host:
        err = _gate_playbook([host], playbook)
        if err:
            return f"BLOCKED: {err}"
        result = await run_playbook(host, playbook)
        return json.dumps(result, indent=2)
    hosts = _resolve_group(group_tag)
    if not hosts:
        return json.dumps([{"error": f"No hosts with tag {group_tag!r}"}], indent=2)
    err = _gate_playbook(hosts, playbook)
    if err:
        return f"BLOCKED: {err}"
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
        return "No hosts to ping (registry empty and no hosts_json provided)."

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
    `$XDG_CONFIG_HOME/portal-mcp-server/policies.yaml` (or
    `./config/policies.yaml`)
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
        blocklist, allowlist, rate limits, sandbox users).

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
            "max_concurrent": pol.max_concurrent,
            "connection_timeout_s": pol.connection_timeout,
            "sandbox_users": pol.sandbox_users,
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
    return f'Error: unknown view {view!r}. Valid: snapshot, history, stats, policy.'


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
        return f"BLOCKED: {err}"
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
        return f"BLOCKED: {err}"
    try:
        patches = json.loads(patches_json)
    except Exception as e:
        return json.dumps({"result": "error", "reason": f"invalid patches_json: {e}"})
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
        return f"BLOCKED: {err}"
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
        return f"BLOCKED: {err}"
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
        return f"BLOCKED: {err}"
    res = await _re_glob(host, pattern, path=path)
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_bash(host: str, command: str, timeout: float = 60.0) -> str:
    """Run a command in the persistent bash session for <host>.

    Behavior:
      - First call for a host auto-creates a `bash -i` session via SSH.
      - Subsequent calls reuse the same shell, so cwd and exported env vars survive.
        Example: `cd /tmp` in one call, `pwd` in the next prints `/tmp`.
      - Output is sentinel-bounded; PTY echo is disabled so output is clean.

    ⚠️ Safety: by default, write operations should target /tmp/ on the remote
       unless the user has explicitly approved a different path. This tool does
       NOT enforce that — it's a convention for the agent's skill prompt.
    """
    err = _gate(host, command)
    if err:
        return f"BLOCKED: {err}"
    res = await _re_bash(host, command, timeout=timeout)
    audit_log(host, command, "ok", operation="remote_bash")
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def portal_bash_close(host: str) -> str:
    """Close the cached default bash session for <host> (next call will reopen)."""
    err = _gate(host)
    if err:
        return f"BLOCKED: {err}"
    return await _re_bash_close(host)


@mcp.tool()
async def portal_bash_status() -> str:
    """List host -> session_id mappings for cached default bash sessions."""
    return json.dumps(_re_bash_status(), indent=2)


# ═══════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    """CLI entrypoint registered as `portal-mcp-server` (and the legacy
    `portal-mcp-server` alias for backward compat with existing .mcp.json)."""
    import argparse
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

    class TokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if not MCP_AUTH_TOKEN:
                return await call_next(request)
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {MCP_AUTH_TOKEN}":
                return Response("Unauthorized", status_code=401)
            return await call_next(request)

    parser = argparse.ArgumentParser(
        prog="portal-mcp-server",
        description="portal-mcp-server — Agent-feels-local SSH orchestration MCP server",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable_http"], default="stdio",
                        help="MCP transport (default: stdio)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    args = parser.parse_args()

    logger.info(f"portal-mcp-server starting | transport={args.transport}")

    if args.transport == "streamable_http":
        import uvicorn
        app = mcp.streamable_http_app()
        if MCP_AUTH_TOKEN:
            app.add_middleware(TokenAuthMiddleware)
            logger.info("Bearer token auth enabled")
        logger.info(f"HTTP transport on {args.host}:{args.port}/mcp")
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        logger.info("stdio transport active")
        mcp.run()


if __name__ == "__main__":
    main()
