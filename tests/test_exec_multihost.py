"""portal_exec multi-host / multi-command fan-out (absorbed from portal_multi_exec).

The three old multi_exec modes collapse into orthogonal flags on portal_exec:
  parallel  -> host=[...]                       (default)
  rolling   -> host=[...], serialize=True, ...
  broadcast -> host=[...], commands=[...]
Single host + single command returns one dict; any fan-out returns a list, and
a multi-command host carries {host, results:[...]}.
"""
from __future__ import annotations

import json

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from portal_mcp_server import cli, security


@pytest.fixture
def permissive(monkeypatch, tmp_path):
    """A permissive policy (empty allowlists) so gating never blocks."""
    pol = security.SecurityPolicy(policies_yaml=tmp_path / "none.yaml")
    monkeypatch.setattr(cli, "get_policy", lambda: pol)
    return pol


def _fake_exec_factory(record):
    async def fake_exec(host, command, timeout=60):
        record.append((host, command))
        code = 7 if "FAIL" in command else 0
        return {"host": host, "command": command, "exit_code": code,
                "stdout": f"out:{host}", "stderr": "", "elapsed_s": 0.01}
    return fake_exec


@pytest.mark.asyncio
async def test_single_host_single_command_returns_dict(permissive, monkeypatch):
    rec: list = []
    monkeypatch.setattr(cli, "ssh_exec", _fake_exec_factory(rec))
    out = json.loads(await cli.portal_exec(host="web01", command="uptime"))
    assert isinstance(out, dict)
    assert out["host"] == "web01" and out["exit_code"] == 0
    assert rec == [("web01", "uptime")]


@pytest.mark.asyncio
async def test_multi_host_parallel_returns_list(permissive, monkeypatch):
    rec: list = []
    monkeypatch.setattr(cli, "ssh_exec", _fake_exec_factory(rec))
    out = json.loads(await cli.portal_exec(host=["web01", "web02"], command="uptime"))
    assert isinstance(out, list) and len(out) == 2
    assert {r["host"] for r in out} == {"web01", "web02"}
    assert {h for h, _ in rec} == {"web01", "web02"}


@pytest.mark.asyncio
async def test_command_sequence_runs_in_order_per_host(permissive, monkeypatch):
    rec: list = []
    monkeypatch.setattr(cli, "ssh_exec", _fake_exec_factory(rec))
    out = json.loads(await cli.portal_exec(host="web01", commands=["a", "b", "c"]))
    assert out["host"] == "web01"
    assert [r["command"] for r in out["results"]] == ["a", "b", "c"]
    assert [c for _, c in rec] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_sequence_stops_on_error(permissive, monkeypatch):
    rec: list = []
    monkeypatch.setattr(cli, "ssh_exec", _fake_exec_factory(rec))
    out = json.loads(await cli.portal_exec(host="web01", commands=["ok1", "FAIL", "ok2"]))
    assert [c for _, c in rec] == ["ok1", "FAIL"], "must stop after the failing command"
    assert any("stopped" in r.get("info", "") for r in out["results"] if "info" in r)


@pytest.mark.asyncio
async def test_serialize_stops_at_failing_host(permissive, monkeypatch):
    rec: list = []

    async def fake_exec(host, command, timeout=60):
        rec.append(host)
        return {"host": host, "command": command,
                "exit_code": 1 if host == "b" else 0,
                "stdout": "", "stderr": "", "elapsed_s": 0.0}

    monkeypatch.setattr(cli, "ssh_exec", fake_exec)
    out = json.loads(await cli.portal_exec(
        host=["a", "b", "c"], command="x", serialize=True, stop_on_error=True))
    # a runs (ok), b runs (fails) → rollout halts, c never runs.
    assert rec == ["a", "b"]
    assert any(isinstance(e, dict) and "stopped at host" in e.get("info", "")
               for e in out)


@pytest.mark.asyncio
async def test_serialize_does_not_stop_when_stop_on_error_false(permissive, monkeypatch):
    rec: list = []

    async def fake_exec(host, command, timeout=60):
        rec.append(host)
        return {"host": host, "command": command,
                "exit_code": 1 if host == "b" else 0,
                "stdout": "", "stderr": "", "elapsed_s": 0.0}

    monkeypatch.setattr(cli, "ssh_exec", fake_exec)
    await cli.portal_exec(host=["a", "b", "c"], command="x",
                          serialize=True, stop_on_error=False)
    assert rec == ["a", "b", "c"], "all hosts run when stop_on_error=False"


@pytest.mark.asyncio
async def test_missing_target_errors(permissive):
    with pytest.raises(ToolError, match="provide a target"):
        await cli.portal_exec(command="uptime")


@pytest.mark.asyncio
async def test_missing_command_errors(permissive):
    with pytest.raises(ToolError, match="provide command"):
        await cli.portal_exec(host="web01")


@pytest.mark.asyncio
async def test_host_and_group_tag_conflict(permissive):
    with pytest.raises(ToolError, match="either host or group_tag"):
        await cli.portal_exec(host="web01", group_tag="prod", command="uptime")


# ── sudo across the fan-out ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_host_sudo_missing_password_raises(permissive, monkeypatch):
    async def no_pw(host):
        return None
    monkeypatch.setattr("portal_mcp_server.sudo_creds.resolve_sudo_password", no_pw)
    with pytest.raises(ToolError, match="No sudo password"):
        await cli.portal_exec(host="a", command="id", use_sudo=True)


@pytest.mark.asyncio
async def test_multihost_sudo_missing_password_is_per_host_error(permissive, monkeypatch):
    async def no_pw(host):
        return None
    monkeypatch.setattr("portal_mcp_server.sudo_creds.resolve_sudo_password", no_pw)
    out = json.loads(await cli.portal_exec(host=["a", "b"], command="id", use_sudo=True))
    assert isinstance(out, list) and len(out) == 2
    assert all("no sudo password" in r.get("error", "") for r in out)


# ── parallel fan-out: one host raising is isolated, not fatal ────────────────

@pytest.mark.asyncio
async def test_parallel_fanout_isolates_one_host_exception(permissive, monkeypatch):
    """An exception raised for one host in the parallel fan-out is converted to
    {host, error, exit_code:-1} (via gather(return_exceptions=True)) and must
    NOT crash the batch — the healthy host's result still comes back."""
    async def fake_exec(host, command, timeout=60):
        if host == "bad":
            raise RuntimeError("boom on bad")
        return {"host": host, "command": command, "exit_code": 0,
                "stdout": "ok", "stderr": "", "elapsed_s": 0.0}

    monkeypatch.setattr(cli, "ssh_exec", fake_exec)
    out = json.loads(await cli.portal_exec(host=["good", "bad"], command="x"))
    by_host = {r["host"]: r for r in out}
    assert by_host["good"]["exit_code"] == 0
    assert by_host["bad"]["exit_code"] == -1
    assert "boom on bad" in by_host["bad"]["error"]
