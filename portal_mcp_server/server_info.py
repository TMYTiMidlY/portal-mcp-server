"""Server-level metadata exposed via portal_audit (view="server" + snapshot).

Static fields (version, pid, python_version, started_at) are evaluated at
module import / process start. ``transport`` is set later by ``cli.main()``
once argparse has decided. ``uptime_s`` is recomputed on every call.

Used as a diagnostic surface so the agent can answer "which version of
portal-mcp-server am I actually talking to?" without needing
``portal_local_exec`` or shell access. Lives in its own module so the
metadata sources stay in one place rather than scattered across cli.py.
"""
from __future__ import annotations

import importlib.metadata
import os
import sys
import time

_STARTED_AT = time.time()
_PID = os.getpid()
_TRANSPORT: str | None = None  # populated by cli.main() once argparse runs


try:
    _VERSION = importlib.metadata.version("portal-mcp-server")
except importlib.metadata.PackageNotFoundError:
    # Editable/uninstalled checkout — pyproject.toml is the source of truth
    # but we don't bother parsing it here; "unknown" is fine for diagnostics.
    _VERSION = "unknown"


def set_transport(transport: str) -> None:
    """Record the chosen MCP transport. Called once from cli.main()."""
    global _TRANSPORT
    _TRANSPORT = transport


def server_info() -> dict:
    """Snapshot of static + dynamic server metadata.

    Returned shape:
        {
          "version":        "<pep440>",         # importlib.metadata
          "python_version": "<major.minor.micro>",
          "pid":            <int>,
          "started_at":     <epoch float>,
          "uptime_s":       <float>,
          "transport":      "stdio" | "streamable_http" | None,
          "config": {
              "hosts_yaml":    "<resolved path>",
              "policies_yaml": "<resolved path>",
              "secrets_yaml":  "<resolved path>",
              "log_dir":       "<resolved path>",
          },
        }
    """
    # Imported lazily so this module stays cheap to import (no Path
    # construction at process start) and dodges any future circular-
    # import risk with paths.py.
    from .paths import (default_log_dir, hosts_yaml_path,
                        policies_yaml_path, secrets_yaml_path)
    now = time.time()
    return {
        "version": _VERSION,
        "python_version": (f"{sys.version_info.major}."
                           f"{sys.version_info.minor}."
                           f"{sys.version_info.micro}"),
        "pid": _PID,
        "started_at": round(_STARTED_AT, 1),
        "uptime_s": round(now - _STARTED_AT, 1),
        "transport": _TRANSPORT,
        "config": {
            "hosts_yaml":    str(hosts_yaml_path()),
            "policies_yaml": str(policies_yaml_path()),
            "secrets_yaml":  str(secrets_yaml_path()),
            "log_dir":       str(default_log_dir()),
        },
    }
