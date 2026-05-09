"""Tests for orchestrator + audit consistency.

Audit findings addressed
------------------------
* ``run_playbook_on_group`` used ``return_exceptions=False``: a single failing
  host blew up ``asyncio.gather`` and crashed the entire fleet operation.
* ``audit_log`` only ever logged a warning on write failure. For deployments
  where the audit log is a compliance requirement, we now offer a fail-closed
  mode driven by the ``SSH_MCP_AUDIT_FAIL_CLOSED`` env var.
"""
from __future__ import annotations

import pytest


# ════════════════════════════════════════════════════════════════════════════
#  orchestrator.run_playbook_on_group — failures isolated per host
# ════════════════════════════════════════════════════════════════════════════

class TestPlaybookOnGroupFailureIsolation:
    @pytest.mark.asyncio
    async def test_failing_host_does_not_abort_others(self, monkeypatch, tmp_path):
        from ssh_remote_mcp import orchestrator
        from ssh_remote_mcp.connection_manager import ConnectionManager

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
#  audit.audit_log — fail-closed mode raises on write failure
# ════════════════════════════════════════════════════════════════════════════

class TestAuditFailClosed:
    def test_default_open_swallows_write_error(self, monkeypatch, caplog):
        from ssh_remote_mcp import audit
        monkeypatch.delenv(audit._FAIL_CLOSED_ENV, raising=False)
        # Force the file write to fail.
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

    def test_fail_closed_raises(self, monkeypatch):
        from ssh_remote_mcp import audit
        monkeypatch.setenv(audit._FAIL_CLOSED_ENV, "1")
        import builtins
        real_open = builtins.open
        def fake_open(path, *a, **k):
            if str(path).endswith("audit.jsonl"):
                raise OSError("disk full")
            return real_open(path, *a, **k)
        monkeypatch.setattr(builtins, "open", fake_open)

        with pytest.raises(RuntimeError, match="Audit write failed"):
            audit.audit_log("h", "cmd", "ok")
