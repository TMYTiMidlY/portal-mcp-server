"""local_exec — run a one-shot command on the *MCP server host* with named
secrets injected as environment variables.

Unlike every other portal_* capability (which runs over SSH on a remote host),
this executes locally. That is a strictly larger threat surface, so the
``portal_local_exec`` tool is OFF by default and must be enabled with
``PORTAL_ALLOW_LOCAL_EXEC=1`` (see :mod:`cli`).

Secrets are passed through the child process **environment** (never on argv, so
they stay out of ``ps`` and the audit log). The caller resolves names to values
and redacts the returned output.
"""
from __future__ import annotations

import asyncio
import os


async def local_exec_with_env(command: str, env: dict,
                              timeout: float = 3600.0) -> dict:
    """Run ``command`` in a shell with ``env`` overlaid on ``os.environ``.

    ``env`` maps already-resolved ``ENV_VAR_NAME -> value``. Returns
    ``{output, exit_code}``; the caller redacts the output.
    """
    full_env = {**os.environ, **{k: str(v) for k, v in env.items()}}
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
    except OSError as e:
        return {"output": f"[error] failed to start command: {e}", "exit_code": -1}

    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"output": f"[error] command timed out after {timeout}s",
                "exit_code": -1}

    stdout = out_b.decode("utf-8", errors="backslashreplace")
    stderr = err_b.decode("utf-8", errors="backslashreplace").strip()
    output = stdout.rstrip("\n")
    if stderr:
        output = (output + "\n" if output else "") + f"[stderr] {stderr}"
    return {"output": output, "exit_code": proc.returncode}
