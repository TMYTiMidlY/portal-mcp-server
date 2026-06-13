"""Optional semantic command gate backed by **cc-safety-net**.

Why this module exists
----------------------
``portal_exec`` / ``portal_local_exec`` / ``portal_shell`` run shell commands
that never pass through the agent's own ``bash`` tool. The Copilot-CLI
cc-safety-net PreToolUse hook only inspects ``toolName == "bash"`` (see its
``isSupported`` adapter), so every command issued through a portal_* MCP tool
slips past the safety net unchecked. This module re-applies the *same* engine on
the server side by shelling out to ``cc-safety-net explain --json`` before a
command is dispatched, so a ``git reset --hard`` / ``rm -rf ~`` / blocked custom
rule is caught no matter which portal_* tool the agent picks.

Design choices
--------------
* **Opt-in.** Only active when ``policies.safety_net.enabled: true``. When
  disabled the server behaves exactly as before, with zero subprocess cost.
* **Reuse, don't reimplement.** cc-safety-net's value is *bypass-resistant
  semantic* analysis — it unwraps shell wrappers (``bash -c "..."``), detects
  interpreter one-liners (``python -c "..."``), and does real ``rm`` path
  analysis. Re-coding that in Python would silently drift and reintroduce the
  very bypasses it closes, so we invoke the real binary.
* **Fail-closed (default).** If the checker is *configured* but cannot produce a
  verdict (binary missing, timeout, crash, unparseable output) the command is
  REFUSED with an actionable message. A broken safety net must never silently
  degrade into an open door. Set ``fail_closed: false`` to allow-through instead.
* **Ordered before any SSH/local work.** The policy gate runs *before* any
  SSH/local execution, so this adds one short-lived subprocess (~100-300 ms) per
  command at gate time, bounded by ``timeout_s``. It does not interleave with the
  command's own runtime or the keepalive heartbeat. The check is implemented as
  ``async`` (``asyncio.create_subprocess_exec``) so the MCP server's event loop
  keeps scheduling other coroutines while the npx subprocess runs — a sync
  ``subprocess.run`` here would freeze the entire process for ``timeout_s``
  seconds (see ``CONTRIBUTING.md`` §代码规范).

``explain`` always exits 0 (it is a debug/analysis subcommand) — the verdict is
the JSON ``result`` field (``"allowed"`` / ``"blocked"``), never the exit code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("portal_mcp.safety_net")

# Default invocation. ``@latest`` is intentional (per operator request): each run
# uses the npx-cached resolution, refreshed against the registry. Pin a version
# or point at a local binary via ``safety_net.command`` if you need offline
# determinism — remember that with ``fail_closed: true`` a checker that cannot be
# resolved (e.g. offline) blocks every gated command.
_DEFAULT_COMMAND: tuple[str, ...] = ("npx", "-y", "cc-safety-net@latest")
_DEFAULT_TIMEOUT_S = 15.0


@dataclass
class SafetyNetChecker:
    """A thin, reusable wrapper around ``cc-safety-net explain --json``.

    Construct via :meth:`from_config` with the ``policies.safety_net`` mapping.
    When :attr:`enabled` is false, :meth:`check` is a no-op returning ``None``.
    """

    enabled: bool = False
    command: list[str] = field(default_factory=lambda: list(_DEFAULT_COMMAND))
    fail_closed: bool = True
    rulebook_cwd: Optional[str] = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = _DEFAULT_TIMEOUT_S

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "SafetyNetChecker":
        """Build a checker from the ``policies.safety_net`` mapping.

        Tolerant of missing / malformed fields: anything unusable falls back to
        a safe default and is logged, never raised — a config typo must not crash
        the server at startup.
        """
        if not cfg or not isinstance(cfg, dict):
            return cls(enabled=False)

        command = cfg.get("command") or list(_DEFAULT_COMMAND)
        if isinstance(command, str):
            command = [command]
        if (not isinstance(command, list)
                or not command
                or not all(isinstance(x, str) for x in command)):
            logger.warning(
                "safety_net.command invalid (%r); falling back to default %r",
                cfg.get("command"), list(_DEFAULT_COMMAND),
            )
            command = list(_DEFAULT_COMMAND)

        env_raw = cfg.get("env") or {}
        env: dict[str, str] = (
            {str(k): str(v) for k, v in env_raw.items()}
            if isinstance(env_raw, dict) else {}
        )

        rulebook_cwd = cfg.get("rulebook_cwd")
        rulebook_cwd = str(rulebook_cwd) if rulebook_cwd else None

        try:
            timeout_s = float(cfg.get("timeout_s", _DEFAULT_TIMEOUT_S))
            if timeout_s <= 0:
                raise ValueError
        except (TypeError, ValueError):
            logger.warning(
                "safety_net.timeout_s invalid (%r); using default %s",
                cfg.get("timeout_s"), _DEFAULT_TIMEOUT_S,
            )
            timeout_s = _DEFAULT_TIMEOUT_S

        return cls(
            enabled=bool(cfg.get("enabled", False)),
            command=list(command),
            fail_closed=bool(cfg.get("fail_closed", True)),
            rulebook_cwd=rulebook_cwd,
            env=env,
            timeout_s=timeout_s,
        )

    # ── the gate ────────────────────────────────────────────────────────────
    async def check(self, command: str) -> Optional[str]:
        """Return ``None`` if the command is allowed, else a block reason string.

        * Disabled checker → always ``None``.
        * Empty/blank command → ``None`` (nothing to analyze).
        * ``result == "allowed"`` → ``None``.
        * ``result == "blocked"`` → the cc-safety-net reason, prefixed.
        * Checker cannot run / unparseable → fail-closed refusal string (or
          ``None`` when ``fail_closed`` is false).

        Async by design: this runs inside the asyncio event loop of the MCP
        server, so a blocking ``subprocess.run`` here would freeze the entire
        loop for ``timeout_s`` seconds (no other tool call processed, no
        progress notifications, no keep-alive heartbeat) — see
        ``CONTRIBUTING.md`` §代码规范. ``asyncio.create_subprocess_exec`` lets
        the loop keep scheduling other coroutines while the npx subprocess
        runs in the kernel.
        """
        if not self.enabled:
            return None
        if not command or not command.strip():
            return None

        argv = [*self.command, "explain", "--json", command]
        run_env = {**os.environ, **self.env}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.rulebook_cwd,  # None → inherit the server's cwd
                env=run_env,
            )
        except FileNotFoundError:
            return self._unavailable(
                f"the checker binary {self.command[0]!r} was not found on PATH")
        except OSError as e:
            return self._unavailable(
                f"the checker could not be launched ({type(e).__name__}: {e})")

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            # Clean up the runaway process so we do not leak a zombie / a
            # backgrounded npx that keeps holding pipes open.
            await self._terminate(proc)
            return self._unavailable(
                f"the checker timed out after {self.timeout_s}s")

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        out = stdout.strip()
        if not out:
            err = stderr.strip()
            return self._unavailable(
                f"the checker produced no output (exit {proc.returncode})"
                + (f"; stderr: {err[:200]!r}" if err else ""))

        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            return self._unavailable(
                f"the checker output was not valid JSON (exit {proc.returncode})")

        result = data.get("result") if isinstance(data, dict) else None
        if result == "allowed":
            return None
        if result == "blocked":
            reason = (isinstance(data, dict) and data.get("reason")) \
                or "command blocked by Safety Net"
            return f"Safety Net blocked this command: {reason}"
        return self._unavailable(
            f"the checker returned an unrecognized verdict {result!r}")

    @staticmethod
    async def _terminate(proc: "asyncio.subprocess.Process") -> None:
        """Best-effort kill of a stuck subprocess (timeout path)."""
        for sig in ("terminate", "kill"):
            try:
                getattr(proc, sig)()
            except (ProcessLookupError, OSError):
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                return
            except asyncio.TimeoutError:
                continue
            except ProcessLookupError:
                return

    # ── fail-closed messaging ────────────────────────────────────────────────
    def _unavailable(self, detail: str) -> Optional[str]:
        """Decide what to do when no verdict could be obtained.

        Fail-closed: return an actionable refusal (the caller turns it into a
        BLOCKED tool error). Fail-open: log and return ``None`` (allow through).
        """
        if self.fail_closed:
            logger.error("Safety Net fail-closed (command refused): %s", detail)
            return (
                "NOT executed — the Safety Net command check could not run, so "
                f"the command was refused (fail-closed). Cause: {detail}. "
                "To fix: verify the `safety_net` block in policies.yaml "
                "(`command` / `timeout_s`), confirm cc-safety-net is installed "
                "and runnable (`npx -y cc-safety-net@latest doctor`), or set "
                "`safety_net.fail_closed: false` to allow commands through when "
                "the checker is unavailable."
            )
        logger.warning(
            "Safety Net unavailable; failing open (command allowed): %s", detail)
        return None
