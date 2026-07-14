"""Behavioral tests for the OSC 133 (FinalTerm) persistent-session protocol.

These pin the new command-boundary protocol that replaced the in-band sentinel:

  * the shell emits ``\\x1b]133;D;<exit>\\x07`` after each command and we parse
    it out of the raw byte stream (exit codes, literal-marker immunity);
  * bootstrap drains every script-sourcing D via a readiness marker so the
    first business command's D is its own (no whole-session off-by-one);
  * a command that wedges on an interactive prompt is Ctrl-C'd and the session
    is KEPT alive (cwd/env survive) — ``InteractivePromptBlocked``;
  * multi-step ``remote_bash_many`` runs a sequence in one session with state
    carried across steps;
  * shell sniffing picks bash / zsh and falls back to bash for anything else.

The fakes drive ``SessionManager`` / ``remote_bash`` without a real SSH channel.
The channel runs in bytes mode (encoding=None), so all scripted chunks are
bytes and the fake stdin records bytes.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from portal_mcp_server import session_manager as smod


def _d(code: int) -> bytes:
    """OSC 133 ; D ; <code> ST — the command-finished marker."""
    return f"\x1b]133;D;{code}\x07".encode()


_MARKER_RE = re.compile(rb"__P133_READY_[0-9a-f]+__")


# ─── Reactive fake process ───────────────────────────────────────────────────

class _Stdin:
    def __init__(self, proc):
        self.proc = proc
        self.writes: list[bytes] = []
        self.fail_with: BaseException | None = None

    def write(self, data: bytes) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.writes.append(data)
        self.proc._on_write(data)


class _Stdout:
    def __init__(self, proc):
        self.proc = proc

    async def read(self, _n: int) -> bytes:
        # Block (briefly) when nothing is queued, like a real PTY awaiting more
        # output — the caller's wait_for(0.3) loops over this.
        while not self.proc.queue:
            await asyncio.sleep(0.005)
        return self.proc.queue.pop(0)


class _Proc:
    """Reactive fake SSHClientProcess.

    Bootstrap is automatic: when a write contains the readiness marker (the
    integration-script payload ends with ``echo <marker>``) we queue
    ``boot_strays`` stray D's, then the marker text, then its D — exercising the
    drain. Per-command behaviour is delegated to ``on_command(proc, data)``.
    """

    def __init__(self, on_command=None, *, with_drain=False, boot_strays=1):
        self.queue: list[bytes] = [b""] if with_drain else []
        self.stdin = _Stdin(self)
        self.stdout = _Stdout(self)
        self.on_command = on_command or (lambda proc, data: None)
        self.boot_strays = boot_strays
        self.closed = False

    def _on_write(self, data: bytes) -> None:
        if b"__P133_READY_" in data:          # readiness marker (separate write)
            m = _MARKER_RE.search(data)
            self.queue.append(m.group(0) + b"\r\n")
            self.queue.append(_d(0))
            return
        if b"__p133_done" in data:            # integration-script injection
            for _ in range(self.boot_strays):
                self.queue.append(_d(0))      # bootstrap D's the drain consumes
            return
        self.on_command(self, data)

    def close(self) -> None:
        self.closed = True

    async def wait(self) -> int:
        return 0


def _install(monkeypatch, proc, *, probe_stdout: str | None = None):
    """Point ConnectionManager at a conn that hands back ``proc`` and, if
    given, returns ``probe_stdout`` from ``conn.run`` (the shell probe)."""
    from portal_mcp_server import connection_manager

    class _Conn:
        async def create_process(self, *a, **k):
            return proc

        async def run(self, *a, **k):
            class _R:
                stdout = probe_stdout or ""
                stderr = ""
                returncode = 0
            return _R()

    async def fake_get(self, host):
        return _Conn()

    def fake_release(self, host, conn):
        pass

    monkeypatch.setattr(connection_manager.ConnectionManager, "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager, "release_connection", fake_release)


async def _make_ready_session(monkeypatch, proc):
    """create_session + bootstrap_osc133 against ``proc`` (which must be
    constructed with ``with_drain=True``). Returns (sm, sid)."""
    monkeypatch.setattr(smod, "BOOT_QUIET", 0.05)  # keep the drain window short
    _install(monkeypatch, proc)
    sm = smod.SessionManager()
    sid = await sm.create_session("h", shell="bash")
    await sm.bootstrap_osc133(sid, smod.OSC133_INTEGRATION_SCRIPTS["bash"])
    return sm, sid


# ════════════════════════════════════════════════════════════════════════════
#  Bootstrap: strict drain → first command's D is its own
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bootstrap_strict_drain(monkeypatch):
    """Several stray bootstrap D's precede the readiness marker; bootstrap must
    consume them all so the first business command reads ITS OWN D (exit 7),
    not a leftover bootstrap D (exit 0)."""
    def on_command(proc, data):
        proc.queue.append(_d(7))

    proc = _Proc(on_command, with_drain=True, boot_strays=3)
    sm, sid = await _make_ready_session(monkeypatch, proc)

    out, code, truncated = await sm.execute_in_session(sid, "echo hi", timeout=5)
    assert code == 7, "first command must get its own D, not a stray bootstrap D"
    assert truncated is False


# ════════════════════════════════════════════════════════════════════════════
#  Exit codes + output
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_exit_code_zero_and_nonzero(monkeypatch):
    seq = iter([
        (b"hi\r\n", 0),     # echo hi
        (b"", 1),           # false
        (b"", 42),          # bash -c 'exit 42'
    ])

    def on_command(proc, data):
        out, code = next(seq)
        proc.queue.append(out + _d(code))

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)

    out, code, _ = await sm.execute_in_session(sid, "echo hi", timeout=5)
    assert (out, code) == ("hi", 0)
    _, code, _ = await sm.execute_in_session(sid, "false", timeout=5)
    assert code == 1
    _, code, _ = await sm.execute_in_session(sid, "bash -c 'exit 42'", timeout=5)
    assert code == 42


@pytest.mark.asyncio
async def test_aborted_marker_maps_to_minus_two(monkeypatch):
    """A FinalTerm abort-D (no exit digits) maps to exit_code -2."""
    def on_command(proc, data):
        proc.queue.append(b"\x1b]133;D\x07")  # bare D, no ; <exit>

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)
    _, code, _ = await sm.execute_in_session(sid, "weird", timeout=5)
    assert code == -2


# ════════════════════════════════════════════════════════════════════════════
#  Literal marker text in output must NOT be mistaken for a real D
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_literal_marker_text_not_fooled(monkeypatch):
    """Output that literally spells ``]133;D;0`` (no ESC byte) is preserved and
    only the real ESC-prefixed D ends the command."""
    def on_command(proc, data):
        proc.queue.append(b"]133;D;0 not a real marker\r\n" + _d(5))

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)
    out, code, _ = await sm.execute_in_session(sid, "echo ...", timeout=5)
    assert code == 5, "the real ESC-prefixed D must win"
    assert "]133;D;0 not a real marker" in out


# ════════════════════════════════════════════════════════════════════════════
#  Large output → truncated flag, exit code still correct
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_large_output_truncated(monkeypatch):
    monkeypatch.setattr(smod, "MAX_OUTPUT_BYTES", 1024)

    def on_command(proc, data):
        proc.queue.append(b"X" * 4096 + _d(0))

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)
    out, code, truncated = await sm.execute_in_session(sid, "yes | head", timeout=5)
    assert code == 0
    assert truncated is True
    assert len(out) <= 1024


# ════════════════════════════════════════════════════════════════════════════
#  Interactive prompt → soft cancel → session preserved
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_interactive_prompt_blocked_then_session_preserved(monkeypatch):
    monkeypatch.setattr(smod, "INTERACTIVE_GRACE_SEC", 0.2)
    monkeypatch.setattr(smod, "SOFT_CANCEL_TIMEOUT", 1.0)

    state = {"cmd": 0}

    def on_command(proc, data):
        if data == b"\x03":              # Ctrl-C → shell recovers, D arrives
            proc.queue.append(_d(130))
            return
        state["cmd"] += 1
        if state["cmd"] == 1:
            proc.queue.append(b"[sudo] password for user: ")  # wedge, no D
        else:
            proc.queue.append(b"recovered\r\n" + _d(0))

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)

    with pytest.raises(smod.InteractivePromptBlocked) as exc:
        await sm.execute_in_session(sid, "sudo whoami", timeout=10)
    assert exc.value.session_id == sid

    # Session is PRESERVED (not invalidated) — the next command still works and
    # the soft-cancel verified the shell came back.
    sm._get(sid)  # no KeyError
    out, code, _ = await sm.execute_in_session(sid, "echo recovered", timeout=5)
    assert code == 0
    assert "recovered" in out


@pytest.mark.asyncio
async def test_soft_cancel_fails_falls_back_to_session_dead(monkeypatch):
    """If no D arrives after Ctrl-C, the session is untrustworthy → SessionDead
    + eviction."""
    monkeypatch.setattr(smod, "INTERACTIVE_GRACE_SEC", 0.2)
    monkeypatch.setattr(smod, "SOFT_CANCEL_TIMEOUT", 0.4)

    def on_command(proc, data):
        if data == b"\x03":
            return  # no recovery D
        proc.queue.append(b"[sudo] password for user: ")

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)

    with pytest.raises(smod.SessionDead):
        await sm.execute_in_session(sid, "sudo whoami", timeout=10)
    with pytest.raises(KeyError):
        sm._get(sid)


@pytest.mark.asyncio
async def test_channel_dead_still_session_dead(monkeypatch):
    """A broken stdin (channel closed by peer) raises SessionDead and evicts —
    same paradigm as the encoding-resilience tests, kept under OSC 133."""
    proc = _Proc(with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)
    proc.stdin.fail_with = BrokenPipeError("channel gone")

    with pytest.raises(smod.SessionDead) as exc:
        await sm.execute_in_session(sid, "echo hi", timeout=5)
    assert isinstance(exc.value.original, BrokenPipeError)
    with pytest.raises(KeyError):
        sm._get(sid)


# ════════════════════════════════════════════════════════════════════════════
#  Multi-step (remote_bash_many)
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def clean_mgr():
    from portal_mcp_server import remote_bash
    smgr = smod.get_session_manager()
    smgr._sessions.clear()
    remote_bash._HOST_SESSIONS.clear()
    remote_bash._HOST_LOCKS.clear()
    yield
    smgr._sessions.clear()
    remote_bash._HOST_SESSIONS.clear()
    remote_bash._HOST_LOCKS.clear()


def _register_multistep(monkeypatch, on_command):
    """Pre-register a ready session in the GLOBAL manager and short-circuit
    _ensure_session so remote_bash_many runs its loop against ``proc``."""
    from portal_mcp_server import remote_bash
    smgr = smod.get_session_manager()
    proc = _Proc(on_command)  # no drain, already "ready"
    sess = smod.ShellSession(session_id="sid-m", host_name="h",
                             process=proc, conn=object())
    smgr._sessions["sid-m"] = sess

    async def fake_ensure(host):
        return "sid-m"

    monkeypatch.setattr(remote_bash, "_ensure_session", fake_ensure)
    return remote_bash, proc


def _scripted(responses):
    """on_command that plays (output, exit_code) tuples in order; the string
    'block' wedges (prompt then recover on Ctrl-C)."""
    it = iter(responses)

    def on_command(proc, data):
        if data == b"\x03":
            proc.queue.append(_d(130))
            return
        try:
            resp = next(it)
        except StopIteration:
            proc.queue.append(_d(0))
            return
        if resp == "block":
            proc.queue.append(b"[sudo] password for user: ")
            return
        if resp == "dead":
            proc.queue.append(b"")  # EOF on read → SessionDead
            return
        out, code = resp
        proc.queue.append(out.encode() + _d(code))

    return on_command


@pytest.mark.asyncio
async def test_multi_step_sequence(monkeypatch, clean_mgr):
    on_cmd = _scripted([("", 0), ("", 0), ("/tmp", 0), ("bar", 0), ("", 1)])
    remote_bash, _ = _register_multistep(monkeypatch, on_cmd)

    res = await remote_bash.remote_bash_many("h", [
        "cd /tmp", "export FOO=bar", "pwd", "echo $FOO", "false", "echo skipped",
    ])
    cmds = [r["command"] for r in res["results"]]
    assert cmds == ["cd /tmp", "export FOO=bar", "pwd", "echo $FOO", "false"]
    assert res["results"][-1]["exit_code"] == 1
    assert res["stopped_at"] == "false"
    assert "echo skipped" not in cmds


@pytest.mark.asyncio
async def test_multi_step_stop_on_error_false(monkeypatch, clean_mgr):
    on_cmd = _scripted([("", 0), ("", 1), ("still", 0)])
    remote_bash, _ = _register_multistep(monkeypatch, on_cmd)

    res = await remote_bash.remote_bash_many(
        "h", ["cd /tmp", "false", "echo still-runs"], stop_on_error=False)
    assert len(res["results"]) == 3
    assert res["results"][1]["exit_code"] == 1
    assert res["results"][2]["exit_code"] == 0
    assert "stopped_at" not in res


@pytest.mark.asyncio
async def test_multi_step_interactive_blocked_in_middle(monkeypatch, clean_mgr):
    monkeypatch.setattr(smod, "INTERACTIVE_GRACE_SEC", 0.2)
    monkeypatch.setattr(smod, "SOFT_CANCEL_TIMEOUT", 1.0)
    on_cmd = _scripted([("a", 0), "block", ("b", 0)])
    remote_bash, _ = _register_multistep(monkeypatch, on_cmd)

    res = await remote_bash.remote_bash_many(
        "h", ["echo a", "sudo whoami", "echo b"])
    assert len(res["results"]) == 2
    blocked = res["results"][1]
    assert blocked["error"] == "interactive_prompt_blocked"
    assert blocked["session_preserved"] is True
    assert res["stopped_at"] == "sudo whoami"
    # Session preserved across the block.
    assert smod.get_session_manager()._get("sid-m")


@pytest.mark.asyncio
async def test_multi_step_session_dead_stops_batch(monkeypatch, clean_mgr):
    """A channel death mid-batch records session_dead, evicts the host->sid
    cache, and stops — we don't silently rebuild and lose cwd/env continuity."""
    on_cmd = _scripted([("a", 0), "dead", ("never", 0)])
    remote_bash, _ = _register_multistep(monkeypatch, on_cmd)

    res = await remote_bash.remote_bash_many("h", ["echo a", "boom", "echo never"])
    assert len(res["results"]) == 2
    assert res["results"][0]["exit_code"] == 0
    assert res["results"][1]["error"] == "session_dead"
    assert res["stopped_at"] == "boom"
    assert "h" not in remote_bash._HOST_SESSIONS


@pytest.mark.asyncio
async def test_multi_step_single_command_compat(monkeypatch, clean_mgr):
    """Single-step remote_bash returns the legacy single dict (no 'results');
    remote_bash_many returns the multi shape (with 'results')."""
    on_cmd = _scripted([("only", 0)])
    remote_bash, _ = _register_multistep(monkeypatch, on_cmd)

    single = await remote_bash.remote_bash("h", "echo only")
    assert "results" not in single
    assert single["command"] == "echo only"
    assert single["exit_code"] == 0
    assert single["output"] == "only"

    on_cmd2 = _scripted([("only", 0)])
    _register_multistep(monkeypatch, on_cmd2)
    multi = await remote_bash.remote_bash_many("h", ["echo only"])
    assert "results" in multi
    assert multi["results"][0]["command"] == "echo only"


# ════════════════════════════════════════════════════════════════════════════
#  Shell sniffing
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("probe, expected", [
    ("/bin/bash\n/usr/bin/bash\n", "bash"),
    ("/usr/bin/zsh\n/usr/bin/bash\n", "zsh"),
    ("/bin/dash\n/usr/bin/bash\n", "bash"),   # unsupported shell, bash present
    ("/bin/sh\n/bin/bash\n", "bash"),
])
async def test_shell_detection(monkeypatch, probe, expected):
    from portal_mcp_server import remote_bash
    _install(monkeypatch, _Proc(), probe_stdout=probe)
    assert await remote_bash._detect_remote_shell("h") == expected


@pytest.mark.asyncio
async def test_shell_detection_bash_required(monkeypatch):
    """Unsupported shell AND no bash → BashRequired (no silent degrade)."""
    from portal_mcp_server import remote_bash
    _install(monkeypatch, _Proc(), probe_stdout="/bin/sh\n\n")
    with pytest.raises(smod.BashRequired):
        await remote_bash._detect_remote_shell("h")


@pytest.mark.asyncio
async def test_setup_session_selects_detected_shell_script(monkeypatch):
    """_setup_session must inject the integration script matching the sniffed
    shell (zsh here), spawning with the zsh command line."""
    from portal_mcp_server import remote_bash
    recorded = {}

    async def fake_detect(host):
        return "zsh"

    async def fake_create(self, host, shell="bash"):
        recorded["shell"] = shell
        return "sid-z"

    async def fake_boot(self, sid, script):
        recorded["script"] = script

    monkeypatch.setattr(remote_bash, "_detect_remote_shell", fake_detect)
    monkeypatch.setattr(smod.SessionManager, "create_session", fake_create)
    monkeypatch.setattr(smod.SessionManager, "bootstrap_osc133", fake_boot)

    sid = await remote_bash._setup_session("h")
    assert sid == "sid-z"
    assert recorded["shell"] == "zsh"
    assert recorded["script"] == smod.OSC133_INTEGRATION_SCRIPTS["zsh"]


def test_integration_scripts_present():
    """bash + zsh integration scripts are activated; fish deferred (not shipped
    active until spiked on real hardware)."""
    assert set(smod.OSC133_INTEGRATION_SCRIPTS) == {"bash", "zsh"}
    assert smod.SUPPORTED_SHELLS == ("bash", "zsh")
    assert "fish" not in smod.SHELL_COMMAND_LINES


# ════════════════════════════════════════════════════════════════════════════
#  Multi-line command → ONE compound command (brace-group wrap)
#
#  Regression for the bug where a ``command`` with embedded newlines was written
#  to the interactive shell line-by-line, so PROMPT_COMMAND fired (and emitted a
#  D marker) per line. The reader returns at the FIRST D, so only line one ran;
#  the rest stayed queued and desynced every later call by one marker. The fix
#  wraps multi-line commands in a brace group ``{ … }`` so the shell stays in PS2
#  continuation and emits exactly one D for the whole command.
# ════════════════════════════════════════════════════════════════════════════

def test_wrap_compound_unit():
    w = smod._wrap_compound
    # Single line (incl. `;`-joined): unchanged — the hot path stays identical.
    assert w("echo hi") == "echo hi"
    assert w("echo a; sleep 1; echo b") == "echo a; sleep 1; echo b"
    # Multi-line: wrapped as one brace group.
    assert w("echo a\nsleep 1\necho b") == "{\necho a\nsleep 1\necho b\n}"
    # A trailing newline is trimmed so `}` sits on its own line after the last cmd.
    assert w("echo a\n") == "{\necho a\n}"
    assert w("echo a\nb\n\n") == "{\necho a\nb\n}"


@pytest.mark.asyncio
async def test_multiline_command_sends_brace_group(monkeypatch):
    """A multi-line command reaches the shell as a single ``{ … }`` write, and
    its exit code (from the one D) is passed through."""
    seen: list[bytes] = []

    def on_command(proc, data):
        seen.append(data)
        proc.queue.append(b"grouped\r\n" + _d(7))

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)
    out, code, _ = await sm.execute_in_session(
        sid, "echo a\nsleep 1\necho b", timeout=5)

    assert len(seen) == 1, "the whole multi-line command is a single write"
    assert seen[0].decode() == "{\necho a\nsleep 1\necho b\n}\n"
    assert out == "grouped"
    assert code == 7, "the one D's exit code is the group's last command"


@pytest.mark.asyncio
async def test_singleline_command_not_wrapped(monkeypatch):
    """Single-line commands (incl. `;`-joined) are written verbatim — no brace
    wrap, so the well-exercised path is byte-for-byte unchanged."""
    seen: list[bytes] = []

    def on_command(proc, data):
        seen.append(data)
        proc.queue.append(_d(0))

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)
    await sm.execute_in_session(sid, "echo a; sleep 1; echo b", timeout=5)

    assert seen == [b"echo a; sleep 1; echo b\n"]


@pytest.mark.asyncio
async def test_multiline_does_not_desync_next_call(monkeypatch):
    """Model an interactive shell that emits one D per top-level line for a raw
    multi-line write, but ONE D for a brace group. With the fix a multi-line
    command consumes exactly one D, so the NEXT call reads its own output and no
    stray marker is left queued.

    This fails if the brace wrap is reverted: a raw 3-line write would queue 3
    D's, the reader would return at the first, and 2 strays would remain to
    desync the follow-up — leaving ``proc.queue`` non-empty.
    """
    def on_command(proc, data):
        text = data.decode()
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            proc.queue.append(b"grouped\r\n" + _d(0))           # one compound D
        else:
            lines = [ln for ln in text.split("\n") if ln.strip()]
            proc.queue.append(b"out\r\n")
            for _ in lines:                                      # one D per line
                proc.queue.append(_d(0))

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)

    out1, code1, _ = await sm.execute_in_session(
        sid, "echo A\nsleep 1\necho B", timeout=5)
    assert out1 == "grouped", "multi-line must run as one compound command"
    assert code1 == 0

    out2, _, _ = await sm.execute_in_session(sid, "echo NEXT", timeout=5)
    assert out2 == "out", "follow-up reads its own output (no leftover D)"
    assert proc.queue == [], "no stray D markers left queued — no desync"


# ── timeout interrupts the remote command and resyncs (or drops) the session ──
@pytest.mark.asyncio
async def test_timeout_ctrl_c_recovers_and_preserves_session(monkeypatch):
    """On timeout the command is Ctrl-C'd; if a clean prompt comes back the
    session (and cwd/env) is preserved and reusable."""
    monkeypatch.setattr(smod, "SOFT_CANCEL_TIMEOUT", 1.0)
    state = {"cmd": 0}

    def on_command(proc, data):
        if data == b"\x03":                 # Ctrl-C after the timeout -> clean D
            proc.queue.append(_d(130))
            return
        state["cmd"] += 1
        if state["cmd"] == 1:
            proc.queue.append(b"working...\r\n")   # output but never a D -> times out
        else:
            proc.queue.append(b"after\r\n" + _d(0))

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)

    out, code, _ = await sm.execute_in_session(sid, "slow", timeout=0.3)
    assert code is None and "[timeout]" in out
    assert b"\x03" in proc.stdin.writes            # the remote command was interrupted
    sm._get(sid)                                    # session PRESERVED (no KeyError)
    out2, code2, _ = await sm.execute_in_session(sid, "echo after", timeout=5)
    assert code2 == 0 and "after" in out2


@pytest.mark.asyncio
async def test_timeout_no_recovery_invalidates_session(monkeypatch):
    """If Ctrl-C doesn't bring back a clean prompt, the session is desynced and
    must be dropped so it can't corrupt the next command."""
    monkeypatch.setattr(smod, "SOFT_CANCEL_TIMEOUT", 0.3)

    def on_command(proc, data):
        if data == b"\x03":
            return                          # no recovery D
        proc.queue.append(b"stuck...\r\n")  # never a D

    proc = _Proc(on_command, with_drain=True)
    sm, sid = await _make_ready_session(monkeypatch, proc)

    out, code, _ = await sm.execute_in_session(sid, "hang", timeout=0.3)
    assert code is None and "[timeout]" in out
    assert b"\x03" in proc.stdin.writes
    with pytest.raises(KeyError):
        sm._get(sid)                        # session invalidated (desynced)
