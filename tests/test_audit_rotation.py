"""Audit log moved to a stdlib RotatingFileHandler for mature size-based
rotation, subclassed to keep the fail-closed guarantee (re-raise instead of
swallow). These tests pin both halves."""
from __future__ import annotations

import json
import logging

import pytest

from portal_mcp_server import audit


def _logger(name, handler):
    lg = logging.getLogger(name)
    lg.handlers = []
    lg.propagate = False
    lg.setLevel(logging.INFO)
    lg.addHandler(handler)
    return lg


def test_rotating_handler_rotates(tmp_path):
    f = tmp_path / "audit.jsonl"
    h = audit._FailClosedRotatingHandler(
        str(f), maxBytes=200, backupCount=3, encoding="utf-8", delay=True)
    lg = _logger("test.audit.rot", h)
    for i in range(60):
        lg.info(json.dumps({"i": i, "pad": "x" * 40}))
    h.close()
    assert f.exists()
    assert (tmp_path / "audit.jsonl.1").exists()  # rotation actually happened


def test_handler_reraises_on_write_failure(tmp_path):
    # parent is a regular file -> opening the log path fails on first emit
    (tmp_path / "blocker").write_text("not a directory")
    bad = tmp_path / "blocker" / "audit.jsonl"
    h = audit._FailClosedRotatingHandler(str(bad), delay=True)
    lg = _logger("test.audit.fc", h)
    with pytest.raises(Exception):
        lg.info("x")


def test_audit_log_fail_closed_raises(monkeypatch):
    class Boom:
        def info(self, msg):
            raise OSError("disk full")

    monkeypatch.setattr(audit, "_audit_writer", Boom())
    monkeypatch.delenv("PORTAL_AUDIT_FAIL_OPEN", raising=False)
    with pytest.raises(RuntimeError):
        audit.audit_log("h", "cmd", "ok")


def test_audit_log_fail_open_swallows(monkeypatch):
    class Boom:
        def info(self, msg):
            raise OSError("disk full")

    monkeypatch.setattr(audit, "_audit_writer", Boom())
    monkeypatch.setenv("PORTAL_AUDIT_FAIL_OPEN", "1")
    audit.audit_log("h", "cmd", "ok")  # must NOT raise


def test_audit_log_still_fills_history(monkeypatch):
    """The in-memory ring buffer (inspect views) is independent of the
    file write and must still be populated."""
    class Sink:
        def info(self, msg):
            pass

    monkeypatch.setattr(audit, "_audit_writer", Sink())
    before = len(audit._history)
    audit.audit_log("h7", "echo hi", "ok", operation="exec")
    assert len(audit._history) == before + 1
    assert audit._history[-1]["host"] == "h7"
