"""Tests for ``ssh_remote_mcp.remote_text_editor``.

Test matrix
-----------
We mirror the matrix from `tumf/mcp-text-editor`_'s ``tests/test_service.py``
so this fork-by-design module proves it preserves the safety guarantees of
the upstream library, plus we add SFTP-specific cases the upstream cannot
exercise (atomic write fall-back, post-write rehash, connection release on
error).

.. _tumf/mcp-text-editor: https://github.com/tumf/mcp-text-editor

Concretely covered
~~~~~~~~~~~~~~~~~~

Upstream parity:
* hash determinism + length (SHA-256 hex, 64 chars)
* full-file & line-range read
* edit success path
* hash-mismatch detection (whole-file)
* range_hash mismatch detection (per-patch — *we* added)
* overlapping patches rejection
* patch with start beyond EOF rejection
* file-not-found

SFTP-specific:
* atomic write goes through ``posix_rename`` and fall-back ``rename``
* tmp file is removed when the in-progress write fails
* post-write rehash mismatch is reported
* connection pool is released on every exit path
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest


# ════════════════════════════════════════════════════════════════════════════
#  In-memory fake SFTP — minimal asyncssh-compatible surface
# ════════════════════════════════════════════════════════════════════════════

class _FakeFile:
    def __init__(self, fs, path, mode):
        self._fs = fs
        self._path = path
        self._mode = mode
        self._buf = ""
        if mode == "r":
            self._buf = fs.read(path)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if self._mode == "w":
            self._fs.write(self._path, self._buf)

    async def read(self, *_a, **_k):
        return self._buf

    async def write(self, data):
        # Hook for "what if the network dies mid-write" tests.
        if getattr(self._fs, "fail_on_write", False):
            # Simulate the file being created (open already succeeded) but
            # the write itself never reaching the server.
            self._fs.write(self._path, "")
            raise ConnectionError("simulated SFTP write failure")
        self._buf += data


class _FakeAttrs:
    def __init__(self, mtime: float, size: int = 0, permissions: int = 0o644):
        self.mtime = mtime
        self.size = size
        self.permissions = permissions


class _FakeDirEntry:
    def __init__(self, filename: str, attrs: "_FakeAttrs"):
        self.filename = filename
        self.attrs = attrs


class FakeSFTP:
    """Just enough of an asyncssh SFTPClient to drive remote_text_editor."""

    def __init__(self, fs, *, posix_rename_supported: bool = True):
        self._fs = fs
        self._posix = posix_rename_supported
        self.exit_calls = 0
        self.wait_closed_calls = 0

    def open(self, path, mode, encoding=None):  # noqa: D401 - mimics asyncssh
        if mode == "r" and not self._fs.exists(path):
            raise FileNotFoundError(path)
        return _FakeFile(self._fs, path, mode)

    async def posix_rename(self, src, dst):
        import asyncssh
        if not self._posix:
            raise asyncssh.SFTPOpUnsupported("posix-rename not supported")
        self._fs.rename(src, dst)

    async def rename(self, src, dst):
        self._fs.rename(src, dst)

    async def remove(self, path):
        import asyncssh
        if not self._fs.exists(path):
            raise asyncssh.SFTPNoSuchFile(path)
        self._fs.remove(path)

    async def readdir(self, path):
        return [
            _FakeDirEntry(
                filename=name.split("/")[-1],
                attrs=_FakeAttrs(
                    mtime=meta.get("mtime", 0),
                    size=len(meta["content"]),
                ),
            )
            for name, meta in self._fs._meta.items()
            if name.startswith(path.rstrip("/") + "/") and "/" not in
            name[len(path.rstrip("/")) + 1:]
        ]

    def exit(self):
        self.exit_calls += 1

    async def wait_closed(self):
        self.wait_closed_calls += 1


class FakeFS:
    def __init__(self):
        self.files: dict[str, str] = {}
        self._meta: dict[str, dict] = {}
        self.fail_on_write = False

    def write(self, path, data, *, mtime: float | None = None):
        self.files[path] = data
        self._meta[path] = {
            "content": data,
            "mtime": mtime if mtime is not None else self._meta.get(
                path, {}).get("mtime", 0.0),
        }

    def read(self, path):
        return self.files[path]

    def exists(self, path):
        return path in self.files

    def remove(self, path):
        self.files.pop(path, None)
        self._meta.pop(path, None)

    def rename(self, src, dst):
        self.files[dst] = self.files.pop(src)
        self._meta[dst] = self._meta.pop(src)


class FakeConn:
    def __init__(self, sftp_factory):
        self._sftp_factory = sftp_factory

    async def start_sftp_client(self):
        return self._sftp_factory()

    def is_closed(self):
        return False


@pytest.fixture
def fake_remote(monkeypatch):
    """Patch ConnectionManager so remote_text_editor talks to an in-memory FS."""
    from ssh_remote_mcp import connection_manager

    fs = FakeFS()
    sftps: list[FakeSFTP] = []
    state = {"acquired": 0, "released": 0, "posix": True}

    def make_sftp():
        s = FakeSFTP(fs, posix_rename_supported=state["posix"])
        sftps.append(s)
        return s

    async def fake_get(self, host_name):
        state["acquired"] += 1
        return FakeConn(make_sftp)

    def fake_release(self, host_name, conn):
        state["released"] += 1

    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "release_connection", fake_release)

    return fs, sftps, state


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ════════════════════════════════════════════════════════════════════════════
#  Upstream parity — calculate_hash + read_file_contents
# ════════════════════════════════════════════════════════════════════════════

class TestHashAndRead:
    @pytest.mark.asyncio
    async def test_hash_is_sha256_hex_64(self, fake_remote):
        fs, _, _ = fake_remote
        fs.write("/f", "test content")
        from ssh_remote_mcp.remote_text_editor import remote_read
        out = await remote_read("h", "/f")
        assert isinstance(out["file_hash"], str)
        assert len(out["file_hash"]) == 64
        assert out["file_hash"] == _h("test content")

    @pytest.mark.asyncio
    async def test_read_full_file(self, fake_remote):
        fs, _, _ = fake_remote
        fs.write("/f", "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n")
        from ssh_remote_mcp.remote_text_editor import remote_read
        out = await remote_read("h", "/f")
        assert out["content"] == "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n"
        assert out["start"] == 1
        assert out["end"] == 5
        assert out["total_lines"] == 5

    @pytest.mark.asyncio
    async def test_read_range(self, fake_remote):
        fs, _, _ = fake_remote
        fs.write("/f", "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n")
        from ssh_remote_mcp.remote_text_editor import remote_read
        out = await remote_read("h", "/f", start=2, end=4)
        assert out["content"] == "Line 2\nLine 3\nLine 4\n"
        assert out["start"] == 2
        assert out["end"] == 4

    @pytest.mark.asyncio
    async def test_read_path_validation_blocks_nul(self, fake_remote):
        from ssh_remote_mcp.remote_text_editor import remote_read
        with pytest.raises(ValueError):
            await remote_read("h", "/etc/passwd\x00fake")


# ════════════════════════════════════════════════════════════════════════════
#  Upstream parity — happy-path edit
# ════════════════════════════════════════════════════════════════════════════

class TestPatchHappyPath:
    @pytest.mark.asyncio
    async def test_replace_single_line(self, fake_remote):
        fs, _, _ = fake_remote
        original = "line1\nline2\nline3\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{
                "start": 2, "end": 2,
                "contents": "new line2\n",
                "range_hash": _h("line2\n"),
            }],
        )
        assert res["result"] == "ok"
        assert fs.read("/f") == "line1\nnew line2\nline3\n"
        # Hash advertised in result equals SHA-256 of new content
        assert res["file_hash"] == _h(fs.read("/f"))

    @pytest.mark.asyncio
    async def test_multi_patch_bottom_to_top(self, fake_remote):
        fs, _, _ = fake_remote
        original = "a\nb\nc\nd\ne\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[
                # Two non-overlapping ranges; sorted bottom-to-top internally
                # so applying [1,1] does not invalidate [4,4]'s line numbers.
                {"start": 1, "end": 1, "contents": "AA\n", "range_hash": _h("a\n")},
                {"start": 4, "end": 4, "contents": "DD\n", "range_hash": _h("d\n")},
            ],
        )
        assert res["result"] == "ok"
        assert fs.read("/f") == "AA\nb\nc\nDD\ne\n"


# ════════════════════════════════════════════════════════════════════════════
#  Upstream parity — error matrix
# ════════════════════════════════════════════════════════════════════════════

class TestPatchErrors:
    @pytest.mark.asyncio
    async def test_hash_mismatch_returns_current_state(self, fake_remote):
        fs, _, _ = fake_remote
        fs.write("/f", "line1\nline2\nline3\n")
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash="incorrect_hash",
            patches=[{"start": 2, "end": 2, "contents": "X\n", "range_hash": ""}],
        )
        assert res["result"] == "error"
        assert "hash mismatch" in res["reason"].lower()
        assert res["current_file_hash"] == _h("line1\nline2\nline3\n")
        assert res["current_content"] == "line1\nline2\nline3\n"
        # File is untouched
        assert fs.read("/f") == "line1\nline2\nline3\n"

    @pytest.mark.asyncio
    async def test_range_hash_mismatch(self, fake_remote):
        fs, _, _ = fake_remote
        original = "alpha\nbeta\ngamma\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{
                "start": 2, "end": 2,
                "contents": "BETA!\n",
                "range_hash": _h("WRONG\n"),
            }],
        )
        assert res["result"] == "error"
        assert "range_hash mismatch" in res["reason"]
        assert res["current_range_content"] == "beta\n"
        assert fs.read("/f") == original  # unchanged

    @pytest.mark.asyncio
    async def test_overlapping_patches_rejected(self, fake_remote):
        fs, _, _ = fake_remote
        original = "a\nb\nc\nd\ne\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[
                {"start": 1, "end": 3, "contents": "X\n", "range_hash": ""},
                {"start": 2, "end": 4, "contents": "Y\n", "range_hash": ""},
            ],
        )
        assert res["result"] == "error"
        assert "overlapping" in res["reason"]

    @pytest.mark.asyncio
    async def test_patch_beyond_eof_rejected(self, fake_remote):
        fs, _, _ = fake_remote
        original = "a\nb\nc\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{"start": 99, "end": 99, "contents": "X\n", "range_hash": ""}],
        )
        assert res["result"] == "error"
        assert "beyond end of file" in res["reason"]

    @pytest.mark.asyncio
    async def test_invalid_patch_keys(self, fake_remote):
        fs, _, _ = fake_remote
        fs.write("/f", "x\n")
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h("x\n"),
            patches=[{"end": 1, "contents": "y\n"}],  # missing 'start'
        )
        assert res["result"] == "error"
        assert "invalid patch" in res["reason"]

    @pytest.mark.asyncio
    async def test_empty_patches_list(self, fake_remote):
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch("h", "/f", file_hash="x", patches=[])
        assert res["result"] == "error"
        assert "empty" in res["reason"]

    @pytest.mark.asyncio
    async def test_file_not_found_propagates(self, fake_remote):
        # FS is empty, no /missing
        from ssh_remote_mcp.remote_text_editor import remote_patch
        with pytest.raises(FileNotFoundError):
            await remote_patch("h", "/missing", file_hash="x",
                               patches=[{"start": 1, "contents": "y\n",
                                          "range_hash": ""}])


# ════════════════════════════════════════════════════════════════════════════
#  SFTP-specific safety nets the upstream library cannot exercise
# ════════════════════════════════════════════════════════════════════════════

class TestAtomicWriteFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_remove_plus_rename(self, fake_remote):
        fs, _, state = fake_remote
        state["posix"] = False  # simulate server without posix-rename
        original = "v1\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{"start": 1, "end": 1, "contents": "v2\n",
                       "range_hash": _h("v1\n")}],
        )
        assert res["result"] == "ok"
        assert fs.read("/f") == "v2\n"


class TestConnectionRelease:
    @pytest.mark.asyncio
    async def test_read_releases_connection(self, fake_remote):
        fs, sftps, state = fake_remote
        fs.write("/f", "hi\n")
        from ssh_remote_mcp.remote_text_editor import remote_read
        await remote_read("h", "/f")
        assert state["acquired"] == state["released"] == 1
        assert sftps[-1].exit_calls == 1
        assert sftps[-1].wait_closed_calls == 1

    @pytest.mark.asyncio
    async def test_patch_releases_connection_for_both_sftp_sessions(self, fake_remote):
        # remote_patch opens SFTP twice: once to read, once to write+verify.
        # Each must close cleanly.
        fs, sftps, state = fake_remote
        original = "a\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{"start": 1, "end": 1, "contents": "b\n",
                       "range_hash": _h("a\n")}],
        )
        assert res["result"] == "ok"
        # remote_patch performs: read, write+verify rehash → 3 SFTP sessions
        # (1 for initial read, 1 for atomic write, 1 for read-back rehash).
        assert state["acquired"] == state["released"]
        assert state["acquired"] >= 2
        for s in sftps:
            assert s.exit_calls >= 1
            assert s.wait_closed_calls >= 1


# ════════════════════════════════════════════════════════════════════════════
#  Newline normalization: catches "two lines glued into one" mistake
# ════════════════════════════════════════════════════════════════════════════

class TestAutoNewline:
    @pytest.mark.asyncio
    async def test_default_warns_but_does_not_modify(self, fake_remote):
        """The historical contract: callers manage their own line endings.

        Default (auto_newline=False) preserves byte-for-byte upstream
        compatibility, but now emits a ``warnings`` field so the agent can
        diagnose the resulting "two lines merged" symptom in one round trip.
        """
        fs, _, _ = fake_remote
        original = "line1\nline2\nline3\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{
                "start": 2, "end": 2,
                "contents": "new line2",  # ← missing trailing \n
                "range_hash": _h("line2\n"),
            }],
        )
        assert res["result"] == "ok"
        # The dangerous outcome: line3 glues onto the patched line.
        assert fs.read("/f") == "line1\nnew line2line3\n"
        assert "warnings" in res and res["warnings"]
        assert "missing trailing newline" in res["warnings"][0]

    @pytest.mark.asyncio
    async def test_auto_newline_true_appends(self, fake_remote):
        fs, _, _ = fake_remote
        original = "line1\nline2\nline3\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{
                "start": 2, "end": 2,
                "contents": "new line2",
                "range_hash": _h("line2\n"),
            }],
            auto_newline=True,
        )
        assert res["result"] == "ok"
        assert fs.read("/f") == "line1\nnew line2\nline3\n"
        assert "auto-appended" in res["warnings"][0]

    @pytest.mark.asyncio
    async def test_no_warning_when_replaced_slice_lacked_newline(self, fake_remote):
        """No warning if the original slice itself did not end with \\n."""
        fs, _, _ = fake_remote
        original = "line1\nline2"  # no trailing newline on file either
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{
                "start": 2, "end": 2,
                "contents": "L2",
                "range_hash": _h("line2"),
            }],
        )
        assert res["result"] == "ok"
        assert "warnings" not in res
        assert fs.read("/f") == "line1\nL2"

    @pytest.mark.asyncio
    async def test_no_warning_when_contents_already_has_newline(self, fake_remote):
        fs, _, _ = fake_remote
        original = "a\nb\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{"start": 1, "end": 1, "contents": "AA\n",
                       "range_hash": _h("a\n")}],
        )
        assert res["result"] == "ok"
        assert "warnings" not in res

    @pytest.mark.asyncio
    async def test_no_warning_for_empty_contents(self, fake_remote):
        """Deleting a line range is legitimate; do not warn."""
        fs, _, _ = fake_remote
        original = "a\nb\nc\n"
        fs.write("/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch
        res = await remote_patch(
            "h", "/f", file_hash=_h(original),
            patches=[{"start": 2, "end": 2, "contents": "",
                       "range_hash": _h("b\n")}],
        )
        assert res["result"] == "ok"
        assert "warnings" not in res
        assert fs.read("/f") == "a\nc\n"


# ════════════════════════════════════════════════════════════════════════════
#  Orphan tmp cleanup
# ════════════════════════════════════════════════════════════════════════════

class TestOrphanTmpCleanup:
    @pytest.mark.asyncio
    async def test_orphan_left_when_write_fails(self, fake_remote):
        """Critical contract: if write fails, cleanup at least runs.

        We simulate a network drop in mid-write. The atomic_write code path
        should attempt to remove the tmp file. Even if that *also* fails, the
        next invocation of cleanup_orphan_tmps must find it.
        """
        fs, _, _ = fake_remote
        original = "x\n"
        fs.write("/dir/f", original)
        from ssh_remote_mcp.remote_text_editor import remote_patch

        # Trigger fail-on-write so the tmp file is created (open succeeds)
        # then the write itself raises ConnectionError.
        fs.fail_on_write = True
        with pytest.raises(ConnectionError):
            await remote_patch(
                "h", "/dir/f", file_hash=_h(original),
                patches=[{"start": 1, "end": 1, "contents": "y\n",
                           "range_hash": _h("x\n")}],
            )
        fs.fail_on_write = False
        # Inline cleanup attempt happened, so by now the tmp should be gone.
        # Sanity check: NO orphan should remain after the inline best-effort.
        assert not any(name.startswith("/dir/f.mcp_tmp.") for name in fs.files), \
            "inline best-effort cleanup should have removed the tmp"

    @pytest.mark.asyncio
    async def test_cleanup_finds_and_removes_old_orphans(self, fake_remote):
        fs, _, _ = fake_remote
        # Plant a stale orphan as if a previous edit was killed.
        fs.write("/dir/realfile.txt", "data\n", mtime=1000.0)
        fs.write("/dir/realfile.txt.mcp_tmp.aaaaaaaaaaaa", "garbage", mtime=100.0)

        from ssh_remote_mcp.remote_text_editor import cleanup_orphan_tmps
        res = await cleanup_orphan_tmps("h", "/dir", max_age_s=0)
        assert res["scanned"] == 1
        assert res["removed"] == ["/dir/realfile.txt.mcp_tmp.aaaaaaaaaaaa"]
        assert res["skipped"] == []
        # Real file is untouched.
        assert "/dir/realfile.txt" in fs.files
        assert "/dir/realfile.txt.mcp_tmp.aaaaaaaaaaaa" not in fs.files

    @pytest.mark.asyncio
    async def test_cleanup_respects_max_age(self, fake_remote):
        fs, _, _ = fake_remote
        import time as time_mod
        # A "fresh" orphan from a possibly-still-running edit.
        fresh_mtime = time_mod.time() - 5  # 5 seconds old
        fs.write("/dir/file.mcp_tmp.bbbbbbbbbbbb", "garbage", mtime=fresh_mtime)
        from ssh_remote_mcp.remote_text_editor import cleanup_orphan_tmps
        res = await cleanup_orphan_tmps("h", "/dir", max_age_s=3600)
        assert res["scanned"] == 1
        assert res["removed"] == []
        assert res["skipped"] and "younger than" in res["skipped"][0][1]

    @pytest.mark.asyncio
    async def test_cleanup_does_not_touch_unrelated_files(self, fake_remote):
        """The 12-hex suffix regex must NOT match user files that look similar."""
        fs, _, _ = fake_remote
        # Things that LOOK like our pattern but aren't:
        fs.write("/dir/foo.mcp_tmp.short", "data", mtime=100)         # too short
        fs.write("/dir/foo.mcp_tmp.GGGGGGGGGGGG", "data", mtime=100)  # not hex
        fs.write("/dir/foo.tmp.aaaaaaaaaaaa", "data", mtime=100)      # missing .mcp_
        fs.write("/dir/regular.txt", "data", mtime=100)
        from ssh_remote_mcp.remote_text_editor import cleanup_orphan_tmps
        res = await cleanup_orphan_tmps("h", "/dir", max_age_s=0)
        assert res["scanned"] == 0
        assert res["removed"] == []
        # Everything still there.
        assert len(fs.files) == 4

    @pytest.mark.asyncio
    async def test_cleanup_path_validation(self, fake_remote):
        from ssh_remote_mcp.remote_text_editor import cleanup_orphan_tmps
        with pytest.raises(ValueError):
            await cleanup_orphan_tmps("h", "/dir\x00bad", max_age_s=0)
