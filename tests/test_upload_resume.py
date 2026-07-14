"""T2 — remote_transfer UPLOAD resume: append the missing tail of an interrupted
transfer instead of restarting, verify sha256 on resume, restart once on
mismatch, and honour resume=False.
"""
import contextlib
import types

import pytest

from portal_mcp_server import file_ops


class _FakeSFTP:
    def __init__(self, remote_size=None):
        self.remote_size = remote_size
        self.put_called = False
        self.append_opened = False
        self.appended = b""

    async def stat(self, path):
        if self.remote_size is None:
            raise FileNotFoundError(path)
        return types.SimpleNamespace(size=self.remote_size)

    def open(self, path, mode):
        assert "a" in mode  # resume path opens append-mode
        self.append_opened = True
        outer = self

        class _F:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def write(self, data):
                outer.appended += data

        return _F()

    async def put(self, local, remote, preserve=True, progress_handler=None):
        self.put_called = True


def _wire(monkeypatch, sftp, local_hash="aaa", remote_hash="aaa"):
    @contextlib.asynccontextmanager
    async def fake_cs(host):
        yield (object(), sftp)

    monkeypatch.setattr(file_ops, "_conn_and_sftp", fake_cs)

    async def lh(path):
        return local_hash
    async def rh(conn, path):
        return remote_hash
    monkeypatch.setattr(file_ops, "_local_sha256", lh)
    monkeypatch.setattr(file_ops, "_remote_sha256", rh)


@pytest.fixture
def localfile(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"x" * 100)
    return str(f)


@pytest.mark.asyncio
async def test_fresh_upload_when_remote_missing(monkeypatch, localfile):
    sftp = _FakeSFTP(remote_size=None)
    _wire(monkeypatch, sftp)
    res = await file_ops.ssh_upload_file("h", localfile, "/r/big.bin")
    assert res["status"] == "ok" and res["resumed"] is False
    assert sftp.put_called and not sftp.append_opened


@pytest.mark.asyncio
async def test_resume_appends_tail_and_verifies(monkeypatch, localfile):
    sftp = _FakeSFTP(remote_size=40)
    _wire(monkeypatch, sftp, local_hash="H", remote_hash="H")  # match
    res = await file_ops.ssh_upload_file("h", localfile, "/r/big.bin")
    assert res["status"] == "ok" and res["resumed"] is True
    assert sftp.append_opened and sftp.appended == b"x" * 60  # only the tail
    assert not sftp.put_called                                # no restart


@pytest.mark.asyncio
async def test_resume_hash_mismatch_restarts_once(monkeypatch, localfile):
    sftp = _FakeSFTP(remote_size=40)
    _wire(monkeypatch, sftp, local_hash="H", remote_hash="DIFFERENT")
    res = await file_ops.ssh_upload_file("h", localfile, "/r/big.bin")
    assert res["status"] == "ok"
    assert sftp.append_opened                     # tried the tail first
    assert sftp.put_called                         # then re-uploaded fresh
    assert res["resumed"] is False
    assert res.get("restarted_after_mismatch") is True


@pytest.mark.asyncio
async def test_resume_false_forces_fresh(monkeypatch, localfile):
    sftp = _FakeSFTP(remote_size=40)  # a partial exists...
    _wire(monkeypatch, sftp)
    res = await file_ops.ssh_upload_file("h", localfile, "/r/big.bin",
                                         resume=False)
    assert res["status"] == "ok" and res["resumed"] is False
    assert sftp.put_called and not sftp.append_opened  # ...but ignored


@pytest.mark.asyncio
async def test_remote_not_smaller_uploads_fresh(monkeypatch, localfile):
    sftp = _FakeSFTP(remote_size=100)  # == local size, not a resumable partial
    _wire(monkeypatch, sftp)
    res = await file_ops.ssh_upload_file("h", localfile, "/r/big.bin")
    assert res["status"] == "ok" and res["resumed"] is False
    assert sftp.put_called and not sftp.append_opened


@pytest.mark.asyncio
async def test_resume_unverifiable_restarts_fresh(monkeypatch, localfile):
    """No remote sha256sum (rh=None) => can't verify the appended prefix, so
    the resume is not trusted: re-upload fresh and flag it."""
    sftp = _FakeSFTP(remote_size=40)
    _wire(monkeypatch, sftp, local_hash="H", remote_hash=None)
    res = await file_ops.ssh_upload_file("h", localfile, "/r/big.bin")
    assert res["status"] == "ok"
    assert sftp.append_opened                      # tried the tail first
    assert sftp.put_called                          # then re-uploaded fresh
    assert res["resumed"] is False
    assert res.get("restarted_unverifiable") is True
