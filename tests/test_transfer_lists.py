"""upload-list / download-list explicit-pair transfers with skip-existing.

Drives ssh_upload_list / ssh_download_list against an in-memory fake SFTP+conn
so the per-file skip logic (size+mtime, and sha256 when checksum=True), the
failed[] collection (single failure does not abort the batch), and the
structured status dicts are exercised without a live host.
"""
from __future__ import annotations

import hashlib
import os

import pytest

from mcp.server.fastmcp.exceptions import ToolError


class FakeStat:
    def __init__(self, size, mtime, permissions=0o100644):
        self.size = size
        self.mtime = mtime
        self.permissions = permissions


class FakeRun:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class FakeSFTP:
    """In-memory remote tree keyed by posix path.

    remote_files: {posix_path: {"size","mtime","data"}}. ``fail_paths`` forces a
    put/get on those targets to raise, to exercise failed[] collection.
    """

    def __init__(self, remote_files=None, fail_paths=None):
        self.remote_files = remote_files or {}
        self.fail_paths = set(fail_paths or [])
        self.put_calls = []
        self.get_calls = []
        self.made_dirs = []

    async def stat(self, path):
        if path in self.remote_files:
            f = self.remote_files[path]
            return FakeStat(f["size"], f["mtime"])
        raise FileNotFoundError(path)

    async def makedirs(self, path, exist_ok=False):
        self.made_dirs.append(path)

    async def put(self, local, remote, preserve=False, progress_handler=None):
        if remote in self.fail_paths:
            raise OSError(f"permission denied: {remote}")
        self.put_calls.append(remote)
        if progress_handler is not None:
            progress_handler(local, remote, 10, 10)
        mtime = os.path.getmtime(local) if preserve else 9_999_999_999
        self.remote_files[remote] = {
            "size": os.path.getsize(local),
            "mtime": mtime,
            "data": open(local, "rb").read(),
        }

    async def get(self, remote, local, preserve=False, progress_handler=None):
        if local in self.fail_paths:
            raise OSError(f"permission denied: {local}")
        self.get_calls.append(remote)
        if progress_handler is not None:
            progress_handler(remote, local, 10, 10)
        f = self.remote_files[remote]
        with open(local, "wb") as fh:
            fh.write(f["data"])
        if preserve:
            os.utime(local, (f["mtime"], f["mtime"]))

    def exit(self):
        pass

    async def wait_closed(self):
        pass


class FakeConn:
    def __init__(self, sftp):
        self._sftp = sftp

    async def start_sftp_client(self):
        return self._sftp

    async def run(self, cmd, check=False, errors=None):
        import shlex
        path = shlex.split(cmd)[-1]
        f = self._sftp.remote_files.get(path)
        if f is None or "data" not in f:
            return FakeRun(1, "")
        digest = hashlib.sha256(f["data"]).hexdigest()
        return FakeRun(0, f"{digest} *{path}\n")

    def is_closed(self):
        return False


@pytest.fixture
def patch_manager(monkeypatch):
    from portal_mcp_server import connection_manager

    state = {}

    async def fake_get(self, host_name):
        return state["conn"]

    def fake_release(self, host_name, conn):
        pass

    monkeypatch.setattr(connection_manager.ConnectionManager, "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager, "release_connection", fake_release)
    return state


# ── upload-list ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_list_transfers_all_new(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_upload_list

    a = tmp_path / "a.txt"
    a.write_text("hello")
    b = tmp_path / "b.txt"
    b.write_text("world!!")

    sftp = FakeSFTP()
    patch_manager["conn"] = FakeConn(sftp)

    pairs = [(str(a), "/etc/app/a.conf"), (str(b), "/etc/app/b.conf")]
    res = await ssh_upload_list("h", pairs)
    assert res["status"] == "ok", res
    assert res["uploaded"] == 2
    assert res["skipped"] == 0
    assert res["failed"] == []
    assert res["bytes_total"] == len("hello") + len("world!!")
    assert sorted(sftp.put_calls) == ["/etc/app/a.conf", "/etc/app/b.conf"]


@pytest.mark.asyncio
async def test_upload_list_skips_matching_size_mtime(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_upload_list

    a = tmp_path / "a.txt"
    a.write_text("hello")
    b = tmp_path / "b.txt"
    b.write_text("changed")
    mt = a.stat().st_mtime

    # a already present, identical size+mtime → skip; b differs → upload
    sftp = FakeSFTP({"/r/a": {"size": 5, "mtime": mt, "data": b"hello"},
                     "/r/b": {"size": 999, "mtime": 1, "data": b"x" * 999}})
    patch_manager["conn"] = FakeConn(sftp)

    res = await ssh_upload_list("h", [(str(a), "/r/a"), (str(b), "/r/b")])
    assert res["uploaded"] == 1
    assert res["skipped"] == 1
    assert sftp.put_calls == ["/r/b"]


@pytest.mark.asyncio
async def test_upload_list_checksum_skips_identical_content(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_upload_list

    a = tmp_path / "a.txt"
    a.write_text("hello")
    # same size, OLDER mtime (size+mtime would re-upload) but identical bytes
    sftp = FakeSFTP({"/r/a": {"size": 5, "mtime": 1, "data": b"hello"}})
    patch_manager["conn"] = FakeConn(sftp)

    res = await ssh_upload_list("h", [(str(a), "/r/a")], checksum=True)
    assert res["skipped"] == 1, res
    assert res["uploaded"] == 0


@pytest.mark.asyncio
async def test_upload_list_single_failure_does_not_abort(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_upload_list

    a = tmp_path / "a.txt"
    a.write_text("hello")
    b = tmp_path / "b.txt"
    b.write_text("world")
    missing = tmp_path / "nope.txt"

    sftp = FakeSFTP(fail_paths={"/r/b"})
    patch_manager["conn"] = FakeConn(sftp)

    pairs = [(str(a), "/r/a"), (str(b), "/r/b"), (str(missing), "/r/c")]
    res = await ssh_upload_list("h", pairs)
    assert res["status"] == "partial", res
    assert res["uploaded"] == 1            # only a.txt
    assert sftp.put_calls == ["/r/a"]
    failed_paths = {f["path"] for f in res["failed"]}
    assert failed_paths == {str(b), str(missing)}


@pytest.mark.asyncio
async def test_upload_list_rejects_bad_remote_path(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_upload_list

    a = tmp_path / "a.txt"
    a.write_text("hi")
    sftp = FakeSFTP()
    patch_manager["conn"] = FakeConn(sftp)

    res = await ssh_upload_list("h", [(str(a), "/r/a\x00evil")])
    assert res["uploaded"] == 0
    assert len(res["failed"]) == 1
    assert "Invalid remote_path" in res["failed"][0]["error"]


# ── download-list ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_download_list_transfers_and_skips(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_download_list

    remote = {"/r/a": {"size": 5, "mtime": 100, "data": b"hello"},
              "/r/b": {"size": 3, "mtime": 100, "data": b"abc"}}
    sftp = FakeSFTP(remote)
    patch_manager["conn"] = FakeConn(sftp)

    la = tmp_path / "out" / "a.txt"
    lb = tmp_path / "out" / "b.txt"
    pairs = [("/r/a", str(la)), ("/r/b", str(lb))]

    res = await ssh_download_list("h", pairs)
    assert res["status"] == "ok", res
    assert res["downloaded"] == 2
    assert la.read_text() == "hello"
    assert lb.read_text() == "abc"

    # second run: preserve=True set local mtime == remote mtime → skip both
    res2 = await ssh_download_list("h", pairs)
    assert res2["downloaded"] == 0
    assert res2["skipped"] == 2


@pytest.mark.asyncio
async def test_download_list_missing_remote_into_failed(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_download_list

    sftp = FakeSFTP({"/r/a": {"size": 5, "mtime": 100, "data": b"hello"}})
    patch_manager["conn"] = FakeConn(sftp)

    la = tmp_path / "a.txt"
    lb = tmp_path / "b.txt"
    pairs = [("/r/a", str(la)), ("/r/missing", str(lb))]

    res = await ssh_download_list("h", pairs)
    assert res["status"] == "partial", res
    assert res["downloaded"] == 1
    assert la.read_text() == "hello"
    assert res["failed"][0]["path"] == "/r/missing"
    assert "not found" in res["failed"][0]["error"]


# ── cli dispatch ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_portal_transfer_upload_list_dispatch(patch_manager, tmp_path):
    import json as _json
    from portal_mcp_server import cli

    a = tmp_path / "a.txt"
    a.write_text("hello")
    sftp = FakeSFTP()
    patch_manager["conn"] = FakeConn(sftp)

    paths_json = _json.dumps([{"local": str(a), "remote": "/r/a"}])
    out = await cli.portal_transfer(
        direction="upload-list", host="h", local_path="", remote_path="",
        ctx=None, paths_json=paths_json)
    res = _json.loads(out)
    assert res["direction"] == "upload-list"
    assert res["uploaded"] == 1
    assert sftp.put_calls == ["/r/a"]


@pytest.mark.asyncio
async def test_portal_transfer_bad_paths_json(patch_manager):
    from portal_mcp_server import cli

    with pytest.raises(ToolError, match="paths_json is not valid JSON"):
        await cli.portal_transfer(
            direction="upload-list", host="h", local_path="", remote_path="",
            ctx=None, paths_json="not json")

    with pytest.raises(ToolError, match="non-empty JSON array"):
        await cli.portal_transfer(
            direction="download-list", host="h", local_path="", remote_path="",
            ctx=None, paths_json="[]")


@pytest.mark.asyncio
async def test_portal_transfer_list_blocked_by_policy(monkeypatch, tmp_path):
    """A host outside the allowlist must be rejected by _gate before any
    transfer happens — same gate every other state-changing tool goes through.
    """
    from portal_mcp_server import security, cli

    pol_yaml = tmp_path / "p.yaml"
    pol_yaml.write_text(
        "policies:\n"
        "  host_allowlist:\n"
        "    - 'safe-*'\n"
        "  rate_limit_rps: 1000\n"
    )
    pol = security.SecurityPolicy(policies_yaml=pol_yaml)
    monkeypatch.setattr(security, "_policy", pol)
    monkeypatch.setattr(cli, "get_policy", lambda: pol)

    paths_json = '[{"local": "/tmp/a", "remote": "/r/a"}]'
    with pytest.raises(ToolError, match="BLOCKED:"):
        await cli.portal_transfer(
            direction="upload-list", host="evil-host", local_path="",
            remote_path="", ctx=None, paths_json=paths_json)

    with pytest.raises(ToolError, match="BLOCKED:"):
        await cli.portal_transfer(
            direction="download-list", host="evil-host", local_path="",
            remote_path="", ctx=None, paths_json=paths_json)
