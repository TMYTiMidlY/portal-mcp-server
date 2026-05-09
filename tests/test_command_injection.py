"""Tests proving command-injection sinks are now plugged.

For each historical sink we capture the *exact* shell string that would have
been sent to ``conn.run``. The pre-fix expectation is documented in a comment
("BEFORE the fix this was X") so the regression intent is obvious.

Strategy: monkeypatch :class:`ConnectionManager.get_connection` to return a
``DummyConn`` that records every call to ``.run()`` and ``.create_process()``.
No real SSH server is required.
"""
from __future__ import annotations

import pytest


class DummyResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class DummyConn:
    def __init__(self):
        self.calls = []  # list of (args_str, env_dict)

    async def run(self, cmd, env=None, check=False, **_):
        self.calls.append((cmd, env))
        return DummyResult()

    def is_closed(self):
        return False


@pytest.fixture
def dummy_conn(monkeypatch):
    """Replace the connection pool with a recorder. Returns the recorder."""
    from ssh_remote_mcp import connection_manager

    conn = DummyConn()

    async def fake_get_connection(self, host_name):
        return conn

    def fake_release(self, host_name, c):
        return None

    monkeypatch.setattr(
        connection_manager.ConnectionManager,
        "get_connection",
        fake_get_connection,
    )
    monkeypatch.setattr(
        connection_manager.ConnectionManager,
        "release_connection",
        fake_release,
    )
    return conn


# ─── Helper to assert no unquoted shell metacharacters leak through ────────

def _assert_no_unquoted_metas(payload: str, payload_pieces: list[str]):
    """For each suspicious piece, verify it doesn't appear *unquoted* in the
    final shell line. ``shlex.quote`` wraps everything in single quotes, so
    after stripping quoted regions the dangerous bytes must be gone.
    """
    import re
    # Crude but effective: remove all single-quoted regions, then check.
    stripped = re.sub(r"'[^']*'", "", payload)
    for piece in payload_pieces:
        assert piece not in stripped, (
            f"Unquoted dangerous fragment {piece!r} survived in: {payload!r}"
        )


# ════════════════════════════════════════════════════════════════════════════
#  shell_engine.ssh_exec — `cwd` parameter
# ════════════════════════════════════════════════════════════════════════════

class TestCwdInjection:
    @pytest.mark.asyncio
    async def test_normal_cwd_quoted(self, dummy_conn):
        from ssh_remote_mcp.shell_engine import ssh_exec
        await ssh_exec("h", "ls", cwd="/var/log")
        cmd, _ = dummy_conn.calls[-1]
        assert cmd == "cd /var/log && ls"

    @pytest.mark.asyncio
    async def test_metachar_cwd_neutralized(self, dummy_conn):
        # BEFORE the fix this produced:  cd /tmp; rm -rf / && id
        # which would have run `rm -rf /` as a separate command.
        from ssh_remote_mcp.shell_engine import ssh_exec
        await ssh_exec("h", "id", cwd="/tmp; rm -rf /")
        cmd, _ = dummy_conn.calls[-1]
        _assert_no_unquoted_metas(cmd, ["; rm -rf /", "&& rm"])
        assert cmd.startswith("cd '/tmp; rm -rf /'")
        assert cmd.endswith("&& id")

    @pytest.mark.asyncio
    async def test_command_substitution_cwd_neutralized(self, dummy_conn):
        from ssh_remote_mcp.shell_engine import ssh_exec
        await ssh_exec("h", "id", cwd="$(reboot)")
        cmd, _ = dummy_conn.calls[-1]
        # The dollar-sign / parens must be inside single quotes.
        _assert_no_unquoted_metas(cmd, ["$(", ")"])

    @pytest.mark.asyncio
    async def test_nul_in_cwd_rejected_no_exec(self, dummy_conn):
        from ssh_remote_mcp.shell_engine import ssh_exec
        out = await ssh_exec("h", "id", cwd="/tmp\x00/etc")
        assert "Invalid input" in out["error"]
        assert dummy_conn.calls == []  # we never reached conn.run


# ════════════════════════════════════════════════════════════════════════════
#  shell_engine.ssh_exec_with_env — env kv injection
# ════════════════════════════════════════════════════════════════════════════

class TestEnvInjection:
    @pytest.mark.asyncio
    async def test_env_passed_via_protocol_not_string(self, dummy_conn):
        # BEFORE the fix the command was:
        #   env "FOO"='$(reboot)' id
        # which the remote shell *might* still expand depending on quoting.
        # AFTER: env is passed through asyncssh's env channel — no shell
        # interpolation possible.
        from ssh_remote_mcp.shell_engine import ssh_exec_with_env
        await ssh_exec_with_env("h", "id", {"FOO": "$(reboot)"})
        cmd, env = dummy_conn.calls[-1]
        assert cmd == "id"  # no `env FOO=...` prefix anymore
        assert env == {"FOO": "$(reboot)"}

    @pytest.mark.asyncio
    async def test_bad_env_key_rejected_no_exec(self, dummy_conn):
        from ssh_remote_mcp.shell_engine import ssh_exec_with_env
        out = await ssh_exec_with_env("h", "id", {"BAD KEY": "x"})
        assert "Invalid input" in out["error"]
        assert dummy_conn.calls == []

    @pytest.mark.asyncio
    async def test_nul_in_env_value_rejected(self, dummy_conn):
        from ssh_remote_mcp.shell_engine import ssh_exec_with_env
        out = await ssh_exec_with_env("h", "id", {"FOO": "x\x00y"})
        assert "Invalid input" in out["error"]
        assert dummy_conn.calls == []


# ════════════════════════════════════════════════════════════════════════════
#  shell_engine.ssh_exec_script — interpreter injection
# ════════════════════════════════════════════════════════════════════════════

class TestScriptInjection:
    @pytest.mark.asyncio
    async def test_unknown_interpreter_rejected(self, dummy_conn):
        from ssh_remote_mcp.shell_engine import ssh_exec_script
        out = await ssh_exec_script("h", "echo hi", interpreter="bash; reboot")
        assert "Invalid interpreter" in out["error"]
        # Must not have opened SFTP / written anything
        assert dummy_conn.calls == []

    @pytest.mark.asyncio
    async def test_path_traversal_interpreter_rejected(self, dummy_conn):
        from ssh_remote_mcp.shell_engine import ssh_exec_script
        out = await ssh_exec_script("h", "echo hi", interpreter="../bash")
        assert "Invalid interpreter" in out["error"]


# ════════════════════════════════════════════════════════════════════════════
#  process_manager.ssh_kill_process — signal & pid validation
# ════════════════════════════════════════════════════════════════════════════

class TestKillInjection:
    @pytest.mark.asyncio
    async def test_signal_injection_rejected(self, dummy_conn):
        from ssh_remote_mcp.process_manager import ssh_kill_process
        out = await ssh_kill_process("h", 123, "TERM; rm -rf /")
        assert "rejected" in out
        assert dummy_conn.calls == []

    @pytest.mark.asyncio
    async def test_negative_pid_rejected(self, dummy_conn):
        from ssh_remote_mcp.process_manager import ssh_kill_process
        out = await ssh_kill_process("h", -1, "TERM")
        assert "rejected" in out
        assert dummy_conn.calls == []

    @pytest.mark.asyncio
    async def test_normal_kill_call(self, dummy_conn):
        from ssh_remote_mcp.process_manager import ssh_kill_process
        await ssh_kill_process("h", 1234, "term")
        cmd, _ = dummy_conn.calls[-1]
        assert cmd == "kill -TERM 1234"


# ════════════════════════════════════════════════════════════════════════════
#  process_manager.ssh_background_process — log_file / command quoting
# ════════════════════════════════════════════════════════════════════════════

class TestBackgroundProcessInjection:
    @pytest.mark.asyncio
    async def test_log_file_quoted(self, dummy_conn):
        from ssh_remote_mcp.process_manager import ssh_background_process
        # A log_file with a space + redirect would otherwise re-direct
        # stderr to the wrong place.
        await ssh_background_process(
            "h", "echo hi", name="proc", log_file="/tmp/a b.log; reboot"
        )
        cmd, _ = dummy_conn.calls[-1]
        _assert_no_unquoted_metas(cmd, [" reboot", "; reboot"])


# ════════════════════════════════════════════════════════════════════════════
#  session_manager.set_env — key/value injection
# ════════════════════════════════════════════════════════════════════════════

class TestSessionEnvInjection:
    def test_bad_env_key_rejected(self):
        from ssh_remote_mcp.session_manager import SessionManager, ShellSession

        sm = SessionManager()

        class _FakeStdin:
            def __init__(self):
                self.writes = []
            def write(self, x):
                self.writes.append(x)

        class _FakeProc:
            def __init__(self):
                self.stdin = _FakeStdin()

        s = ShellSession(
            session_id="x", host_name="h",
            process=_FakeProc(),  # type: ignore[arg-type]
        )
        sm._sessions["x"] = s
        with pytest.raises(ValueError):
            sm.set_env("x", "BAD KEY", "v")
        # Nothing was written to the shell.
        assert s.process.stdin.writes == []

    def test_value_with_metachars_quoted(self):
        from ssh_remote_mcp.session_manager import SessionManager, ShellSession

        sm = SessionManager()

        class _FakeStdin:
            def __init__(self):
                self.writes = []
            def write(self, x):
                self.writes.append(x)

        class _FakeProc:
            def __init__(self):
                self.stdin = _FakeStdin()

        s = ShellSession(session_id="x", host_name="h",
                         process=_FakeProc())  # type: ignore[arg-type]
        sm._sessions["x"] = s
        sm.set_env("x", "FOO", "$(reboot)")
        # BEFORE the fix the line was:  export FOO='$(reboot)'\n  (via repr)
        # which is *coincidentally* safe but only because Python's repr happened
        # to use single quotes. AFTER the fix we explicitly use shlex.quote.
        line = s.process.stdin.writes[-1]
        assert line.startswith("export FOO=")
        assert "$(reboot)" not in line.split("=", 1)[1].replace("'", "")[:0]
        # Definitively verify the value is single-quoted.
        assert line == "export FOO='$(reboot)'\n"
