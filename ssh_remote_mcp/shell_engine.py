"""
Shell Engine — one-off command execution, streaming, and batch operations.
"""
import asyncio
import time
import logging
from typing import AsyncGenerator

import asyncssh
from .connection_manager import get_manager
from .audit import audit_log

logger = logging.getLogger("ssh_mcp.shell")


async def ssh_exec(host_name: str, command: str, timeout: int = 60,
                   env: dict = None, cwd: str = None) -> dict:
    """Execute a single command on a remote host and return result."""
    mgr = get_manager()
    conn = await mgr.get_connection(host_name)
    full_cmd = command
    if cwd:
        full_cmd = f"cd {cwd} && {command}"
    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            conn.run(full_cmd, env=env or {}, check=False),
            timeout=timeout,
        )
        elapsed = round(time.time() - t0, 3)
        out = {
            "host": host_name,
            "command": command,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "elapsed_s": elapsed,
        }
        audit_log(host_name, command, result.returncode)
        return out
    except asyncio.TimeoutError:
        audit_log(host_name, command, "TIMEOUT")
        return {"host": host_name, "command": command,
                "error": f"Timed out after {timeout}s", "exit_code": -1}
    except Exception as e:
        audit_log(host_name, command, f"ERROR:{e}")
        return {"host": host_name, "command": command, "error": str(e), "exit_code": -1}
    finally:
        mgr.release_connection(host_name, conn)


async def ssh_exec_stream(host_name: str, command: str,
                          timeout: int = 120) -> AsyncGenerator[str, None]:
    """Stream command output line by line as it arrives."""
    mgr = get_manager()
    conn = await mgr.get_connection(host_name)
    try:
        async with conn.create_process(command) as proc:
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                    if not line:
                        break
                    yield line.rstrip("\n")
                except asyncio.TimeoutError:
                    continue
    except Exception as e:
        yield f"[stream error] {e}"
    finally:
        mgr.release_connection(host_name, conn)

async def ssh_exec_batch(host_name: str, commands: list[str],
                          stop_on_error: bool = True,
                          timeout: int = 60) -> list[dict]:
    """Execute a list of commands sequentially on a host."""
    results = []
    for cmd in commands:
        result = await ssh_exec(host_name, cmd, timeout=timeout)
        results.append(result)
        if stop_on_error and result.get("exit_code", 0) != 0:
            results.append({"info": f"Stopped at: {cmd} (exit {result.get('exit_code')})"})
            break
    return results


async def ssh_exec_script(host_name: str, script_content: str,
                           interpreter: str = "bash",
                           timeout: int = 120) -> dict:
    """Upload and execute a script on the remote host."""
    remote_path = f"/tmp/_mcp_script_{int(time.time())}.sh"
    mgr = get_manager()
    conn = await mgr.get_connection(host_name)
    try:
        async with conn.start_sftp_client() as sftp:
            async with sftp.open(remote_path, "w") as f:
                await f.write(script_content)
        result = await ssh_exec(host_name, f"chmod +x {remote_path} && {interpreter} {remote_path}", timeout=timeout)
        # cleanup
        await ssh_exec(host_name, f"rm -f {remote_path}", timeout=10)
        return result
    except Exception as e:
        return {"error": str(e), "host": host_name}
    finally:
        mgr.release_connection(host_name, conn)


async def ssh_exec_with_env(host_name: str, command: str,
                             env_vars: dict, timeout: int = 60) -> dict:
    """Execute a command with injected environment variables."""
    env_prefix = " ".join(f"{k}={v!r}" for k, v in env_vars.items())
    full_cmd = f"env {env_prefix} {command}" if env_vars else command
    return await ssh_exec(host_name, full_cmd, timeout=timeout)
