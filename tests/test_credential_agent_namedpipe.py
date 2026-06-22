r"""W-XPLAT (Windows): live named-pipe credential-agent round trip.

This is the Windows counterpart to the AF_UNIX ``agent_socket`` round-trip
tests (which are skipped on Windows in conftest). It exercises the *real*
transport end to end on a real Windows runner in CI:

    serve_forever()  --(\\.\pipe\...)-->  ssh_creds / secrets_store client

i.e. the named-pipe server (``serve_async_pipe`` via ``start_serving_pipe``)
plus the file-handle pipe client (``_request_named_pipe``), both using the
transport-agnostic newline-delimited framing. There is no way to run this on
Linux/macOS — named pipes are Windows-only — so the module is skipped off
Windows and the GitHub Actions ``windows-latest`` job is its verification.
"""
from __future__ import annotations

import sys
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="named pipes are Windows-only; verified by the windows-latest CI job",
)


@pytest.fixture
def pipe_agent(_isolate_credential_agent, monkeypatch):
    """Start a credential agent on a private per-run named pipe.

    Declares ``_isolate_credential_agent`` as an explicit dependency (mirroring
    conftest's ``agent_socket``) so this fixture's ``PORTAL_CREDENTIAL_AGENT_SOCKET``
    setenv deterministically wins over the autouse isolation fixture's, and both
    server and client resolve to the same pipe.
    """
    from portal_mcp_server import credential_agent

    pipe_name = rf"\\.\pipe\portal-mcp-test-{uuid.uuid4().hex}"
    monkeypatch.setenv("PORTAL_CREDENTIAL_AGENT_SOCKET", pipe_name)

    thread = threading.Thread(target=credential_agent.serve_forever, daemon=True)
    thread.start()

    # The pipe client retries on a not-yet-ready pipe, but block here until the
    # server answers a status probe so failures point at setup, not round trip.
    deadline = time.monotonic() + 10
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if credential_agent.status().get("status") == "ok":
                break
        except Exception as e:  # pragma: no cover - readiness polling
            last_exc = e
        time.sleep(0.05)
    else:  # pragma: no cover - only on a broken Windows runner
        pytest.fail(f"named-pipe agent never became ready: {last_exc!r}")
    return pipe_name


def test_named_pipe_status_ok(pipe_agent):
    from portal_mcp_server import credential_agent

    assert credential_agent.status().get("status") == "ok"


def test_named_pipe_ssh_password_round_trip(pipe_agent):
    from portal_mcp_server import ssh_creds

    ssh_creds.clear_ssh_password()
    resp = ssh_creds.send_ssh_password("web01", "live-secret", ttl=60)
    assert resp.get("status") == "ok", resp
    assert ssh_creds.fetch_ssh_password_from_agent("web01") == "live-secret"


def test_named_pipe_passphrase_round_trip(pipe_agent):
    from portal_mcp_server import passphrase_creds

    passphrase_creds.clear_passphrase()
    resp = passphrase_creds.send_passphrase("web01", "key-secret", ttl=60)
    assert resp.get("status") == "ok", resp
    assert passphrase_creds.fetch_passphrase_from_agent("web01") == "key-secret"


def test_named_pipe_secret_round_trip(pipe_agent):
    from portal_mcp_server import credential_agent, secrets_store

    resp = secrets_store.send_secret("API_TOKEN", "shhh", ttl=60)
    assert resp.get("status") == "ok", resp
    assert credential_agent.fetch("secret", "API_TOKEN") == "shhh"


def test_named_pipe_clear_removes_entry(pipe_agent):
    from portal_mcp_server import credential_agent, ssh_creds

    ssh_creds.send_ssh_password("web02", "temp", ttl=60)
    assert ssh_creds.fetch_ssh_password_from_agent("web02") == "temp"
    credential_agent.clear("ssh", "web02")
    assert ssh_creds.fetch_ssh_password_from_agent("web02") is None


# ── Real scheduled-task install/query/delete (Windows Task Scheduler) ────────
#
# Exercises the *install* path end to end on a real Windows runner: register the
# per-user logon task from generated XML, confirm Task Scheduler accepted it,
# then uninstall and confirm it's gone. enable_now is False — we don't /Run it
# (an InteractiveToken task won't start in CI's non-interactive session; the
# live transport is covered by the round-trip tests above).

@pytest.fixture
def _cleanup_task():
    yield
    import subprocess

    from portal_mcp_server.paths import default_scheduled_task_name
    subprocess.run(["schtasks", "/Delete", "/TN", default_scheduled_task_name(),
                    "/F"], capture_output=True)


def test_scheduled_task_install_query_uninstall(_cleanup_task):
    import subprocess

    from portal_mcp_server import credential_agent
    from portal_mcp_server.paths import default_scheduled_task_name

    name = default_scheduled_task_name()

    res = credential_agent.install_agent(enable_now=False)
    assert res["backend"] == "schtasks"

    query = subprocess.run(["schtasks", "/Query", "/TN", name],
                           capture_output=True, text=True)
    assert query.returncode == 0, query.stderr or query.stdout

    out = credential_agent.uninstall_agent(stop_now=True, remove_config=True)
    assert out["backend"] == "schtasks"

    gone = subprocess.run(["schtasks", "/Query", "/TN", name],
                          capture_output=True, text=True)
    assert gone.returncode != 0, "task should be deleted"
