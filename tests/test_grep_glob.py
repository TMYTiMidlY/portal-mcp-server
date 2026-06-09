"""portal_grep / portal_glob — Claude-Code-faithful schema, structured output.

These mock the SSH connection to feed canned rg/grep/glob stdout, so they pin
the parsing + the output-shape contract (output modes, mtime ordering,
head_limit total cap + truncated flag, offset pagination, path relativization)
without a live host.
"""
from __future__ import annotations

import json

import pytest

from portal_mcp_server import connection_manager as cm
from portal_mcp_server import remote_search as rs


def _install(monkeypatch, stdout: str, rg: bool = True):
    """Make the connection hand back `stdout` for the single run() the engine
    issues, and pin the tool probe so no extra round trip happens."""
    recorded: list[str] = []

    class _Result:
        def __init__(self, out):
            self.stdout = out
            self.stderr = ""
            self.returncode = 0

    class _Conn:
        async def run(self, cmd, **k):
            recorded.append(cmd)
            return _Result(stdout)

    async def fake_get(self, host):
        return _Conn()

    def fake_release(self, host, conn):
        pass

    async def fake_probe(host):
        return {"rg": rg, "find": True}

    monkeypatch.setattr(cm.ConnectionManager, "get_connection", fake_get)
    monkeypatch.setattr(cm.ConnectionManager, "release_connection", fake_release)
    monkeypatch.setattr(rs, "_probe_tools", fake_probe)
    return recorded


def _match(path, line, text):
    return json.dumps({"type": "match", "data": {
        "path": {"text": path}, "lines": {"text": text + "\n"},
        "line_number": line}})


def _context(path, line, text):
    return json.dumps({"type": "context", "data": {
        "path": {"text": path}, "lines": {"text": text + "\n"},
        "line_number": line}})


# ── files_with_matches (default) ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_files_with_matches_newest_first(monkeypatch):
    # rg --sort modified prints ascending mtime; the engine reverses it.
    rec = _install(monkeypatch, "oldest.py\nmiddle.py\nnewest.py\n")
    res = await rs.remote_grep("h", "foo", ".", output_mode="files_with_matches")
    assert res["output_mode"] == "files_with_matches"
    assert res["engine"] == "rg"
    assert res["files"] == ["newest.py", "middle.py", "oldest.py"]
    assert res["num_files"] == 3
    assert res["truncated"] is False
    # default mode uses -l and --sort modified
    assert "-l" in rec[0] and "--sort modified" in rec[0]


@pytest.mark.asyncio
async def test_grep_default_mode_is_files_with_matches(monkeypatch):
    _install(monkeypatch, "a.py\n")
    res = await rs.remote_grep("h", "foo", ".")
    assert res["output_mode"] == "files_with_matches"


# ── content ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_content_parses_matches_and_relativizes(monkeypatch):
    out = "\n".join([
        _match("/repo/src/a.py", 10, "def foo():"),
        _match("/repo/src/b.py", 22, "    foo()"),
    ])
    rec = _install(monkeypatch, out)
    res = await rs.remote_grep("h", "foo", "/repo", output_mode="content")
    assert res["output_mode"] == "content"
    assert res["total"] == 2
    assert res["matches"][0] == {"file": "src/a.py", "line": 10, "text": "def foo():"}
    assert res["matches"][1]["file"] == "src/b.py"
    assert "--json" in rec[0]


@pytest.mark.asyncio
async def test_grep_content_head_limit_caps_total_and_flags_truncated(monkeypatch):
    out = "\n".join(_match("a.py", i, f"line{i}") for i in range(1, 11))
    _install(monkeypatch, out)
    res = await rs.remote_grep("h", "x", ".", output_mode="content", head_limit=4)
    assert res["returned"] == 4
    assert res["total"] == 10
    assert res["truncated"] is True
    assert [m["line"] for m in res["matches"]] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_grep_content_offset_pagination(monkeypatch):
    out = "\n".join(_match("a.py", i, f"line{i}") for i in range(1, 11))
    _install(monkeypatch, out)
    res = await rs.remote_grep("h", "x", ".", output_mode="content",
                               head_limit=3, offset=4)
    assert [m["line"] for m in res["matches"]] == [5, 6, 7]
    assert res["offset"] == 4
    assert res["truncated"] is True


@pytest.mark.asyncio
async def test_grep_content_context_lines_tagged(monkeypatch):
    out = "\n".join([
        _context("a.py", 9, "before"),
        _match("a.py", 10, "MATCH"),
        _context("a.py", 11, "after"),
    ])
    rec = _install(monkeypatch, out)
    res = await rs.remote_grep("h", "MATCH", ".", output_mode="content", context=1)
    kinds = [("context" in m) for m in res["matches"]]
    assert kinds == [True, False, True]
    assert "-C 1" in rec[0]


# ── count ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_count_mode(monkeypatch):
    rec = _install(monkeypatch, "src/a.py:3\nsrc/b.py:5\n")
    res = await rs.remote_grep("h", "foo", ".", output_mode="count")
    assert res["output_mode"] == "count"
    assert res["counts"] == [{"file": "src/a.py", "count": 3},
                             {"file": "src/b.py", "count": 5}]
    assert res["total_matches"] == 8
    assert res["num_files"] == 2
    assert "-c" in rec[0]


# ── filters reach the rg command line ───────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_filters_in_command(monkeypatch):
    rec = _install(monkeypatch, "a.py\n")
    await rs.remote_grep("h", "foo", ".", glob="*.py", file_type="py",
                         ignore_case=True, multiline=True)
    cmd = rec[0]
    assert "-i" in cmd and "--multiline" in cmd
    assert "--glob" in cmd and "--type" in cmd


# ── fallback to grep when rg is absent ──────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_fallback_to_grep(monkeypatch):
    rec = _install(monkeypatch, "src/a.py:10:def foo():\n", rg=False)
    res = await rs.remote_grep("h", "foo", ".", output_mode="content")
    assert res["engine"] == "grep"
    assert res["matches"][0] == {"file": "src/a.py", "line": 10,
                                 "text": "def foo():"}
    assert rec[0].startswith("grep")


@pytest.mark.asyncio
async def test_grep_fallback_forces_filename_and_ere(monkeypatch):
    """Regression: grep -r drops the filename prefix for a single-file arg and
    treats `|` as a literal under BRE. Every fallback mode must pass -H and -E
    so parsing survives and alternation matches the rg path."""
    for mode in ("content", "count", "files_with_matches"):
        rec = _install(monkeypatch, "f.txt:1:x\n", rg=False)
        await rs.remote_grep("h", "a|b", "/etc/caddy/Caddyfile",
                             output_mode=mode)
        toks = rec[0].split()
        assert "-H" in toks, f"{mode}: missing -H -> {rec[0]}"
        assert "-E" in toks, f"{mode}: missing -E -> {rec[0]}"


# ── portal_glob ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_glob_newest_first_and_shape(monkeypatch):
    rec = _install(monkeypatch, "old.py\nmid.py\nnew.py\n")
    res = await rs.remote_glob("h", "**/*.py", ".")
    assert res["filenames"] == ["new.py", "mid.py", "old.py"]
    assert res["num_files"] == 3
    assert res["truncated"] is False
    assert "duration_ms" in res
    # uses rg --files -g, ignoring .gitignore (--no-ignore)
    assert "rg --files" in rec[0] and "--no-ignore" in rec[0] and "-g" in rec[0]


@pytest.mark.asyncio
async def test_glob_hard_caps_at_100(monkeypatch):
    out = "\n".join(f"f{i}.py" for i in range(150))
    _install(monkeypatch, out)
    res = await rs.remote_glob("h", "*.py", ".")
    assert len(res["filenames"]) == 100
    assert res["num_files"] == 150
    assert res["truncated"] is True


@pytest.mark.asyncio
async def test_glob_find_fallback_preserves_dotfiles(monkeypatch):
    """When rg is absent, glob uses `find .` which emits './'-prefixed paths.
    Stripping the prefix must use removeprefix, NOT lstrip (which greedily ate
    leading dots: './.github/x' -> 'github/x', './.env' -> 'env'). Regression."""
    rec = _install(
        monkeypatch,
        "./.github/workflows/ci.yml\n./.env\n./src/app.py\n",
        rg=False,
    )
    res = await rs.remote_glob("h", "**/*", ".")
    assert res["engine"] == "find"
    assert set(res["filenames"]) == {
        ".github/workflows/ci.yml", ".env", "src/app.py"}
    assert "find ." in rec[0]
