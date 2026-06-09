"""
Audit Logger — structured logging of all agent actions.
Writes JSONL to <log_dir>/audit.jsonl via a size-rotating stdlib logging
handler (rotates to audit.jsonl.1 .. .N). Tunables:

* ``PORTAL_AUDIT_MAX_BYTES`` — rotate after this many bytes (default 10 MiB).
* ``PORTAL_AUDIT_BACKUPS``   — how many rotated files to keep (default 5).

Failure-mode policy
-------------------
By default a write failure raises a :class:`RuntimeError` which propagates
back to the caller, **aborting the operation**. This fail-closed default
matches the cybersecurity positioning advertised in the README ("every
state-changing operation is recorded") — if the audit log cannot be
written, we refuse to act. Because stdlib logging handlers normally swallow
write errors, the rotating handler is subclassed to re-raise them
(:class:`_FailClosedRotatingHandler`) so this guarantee survives the move to
``logging.handlers``.

Set the environment variable ``PORTAL_AUDIT_FAIL_OPEN=1`` to switch to
fail-open behaviour (write failure is logged but the operation proceeds).
This is appropriate for development / test environments where audit
durability is not required.
"""
import json
import logging
import logging.handlers
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


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


class _FailClosedRotatingHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that PROPAGATES write/rotate errors instead of
    swallowing them. The stock handler routes failures to ``handleError``,
    which prints to stderr and returns; re-raising there is what lets
    ``audit_log`` keep its fail-closed guarantee — a failed audit write must
    surface and abort the operation, not vanish.
    """

    def handleError(self, record):  # noqa: D102 - see class docstring
        raise


# Mature size-based rotation via stdlib logging (audit.jsonl -> .1 .. .N) on a
# dedicated, non-propagating logger so the JSON lines are written verbatim and
# the warning ``logger`` above stays a separate stream. Defaults: 10 MiB x 5.
_AUDIT_MAX_BYTES = _int_env("PORTAL_AUDIT_MAX_BYTES", 10 * 1024 * 1024)
_AUDIT_BACKUPS = _int_env("PORTAL_AUDIT_BACKUPS", 5)

_audit_writer = logging.getLogger("portal_mcp.audit.jsonl")
_audit_writer.setLevel(logging.INFO)
_audit_writer.propagate = False
if not _audit_writer.handlers:
    _h = _FailClosedRotatingHandler(
        _audit_file, maxBytes=_AUDIT_MAX_BYTES, backupCount=_AUDIT_BACKUPS,
        encoding="utf-8", delay=True,
    )
    _h.setFormatter(logging.Formatter("%(message)s"))
    _audit_writer.addHandler(_h)

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
    # Append to the JSONL file through a size-rotating, fail-closed logging
    # handler (_FailClosedRotatingHandler). Rotation is stdlib; re-raising in the
    # handler's handleError keeps the fail-closed guarantee, so a failed write
    # surfaces here and aborts the operation unless PORTAL_AUDIT_FAIL_OPEN is set.
    try:
        _audit_writer.info(json.dumps(entry))
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
