"""
Orchestrator — multi-host fleet operations: parallel, rolling, broadcast, groups.
"""
import asyncio
import logging
import time
from typing import Callable

from .shell_engine import ssh_exec, ssh_exec_batch
from .connection_manager import get_manager
from .audit import audit_log

logger = logging.getLogger("ssh_mcp.orchestrator")


async def ssh_parallel_exec(host_names: list[str], command: str,
                             timeout: int = 60) -> list[dict]:
    """Execute the same command on all hosts simultaneously."""
    tasks = [ssh_exec(h, command, timeout=timeout) for h in host_names]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = []
    for host, r in zip(host_names, results):
        if isinstance(r, Exception):
            out.append({"host": host, "error": str(r), "exit_code": -1})
        else:
            out.append(r)
    audit_log(",".join(host_names), command, "parallel_exec", operation="parallel_exec")
    return out


async def ssh_rolling_exec(host_names: list[str], command: str,
                            delay_s: float = 2.0,
                            stop_on_error: bool = True,
                            timeout: int = 60) -> list[dict]:
    """Execute command sequentially across hosts with delay between each."""
    results = []
    for i, host in enumerate(host_names):
        logger.info(f"Rolling exec [{i+1}/{len(host_names)}]: {host}")
        result = await ssh_exec(host, command, timeout=timeout)
        results.append(result)
        if stop_on_error and result.get("exit_code", 0) != 0:
            results.append({"info": f"Rolling stopped at host {host}"})
            break
        if i < len(host_names) - 1:
            await asyncio.sleep(delay_s)
    return results


async def ssh_exec_on_group(group_tag: str, command: str,
                             timeout: int = 60) -> list[dict]:
    """Execute a command on all hosts with a given tag."""
    mgr = get_manager()
    hosts = [h.name for h in mgr._registry.values() if group_tag in h.tags]
    if not hosts:
        return [{"error": f"No hosts found with tag '{group_tag}'"}]
    return await ssh_parallel_exec(hosts, command, timeout=timeout)


async def ssh_broadcast(host_names: list[str], commands: list[str],
                         timeout: int = 60) -> dict[str, list]:
    """Broadcast a sequence of commands to all hosts in parallel."""
    async def run_batch(host):
        return await ssh_exec_batch(host, commands, timeout=timeout)
    results_list = await asyncio.gather(
        *[run_batch(h) for h in host_names], return_exceptions=True
    )
    results = {}
    for host, r in zip(host_names, results_list):
        if isinstance(r, Exception):
            results[host] = [{"error": str(r)}]
        else:
            results[host] = r
    return results


async def run_playbook(host_name: str, playbook: dict) -> dict:
    """
    Execute a named playbook definition on a host.
    Playbook format:
      name: deploy_docker_stack
      steps:
        - apt update
        - apt install docker.io -y
        - systemctl enable --now docker
      on_error: stop   # or 'continue'
    """
    name = playbook.get("name", "unnamed")
    steps = playbook.get("steps", [])
    on_error = playbook.get("on_error", "stop")
    results = []
    t0 = time.time()
    logger.info(f"Playbook '{name}' starting on {host_name} ({len(steps)} steps)")
    for i, step in enumerate(steps):
        logger.info(f"  Step {i+1}/{len(steps)}: {step[:80]}")
        result = await ssh_exec(host_name, step, timeout=120)
        results.append({"step": i + 1, "command": step, **result})
        if result.get("exit_code", 0) != 0 and on_error == "stop":
            results.append({"info": f"Playbook stopped at step {i+1}"})
            break
    elapsed = round(time.time() - t0, 2)
    audit_log(host_name, f"playbook:{name}", f"elapsed:{elapsed}s", operation="playbook")
    return {
        "playbook": name, "host": host_name,
        "steps_run": len(results),
        "elapsed_s": elapsed,
        "results": results,
    }


async def run_playbook_on_group(group_tag: str, playbook: dict) -> list[dict]:
    """Run a playbook against all hosts in a group simultaneously."""
    mgr = get_manager()
    hosts = [h.name for h in mgr._registry.values() if group_tag in h.tags]
    if not hosts:
        return [{"error": f"No hosts with tag '{group_tag}'"}]
    tasks = [run_playbook(h, playbook) for h in hosts]
    return await asyncio.gather(*tasks, return_exceptions=False)
