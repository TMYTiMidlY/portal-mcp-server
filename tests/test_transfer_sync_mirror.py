"""sync/mirror incremental short-circuit + structured returns (A2/A3/A4).

Drives ssh_sync_directory / ssh_mirror_directory against an in-memory fake
SFTP+conn so the skip logic (size+mtime, and sha256 when checksum=True) and the
structured status dicts are exercised without a live host.
"""
from __future__ import annotations

import hashlib

import pytest


class FakeAttrs:
    def __init__(self, size, mtime, permissions):
        self.size = size
        self.mtime = mtime
        self.permissions = permissions


class FakeName:
    def __init__(self, filename, attrs):
        self.filename = filename
        self.attrs = attrs


class FakeStat:
    def __init__(self, size, mtime, permissions):
        self.size = size
        self.mtime = mtime
        self.permissions = permissions


class FakeRun:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class FakeSFTP:
    """In-memory remote tree.

    remote_files: {posix_path: {"size","mtime","data"}}
    Records put/get targets so tests can assert what actually transferred.
    """

    def __init__(self, remote_files=None):
        self.remote_files = remote_files or {}
        self.put_calls = []
        self.get_calls = []
        self.made_dirs = []

    async def stat(self, path):
        if path in self.remote_files:
            f = self.remote_files[path]
            return FakeStat(f["size"], f["mtime"], 0o100644)
        # treat any path that is a parent of a known file as a directory
        if any(p.startswith(path.rstrip("/") + "/") for p in self.remote_files):
            return FakeStat(0, 0, 0o040755)
        raise FileNotFoundError(path)

    async def makedirs(self, path, exist_ok=False):
        self.made_dirs.append(path)

    async def put(self, local, remote, preserve=False, progress_handler=None):
        self.put_calls.append(remote)
        if progress_handler is not None:
            progress_handler(local, remote, 10, 10)
        import os
        # preserve=True propagates the source mtime to the destination (asyncssh
        # behaviour); without it the remote would get a fresh timestamp.
        mtime = os.path.getmtime(local) if preserve else 9_999_999_999
        self.remote_files[remote] = {
            "size": os.path.getsize(local),
            "mtime": mtime,
            "data": open(local, "rb").read(),
        }

    async def get(self, remote, local, preserve=False, progress_handler=None):
        self.get_calls.append(remote)
        if progress_handler is not None:
            progress_handler(remote, local, 10, 10)
        import os
        f = self.remote_files[remote]
        with open(local, "wb") as fh:
            fh.write(f["data"])
        if preserve:
            os.utime(local, (f["mtime"], f["mtime"]))

    async def readdir(self, path):
        base = path.rstrip("/")
        seen = {}
        for p, meta in self.remote_files.items():
            if not p.startswith(base + "/"):
                continue
            rest = p[len(base) + 1:]
            head = rest.split("/", 1)
            if len(head) == 1:
                seen[head[0]] = FakeName(head[0], FakeAttrs(meta["size"], meta["mtime"], 0o100644))
            else:
                seen.setdefault(head[0], FakeName(head[0], FakeAttrs(0, 0, 0o040755)))
        return list(seen.values())

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
        # emulate `sha256sum -b -- <path>` against the in-memory data
        import shlex
        parts = shlex.split(cmd)
        path = parts[-1]
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


@pytest.mark.asyncio
async def test_sync_uploads_and_returns_structured(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_sync_directory

    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world!!")

    sftp = FakeSFTP()
    patch_manager["conn"] = FakeConn(sftp)

    res = await ssh_sync_directory("h", str(tmp_path), "/remote/dir")
    assert res["status"] == "ok", res
    assert res["uploaded"] == 2
    assert res["skipped"] == 0
    assert res["failed"] == []
    assert res["bytes_total"] == len("hello") + len("world!!")
    assert "duration_s" in res
    assert sorted(sftp.put_calls) == ["/remote/dir/a.txt", "/remote/dir/sub/b.txt"]


@pytest.mark.asyncio
async def test_sync_skips_matching_size_mtime(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_sync_directory

    f = tmp_path / "a.txt"
    f.write_text("hello")
    local_mtime = f.stat().st_mtime

    # remote already has it, same size, SAME mtime (as preserve=True would set) → skip
    sftp = FakeSFTP({"/remote/dir/a.txt": {"size": 5, "mtime": local_mtime, "data": b"hello"}})
    patch_manager["conn"] = FakeConn(sftp)

    res = await ssh_sync_directory("h", str(tmp_path), "/remote/dir")
    assert res["uploaded"] == 0
    assert res["skipped"] == 1
    assert sftp.put_calls == []


@pytest.mark.asyncio
async def test_sync_reuploads_when_mtime_differs(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_sync_directory

    f = tmp_path / "a.txt"
    f.write_text("hello")
    local_mtime = f.stat().st_mtime
    # same size but mtime well outside the tolerance window → out-of-band change,
    # must re-upload (the old `>=` logic would have wrongly skipped this).
    sftp = FakeSFTP({"/remote/dir/a.txt": {"size": 5, "mtime": local_mtime + 100, "data": b"DIFF!"}})
    patch_manager["conn"] = FakeConn(sftp)

    res = await ssh_sync_directory("h", str(tmp_path), "/remote/dir")
    assert res["uploaded"] == 1
    assert res["skipped"] == 0


@pytest.mark.asyncio
async def test_sync_reuploads_when_size_differs(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_sync_directory

    f = tmp_path / "a.txt"
    f.write_text("hello")
    sftp = FakeSFTP({"/remote/dir/a.txt": {"size": 999, "mtime": 9e12, "data": b"x" * 999}})
    patch_manager["conn"] = FakeConn(sftp)

    res = await ssh_sync_directory("h", str(tmp_path), "/remote/dir")
    assert res["uploaded"] == 1
    assert res["skipped"] == 0


@pytest.mark.asyncio
async def test_sync_checksum_skips_identical_content(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_sync_directory

    f = tmp_path / "a.txt"
    f.write_text("hello")
    # same size, OLDER mtime (size+mtime would re-upload) but identical bytes
    sftp = FakeSFTP({"/remote/dir/a.txt": {"size": 5, "mtime": 1, "data": b"hello"}})
    patch_manager["conn"] = FakeConn(sftp)

    res = await ssh_sync_directory("h", str(tmp_path), "/remote/dir", checksum=True)
    assert res["skipped"] == 1, res
    assert res["uploaded"] == 0


@pytest.mark.asyncio
async def test_sync_checksum_reuploads_when_sha256sum_unavailable(patch_manager, tmp_path):
    """checksum=True but the remote has no working sha256sum (run returns rc=1
    for every path): a size-matching file must be RE-UPLOADED, never silently
    skipped. Guards the conservative `rhash is None -> re-transfer` branch."""
    from portal_mcp_server.file_ops import ssh_sync_directory

    f = tmp_path / "a.txt"
    f.write_text("hello")

    class _NoSha256Conn(FakeConn):
        async def run(self, cmd, check=False, errors=None):
            return FakeRun(1, "")  # sha256sum missing / non-zero for any path

    # Same size as local so the only thing that can force a re-transfer is the
    # (now unavailable) checksum comparison.
    sftp = FakeSFTP({"/remote/dir/a.txt": {"size": 5, "mtime": 1, "data": b"hello"}})
    patch_manager["conn"] = _NoSha256Conn(sftp)

    res = await ssh_sync_directory("h", str(tmp_path), "/remote/dir", checksum=True)
    assert res["uploaded"] == 1, res
    assert res["skipped"] == 0


@pytest.mark.asyncio
async def test_mirror_downloads_and_skips(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_mirror_directory

    remote = {
        "/remote/dir/a.txt": {"size": 5, "mtime": 100, "data": b"hello"},
        "/remote/dir/sub/b.txt": {"size": 3, "mtime": 100, "data": b"abc"},
    }
    sftp = FakeSFTP(remote)
    patch_manager["conn"] = FakeConn(sftp)
    local = tmp_path / "dest"

    res = await ssh_mirror_directory("h", "/remote/dir", str(local))
    assert res["status"] == "ok", res
    assert res["downloaded"] == 2
    assert (local / "a.txt").read_text() == "hello"
    assert (local / "sub" / "b.txt").read_text() == "abc"

    # second run: preserve=True set local mtime == remote mtime → equality skip
    res2 = await ssh_mirror_directory("h", "/remote/dir", str(local))
    assert res2["downloaded"] == 0
    assert res2["skipped"] == 2


@pytest.mark.asyncio
async def test_mirror_missing_remote_dir(patch_manager, tmp_path):
    from portal_mcp_server.file_ops import ssh_mirror_directory

    sftp = FakeSFTP({})
    patch_manager["conn"] = FakeConn(sftp)
    res = await ssh_mirror_directory("h", "/nope", str(tmp_path / "d"))
    assert res["status"] == "error"
    assert "not found" in res["error"]


# ── symlink safety: don't follow local symlinks out of the requested tree ────
def _try_symlink(src, dst):
    import os
    try:
        os.symlink(src, dst)
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.mark.asyncio
async def test_sync_skips_local_symlinks(patch_manager, tmp_path):
    """A symlink inside local_dir pointing OUTSIDE it must not be uploaded."""
    from portal_mcp_server.file_ops import ssh_sync_directory
    (tmp_path / "a.txt").write_text("hello")
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("SECRET")
    if not _try_symlink(outside, tmp_path / "link.txt"):
        pytest.skip("symlinks not supported here")
    sftp = FakeSFTP()
    patch_manager["conn"] = FakeConn(sftp)
    res = await ssh_sync_directory("h", str(tmp_path), "/remote/dir")
    assert res["uploaded"] == 1                       # only a.txt
    assert res.get("skipped_symlinks") == 1
    assert "/remote/dir/a.txt" in sftp.put_calls
    assert all("link" not in p for p in sftp.put_calls)


@pytest.mark.asyncio
async def test_mirror_refuses_symlink_destination(patch_manager, tmp_path):
    """A pre-existing local symlink at the download destination must not be
    written through (it could redirect the write outside local_dir)."""
    from portal_mcp_server.file_ops import ssh_mirror_directory
    local = tmp_path / "dst"
    local.mkdir()
    target = tmp_path / "real_a.txt"
    target.write_text("original")
    if not _try_symlink(target, local / "a.txt"):
        pytest.skip("symlinks not supported here")
    sftp = FakeSFTP({"/remote/dir/a.txt": {"size": 3, "mtime": 1, "data": b"NEW"}})
    patch_manager["conn"] = FakeConn(sftp)
    res = await ssh_mirror_directory("h", "/remote/dir", str(local))
    assert res["downloaded"] == 0
    assert res["failed"] and "symlink" in res["failed"][0]["error"]
    assert target.read_text() == "original"           # not overwritten through the link
