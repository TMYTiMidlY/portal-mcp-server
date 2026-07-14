"""
Security Controls — host allowlist, command blocking, rate limiting, policy enforcement.
Loads policy from the file resolved by paths.policies_yaml_path() (default
~/.config/portal-mcp-server/policies.yaml; override via PORTAL_POLICIES_YAML).
"""
import fnmatch
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import yaml

from .safety import normalize_host_name

logger = logging.getLogger("portal_mcp.security")


class SecurityPolicy:
    def __init__(self, policies_yaml: str | os.PathLike | None = None):
        from .paths import policies_yaml_path
        from .safety_net import SafetyNetChecker
        self.host_allowlist: list[str] = []       # empty = all allowed
        self.command_blocklist: list[str] = []    # patterns of blocked commands
        self.command_allowlist: list[str] = []    # if set, only these allowed
        self.rate_limit_rps: float = 10.0         # requests per second per host
        self._rate_counters: dict[str, list[float]] = defaultdict(list)
        # Optional semantic gate (cc-safety-net). Disabled until policies.yaml
        # enables it, so a server with no config keeps its permissive defaults.
        self.safety_net = SafetyNetChecker(enabled=False)
        path = str(policies_yaml) if policies_yaml else str(policies_yaml_path())
        self._load(path)

    def _load(self, path: str):
        from .safety_net import SafetyNetChecker
        p = Path(path)
        if not p.exists():
            logger.warning(f"policies.yaml not found at {p}, using permissive defaults")
            return
        with open(p) as f:
            data = yaml.safe_load(f) or {}
        pol = data.get("policies", {})
        self.host_allowlist = pol.get("host_allowlist", [])
        self.command_blocklist = pol.get("command_blocklist", [])
        self.command_allowlist = pol.get("command_allowlist", [])
        self.rate_limit_rps = float(pol.get("rate_limit_rps", 10.0))
        self.safety_net = SafetyNetChecker.from_config(pol.get("safety_net"))
        logger.info(
            "Security policies loaded (safety_net=%s)",
            "on" if self.safety_net.enabled else "off",
        )

    def check_host(self, host_name: str) -> Optional[str]:
        """Returns error string if host is blocked, None if allowed."""
        host_name = normalize_host_name(host_name)
        if not self.host_allowlist:
            return None
        for pattern in self.host_allowlist:
            if fnmatch.fnmatch(host_name, pattern):
                return None
        return f"Host '{host_name}' is not in the allowlist"

    async def check_command(self, command: str) -> Optional[str]:
        """Returns error string if command is blocked, None if allowed.

        Async because the optional semantic gate (``safety_net.check``) shells
        out to a subprocess; running it synchronously would freeze the MCP
        server's event loop for the whole ``timeout_s``.
        """
        cmd_lower = command.lower().strip()
        for pattern in self.command_blocklist:
            if fnmatch.fnmatch(cmd_lower, pattern.lower()):
                return f"Command blocked by policy: matches '{pattern}'"
        # Semantic Safety Net layer (cc-safety-net), opt-in. Runs as
        # defense-in-depth BEFORE the allowlist short-circuit, so a
        # semantically destructive command (e.g. `bash -c 'git reset --hard'`)
        # is caught even when an allowlist would otherwise wave it through.
        sn_err = await self.safety_net.check(command)
        if sn_err:
            return sn_err
        if self.command_allowlist:
            for pattern in self.command_allowlist:
                if fnmatch.fnmatch(cmd_lower, pattern.lower()):
                    return None
            return "Command not in allowlist"
        return None

    def check_rate_limit(self, host_name: str) -> Optional[str]:
        """Sliding window rate limiter per host. Returns error or None."""
        now = time.time()
        window = 1.0  # 1-second window
        calls = self._rate_counters[host_name]
        # Prune old entries
        calls[:] = [t for t in calls if now - t < window]
        if len(calls) >= self.rate_limit_rps:
            return f"Rate limit exceeded for host '{host_name}' ({self.rate_limit_rps} req/s)"
        calls.append(now)
        return None

    async def enforce(self, host_name: str, command: str = "",
                      *, commit_rate_limit: bool = True) -> Optional[str]:
        """Run all checks. Returns first error found, or None if all pass.

        ``commit_rate_limit=False`` runs the host + command checks but does NOT
        consume a rate-limit token — used by the ``policy_check`` dry-run so a
        pre-flight check never burns the real operation's quota (or
        self-throttles into a spurious "Rate limit exceeded").

        Async because ``check_command`` is async (it may invoke the
        ``safety_net`` subprocess gate).
        """
        err = self.check_host(host_name)
        if err:
            return err
        if command:
            err = await self.check_command(command)
            if err:
                return err
        if commit_rate_limit:
            err = self.check_rate_limit(host_name)
            if err:
                return err
        return None


_policy: Optional[SecurityPolicy] = None

def get_policy() -> SecurityPolicy:
    global _policy
    if _policy is None:
        _policy = SecurityPolicy()
    return _policy
