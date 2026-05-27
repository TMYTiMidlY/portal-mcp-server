"""
Shell Engine — one-off command execution, streaming, and batch operations.

Hardening notes
---------------
* ``cwd`` is interpolated through :func:`safety.build_cwd_prefix`, which uses
  :func:`shlex.quote` so an LLM-supplied ``cwd`` cannot break out of the
  ``cd`` argument. Before this fix, ``cwd="/tmp; rm -rf /"`` would have
  expanded to ``cd /tmp; rm -rf / && <cmd>``.
* ``ssh_exec_with_env`` no longer manually concatenates ``env k=v ...`` —
  it forwards the validated env dict to ``conn.run(env=...)`` so OpenSSH
  handles the protocol-level env channel natively. This sidesteps a whole
  class of value-quoting bugs.
* ``ssh_exec_script``: the interpreter must be in a fixed allowlist
  (:data:`safety._ALLOWED_INTERPRETERS`) and the temp-script path is
  shell-quoted. The cleanup ``rm -f`` runs inside ``finally`` so a failed
  or timed-out script no longer leaks ``/tmp/_mcp_script_*.sh``.
"""
import asyncio
import time
import logging
from typing import AsyncGenerator

from .connection_manager import DEFAULT_DECODE_ERRORS, get_manager
from .audit import audit_log
from .safety import (
    build_cwd_prefix,
    quote_shell,
    validate_env_dict,
    validate_interpreter,
)

logger = logging.getLogger("portal_mcp.shell")


async def ssh_exec(host_name: str, command: str, timeout: int = 60,
                   env: dict = None, cwd: str = None) -> dict:
    """Execute a single command on a remote host and return result."""
    try:
        env_clean = validate_env_dict(env)
        full_cmd = build_cwd_prefix(cwd, command)
    except ValueError as e:
        return {"host": host_name, "command": command,
                "error": f"Invalid input: {e}", "exit_code": -1}
    mgr = get_manager()
    conn = await mgr.get_connection(host_name)
    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            conn.run(full_cmd, env=env_clean, check=False,
                     errors=DEFAULT_DECODE_ERRORS),
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
        async with conn.create_process(command, errors=DEFAULT_DECODE_ERRORS) as proc:
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
    try:
        interpreter = validate_interpreter(interpreter)
    except ValueError as e:
        return {"error": f"Invalid interpreter: {e}", "host": host_name}

    remote_path = f"/tmp/_mcp_script_{int(time.time())}.sh"
    quoted_path = quote_shell(remote_path)
    mgr = get_manager()
    conn = await mgr.get_connection(host_name)
    try:
        async with conn.start_sftp_client() as sftp:
            async with sftp.open(remote_path, "w") as f:
                await f.write(script_content)
        try:
            return await ssh_exec(
                host_name,
                f"chmod +x {quoted_path} && {interpreter} {quoted_path}",
                timeout=timeout,
            )
        finally:
            # cleanup runs even if the inner ssh_exec raised or timed out
            try:
                await ssh_exec(host_name, f"rm -f {quoted_path}", timeout=10)
            except Exception:  # pragma: no cover
                logger.debug(f"script cleanup failed for {remote_path}")
    except Exception as e:
        return {"error": str(e), "host": host_name}
    finally:
        mgr.release_connection(host_name, conn)


async def ssh_exec_with_env(host_name: str, command: str,
                             env_vars: dict, timeout: int = 60) -> dict:
    """Execute a command with injected environment variables.

    The env dict is forwarded as the protocol-level ``env`` channel, so we
    avoid the historical ``f"env K={v!r} {command}"`` string concatenation
    that an attacker could escape from with a crafted key or value.
    """
    return await ssh_exec(host_name, command, timeout=timeout, env=env_vars)
