"""
Audit Logger — structured logging of all agent actions.
Writes to <log_dir>/audit.jsonl and optionally to stdout.
"""
import json
import logging
import os
import time

from .paths import default_log_dir

_log_dir = default_log_dir()
_log_dir.mkdir(parents=True, exist_ok=True)
_audit_file = _log_dir / "audit.jsonl"

logger = logging.getLogger("ssh_mcp.audit")

# In-memory ring buffer for recent operations (for observability tools)
_HISTORY_LIMIT = 500
_history: list[dict] = []


def audit_log(host: str, command: str, result,
              agent_id: str = "agent", operation: str = "exec"):
    """Write a structured audit log entry."""
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "agent_id": agent_id,
        "host": host,
        "operation": operation,
        "command": command[:2000],  # truncate huge commands
        "result": str(result)[:500] if result is not None else None,
    }
    # Ring buffer
    _history.append(entry)
    if len(_history) > _HISTORY_LIMIT:
        _history.pop(0)
    # Append to JSONL file
    try:
        with open(_audit_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"Audit write failed: {e}")


def get_history(limit: int = 50, host_filter: str = "") -> list[dict]:
    """Return recent operations, optionally filtered by host."""
    items = _history[-limit:] if not host_filter else [
        e for e in _history if e.get("host") == host_filter
    ][-limit:]
    return list(reversed(items))


def get_audit_stats() -> dict:
    """Return summary statistics for the audit log."""
    from collections import Counter
    hosts = Counter(e["host"] for e in _history)
    ops = Counter(e["operation"] for e in _history)
    return {
        "total_operations": len(_history),
        "top_hosts": dict(hosts.most_common(10)),
        "operation_types": dict(ops),
        "oldest": _history[0]["timestamp"] if _history else None,
        "newest": _history[-1]["timestamp"] if _history else None,
    }
