"""
System Inspector — infrastructure visibility: disk, memory, network, services, logs.
"""
import asyncio
import logging
from .shell_engine import ssh_exec

logger = logging.getLogger("ssh_mcp.system")


async def ssh_system_info(host_name: str) -> dict:
    """Return comprehensive system information for a host."""
    commands = {
        "hostname":  "hostname -f",
        "os":        "cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'",
        "kernel":    "uname -r",
        "arch":      "uname -m",
        "uptime":    "uptime -p",
        "cpu_model": "grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2 | xargs",
        "cpu_cores": "nproc",
        "load":      "cat /proc/loadavg | awk '{print $1,$2,$3}'",
        "mem_total": "free -h | grep Mem | awk '{print $2}'",
        "mem_used":  "free -h | grep Mem | awk '{print $3}'",
        "ip_addrs":  "hostname -I | tr ' ' '\n' | grep -v '^$'",
    }
    results = {}
    for key, cmd in commands.items():
        r = await ssh_exec(host_name, cmd, timeout=10)
        results[key] = r.get("stdout", "").strip() or r.get("error", "N/A")
    results["host"] = host_name
    return results


async def ssh_disk_usage(host_name: str, path: str = "/") -> str:
    """Show disk usage on a remote host."""
    result = await ssh_exec(host_name, f"df -h {path}", timeout=10)
    return result.get("stdout", result.get("error", ""))


async def ssh_memory_usage(host_name: str) -> str:
    """Show detailed memory usage on a remote host."""
    result = await ssh_exec(host_name, "free -h && echo '---' && cat /proc/meminfo | grep -E 'MemTotal|MemFree|MemAvailable|SwapTotal|SwapFree|Cached'", timeout=10)
    return result.get("stdout", result.get("error", ""))


async def ssh_network_status(host_name: str) -> str:
    """Show network connections and listening ports."""
    cmds = [
        "echo '=== LISTENING PORTS ==='",
        "ss -tulpn 2>/dev/null || netstat -tulpn 2>/dev/null",
        "echo '=== INTERFACES ==='",
        "ip addr show 2>/dev/null || ifconfig 2>/dev/null",
        "echo '=== ROUTES ==='",
        "ip route 2>/dev/null || route -n 2>/dev/null",
    ]
    result = await ssh_exec(host_name, " && ".join(cmds), timeout=15)
    return result.get("stdout", result.get("error", ""))


async def ssh_service_status(host_name: str, service: str = "") -> str:
    """Check systemd service status. If no service given, list all active."""
    if service:
        cmd = f"systemctl status {service} --no-pager -l"
    else:
        cmd = "systemctl list-units --type=service --state=active --no-pager --no-legend | head -40"
    result = await ssh_exec(host_name, cmd, timeout=15)
    return result.get("stdout", "") + result.get("stderr", "")


async def ssh_logs(host_name: str, service: str = "",
                   lines: int = 50, since: str = "") -> str:
    """Retrieve logs from journalctl or syslog."""
    if service:
        cmd = f"journalctl -u {service} -n {lines} --no-pager"
    else:
        cmd = f"journalctl -n {lines} --no-pager"
    if since:
        cmd += f" --since '{since}'"
    result = await ssh_exec(host_name, cmd, timeout=20)
    if result.get("exit_code", 0) != 0:
        # fallback to syslog
        result = await ssh_exec(host_name, f"tail -n {lines} /var/log/syslog 2>/dev/null || tail -n {lines} /var/log/messages 2>/dev/null", timeout=10)
    return result.get("stdout", result.get("error", ""))


async def ssh_docker_status(host_name: str) -> str:
    """Show Docker containers and images on a remote host."""
    cmds = [
        "echo '=== CONTAINERS ==='",
        "docker ps -a --format 'table {{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}' 2>/dev/null || echo 'Docker not available'",
        "echo '=== IMAGES ==='",
        "docker images --format 'table {{.Repository}}\\t{{.Tag}}\\t{{.Size}}' 2>/dev/null | head -20",
    ]
    result = await ssh_exec(host_name, " && ".join(cmds), timeout=20)
    return result.get("stdout", result.get("error", ""))
