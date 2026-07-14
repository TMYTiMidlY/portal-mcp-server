"""T1 — remote_patch / remote_read use_sudo: read+write root-owned files via a
sudo path that preserves the patch hash contract, owner/mode, and atomicity.
"""
import types

import pytest

from portal_mcp_server import remote_text_editor as rte
from portal_mcp_server import sudo_creds


class _FakeSFTPFile:
    def __init__(self, state, name):
        self._state, self._name, self._buf = state, name, ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self._state["store"][self._name] = self._buf
        if self._name.startswith(".portal-mcp-stage"):
            self._state["staged"] = self._buf
        return False

    async def write(self, data):
        self._buf += data


class _FakeSFTP:
    def __init__(self, state):
        self._state = state

    def open(self, name, mode, encoding=None):
        return _FakeSFTPFile(self._state, name)

    async def realpath(self, name):
        return "/home/u/" + name

    async def remove(self, name):
        self._state["store"].pop(name, None)

    def exit(self):
        pass

    async def wait_closed(self):
        pass


class _FakeConn:
    def __init__(self, state):
        self._state = state

    async def run(self, cmd, input=None, **k):
        self._state["runs"].append((cmd, input))
        if "cat --" in cmd:
            return types.SimpleNamespace(
                returncode=0, stdout=self._state["content"], stderr="")
        if "stat -c" in cmd:
            return types.SimpleNamespace(
                returncode=0, stdout=self._state["stat"], stderr="")
        if "bash -c" in cmd:  # the install/place script
            self._state["content"] = self._state.get("staged", "")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    async def start_sftp_client(self):
        return _FakeSFTP(self._state)


class _FakeMgr:
    def __init__(self, state):
        self._state = state

    async def get_connection(self, host):
        return _FakeConn(self._state)

    def release_connection(self, host, conn):
        pass


@pytest.fixture
def wired(monkeypatch):
    state = {"content": "line1\nline2\nline3\n", "stat": "root root 644\n",
             "store": {}, "runs": []}
    from portal_mcp_server import connection_manager
    mgr = _FakeMgr(state)
    # rte's staging path uses its import-bound get_manager; the shared
    # remote_bash._run_sudo_raw resolves get_manager fresh from
    # connection_manager — patch both so every sudo hop hits the fake pool.
    monkeypatch.setattr(rte, "get_manager", lambda: mgr)
    monkeypatch.setattr(connection_manager, "get_manager", lambda: mgr)

    async def fake_pw(host):
        return "sekret"
    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", fake_pw)
    return state


# ── _sudo_cat preserves exact bytes + feeds password on stdin ────────────────
@pytest.mark.asyncio
async def test_sudo_cat_preserves_content_and_feeds_password(wired):
    out = await rte._sudo_cat("h", "/etc/shadow", "utf-8")
    assert out == "line1\nline2\nline3\n"           # trailing newline preserved
    cmd, stdin = wired["runs"][-1]
    assert cmd.startswith("sudo -S -k -p '' cat -- ") and stdin == "sekret\n"


@pytest.mark.asyncio
async def test_sudo_cat_missing_password_raises(wired, monkeypatch):
    async def none_pw(host):
        return None
    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", none_pw)
    with pytest.raises(rte.RemoteEditError, match="sudo password"):
        await rte._sudo_cat("h", "/etc/shadow", "utf-8")


# ── _sudo_write_atomic: stat owner/mode -> stage -> cp/chown/chmod/mv ─────────
@pytest.mark.asyncio
async def test_sudo_write_atomic_preserves_owner_mode(wired):
    await rte._sudo_write_atomic("h", "/etc/hosts", "NEWCONTENT\n", "utf-8")
    assert wired["staged"] == "NEWCONTENT\n"            # content staged via SFTP
    place = [c for c, _ in wired["runs"] if "bash -c" in c][0]
    assert "chown root:root" in place and "chmod 644" in place
    assert "cp --" in place and "mv -f --" in place     # atomic rename place


@pytest.mark.asyncio
async def test_sudo_write_rejects_weird_owner(wired):
    wired["stat"] = "ro;rm -rf / root 644\n"            # injected metachars
    with pytest.raises(rte.RemoteEditError, match="unusual owner"):
        await rte._sudo_write_atomic("h", "/etc/hosts", "x\n", "utf-8")


# ── remote_read(use_sudo=True) uses sudo cat with a valid hash ───────────────
@pytest.mark.asyncio
async def test_remote_read_use_sudo(wired):
    res = await rte.remote_read("h", "/etc/shadow", use_sudo=True)
    assert res["content"] == "line1\nline2\nline3\n"
    assert res["file_hash"] == rte._sha256("line1\nline2\nline3\n")


# ── remote_patch(use_sudo=True) end-to-end: hash-checked, atomic sudo write ──
@pytest.mark.asyncio
async def test_remote_patch_use_sudo_end_to_end(wired):
    full = "line1\nline2\nline3\n"
    patch = [{"start": 1, "end": None, "contents": "NEW\n",
              "range_hash": rte._sha256(full)}]
    res = await rte.remote_patch("h", "/etc/hosts", file_hash=rte._sha256(full),
                                 patches=patch, use_sudo=True)
    assert res["result"] == "ok"
    assert res["file_hash"] == rte._sha256("NEW\n")
    assert wired["content"] == "NEW\n"                  # target actually updated


@pytest.mark.asyncio
async def test_remote_patch_use_sudo_hash_mismatch(wired):
    res = await rte.remote_patch("h", "/etc/hosts", file_hash="deadbeef",
                                 patches=[{"start": 1, "end": None,
                                           "contents": "x\n", "range_hash": ""}],
                                 use_sudo=True)
    assert res["result"] == "error" and "hash mismatch" in res["reason"].lower()
