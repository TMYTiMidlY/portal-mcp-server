"""Tests for orchestrator + audit consistency.

Audit findings addressed
------------------------
* ``run_playbook_on_group`` used ``return_exceptions=False``: a single failing
  host blew up ``asyncio.gather`` and crashed the entire fleet operation.
* ``audit_log`` defaults to fail-closed: a write failure raises
  ``RuntimeError`` so the caller learns the operation was not recorded.
  Set ``PORTAL_AUDIT_FAIL_OPEN=1`` to opt out (operation continues with a
  warning) — appropriate for development / test environments.
"""
from __future__ import annotations

import pytest


# ════════════════════════════════════════════════════════════════════════════
#  orchestrator.run_playbook_on_group — failures isolated per host
# ════════════════════════════════════════════════════════════════════════════

class TestPlaybookOnGroupFailureIsolation:
    @pytest.mark.asyncio
    async def test_failing_host_does_not_abort_others(self, monkeypatch, tmp_path):
        from portal_mcp_server import orchestrator
        from portal_mcp_server.connection_manager import ConnectionManager

        # Build a manager with three hosts in the same tag.
        yml = tmp_path / "hosts.yaml"
        yml.write_text("hosts: {}\n")
        m = ConnectionManager(hosts_yaml=yml)
        for h in ("a", "b", "c"):
            m.register_host(h, "127.0.0.1", tags=["fleet"])
        monkeypatch.setattr(orchestrator, "get_manager", lambda: m)

        async def fake_playbook(host_name, playbook):
            if host_name == "b":
                raise RuntimeError("boom on b")
            return {"playbook": playbook["name"], "host": host_name,
                    "steps_run": 1, "elapsed_s": 0.0, "results": []}

        monkeypatch.setattr(orchestrator, "run_playbook", fake_playbook)

        results = await orchestrator.run_playbook_on_group(
            "fleet", {"name": "test", "steps": ["true"]}
        )
        # All three hosts produced a result; b carries an error field.
        assert len(results) == 3
        by_host = {r["host"]: r for r in results}
        assert by_host["a"]["steps_run"] == 1
        assert by_host["c"]["steps_run"] == 1
        assert "error" in by_host["b"]
        assert "boom on b" in by_host["b"]["error"]
        assert by_host["b"]["playbook"] == "test"


# ════════════════════════════════════════════════════════════════════════════
#  audit.audit_log — default fail-closed; PORTAL_AUDIT_FAIL_OPEN=1 → fail-open
# ════════════════════════════════════════════════════════════════════════════

class TestAuditFailClosed:
    def test_default_closed_raises_on_write_error(self, monkeypatch):
        from portal_mcp_server import audit
        monkeypatch.delenv(audit._FAIL_OPEN_ENV, raising=False)
        # Force the file write to fail.
        import builtins
        real_open = builtins.open
        def fake_open(path, *a, **k):
            if str(path).endswith("audit.jsonl"):
                raise OSError("disk full")
            return real_open(path, *a, **k)
        monkeypatch.setattr(builtins, "open", fake_open)

        with pytest.raises(RuntimeError, match="Audit write failed"):
            audit.audit_log("h", "cmd", "ok")

    def test_fail_open_swallows_write_error(self, monkeypatch, caplog):
        from portal_mcp_server import audit
        monkeypatch.setenv(audit._FAIL_OPEN_ENV, "1")
        import builtins
        real_open = builtins.open
        def fake_open(path, *a, **k):
            if str(path).endswith("audit.jsonl"):
                raise OSError("disk full")
            return real_open(path, *a, **k)
        monkeypatch.setattr(builtins, "open", fake_open)

        with caplog.at_level("WARNING"):
            audit.audit_log("h", "cmd", "ok")  # must NOT raise
        assert any("Audit write failed" in rec.message for rec in caplog.records)
