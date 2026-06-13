"""
ssh-shell-mcp Test Suite
Tests all 5 functional areas: exec, file transfer, sessions, orchestration, tunnels.

HOW TO RUN:
  pip install pytest pytest-asyncio asyncssh
  pytest tests/test_live_ssh.py -v

REQUIREMENTS:
  - SSH server accessible at PORTAL_TEST_HOST (default: 127.0.0.1:22)
  - Set env vars: PORTAL_TEST_HOST, PORTAL_TEST_USER, PORTAL_TEST_KEY_PATH
  - Or edit the PORTAL_TEST_* constants below.

The tests use a real SSH connection; they do NOT mock asyncssh.
"""
import asyncio
import json
import os
import tempfile
import pytest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Test configuration ─────────────────────────────────────────────────────
TEST_HOST_NAME = "test-localhost"
TEST_HOST      = os.environ.get("PORTAL_TEST_HOST", "127.0.0.1")
TEST_PORT      = int(os.environ.get("PORTAL_TEST_PORT", "22"))
TEST_USER      = os.environ.get("PORTAL_TEST_USER", os.environ.get("USER", "root"))
TEST_KEY       = os.environ.get("PORTAL_TEST_KEY_PATH", os.path.expanduser("~/.ssh/id_ed25519"))

# ── Bootstrap: register the test host ─────────────────────────────────────
from portal_mcp_server.connection_manager import get_manager  # noqa: E402
get_manager().register_host(
    name=TEST_HOST_NAME,
    host=TEST_HOST,
    port=TEST_PORT,
    user=TEST_USER,
    key=TEST_KEY if os.path.exists(TEST_KEY) else None,
)

# ════════════════════════════════════════════════════════
# TEST 1: Remote command execution
# ════════════════════════════════════════════════════════

class TestCommandExecution:

    @pytest.mark.asyncio
    async def test_basic_exec(self):
        """Execute a simple command and verify output."""
        from portal_mcp_server.shell_engine import ssh_exec
        result = await ssh_exec(TEST_HOST_NAME, "echo hello-mcp")
        assert result["exit_code"] == 0, f"Exit code: {result}"
        assert "hello-mcp" in result["stdout"]
        print(f"  stdout: {result['stdout'].strip()}")

    @pytest.mark.asyncio
    async def test_exec_with_cwd(self):
        """Execute command in a specific working directory."""
        from portal_mcp_server.shell_engine import ssh_exec
        result = await ssh_exec(TEST_HOST_NAME, "pwd", cwd="/tmp")
        assert result["exit_code"] == 0
        assert "/tmp" in result["stdout"]

    @pytest.mark.asyncio
    async def test_exec_batch(self):
        """Run a batch of commands and check all succeed."""
        from portal_mcp_server.shell_engine import ssh_exec_batch
        cmds = ["echo step1", "echo step2", "echo step3"]
        results = await ssh_exec_batch(TEST_HOST_NAME, cmds)
        assert len(results) == 3
        for r in results:
            assert r["exit_code"] == 0, f"Step failed: {r}"

    @pytest.mark.asyncio
    async def test_exec_failure_captured(self):
        """Non-zero exit code is captured, not raised."""
        from portal_mcp_server.shell_engine import ssh_exec
        result = await ssh_exec(TEST_HOST_NAME, "exit 42", timeout=5)
        assert result["exit_code"] == 42

    @pytest.mark.asyncio
    async def test_exec_with_env(self):
        """Environment variable injection."""
        from portal_mcp_server.shell_engine import ssh_exec_with_env
        result = await ssh_exec_with_env(
            TEST_HOST_NAME, "echo $MY_VAR", {"MY_VAR": "injected"}
        )
        assert "injected" in result["stdout"]

    @pytest.mark.asyncio
    async def test_exec_script(self):
        """Upload and run a script."""
        from portal_mcp_server.shell_engine import ssh_exec_script
        script = "#!/bin/bash\necho 'script-output'\ndate\n"
        result = await ssh_exec_script(TEST_HOST_NAME, script)
        assert result["exit_code"] == 0
        assert "script-output" in result["stdout"]

    @pytest.mark.asyncio
    async def test_exec_stream(self):
        """Stream command output line by line."""
        from portal_mcp_server.shell_engine import ssh_exec_stream
        lines = []
        async for line in ssh_exec_stream(TEST_HOST_NAME, "seq 1 5"):
            lines.append(line)
        assert lines == ["1", "2", "3", "4", "5"]


# ════════════════════════════════════════════════════════
# TEST 2: File transfer (SFTP)
# ════════════════════════════════════════════════════════

class TestFileTransfer:

    @pytest.mark.asyncio
    async def test_upload_and_download(self):
        """Upload a file and download it back, verify content."""
        from portal_mcp_server.file_ops import ssh_upload_file, ssh_download_file, ssh_delete_file
        content = "ssh-shell-mcp test file\nline2\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            local_up = f.name
        remote = "/tmp/_mcp_test_upload.txt"
        local_down = local_up + ".down"
        try:
            up_msg = await ssh_upload_file(TEST_HOST_NAME, local_up, remote)
            assert up_msg["status"] == "ok", up_msg
            down_msg = await ssh_download_file(TEST_HOST_NAME, remote, local_down)
            assert down_msg["status"] == "ok", down_msg
            with open(local_down) as f:
                assert f.read() == content
        finally:
            await ssh_delete_file(TEST_HOST_NAME, remote)
            for p in [local_up, local_down]:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    @pytest.mark.asyncio
    async def test_write_and_read(self):
        """Write a file remotely and read it back."""
        from portal_mcp_server.file_ops import ssh_write_file, ssh_read_file, ssh_delete_file
        remote = "/tmp/_mcp_test_write.txt"
        payload = "written by ssh-shell-mcp\n"
        await ssh_write_file(TEST_HOST_NAME, remote, payload)
        content = await ssh_read_file(TEST_HOST_NAME, remote)
        assert content == payload
        await ssh_delete_file(TEST_HOST_NAME, remote)

    @pytest.mark.asyncio
    async def test_list_directory(self):
        """List /tmp and verify it returns entries."""
        from portal_mcp_server.file_ops import ssh_list_directory
        entries = await ssh_list_directory(TEST_HOST_NAME, "/tmp")
        assert isinstance(entries, list)
        assert len(entries) > 0
        assert "name" in entries[0]

    @pytest.mark.asyncio
    async def test_sync_directory(self):
        """Sync a local temp dir to remote."""
        from portal_mcp_server.file_ops import ssh_sync_directory
        with tempfile.TemporaryDirectory() as d:
            for i in range(3):
                with open(os.path.join(d, f"file{i}.txt"), "w") as f:
                    f.write(f"content {i}")
            result = await ssh_sync_directory(TEST_HOST_NAME, d, "/tmp/_mcp_sync_test")
        assert result["status"] == "ok", result
        assert result["uploaded"] == 3, result


# ════════════════════════════════════════════════════════
# TEST 3: Persistent shell sessions
# ════════════════════════════════════════════════════════

async def _bootstrapped_session(sm):
    """Create a persistent session and inject the OSC 133 integration script,
    mirroring what remote_bash._setup_session does — without it the shell emits
    no D markers and every command times out."""
    from portal_mcp_server.session_manager import OSC133_INTEGRATION_SCRIPTS
    sid = await sm.create_session(TEST_HOST_NAME)
    await sm.bootstrap_osc133(sid, OSC133_INTEGRATION_SCRIPTS["bash"])
    return sid


class TestPersistentSessions:

    @pytest.mark.asyncio
    async def test_create_and_exec(self):
        """Create a session, run commands, verify state persists."""
        from portal_mcp_server.session_manager import get_session_manager
        sm = get_session_manager()
        sid = await _bootstrapped_session(sm)
        assert sid, "Session ID must not be empty"
        # CWD change must persist across calls
        out1, _, _ = await sm.execute_in_session(sid, "cd /tmp && pwd")
        out2, _, _ = await sm.execute_in_session(sid, "pwd")
        assert "/tmp" in out2, f"CWD did not persist: {out2}"
        await sm.close_session(sid)

    @pytest.mark.asyncio
    async def test_env_injection(self):
        """Inject env var into session and read it back."""
        from portal_mcp_server.session_manager import get_session_manager
        sm = get_session_manager()
        sid = await _bootstrapped_session(sm)
        sm.set_env(sid, "MCP_TEST_VAR", "hello123")
        await asyncio.sleep(0.3)
        out, _, _ = await sm.execute_in_session(sid, "echo $MCP_TEST_VAR")
        assert "hello123" in out
        await sm.close_session(sid)

    @pytest.mark.asyncio
    async def test_session_list(self):
        """Session appears in list while active, gone after close."""
        from portal_mcp_server.session_manager import get_session_manager
        sm = get_session_manager()
        sid = await _bootstrapped_session(sm)
        sessions = sm.list_sessions()
        ids = [s["session_id"] for s in sessions]
        assert sid in ids
        await sm.close_session(sid)
        sessions_after = sm.list_sessions()
        ids_after = [s["session_id"] for s in sessions_after]
        assert sid not in ids_after

    @pytest.mark.asyncio
    async def test_multi_session_isolation(self):
        """Two concurrent sessions maintain independent CWDs."""
        from portal_mcp_server.session_manager import get_session_manager
        sm = get_session_manager()
        sid1 = await _bootstrapped_session(sm)
        sid2 = await _bootstrapped_session(sm)
        await sm.execute_in_session(sid1, "cd /tmp")
        await sm.execute_in_session(sid2, "cd /var")
        out1, _, _ = await sm.execute_in_session(sid1, "pwd")
        out2, _, _ = await sm.execute_in_session(sid2, "pwd")
        assert "/tmp" in out1
        assert "/var" in out2
        await sm.close_session(sid1)
        await sm.close_session(sid2)


# ════════════════════════════════════════════════════════
# TEST 4: Multi-host orchestration (now portal_exec fan-out)
# ════════════════════════════════════════════════════════

class TestOrchestration:

    @pytest.fixture(autouse=True)
    def register_extra_hosts(self):
        """Register two aliases of localhost for fleet tests."""
        mgr = get_manager()
        for alias in ["fleet-01", "fleet-02"]:
            mgr.register_host(alias, TEST_HOST, TEST_PORT, TEST_USER,
                              key=TEST_KEY if os.path.exists(TEST_KEY) else None,
                              tags=["fleet"])

    @pytest.mark.asyncio
    async def test_parallel_exec(self):
        """Same command on multiple hosts simultaneously."""
        from portal_mcp_server import cli
        results = json.loads(await cli.portal_exec(
            host=["fleet-01", "fleet-02"], command="hostname"))
        assert len(results) == 2
        for r in results:
            assert r["exit_code"] == 0, f"Parallel exec failed: {r}"

    @pytest.mark.asyncio
    async def test_rolling_exec(self):
        """Sequential (serialized) exec with delay."""
        from portal_mcp_server import cli
        results = json.loads(await cli.portal_exec(
            host=["fleet-01", "fleet-02"], command="echo rolling",
            serialize=True, delay_s=0.1))
        assert len(results) == 2
        for r in results:
            assert "rolling" in r.get("stdout", "")

    @pytest.mark.asyncio
    async def test_group_exec(self):
        """Execute on all hosts with a matching tag."""
        from portal_mcp_server import cli
        results = json.loads(await cli.portal_exec(
            group_tag="fleet", command="echo tagged"))
        assert len(results) >= 1
        for r in results:
            assert r["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_command_sequence(self):
        """Run a multi-step command sequence and verify all steps complete."""
        from portal_mcp_server import cli
        out = json.loads(await cli.portal_exec(
            host=TEST_HOST_NAME,
            commands=["echo step1", "echo step2", "echo step3"]))
        assert len(out["results"]) == 3
        assert out["host"] == TEST_HOST_NAME



# ════════════════════════════════════════════════════════
# TEST 5: SSH tunnels
# ════════════════════════════════════════════════════════

class TestTunnels:

    @pytest.mark.asyncio
    async def test_local_port_forward(self):
        """Open a local tunnel and verify it's tracked."""
        from portal_mcp_server.network_tools import get_tunnel_manager
        tm = get_tunnel_manager()
        result = await tm.open_local_forward(
            TEST_HOST_NAME, local_port=0,
            remote_host="127.0.0.1", remote_port=22
        )
        assert "tunnel_id" in result, f"Tunnel open failed: {result}"
        tid = result["tunnel_id"]
        tunnels = tm.list_tunnels()
        assert any(t["tunnel_id"] == tid for t in tunnels)
        close_msg = await tm.close_tunnel(tid)
        assert tid in close_msg

    @pytest.mark.asyncio
    async def test_socks_proxy(self):
        """Open a SOCKS5 proxy and verify it's tracked."""
        from portal_mcp_server.network_tools import get_tunnel_manager
        tm = get_tunnel_manager()
        result = await tm.open_dynamic_proxy(TEST_HOST_NAME, local_port=0)
        assert "socks5" in result, f"SOCKS proxy failed: {result}"
        tid = result["tunnel_id"]
        await tm.close_tunnel(tid)

    @pytest.mark.asyncio
    async def test_tunnel_lifecycle(self):
        """Full lifecycle: open → list → close → gone."""
        from portal_mcp_server.network_tools import get_tunnel_manager
        tm = get_tunnel_manager()
        r = await tm.open_local_forward(TEST_HOST_NAME, 0, "127.0.0.1", 22)
        tid = r["tunnel_id"]
        before = {t["tunnel_id"] for t in tm.list_tunnels()}
        assert tid in before
        await tm.close_tunnel(tid)
        after = {t["tunnel_id"] for t in tm.list_tunnels()}
        assert tid not in after


# ════════════════════════════════════════════════════════
# TEST 6: Security policy
# ════════════════════════════════════════════════════════

class TestSecurity:

    async def test_command_blocklist(self):
        """Blocked commands are rejected."""
        from portal_mcp_server.security import SecurityPolicy
        from portal_mcp_server.safety_net import SafetyNetChecker
        pol = SecurityPolicy.__new__(SecurityPolicy)
        pol.host_allowlist = []
        pol.command_blocklist = ["rm -rf /"]
        pol.command_allowlist = []
        pol.rate_limit_rps = 1000
        pol._rate_counters = {}
        pol.safety_net = SafetyNetChecker(enabled=False)
        err = await pol.check_command("rm -rf /")
        assert err is not None

    def test_host_allowlist(self):
        """Hosts not in allowlist are rejected."""
        from portal_mcp_server.security import SecurityPolicy
        pol = SecurityPolicy.__new__(SecurityPolicy)
        pol.host_allowlist = ["allowed-host"]
        pol.command_blocklist = []
        pol.command_allowlist = []
        pol.rate_limit_rps = 1000
        pol._rate_counters = {}
        assert pol.check_host("blocked-host") is not None
        assert pol.check_host("allowed-host") is None
