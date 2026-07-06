"""sudo password provisioning — out-of-band sources, never via the LLM.

Mirrors the philosophy of test_password_auth.py: a sudo password must reach
``sudo -S`` only through the in-memory cache (populated by the ``portal sudo set``
side-channel) or a host's ``sudo_password_command`` — never as an MCP tool
parameter that would land in the model's context.
"""
from __future__ import annotations

import inspect

import pytest


# ────────────────────────────────────────────────────────────────────────────
#  LLM-facing safety invariants
# ────────────────────────────────────────────────────────────────────────────

def test_portal_exec_has_no_password_parameter():
    """portal_exec exposes a boolean `use_sudo` switch, never a password."""
    from portal_mcp_server.cli import portal_exec
    params = inspect.signature(portal_exec).parameters
    assert "use_sudo" in params
    assert params["use_sudo"].annotation is bool
    assert not any("password" in p.lower() or "passwd" in p.lower() for p in params), (
        "portal_exec must not take a password — it would leak into tool-call traces"
    )


def test_portal_transfer_has_no_password_parameter():
    from portal_mcp_server.cli import portal_transfer
    params = inspect.signature(portal_transfer).parameters
    assert not any("password" in p.lower() for p in params)


def test_sudo_password_command_is_a_command_not_a_value():
    """HostConfig carries a *command* that prints the password, mirroring
    password_command — the secret itself is never stored on the config."""
    from portal_mcp_server.connection_manager import HostConfig
    cfg = HostConfig(name="x", host="1.2.3.4")
    assert hasattr(cfg, "sudo_password_command")
    assert cfg.sudo_password_command is None
    assert not hasattr(cfg, "sudo_password")


def test_hosts_yaml_can_opt_into_sudo_password_same_as_ssh(tmp_path):
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts:\n"
        "  web01:\n"
        "    host: 1.2.3.4\n"
        "    auth: password\n"
        "    sudo_password_same_as_ssh: true\n"
        "  web02:\n"
        "    host: 1.2.3.5\n"
    )
    m = ConnectionManager(hosts_yaml=yml)

    assert m._registry["web01"].sudo_password_same_as_ssh is True
    assert m.should_cache_ssh_password_as_sudo("web01") is True
    assert m.should_cache_ssh_password_as_sudo("web02") is False
    assert m.should_cache_ssh_password_as_sudo("missing") is False


# ────────────────────────────────────────────────────────────────────────────
#  In-memory TTL cache
# ────────────────────────────────────────────────────────────────────────────

def test_cache_set_get_clear():
    from portal_mcp_server import sudo_creds
    sudo_creds.clear_sudo_password()
    sudo_creds.cache_sudo_password("web01", "s3cret", ttl=60)
    assert sudo_creds._get_cached("web01") == "s3cret"
    sudo_creds.clear_sudo_password("web01")
    assert sudo_creds._get_cached("web01") is None


def test_cache_ttl_expiry():
    from portal_mcp_server import sudo_creds
    sudo_creds.clear_sudo_password()
    sudo_creds.cache_sudo_password("web01", "s3cret", ttl=-1)  # already expired
    assert sudo_creds._get_cached("web01") is None


def test_clear_all():
    from portal_mcp_server import sudo_creds
    sudo_creds.cache_sudo_password("a", "x", ttl=60)
    sudo_creds.cache_sudo_password("b", "y", ttl=60)
    sudo_creds.clear_sudo_password()
    assert sudo_creds._get_cached("a") is None
    assert sudo_creds._get_cached("b") is None


# ────────────────────────────────────────────────────────────────────────────
#  resolve_sudo_password: cache first, then sudo_password_command
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_prefers_cache(monkeypatch):
    from portal_mcp_server import sudo_creds
    sudo_creds.clear_sudo_password()
    sudo_creds.cache_sudo_password("web01", "from-cache", ttl=60)
    pw = await sudo_creds.resolve_sudo_password("web01")
    assert pw == "from-cache"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_command(monkeypatch):
    from portal_mcp_server import sudo_creds, connection_manager

    sudo_creds.clear_sudo_password()

    async def fake_cmd(self, host):
        return "from-command" if host == "web01" else None

    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "sudo_password_command_for", fake_cmd)
    pw = await sudo_creds.resolve_sudo_password("web01")
    assert pw == "from-command"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_source(monkeypatch):
    from portal_mcp_server import sudo_creds, connection_manager

    sudo_creds.clear_sudo_password()

    async def fake_cmd(self, host):
        return None

    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "sudo_password_command_for", fake_cmd)
    assert await sudo_creds.resolve_sudo_password("nope") is None


# ────────────────────────────────────────────────────────────────────────────
#  Live agent round trip (1b): `portal sudo set` client → per-user agent cache
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_control_socket_roundtrip(agent_socket):
    from portal_mcp_server import sudo_creds

    sudo_creds.clear_sudo_password()

    assert sudo_creds.control_socket_path() == agent_socket
    assert oct(agent_socket.stat().st_mode & 0o777) == oct(0o600)

    resp = sudo_creds.send_sudo_password("web01", "live-secret", ttl=60)
    assert resp.get("status") == "ok", resp
    assert await sudo_creds.resolve_sudo_password("web01") == "live-secret"


# ────────────────────────────────────────────────────────────────────────────
#  Leak invariant: the sudo password never reaches the result or the audit log
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sudo_password_never_in_audit_or_output(monkeypatch, tmp_path):
    """The sudo password is fed to `sudo -S` on stdin only — it must appear in
    neither the returned result nor the audit entry. The named-secret path has
    this regression pin (test_secret_injection.py); the sudo path lacked one."""
    from portal_mcp_server import cli, security, sudo_creds
    from portal_mcp_server.audit import get_history

    pol = security.SecurityPolicy(policies_yaml=tmp_path / "none.yaml")
    monkeypatch.setattr(cli, "get_policy", lambda: pol)

    PW = "SUP3R-SECRET-PW"
    sudo_creds.clear_sudo_password()
    sudo_creds.cache_sudo_password("web01", PW, ttl=60)

    captured = {}

    async def fake_sudo_exec(host, cmd, password, env=None, timeout=60):
        captured["password"] = password  # the real impl feeds this on stdin only
        return {"host": host, "command": cmd, "exit_code": 0,
                "stdout": "uid=0(root)", "stderr": ""}

    monkeypatch.setattr(cli, "_re_sudo_exec", fake_sudo_exec)

    out = await cli.portal_exec(host="web01", command="id", use_sudo=True)
    assert captured["password"] == PW           # mechanism got it (for sudo -S)
    assert PW not in out                         # ...never surfaced to the agent
    latest = get_history(limit=1)[0]
    assert PW not in str(latest)                 # ...nor to the audit log
    assert latest["command"] == "sudo: id"       # command/name is fine to record
