"""
Audit Logger — structured logging of all agent actions.
Writes to <log_dir>/audit.jsonl and optionally to stdout.

Failure-mode policy
-------------------
By default a write failure raises a :class:`RuntimeError` which propagates
back to the caller, **aborting the operation**. This fail-closed default
matches the cybersecurity positioning advertised in the README ("every
state-changing operation is recorded") — if the audit log cannot be
written, we refuse to act.

Set the environment variable ``PORTAL_AUDIT_FAIL_OPEN=1`` to switch to
fail-open behaviour (write failure is logged but the operation proceeds).
This is appropriate for development / test environments where audit
durability is not required.
"""
import json
import logging
import os
import time

from .paths import default_log_dir

_log_dir = default_log_dir()
_log_dir.mkdir(parents=True, exist_ok=True)
_audit_file = _log_dir / "audit.jsonl"

logger = logging.getLogger("portal_mcp.audit")

_FAIL_OPEN_ENV = "PORTAL_AUDIT_FAIL_OPEN"


def _fail_closed() -> bool:
    """Read the fail-closed flag at call time so tests / runtime can flip it.

    Default is fail-closed (returns True). Setting ``PORTAL_AUDIT_FAIL_OPEN``
    to a truthy value switches to fail-open (returns False).
    """
    return os.environ.get(_FAIL_OPEN_ENV, "").lower() not in (
        "1", "true", "yes", "on",
    )

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
    # Append to JSONL file.
    # ADR — why direct JSONL writes, not logging.handlers.*: stdlib logging
    # handlers SWALLOW write errors (Handler.handleError prints to stderr and
    # returns), which is incompatible with the fail-closed guarantee above — a
    # failed audit write must raise and abort the operation. We also keep the
    # in-memory ring buffer (_history) for portal_audit. Future enhancement:
    # size-based rotation (RotatingFileHandler-style) for unbounded audit.jsonl.
    try:
        with open(_audit_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"Audit write failed: {e}")
        if _fail_closed():
            raise RuntimeError(
                f"Audit write failed and {_FAIL_OPEN_ENV} is not set: {e}"
            ) from e


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
