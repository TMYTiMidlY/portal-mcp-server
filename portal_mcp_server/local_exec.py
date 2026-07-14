"""local_exec — run a one-shot command on the *MCP server host* with named
secrets injected as environment variables.

Unlike every other portal_* capability (which runs over SSH on a remote host),
this executes locally — which **departs from** this project's core goal of
driving *remote* hosts as if they were local. It's a useful but off-target
derivative (it happens to reuse the secret / sudo credential machinery), so the
``local_exec`` tool is OFF by default and must be explicitly enabled with
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


async def local_sudo_exec_with_env(command: str, password: str, env: dict,
                                   timeout: float = 600.0) -> dict:
    """Run ``command`` under ``sudo`` on the *local* host, feeding ``password``
    on stdin.

    Mirrors :func:`local_exec_with_env` but wraps the command in
    ``sudo -S -k -p '' -- bash -c <body>`` and writes the password to the
    child's **stdin** (never on argv, so it stays out of ``ps`` and the audit
    log). ``-k`` forces a fresh auth so a cached sudo ticket can't mask a wrong
    password; ``-p ''`` suppresses the prompt text.

    ``env`` maps already-resolved ``ENV_VAR_NAME -> value``. Secrets are NOT
    injected via the process environment (sudo's ``env_reset`` would strip them);
    instead each value is fed on stdin right after the password and read back
    inside the elevated shell (see
    :func:`secrets_store.sudo_stdin_secret_script`), so the value reaches the
    command without any sudoers config and without touching argv. Returns
    ``{output, exit_code}``; the caller redacts the output.
    """
    from .safety import quote_shell
    from . import secrets_store

    names = list(env.keys())
    body = secrets_store.sudo_stdin_secret_script(command, names)
    wrapped = f"sudo -S -k -p '' -- bash -c {quote_shell(body)}"
    stdin_data = password + "\n" + secrets_store.sudo_stdin_secret_values(
        [env[n] for n in names])
    try:
        proc = await asyncio.create_subprocess_shell(
            wrapped,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return {"output": f"[error] failed to start command: {e}", "exit_code": -1}

    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode()), timeout=timeout)
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
