"""
Process Manager — remote process listing, killing, starting, monitoring.
"""
import asyncio
import logging
from .shell_engine import ssh_exec

logger = logging.getLogger("ssh_mcp.processes")


async def ssh_process_list(host_name: str, filter_name: str = "") -> list[dict]:
    """List running processes on a remote host."""
    cmd = "ps aux --no-headers"
    result = await ssh_exec(host_name, cmd, timeout=15)
    if result.get("error"):
        return [{"error": result["error"]}]
    procs = []
    for line in result.get("stdout", "").splitlines():
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        proc = {
            "user": parts[0], "pid": parts[1], "cpu": parts[2],
            "mem": parts[3], "vsz": parts[4], "rss": parts[5],
            "stat": parts[7], "command": parts[10],
        }
        if not filter_name or filter_name.lower() in proc["command"].lower():
            procs.append(proc)
    return procs


async def ssh_kill_process(host_name: str, pid: int, signal: str = "TERM") -> str:
    """Send a signal to a remote process by PID."""
    result = await ssh_exec(host_name, f"kill -{signal} {pid}", timeout=10)
    if result.get("exit_code") == 0:
        return f"Sent {signal} to PID {pid} on {host_name}"
    return f"Kill failed: {result.get('stderr', result.get('error'))}"


async def ssh_start_process(host_name: str, command: str,
                             background: bool = False,
                             log_file: str = None) -> str:
    """Start a process on a remote host."""
    if background:
        log = log_file or "/tmp/mcp_process.log"
        cmd = f"nohup {command} > {log} 2>&1 & echo $!"
    else:
        cmd = command
    result = await ssh_exec(host_name, cmd, timeout=30)
    if background:
        pid = result.get("stdout", "").strip()
        return f"Started background process on {host_name}, PID={pid}, log={log}"
    return result.get("stdout", "") + result.get("stderr", "")


async def ssh_background_process(host_name: str, command: str,
                                   name: str = "mcp_proc",
                                   log_file: str = None) -> dict:
    """Start a named background process with logging and PID tracking."""
    log = log_file or f"/tmp/{name}.log"
    pid_file = f"/tmp/{name}.pid"
    cmd = (
        f"nohup bash -c {command!r} > {log} 2>&1 & "
        f"echo $! | tee {pid_file}"
    )
    result = await ssh_exec(host_name, cmd, timeout=15)
    pid = result.get("stdout", "").strip()
    return {
        "host": host_name, "name": name, "pid": pid,
        "log_file": log, "pid_file": pid_file,
        "status": "started" if pid else "failed",
    }


async def ssh_monitor_process(host_name: str, pid: int) -> dict:
    """Check if a process is running and get its resource usage."""
    result = await ssh_exec(host_name, f"ps -p {pid} -o pid,stat,pcpu,pmem,comm --no-headers 2>/dev/null", timeout=10)
    output = result.get("stdout", "").strip()
    if not output:
        return {"pid": pid, "host": host_name, "running": False}
    parts = output.split(None, 4)
    return {
        "pid": pid, "host": host_name, "running": True,
        "stat": parts[1] if len(parts) > 1 else "?",
        "cpu%": parts[2] if len(parts) > 2 else "?",
        "mem%": parts[3] if len(parts) > 3 else "?",
        "command": parts[4] if len(parts) > 4 else "?",
    }
