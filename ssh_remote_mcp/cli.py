"""
ssh-remote-mcp — Production-grade SSH orchestration layer for AI agents.
Exposes 57+ MCP tools covering: exec, sessions, files, processes,
system inspection, multi-host orchestration, tunnels, and security.
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
from .session_manager import get_session_manager
from .shell_engine import ssh_exec, ssh_exec_batch, ssh_exec_script, ssh_exec_with_env
from .file_ops import (ssh_upload_file, ssh_download_file, ssh_list_directory,
                              ssh_read_file, ssh_write_file, ssh_delete_file, ssh_sync_directory)
from .process_manager import (ssh_process_list, ssh_kill_process, ssh_start_process,
                                    ssh_background_process, ssh_monitor_process)
from .system_inspector import (ssh_system_info, ssh_disk_usage, ssh_memory_usage,
                                     ssh_network_status, ssh_service_status, ssh_logs,
                                     ssh_docker_status)
from .network_tools import get_tunnel_manager
from .orchestrator import (ssh_parallel_exec, ssh_rolling_exec, ssh_exec_on_group,
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
from .safety import quote_shell, validate_tmux_name

_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
try:
    _log_dir = default_log_dir()
    _log_dir.mkdir(parents=True, exist_ok=True)
    _log_handlers.append(logging.FileHandler(_log_dir / "server.log", encoding="utf-8"))
except Exception as _log_err:
    print(f"[ssh-remote-mcp] WARNING: could not open log file: {_log_err}", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("ssh_mcp")

mcp = FastMCP("ssh-remote-mcp")

# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def _gate(host: str, command: str = "") -> str | None:
    """Returns error string if blocked, None if allowed."""
    return get_policy().enforce(host, command)


def _gate_many(hosts: list[str], command: str = "") -> str | None:
    """Multi-host policy gate.

    For each host: check_host + check_rate_limit. The command (if given) is
    checked once against the blocklist/allowlist (it's the same string for
    all hosts). Returns the first error found, or None if all checks pass.

    Used by multi-host orchestration tools (ssh_rolling, ssh_group_exec,
    ssh_broadcast_batch, ssh_playbook_on_group) so they cannot bypass the
    policy gate that ssh_run / ssh_exec_with_env etc. enforce.
    """
    pol = get_policy()
    if command:
        err = pol.check_command(command)
        if err:
            return err
    for h in hosts:
        err = pol.check_host(h)
        if err:
            return f"{h}: {err}"
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
    playbook even runs.
    """
    pol = get_policy()
    steps = playbook.get("steps", []) or []
    for step in steps:
        if not isinstance(step, str):
            continue
        err = pol.check_command(step)
        if err:
            return f"step {step[:60]!r}: {err}"
    for h in hosts:
        err = pol.check_host(h)
        if err:
            return f"{h}: {err}"
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
# 1. CONNECTION / HOST REGISTRY TOOLS
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def ssh_register_host(name: str, host: str, user: str = "root", port: int = 22,
                       key_path: str = "",
                       tags: str = "") -> str:
    """Register a new SSH host in the registry.

    Args:
        name: Unique name for this host (e.g. 'web01')
        host: IP address or hostname
        user: SSH username (default: root)
        port: SSH port (default: 22)
        key_path: Path to private key file (e.g. ~/.ssh/id_ed25519). If
                  empty, asyncssh falls back to default keys (~/.ssh/id_*)
                  or the SSH agent. Password authentication is not supported.
        tags: Comma-separated group tags (e.g. 'web,production')
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    return get_manager().register_host(
        name=name, host=host, user=user, port=port,
        key=key_path or None, tags=tag_list
    )


@mcp.tool()
def ssh_list_hosts() -> str:
    """List all registered SSH hosts with their connection details."""
    hosts = get_manager().list_hosts()
    return json.dumps(hosts, indent=2) if hosts else "No hosts registered."


@mcp.tool()
def ssh_remove_host(name: str) -> str:
    """Remove a host from the registry.

    Args:
        name: Host name to remove
    """
    return get_manager().remove_host(name)


@mcp.tool()
def ssh_connection_status() -> str:
    """Show the current state of all pooled SSH connections."""
    status = get_manager().pool_status()
    return json.dumps(status, indent=2) if status else "No active connections."


# ═══════════════════════════════════════════════════════════════════
# 2. PERSISTENT SHELL SESSIONS
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_create_session(host_name: str, env_json: str = "{}") -> str:
    """Create a persistent interactive shell session on a remote host.

    Args:
        host_name: Name of the registered host
        env_json: JSON object of environment variables to inject (e.g. '{"DEBUG":"1"}')

    Returns:
        session_id to use with ssh_session_exec
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    sm = get_session_manager()
    sid = await sm.create_session(host_name, env=_parse_env(env_json))
    audit_log(host_name, "create_session", sid, operation="session")
    return f"Session created: {sid}"


@mcp.tool()
async def ssh_session_exec(session_id: str, command: str, timeout: float = 30.0) -> str:
    """Execute a command inside a persistent shell session (maintains state/CWD).

    Args:
        session_id: Session ID from ssh_create_session
        command: Shell command to run (e.g. 'cd /opt && ls')
        timeout: Max seconds to wait for output
    """
    sm = get_session_manager()
    sess = sm._sessions.get(session_id)
    if sess is None:
        return f"Error: session '{session_id}' not found"
    err = _gate(sess.host_name, command)
    if err:
        return f"BLOCKED: {err}"
    try:
        return await sm.execute_in_session(session_id, command, timeout=timeout)
    except KeyError as e:
        return f"Error: {e}"


@mcp.tool()
def ssh_session_read_buffer(session_id: str, lines: int = 100) -> str:
    """Read recent output from a persistent session's buffer.

    Args:
        session_id: Session ID
        lines: Number of recent lines to return
    """
    sm = get_session_manager()
    try:
        return sm.read_buffer(session_id, lines=lines)
    except KeyError as e:
        return f"Error: {e}"


@mcp.tool()
async def ssh_close_session(session_id: str) -> str:
    """Close and destroy a persistent shell session.

    Args:
        session_id: Session ID to close
    """
    return await get_session_manager().close_session(session_id)


@mcp.tool()
def ssh_session_list() -> str:
    """List all active persistent shell sessions."""
    sessions = get_session_manager().list_sessions()
    return json.dumps(sessions, indent=2) if sessions else "No active sessions."


@mcp.tool()
def ssh_session_set_env(session_id: str, key: str, value: str) -> str:
    """Inject an environment variable into a persistent session.

    Args:
        session_id: Session ID
        key: Variable name
        value: Variable value
    """
    sm = get_session_manager()
    try:
        sm.set_env(session_id, key, value)
        return f"Set {key}={value} in session {session_id}"
    except KeyError as e:
        return f"Error: {e}"

# ═══════════════════════════════════════════════════════════════════
# 3. COMMAND EXECUTION ENGINE
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_run(host_name: str, command: str, timeout: int = 60,
                   cwd: str = "", env_json: str = "{}") -> str:
    """Execute a single command on a remote host (one-off).

    Args:
        host_name: Registered host name
        command: Shell command to execute
        timeout: Max execution time in seconds
        cwd: Working directory on remote (e.g. '/opt/app')
        env_json: JSON environment variables (e.g. '{"APP_ENV":"prod"}')
    """
    err = _gate(host_name, command)
    if err:
        return f"BLOCKED: {err}"
    env = _parse_env(env_json)
    result = await ssh_exec(host_name, command, timeout=timeout,
                             env=env or None, cwd=cwd or None)
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_run_batch(host_name: str, commands_json: str,
                         stop_on_error: bool = True, timeout: int = 60) -> str:
    """Execute a sequence of commands on a remote host.

    Args:
        host_name: Registered host name
        commands_json: JSON array of commands (e.g. '["apt update","apt install nginx -y"]')
        stop_on_error: Halt on first non-zero exit code
        timeout: Per-command timeout
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    try:
        commands = json.loads(commands_json)
        if not isinstance(commands, list):
            return "commands_json must be a JSON array"
    except Exception as e:
        return f"Invalid commands_json: {e}"
    results = await ssh_exec_batch(host_name, commands, stop_on_error=stop_on_error, timeout=timeout)
    return json.dumps(results, indent=2)


@mcp.tool()
async def ssh_run_script(host_name: str, script: str,
                          interpreter: str = "bash", timeout: int = 120) -> str:
    """Upload and execute a script on the remote host.

    Args:
        host_name: Registered host name
        script: Full script content to execute remotely
        interpreter: Interpreter to use (bash, python3, sh, etc.)
        timeout: Max execution time
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_exec_script(host_name, script, interpreter=interpreter, timeout=timeout)
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_run_with_env(host_name: str, command: str,
                            env_json: str, timeout: int = 60) -> str:
    """Execute a command with explicitly injected environment variables.

    Args:
        host_name: Registered host name
        command: Command to run
        env_json: JSON object of env vars (e.g. '{"DB_HOST":"localhost","DB_PORT":"5432"}')
        timeout: Max execution time
    """
    err = _gate(host_name, command)
    if err:
        return f"BLOCKED: {err}"
    env = _parse_env(env_json)
    if not env:
        return "Invalid env_json: must be a non-empty JSON object"
    result = await ssh_exec_with_env(host_name, command, env, timeout=timeout)
    return json.dumps(result, indent=2)


# ═══════════════════════════════════════════════════════════════════
# 4. FILE OPERATIONS
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_upload(host_name: str, local_path: str, remote_path: str) -> str:
    """Upload a local file to a remote host via SFTP.

    Args:
        host_name: Target host
        local_path: Absolute local file path
        remote_path: Destination path on remote (e.g. '/opt/app/docker-compose.yml')
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_upload_file(host_name, local_path, remote_path)
    audit_log(host_name, f"upload:{local_path}→{remote_path}", "ok", operation="file_upload")
    return result


@mcp.tool()
async def ssh_download(host_name: str, remote_path: str, local_path: str) -> str:
    """Download a file from a remote host to local disk.

    Args:
        host_name: Source host
        remote_path: File path on remote
        local_path: Local destination path
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_download_file(host_name, remote_path, local_path)
    audit_log(host_name, f"download:{remote_path}", "ok", operation="file_download")
    return result


@mcp.tool()
async def ssh_ls(host_name: str, remote_path: str = ".") -> str:
    """List directory contents on a remote host.

    Args:
        host_name: Target host
        remote_path: Directory to list (default: current directory)
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_list_directory(host_name, remote_path)
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_cat(host_name: str, remote_path: str, max_kb: int = 512) -> str:
    """Read a remote file's contents.

    Args:
        host_name: Target host
        remote_path: File to read
        max_kb: Maximum kilobytes to read (default 512)
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    return await ssh_read_file(host_name, remote_path, max_bytes=max_kb * 1024)


@mcp.tool()
async def ssh_write(host_name: str, remote_path: str, content: str) -> str:
    """Write content to a remote file (overwrites existing).

    Args:
        host_name: Target host
        remote_path: File path to write
        content: Text content to write
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_write_file(host_name, remote_path, content)
    audit_log(host_name, f"write:{remote_path}", "ok", operation="file_write")
    return result


@mcp.tool()
async def ssh_rm(host_name: str, remote_path: str) -> str:
    """Delete a file on a remote host.

    Args:
        host_name: Target host
        remote_path: File path to delete
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_delete_file(host_name, remote_path)
    audit_log(host_name, f"delete:{remote_path}", "ok", operation="file_delete")
    return result


@mcp.tool()
async def ssh_sync(host_name: str, local_dir: str, remote_dir: str) -> str:
    """Recursively sync a local directory to a remote host.

    Args:
        host_name: Target host
        local_dir: Local directory to sync from
        remote_dir: Remote destination directory
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_sync_directory(host_name, local_dir, remote_dir)
    audit_log(host_name, f"sync:{local_dir}→{remote_dir}", "ok", operation="file_sync")
    return result

# ═══════════════════════════════════════════════════════════════════
# 5. PROCESS MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_ps(host_name: str, filter_name: str = "") -> str:
    """List running processes on a remote host.

    Args:
        host_name: Target host
        filter_name: Optional string to filter process names
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    procs = await ssh_process_list(host_name, filter_name=filter_name)
    return json.dumps(procs, indent=2)


@mcp.tool()
async def ssh_kill(host_name: str, pid: int, signal: str = "TERM") -> str:
    """Send a signal to a remote process.

    Args:
        host_name: Target host
        pid: Process ID to signal
        signal: Signal name (TERM, KILL, HUP, USR1, USR2)
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_kill_process(host_name, pid, signal=signal)
    audit_log(host_name, f"kill:{pid}:{signal}", result, operation="kill")
    return result


@mcp.tool()
async def ssh_start(host_name: str, command: str, background: bool = False,
                     log_file: str = "") -> str:
    """Start a process on a remote host.

    Args:
        host_name: Target host
        command: Command to start
        background: Run in background (nohup) if True
        log_file: Log file for background process output
    """
    err = _gate(host_name, command)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_start_process(host_name, command, background=background,
                                      log_file=log_file or None)
    audit_log(host_name, command, "started", operation="start_process")
    return result


@mcp.tool()
async def ssh_background(host_name: str, command: str,
                          name: str = "mcp_proc", log_file: str = "") -> str:
    """Start a named background process with PID tracking and logging.

    Args:
        host_name: Target host
        command: Command to run in background
        name: Friendly name for the process
        log_file: Optional custom log file path
    """
    err = _gate(host_name, command)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_background_process(host_name, command, name=name,
                                           log_file=log_file or None)
    audit_log(host_name, command, f"bg:{result.get('pid')}", operation="background_process")
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_monitor(host_name: str, pid: int) -> str:
    """Monitor resource usage of a remote process by PID.

    Args:
        host_name: Target host
        pid: Process ID to monitor
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_monitor_process(host_name, pid)
    return json.dumps(result, indent=2)


# ═══════════════════════════════════════════════════════════════════
# 6. SYSTEM INSPECTION
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_info(host_name: str) -> str:
    """Get comprehensive system information from a remote host.

    Args:
        host_name: Target host
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_system_info(host_name)
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_df(host_name: str, path: str = "/") -> str:
    """Show disk usage on a remote host.

    Args:
        host_name: Target host
        path: Filesystem path to check (default: /)
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    return await ssh_disk_usage(host_name, path=path)


@mcp.tool()
async def ssh_free(host_name: str) -> str:
    """Show memory and swap usage on a remote host.

    Args:
        host_name: Target host
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    return await ssh_memory_usage(host_name)


@mcp.tool()
async def ssh_netstat(host_name: str) -> str:
    """Show network status, interfaces, ports, and routes on a remote host.

    Args:
        host_name: Target host
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    return await ssh_network_status(host_name)


@mcp.tool()
async def ssh_service(host_name: str, service: str = "") -> str:
    """Check systemd service status on a remote host.

    Args:
        host_name: Target host
        service: Service name (e.g. 'nginx'). If empty, lists all active services.
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    return await ssh_service_status(host_name, service=service)


@mcp.tool()
async def ssh_journalctl(host_name: str, service: str = "",
                          lines: int = 50, since: str = "") -> str:
    """Retrieve journal logs from a remote host.

    Args:
        host_name: Target host
        service: Service name to filter logs (e.g. 'docker', 'nginx')
        lines: Number of log lines to retrieve
        since: Time filter (e.g. '1 hour ago', '2024-01-01')
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    return await ssh_logs(host_name, service=service, lines=lines, since=since)


@mcp.tool()
async def ssh_docker(host_name: str) -> str:
    """Show Docker containers and images on a remote host.

    Args:
        host_name: Target host
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    return await ssh_docker_status(host_name)

# ═══════════════════════════════════════════════════════════════════
# 7. MULTI-HOST ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_parallel(hosts_json: str, command: str, timeout: int = 60) -> str:
    """Execute a command simultaneously on multiple hosts.

    Args:
        hosts_json: JSON array of host names (e.g. '["web01","web02","web03"]')
        command: Command to run on all hosts
        timeout: Per-host timeout
    """
    try:
        hosts = json.loads(hosts_json)
    except Exception as e:
        return f"Invalid hosts_json: {e}"
    err = _gate_many(hosts, command)
    if err:
        return f"BLOCKED: {err}"
    results = await ssh_parallel_exec(hosts, command, timeout=timeout)
    return json.dumps(results, indent=2)


@mcp.tool()
async def ssh_rolling(hosts_json: str, command: str,
                       delay_s: float = 2.0, stop_on_error: bool = True,
                       timeout: int = 60) -> str:
    """Execute a command sequentially across hosts with delay between each.
    Useful for rolling restarts and zero-downtime deployments.

    Args:
        hosts_json: JSON array of host names in execution order
        command: Command to run on each host
        delay_s: Seconds to wait between hosts
        stop_on_error: Stop if any host returns non-zero exit code
        timeout: Per-host timeout
    """
    try:
        hosts = json.loads(hosts_json)
    except Exception as e:
        return f"Invalid hosts_json: {e}"
    err = _gate_many(hosts, command)
    if err:
        return f"BLOCKED: {err}"
    results = await ssh_rolling_exec(hosts, command, delay_s=delay_s,
                                      stop_on_error=stop_on_error, timeout=timeout)
    return json.dumps(results, indent=2)


@mcp.tool()
async def ssh_group_exec(group_tag: str, command: str, timeout: int = 60) -> str:
    """Execute a command on all hosts sharing a group tag.

    Args:
        group_tag: Tag to select hosts (e.g. 'production', 'web', 'database')
        command: Command to run
        timeout: Per-host timeout
    """
    hosts = _resolve_group(group_tag)
    if not hosts:
        return json.dumps([{"error": f"No hosts found with tag '{group_tag}'"}], indent=2)
    err = _gate_many(hosts, command)
    if err:
        return f"BLOCKED: {err}"
    results = await ssh_exec_on_group(group_tag, command, timeout=timeout)
    return json.dumps(results, indent=2)


@mcp.tool()
async def ssh_broadcast_batch(hosts_json: str, commands_json: str,
                               timeout: int = 60) -> str:
    """Broadcast a sequence of commands to multiple hosts in parallel.

    Args:
        hosts_json: JSON array of host names
        commands_json: JSON array of commands to run in sequence on each host
        timeout: Per-command timeout
    """
    try:
        hosts = json.loads(hosts_json)
        commands = json.loads(commands_json)
    except Exception as e:
        return f"Invalid JSON: {e}"
    if not isinstance(commands, list):
        return "Invalid commands_json: must be a JSON array of strings"
    for cmd in commands:
        if not isinstance(cmd, str):
            return "Invalid commands_json: all entries must be strings"
        err = _gate_many(hosts, cmd)
        if err:
            return f"BLOCKED on command {cmd[:60]!r}: {err}"
    results = await ssh_broadcast(hosts, commands, timeout=timeout)
    return json.dumps(results, indent=2)


@mcp.tool()
async def ssh_playbook(host_name: str, playbook_json: str) -> str:
    """Execute an infrastructure playbook on a single host.

    Playbook JSON format:
    {
      "name": "deploy_docker_stack",
      "on_error": "stop",
      "steps": ["apt update", "apt install docker.io -y", "systemctl enable --now docker"]
    }

    Args:
        host_name: Target host
        playbook_json: JSON playbook definition
    """
    try:
        playbook = json.loads(playbook_json)
    except Exception as e:
        return f"Invalid playbook_json: {e}"
    err = _gate_playbook([host_name], playbook)
    if err:
        return f"BLOCKED: {err}"
    result = await run_playbook(host_name, playbook)
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_playbook_on_group(group_tag: str, playbook_json: str) -> str:
    """Execute an infrastructure playbook across all hosts in a group.

    Args:
        group_tag: Tag to select target hosts
        playbook_json: JSON playbook definition (same format as ssh_playbook)
    """
    try:
        playbook = json.loads(playbook_json)
    except Exception as e:
        return f"Invalid playbook_json: {e}"
    hosts = _resolve_group(group_tag)
    if not hosts:
        return json.dumps([{"error": f"No hosts with tag '{group_tag}'"}], indent=2)
    err = _gate_playbook(hosts, playbook)
    if err:
        return f"BLOCKED: {err}"
    results = await run_playbook_on_group(group_tag, playbook)
    return json.dumps(results, indent=2)


# ═══════════════════════════════════════════════════════════════════
# 8. SSH TUNNELS AND PORT FORWARDING
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_port_forward(host_name: str, local_port: int,
                            remote_host: str, remote_port: int,
                            local_bind: str = "127.0.0.1") -> str:
    """Create a local port forward: localhost:local_port → remote_host:remote_port via SSH host.

    Example: Forward local 5432 to a private database at db.internal:5432
    Args:
        host_name: SSH jump host (must be registered)
        local_port: Port to listen on locally (0 = auto-assign)
        remote_host: Target host (can be internal/private)
        remote_port: Target port on remote_host
        local_bind: Local bind address (default: 127.0.0.1)
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    tm = get_tunnel_manager()
    result = await tm.open_local_forward(host_name, local_port, remote_host, remote_port, local_bind)
    audit_log(host_name, f"tunnel:local:{local_port}→{remote_host}:{remote_port}", "ok", operation="tunnel")
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_reverse_tunnel(host_name: str, remote_port: int,
                              local_host: str, local_port: int) -> str:
    """Create a reverse tunnel: host_name:remote_port → local_host:local_port.

    Exposes a local service to the remote host.
    Args:
        host_name: SSH server to bind the remote port on
        remote_port: Port to open on the remote SSH server
        local_host: Local host to forward to (e.g. 127.0.0.1)
        local_port: Local port to forward to
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    tm = get_tunnel_manager()
    result = await tm.open_remote_forward(host_name, remote_port, local_host, local_port)
    audit_log(host_name, f"tunnel:reverse:{remote_port}←{local_host}:{local_port}", "ok", operation="tunnel")
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_socks_proxy(host_name: str, local_port: int = 1080,
                           local_bind: str = "127.0.0.1") -> str:
    """Open a SOCKS5 dynamic proxy through a remote host.

    Route any traffic through the remote host via SOCKS5 proxy.
    Args:
        host_name: SSH host to use as proxy exit node
        local_port: Local SOCKS5 port (default: 1080)
        local_bind: Local bind address
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    tm = get_tunnel_manager()
    result = await tm.open_dynamic_proxy(host_name, local_port, local_bind)
    audit_log(host_name, f"tunnel:socks:{local_port}", "ok", operation="tunnel")
    return json.dumps(result, indent=2)


@mcp.tool()
async def ssh_close_tunnel(tunnel_id: str) -> str:
    """Close an active SSH tunnel by ID.

    Args:
        tunnel_id: Tunnel ID from ssh_port_forward / ssh_reverse_tunnel / ssh_socks_proxy
    """
    tm = get_tunnel_manager()
    result = await tm.close_tunnel(tunnel_id)
    audit_log("tunnel", f"close:{tunnel_id}", "ok", operation="tunnel_close")
    return result


@mcp.tool()
def ssh_active_tunnels() -> str:
    """List all currently active SSH tunnels."""
    tunnels = get_tunnel_manager().list_tunnels()
    return json.dumps(tunnels, indent=2) if tunnels else "No active tunnels."


# ═══════════════════════════════════════════════════════════════════
# 9. SECURITY CONTROLS
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def ssh_security_status() -> str:
    """Show the current security policy in effect (allowlists, rate limits, etc.)."""
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


@mcp.tool()
def ssh_check_host_access(host_name: str) -> str:
    """Check whether a host is accessible under the current security policy.

    Args:
        host_name: Host name to check
    """
    err = get_policy().check_host(host_name)
    if err:
        return f"BLOCKED: {err}"
    reg = get_manager()._registry
    if host_name not in reg:
        return f"ALLOWED by policy but host '{host_name}' is not registered."
    return f"ALLOWED: '{host_name}' passes all security checks."


@mcp.tool()
def ssh_check_command(host_name: str, command: str) -> str:
    """Dry-run a command through the security policy without executing it.

    Args:
        host_name: Target host
        command: Command string to check
    """
    err = _gate(host_name, command)
    if err:
        return f"BLOCKED: {err}"
    return f"ALLOWED: command passes all security checks for host '{host_name}'."


# ═══════════════════════════════════════════════════════════════════
# 10. OBSERVABILITY
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
def ssh_operation_history(limit: int = 50, host_filter: str = "") -> str:
    """Show recent SSH operation history from the audit log.

    Args:
        limit: Number of recent entries to return (default: 50)
        host_filter: Optional host name to filter by
    """
    history = get_history(limit=limit, host_filter=host_filter)
    return json.dumps(history, indent=2) if history else "No operations recorded yet."


@mcp.tool()
def ssh_audit_stats() -> str:
    """Return aggregate statistics from the audit log."""
    return json.dumps(get_audit_stats(), indent=2)


@mcp.tool()
def ssh_full_status() -> str:
    """Return a complete observability snapshot: connections, sessions, tunnels, stats."""
    mgr = get_manager()
    snap = {
        "registered_hosts": len(mgr._registry),
        "hosts": mgr.list_hosts(),
        "connection_pool": mgr.pool_status(),
        "active_sessions": get_session_manager().list_sessions(),
        "active_tunnels": get_tunnel_manager().list_tunnels(),
        "audit_stats": get_audit_stats(),
        "security": {
            "host_allowlist_count": len(get_policy().host_allowlist),
            "command_blocklist_count": len(get_policy().command_blocklist),
            "rate_limit_rps": get_policy().rate_limit_rps,
        },
    }
    return json.dumps(snap, indent=2)


# ═══════════════════════════════════════════════════════════════════
# ADVANCED: TMUX MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_tmux_new(host_name: str, session_name: str) -> str:
    """Create a new tmux session on a remote host.

    Args:
        host_name: Target host
        session_name: tmux session name
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    try:
        session_name = validate_tmux_name(session_name)
    except ValueError as e:
        return f"Invalid session_name: {e}"
    result = await ssh_exec(
        host_name,
        f"tmux new-session -d -s {session_name} 2>&1 || echo 'already exists'",
        timeout=10
    )
    return result.get("stdout", result.get("error", ""))


@mcp.tool()
async def ssh_tmux_send(host_name: str, session_name: str, command: str) -> str:
    """Send a command to a tmux session on a remote host.

    Args:
        host_name: Target host
        session_name: tmux session name
        command: Command to send
    """
    err = _gate(host_name, command)
    if err:
        return f"BLOCKED: {err}"
    try:
        session_name = validate_tmux_name(session_name)
    except ValueError as e:
        return f"Invalid session_name: {e}"
    # ``command`` is sent verbatim to the tmux pane; tmux handles its own
    # parsing. We single-source-quote it via shlex.quote so it survives the
    # outer ``tmux send-keys ... Enter`` shell line, and quote ``session_name``
    # for completeness even though it's restricted by the regex above.
    await ssh_exec(
        host_name,
        f"tmux send-keys -t {quote_shell(session_name)} "
        f"{quote_shell(command)} Enter",
        timeout=10,
    )
    await asyncio.sleep(0.5)
    capture = await ssh_exec(
        host_name,
        f"tmux capture-pane -t {quote_shell(session_name)} -p",
        timeout=10
    )
    return capture.get("stdout", "")


@mcp.tool()
async def ssh_tmux_list(host_name: str) -> str:
    """List all tmux sessions on a remote host.

    Args:
        host_name: Target host
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    result = await ssh_exec(host_name, "tmux list-sessions 2>/dev/null || echo 'no sessions'", timeout=10)
    return result.get("stdout", "")


@mcp.tool()
async def ssh_tmux_kill(host_name: str, session_name: str) -> str:
    """Kill a tmux session on a remote host.

    Args:
        host_name: Target host
        session_name: tmux session to kill
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    try:
        session_name = validate_tmux_name(session_name)
    except ValueError as e:
        return f"Invalid session_name: {e}"
    result = await ssh_exec(host_name, f"tmux kill-session -t {session_name}", timeout=10)
    return f"Killed tmux session '{session_name}'" if result.get("exit_code") == 0 else result.get("stderr", "")


# ═══════════════════════════════════════════════════════════════════
# ADVANCED: AUTO-RETRY + HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════

@mcp.tool()
async def ssh_exec_retry(host_name: str, command: str,
                          retries: int = 3, delay_s: float = 2.0,
                          timeout: int = 60) -> str:
    """Execute a command with automatic retry on failure.

    Args:
        host_name: Target host
        command: Command to execute
        retries: Maximum retry attempts (default: 3)
        delay_s: Seconds between retries (default: 2.0)
        timeout: Per-attempt timeout
    """
    err = _gate(host_name, command)
    if err:
        return f"BLOCKED: {err}"
    last_result: dict = {}
    for attempt in range(1, retries + 1):
        last_result = await ssh_exec(host_name, command, timeout=timeout)
        if last_result.get("exit_code", -1) == 0:
            last_result["attempt"] = attempt
            return json.dumps(last_result, indent=2)
        if attempt < retries:
            await asyncio.sleep(delay_s)
    last_result["attempt"] = retries
    last_result["note"] = f"Failed after {retries} attempts"
    return json.dumps(last_result, indent=2)


@mcp.tool()
async def ssh_ping_host(host_name: str) -> str:
    """Test SSH connectivity to a registered host.

    Args:
        host_name: Host to test
    """
    err = _gate(host_name)
    if err:
        return f"BLOCKED: {err}"
    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            ssh_exec(host_name, "echo pong", timeout=10),
            timeout=12
        )
        elapsed = round(time.time() - t0, 3)
        ok = result.get("stdout", "").strip() == "pong"
        return json.dumps({"host": host_name, "reachable": ok,
                           "latency_s": elapsed, "exit_code": result.get("exit_code")})
    except Exception as e:
        return json.dumps({"host": host_name, "reachable": False, "error": str(e)})


@mcp.tool()
async def ssh_health_check_fleet(hosts_json: str = "[]") -> str:
    """Ping all specified hosts (or all registered hosts) in parallel.

    Args:
        hosts_json: JSON array of host names. Empty array = check all registered hosts.
    """
    mgr = get_manager()
    try:
        hosts = json.loads(hosts_json) if hosts_json.strip() not in ("", "[]") else list(mgr._registry.keys())
    except Exception:
        hosts = list(mgr._registry.keys())
    if not hosts:
        return "No hosts registered."
    tasks = [ssh_ping_host(h) for h in hosts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    parsed = []
    for r in results:
        try:
            parsed.append(json.loads(r) if isinstance(r, str) else {"error": str(r)})
        except Exception:
            parsed.append({"raw": str(r)})
    online = sum(1 for p in parsed if p.get("reachable"))
    return json.dumps({"online": online, "total": len(hosts), "hosts": parsed}, indent=2)


# ═══════════════════════════════════════════════════════════════════
# 12. REMOTE-MCP EXTENSIONS — agent-feels-local tools
# ═══════════════════════════════════════════════════════════════════
# These wrap server.remote_* modules. Designed to be the *primary* tools an
# AI agent uses when working on a remote host. They share one SSH connection
# per host (via the connection pool) and provide:
#   - remote_read / remote_patch :  hash-protected concurrent-safe edits
#   - remote_grep / remote_glob  :  remote ripgrep / find with structured output
#   - remote_bash                :  single persistent bash session (cwd + env survive)

@mcp.tool()
async def remote_read(host: str, path: str, start: int = 1,
                      end: int | None = None, encoding: str = "utf-8") -> str:
    """Read a file (or a 1-based line range) from a remote host with SHA-256 hashes.

    Returns JSON with: content, file_hash, range_hash, start, end, total_lines.
    The file_hash MUST be supplied to remote_patch; if the file changed in
    between, remote_patch will refuse to overwrite.

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
async def remote_patch(host: str, path: str, file_hash: str,
                       patches_json: str, encoding: str = "utf-8",
                       auto_newline: bool = False) -> str:
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
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_cleanup_tmps(host: str, directory: str,
                              max_age_s: int = 3600) -> str:
    """Remove orphan tmp files left by interrupted remote_patch writes.

    remote_patch writes through ``<path>.mcp_tmp.<12hex>`` and renames into
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
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_grep(host: str, path: str, pattern: str,
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
async def remote_glob(host: str, pattern: str, path: str = ".") -> str:
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
async def remote_bash(host: str, command: str, timeout: float = 60.0) -> str:
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
    return json.dumps(res, indent=2, ensure_ascii=False)


@mcp.tool()
async def remote_bash_close(host: str) -> str:
    """Close the cached default bash session for <host> (next call will reopen)."""
    return await _re_bash_close(host)


@mcp.tool()
async def remote_bash_status() -> str:
    """List host -> session_id mappings for cached default bash sessions."""
    return json.dumps(_re_bash_status(), indent=2)


# ═══════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    """CLI entrypoint registered as `ssh-remote-mcp`."""
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
        prog="ssh-remote-mcp",
        description="ssh-remote-mcp — SSH Orchestration MCP Server",
    )
    parser.add_argument("--transport", choices=["stdio", "streamable_http"], default="stdio",
                        help="MCP transport (default: stdio)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    args = parser.parse_args()

    logger.info(f"ssh-remote-mcp starting | transport={args.transport}")

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
