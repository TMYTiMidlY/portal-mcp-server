"""portal_job — background (fire-and-poll) execution lifecycle (L1).

These mock the SSH connection so the JobManager's submit/poll/cancel/list and
the TTL sweep can be exercised without a live host. The fake conn routes by
command substring (the submit spawn ends with `echo $!`, poll uses a `__CHUNK__`
marker, cancel uses `kill -`).
"""
from __future__ import annotations

import json
import time

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from portal_mcp_server import connection_manager as cm
from portal_mcp_server import job_manager, cli, security


def _install_conn(monkeypatch, router):
    recorded: list[str] = []

    class _Result:
        def __init__(self, out, err=""):
            self.stdout = out
            self.stderr = err
            self.returncode = 0

    class _Conn:
        async def run(self, cmd, **k):
            recorded.append(cmd)
            return _Result(router(cmd))

    async def fake_get(self, host):
        return _Conn()

    def fake_release(self, host, conn):
        pass

    monkeypatch.setattr(cm.ConnectionManager, "get_connection", fake_get)
    monkeypatch.setattr(cm.ConnectionManager, "release_connection", fake_release)
    return recorded


def _poll_out(meta, size, alive, chunk):
    """Build a fake poll stdout. `chunk` is plain text/bytes; the real poll
    command base64-encodes it on the wire, so we do the same here."""
    import base64
    raw = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
    b64 = base64.b64encode(raw).decode("ascii")
    return f"META:{meta}\nSIZE:{size}\nALIVE:{alive}\n__CHUNK__\n{b64}"


# ── submit ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_returns_job_id_and_pid(monkeypatch):
    _install_conn(monkeypatch, lambda c: "4242\n" if "echo $!" in c else "")
    jm = job_manager.JobManager()
    res = await jm.submit("h", "sleep 100")
    assert res["job_id"].startswith("job-")
    assert res["remote_pid"] == 4242
    assert res["status"] == "running"
    assert res["host"] == "h"


@pytest.mark.asyncio
async def test_submit_no_pid_raises(monkeypatch):
    _install_conn(monkeypatch, lambda c: "")  # nothing on stdout
    jm = job_manager.JobManager()
    with pytest.raises(RuntimeError, match="could not start"):
        await jm.submit("h", "x")


@pytest.mark.asyncio
async def test_submit_respects_max_live(monkeypatch):
    monkeypatch.setenv("PORTAL_JOB_MAX_LIVE", "1")
    _install_conn(monkeypatch, lambda c: "5\n" if "echo $!" in c else "")
    jm = job_manager.JobManager()
    await jm.submit("h", "a")
    with pytest.raises(RuntimeError, match="too many live jobs"):
        await jm.submit("h", "b")


# ── poll ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_running_then_done(monkeypatch):
    phase = {"v": "running"}

    def router(c):
        if "echo $!" in c:
            return "100\n"
        if "__CHUNK__" in c:
            if phase["v"] == "running":
                return _poll_out("", 5, "yes", "hello")
            # done: since=5, the new bytes are " world" (6) -> total size 11.
            return _poll_out("__JOB_DONE__:0", 11, "no", " world")
        return ""

    _install_conn(monkeypatch, router)
    jm = job_manager.JobManager()
    jid = (await jm.submit("h", "x"))["job_id"]

    p1 = await jm.poll(jid)
    assert p1["status"] == "running"
    assert p1["output_chunk"] == "hello"
    assert p1["new_offset"] == 5
    assert p1["more"] is False  # new_offset (5) == size (5)
    assert "exit_code" not in p1

    phase["v"] = "done"
    p2 = await jm.poll(jid, since=5)
    assert p2["status"] == "done"
    assert p2["exit_code"] == 0
    assert p2["output_chunk"] == " world"
    assert p2["new_offset"] == 11  # since(5) + 6 new bytes
    assert "finished_at" in p2


@pytest.mark.asyncio
async def test_poll_failed_exit_code(monkeypatch):
    def router(c):
        if "echo $!" in c:
            return "100\n"
        if "__CHUNK__" in c:
            return _poll_out("__JOB_DONE__:7", 3, "no", "err")
        return ""

    _install_conn(monkeypatch, router)
    jm = job_manager.JobManager()
    jid = (await jm.submit("h", "x"))["job_id"]
    p = await jm.poll(jid)
    assert p["status"] == "failed"
    assert p["exit_code"] == 7


@pytest.mark.asyncio
async def test_poll_unknown_job(monkeypatch):
    _install_conn(monkeypatch, lambda c: "")
    jm = job_manager.JobManager()
    p = await jm.poll("job-doesnotexist")
    assert p["status"] == "unknown"
    assert "no such job_id" in p["error"]


@pytest.mark.asyncio
async def test_poll_process_gone_without_meta_is_unknown(monkeypatch):
    def router(c):
        if "echo $!" in c:
            return "100\n"
        if "__CHUNK__" in c:
            return _poll_out("", 0, "no", "")  # dead, no exit recorded
        return ""

    _install_conn(monkeypatch, router)
    jm = job_manager.JobManager()
    jid = (await jm.submit("h", "x"))["job_id"]
    p = await jm.poll(jid)
    assert p["status"] == "unknown"


# ── cancel ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_sends_signal_and_marks_cancelled(monkeypatch):
    rec = _install_conn(monkeypatch, lambda c: "100\n" if "echo $!" in c else "")
    jm = job_manager.JobManager()
    jid = (await jm.submit("h", "x"))["job_id"]
    res = await jm.cancel(jid, signal="KILL")
    assert res["signal_sent"] is True
    assert res["signal"] == "KILL"
    assert res["status_after"] == "cancelled"
    assert any("kill -KILL 100" in c for c in rec)


@pytest.mark.asyncio
async def test_cancel_unknown_job(monkeypatch):
    _install_conn(monkeypatch, lambda c: "")
    jm = job_manager.JobManager()
    res = await jm.cancel("job-nope")
    assert res["signal_sent"] is False
    assert res["status_after"] == "unknown"


@pytest.mark.asyncio
async def test_cancelled_job_stays_cancelled_on_poll(monkeypatch):
    def router(c):
        if "echo $!" in c:
            return "100\n"
        if "__CHUNK__" in c:
            return _poll_out("", 0, "no", "")  # no exit recorded; we cancelled
        return ""

    _install_conn(monkeypatch, router)
    jm = job_manager.JobManager()
    jid = (await jm.submit("h", "x"))["job_id"]
    await jm.cancel(jid)
    p = await jm.poll(jid)
    assert p["status"] == "cancelled"


# ── list + TTL sweep ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_jobs(monkeypatch):
    _install_conn(monkeypatch, lambda c: "100\n" if "echo $!" in c else "")
    jm = job_manager.JobManager()
    j1 = (await jm.submit("h", "a"))["job_id"]
    j2 = (await jm.submit("h", "b"))["job_id"]
    jobs = await jm.list_jobs()
    ids = {j["job_id"] for j in jobs}
    assert {j1, j2} <= ids
    assert all("started_at" in j and "age_s" in j for j in jobs)


@pytest.mark.asyncio
async def test_ttl_sweep_removes_finished_and_cleans_remote(monkeypatch):
    rec = _install_conn(monkeypatch, lambda c: "100\n" if "echo $!" in c else "")
    jm = job_manager.JobManager()
    jid = (await jm.submit("h", "x"))["job_id"]
    # Force it terminal + finished long ago so the TTL sweep collects it.
    job = jm._jobs[jid]
    job.status = "done"
    job.finished_at = time.time() - 99999

    jobs = await jm.list_jobs()
    assert all(j["job_id"] != jid for j in jobs), "expired job should be swept"
    assert any("rm -f" in c for c in rec), "remote tmp files should be cleaned"


# ── cli portal_job dispatch + gating ────────────────────────────────────────

@pytest.fixture
def permissive(monkeypatch, tmp_path):
    pol = security.SecurityPolicy(policies_yaml=tmp_path / "none.yaml")
    monkeypatch.setattr(cli, "get_policy", lambda: pol)
    return pol


@pytest.mark.asyncio
async def test_cli_submit_requires_host_and_command(permissive):
    with pytest.raises(ToolError, match="requires"):
        await cli.portal_job(action="submit", command="x")


@pytest.mark.asyncio
async def test_cli_submit_blocked_command(monkeypatch, tmp_path):
    pol = security.SecurityPolicy(policies_yaml=tmp_path / "p.yaml")
    pol.command_blocklist = ["rm -rf*"]
    monkeypatch.setattr(cli, "get_policy", lambda: pol)
    _install_conn(monkeypatch, lambda c: "1\n")
    # fresh job manager so the singleton isn't polluted
    monkeypatch.setattr(cli, "get_job_manager", lambda: job_manager.JobManager())
    with pytest.raises(ToolError, match="BLOCKED"):
        await cli.portal_job(action="submit", host="h", command="rm -rf /")


@pytest.mark.asyncio
async def test_cli_submit_and_list_roundtrip(monkeypatch, permissive):
    _install_conn(monkeypatch, lambda c: "77\n" if "echo $!" in c else "")
    jm = job_manager.JobManager()
    monkeypatch.setattr(cli, "get_job_manager", lambda: jm)
    out = json.loads(await cli.portal_job(action="submit", host="h", command="sleep 1"))
    assert out["remote_pid"] == 77
    listed = json.loads(await cli.portal_job(action="list"))
    assert any(j["job_id"] == out["job_id"] for j in listed)


# ── on-demand paging: max_bytes cap + `more` flag + clean UTF-8 seams ────────

def test_decode_incremental_trims_truncated_multibyte():
    from portal_mcp_server.job_manager import _decode_incremental
    # "ab" + a truncated "中" (e4 b8 ad, last byte missing).
    raw = "ab".encode() + "中".encode()[:2]
    text, consumed = _decode_incremental(raw)
    assert text == "ab", "must not emit escape artifacts for the split char"
    assert consumed == 2, "the 2 incomplete bytes are left for the next poll"
    assert "\\x" not in text


def test_decode_incremental_complete_passthrough():
    from portal_mcp_server.job_manager import _decode_incremental
    raw = "héllo 中文".encode("utf-8")
    text, consumed = _decode_incremental(raw)
    assert text == "héllo 中文"
    assert consumed == len(raw)


def test_decode_incremental_empty():
    from portal_mcp_server.job_manager import _decode_incremental
    assert _decode_incremental(b"") == ("", 0)


def test_decode_incremental_nonutf8_is_escaped_not_dropped():
    from portal_mcp_server.job_manager import _decode_incremental
    # A genuinely invalid byte mid-stream (e.g. GBK) -> backslashreplace, all
    # consumed (it won't "complete" on a re-read).
    raw = b"ok\xff\xfetail"
    text, consumed = _decode_incremental(raw)
    assert consumed == len(raw)
    assert "ok" in text and "tail" in text


@pytest.mark.asyncio
async def test_poll_more_flag_when_backlog_exceeds_chunk(monkeypatch):
    def router(c):
        if "echo $!" in c:
            return "100\n"
        if "__CHUNK__" in c:
            # 10 new bytes returned, but the file is 100 bytes total.
            return _poll_out("", 100, "yes", "0123456789")
        return ""

    _install_conn(monkeypatch, router)
    jm = job_manager.JobManager()
    jid = (await jm.submit("h", "x"))["job_id"]
    p = await jm.poll(jid, since=0)
    assert p["output_chunk"] == "0123456789"
    assert p["new_offset"] == 10
    assert p["more"] is True  # 10 < 100, keep polling with since=10


@pytest.mark.asyncio
async def test_poll_command_caps_chunk_at_max_bytes(monkeypatch):
    rec = _install_conn(monkeypatch, lambda c: "100\n" if "echo $!" in c
                        else _poll_out("", 0, "yes", ""))
    jm = job_manager.JobManager()
    jid = (await jm.submit("h", "x"))["job_id"]
    await jm.poll(jid, since=0, max_bytes=4096)
    poll_cmd = next(c for c in rec if "__CHUNK__" in c)
    # the shell caps N at max_bytes and base64-encodes the chunk
    assert "-gt 4096" in poll_cmd
    assert "base64" in poll_cmd
    assert 'head -c "$N"' in poll_cmd
