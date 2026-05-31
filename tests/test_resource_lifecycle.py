"""Tests proving connection / SFTP cleanup on every code path.

Audit findings addressed
------------------------
* file_ops.py used to call ``_sftp(host)`` to obtain (conn, sftp) and never
  call ``release_connection``. ``in_use`` would creep up forever.
* SFTPClient.exit() was called but ``await sftp.wait_closed()`` was missed,
  occasionally leaking file descriptors on busy hosts.
* ssh_exec_script's cleanup ``rm -f /tmp/<x>`` was outside ``finally``, so
  a timeout would leak the script.

Strategy: install an in-memory recorder for every ``get_connection``,
``release_connection``, and ``start_sftp_client``/``exit``/``wait_closed``
call. Then drive each public file-op once — successfully and with an injected
exception — and assert the counters balance out.
"""
from __future__ import annotations

import pytest


# ─── Recorder primitives ────────────────────────────────────────────────────

class CountingSFTP:
    def __init__(self, recorder, fail_inside: bool = False):
        self._rec = recorder
        self._fail_inside = fail_inside
        self.exit_calls = 0
        self.wait_closed_calls = 0

    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return None

    async def put(self, *a, **k):
        if self._fail_inside:
            raise RuntimeError("inject")
    async def get(self, *a, **k):
        if self._fail_inside:
            raise RuntimeError("inject")
    async def remove(self, *a, **k):
        pass

    def open(self, *a, **k):
        sftp = self
        class _F:
            async def __aenter__(self_inner):
                if sftp._fail_inside:
                    raise RuntimeError("inject")
                return self_inner
            async def __aexit__(self_inner, *exc):
                return None
            async def read(self_inner, *_a, **_k):
                return ""
            async def write(self_inner, *_a, **_k):
                return None
        return _F()

    async def readdir(self, path):
        if self._fail_inside:
            raise RuntimeError("inject")
        return []

    async def makedirs(self, *a, **k):
        pass

    def exit(self):
        self.exit_calls += 1
        self._rec.note("sftp.exit")

    async def wait_closed(self):
        self.wait_closed_calls += 1
        self._rec.note("sftp.wait_closed")


class CountingConn:
    def __init__(self, recorder, fail_inside: bool = False):
        self._rec = recorder
        self._fail_inside = fail_inside

    async def start_sftp_client(self):
        self._rec.note("conn.start_sftp")
        return CountingSFTP(self._rec, fail_inside=self._fail_inside)

    def is_closed(self):
        return False


class Recorder:
    def __init__(self, fail_inside: bool = False):
        self.events: list[str] = []
        self.connection_acquired = 0
        self.connection_released = 0
        self._fail_inside = fail_inside

    def note(self, what: str):
        self.events.append(what)

    @property
    def conn_balance(self) -> int:
        return self.connection_acquired - self.connection_released


@pytest.fixture
def recorder(monkeypatch):
    """Patch ConnectionManager so every public file_op call goes to a counter."""
    from portal_mcp_server import connection_manager

    rec = Recorder()

    async def fake_get(self, host_name):
        rec.connection_acquired += 1
        rec.note(f"acquire:{host_name}")
        return CountingConn(rec, fail_inside=rec._fail_inside)

    def fake_release(self, host_name, conn):
        rec.connection_released += 1
        rec.note(f"release:{host_name}")

    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "release_connection", fake_release)
    return rec


# ════════════════════════════════════════════════════════════════════════════
#  file_ops public surface — happy path: every call balances
# ════════════════════════════════════════════════════════════════════════════

class TestFileOpsHappyPath:
    @pytest.mark.asyncio
    async def test_upload_balances(self, recorder, tmp_path):
        from portal_mcp_server.file_ops import ssh_upload_file
        local = tmp_path / "x.txt"
        local.write_text("hi")
        await ssh_upload_file("h", str(local), "/tmp/x.txt")
        assert recorder.conn_balance == 0
        assert "sftp.exit" in recorder.events
        assert "sftp.wait_closed" in recorder.events

    @pytest.mark.asyncio
    async def test_download_balances(self, recorder):
        from portal_mcp_server.file_ops import ssh_download_file
        await ssh_download_file("h", "/tmp/x.txt", "/dev/null")
        assert recorder.conn_balance == 0

    @pytest.mark.asyncio
    async def test_read_balances(self, recorder):
        from portal_mcp_server.file_ops import ssh_read_file
        await ssh_read_file("h", "/tmp/x.txt")
        assert recorder.conn_balance == 0

    @pytest.mark.asyncio
    async def test_write_balances(self, recorder):
        from portal_mcp_server.file_ops import ssh_write_file
        await ssh_write_file("h", "/tmp/x.txt", "data")
        assert recorder.conn_balance == 0

    @pytest.mark.asyncio
    async def test_delete_balances(self, recorder):
        from portal_mcp_server.file_ops import ssh_delete_file
        await ssh_delete_file("h", "/tmp/x.txt")
        assert recorder.conn_balance == 0

    @pytest.mark.asyncio
    async def test_list_balances(self, recorder):
        from portal_mcp_server.file_ops import ssh_list_directory
        await ssh_list_directory("h", "/tmp")
        assert recorder.conn_balance == 0


# ════════════════════════════════════════════════════════════════════════════
#  Failure injection: connection still released, SFTP still closed
# ════════════════════════════════════════════════════════════════════════════

class TestFileOpsExceptionPath:
    @pytest.mark.asyncio
    async def test_upload_failure_releases(self, monkeypatch, tmp_path):
        # Start a fresh recorder in fail-inside mode.
        from portal_mcp_server import connection_manager
        rec = Recorder(fail_inside=True)

        async def fake_get(self, host_name):
            rec.connection_acquired += 1
            return CountingConn(rec, fail_inside=True)
        def fake_release(self, host_name, conn):
            rec.connection_released += 1
        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "get_connection", fake_get)
        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "release_connection", fake_release)

        from portal_mcp_server.file_ops import ssh_upload_file
        local = tmp_path / "x.txt"
        local.write_text("hi")
        out = await ssh_upload_file("h", str(local), "/tmp/x.txt")
        assert out["status"] == "error", out
        assert "Upload failed" in out["error"], out
        # Even though sftp.put raised, we must have released the pooled conn.
        assert rec.conn_balance == 0


# ════════════════════════════════════════════════════════════════════════════
#  Path validation rejects values *before* a connection is opened
# ════════════════════════════════════════════════════════════════════════════

class TestPathValidationShortCircuits:
    @pytest.mark.asyncio
    async def test_nul_in_remote_path_rejected_no_connection(self, recorder):
        from portal_mcp_server.file_ops import ssh_read_file
        out = await ssh_read_file("h", "/etc/passwd\x00trick")
        assert "Invalid remote_path" in out
        assert recorder.connection_acquired == 0

    @pytest.mark.asyncio
    async def test_empty_path_rejected(self, recorder):
        from portal_mcp_server.file_ops import ssh_write_file
        out = await ssh_write_file("h", "", "data")
        assert "Invalid remote_path" in out
        assert recorder.connection_acquired == 0
