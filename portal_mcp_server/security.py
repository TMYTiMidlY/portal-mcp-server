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

logger = logging.getLogger("portal_mcp.security")


class SecurityPolicy:
    def __init__(self, policies_yaml: str | os.PathLike | None = None):
        from .paths import policies_yaml_path
        self.host_allowlist: list[str] = []       # empty = all allowed
        self.command_blocklist: list[str] = []    # patterns of blocked commands
        self.command_allowlist: list[str] = []    # if set, only these allowed
        self.rate_limit_rps: float = 10.0         # requests per second per host
        self._rate_counters: dict[str, list[float]] = defaultdict(list)
        path = str(policies_yaml) if policies_yaml else str(policies_yaml_path())
        self._load(path)

    def _load(self, path: str):
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
        logger.info("Security policies loaded")

    def check_host(self, host_name: str) -> Optional[str]:
        """Returns error string if host is blocked, None if allowed."""
        if not self.host_allowlist:
            return None
        for pattern in self.host_allowlist:
            if fnmatch.fnmatch(host_name, pattern):
                return None
        return f"Host '{host_name}' is not in the allowlist"

    def check_command(self, command: str) -> Optional[str]:
        """Returns error string if command is blocked, None if allowed."""
        cmd_lower = command.lower().strip()
        for pattern in self.command_blocklist:
            if fnmatch.fnmatch(cmd_lower, pattern.lower()):
                return f"Command blocked by policy: matches '{pattern}'"
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

    def enforce(self, host_name: str, command: str = "",
                *, commit_rate_limit: bool = True) -> Optional[str]:
        """Run all checks. Returns first error found, or None if all pass.

        ``commit_rate_limit=False`` runs the host + command checks but does NOT
        consume a rate-limit token — used by the ``portal_check`` dry-run so a
        pre-flight check never burns the real operation's quota (or
        self-throttles into a spurious "Rate limit exceeded").
        """
        err = self.check_host(host_name)
        if err:
            return err
        if command:
            err = self.check_command(command)
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
