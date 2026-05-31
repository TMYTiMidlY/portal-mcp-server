"""sudo password provisioning — out-of-band sources, never via the LLM.

Mirrors the philosophy of test_password_auth.py: a sudo password must reach
``sudo -S`` only through the in-memory cache (populated by the ``sudo-login``
side-channel) or a host's ``sudo_password_command`` — never as an MCP tool
parameter that would land in the model's context.
"""
from __future__ import annotations

import inspect
import time

import pytest


# ────────────────────────────────────────────────────────────────────────────
#  LLM-facing safety invariants
# ────────────────────────────────────────────────────────────────────────────

def test_portal_bash_has_no_password_parameter():
    """portal_bash exposes a boolean `use_sudo` switch, never a password."""
    from portal_mcp_server.cli import portal_bash
    params = inspect.signature(portal_bash).parameters
    assert "use_sudo" in params
    assert params["use_sudo"].annotation is bool
    assert not any("password" in p.lower() or "passwd" in p.lower() for p in params), (
        "portal_bash must not take a password — it would leak into tool-call traces"
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
#  Live control-socket round trip (1b): sudo-login client → running server cache
# ────────────────────────────────────────────────────────────────────────────

def test_control_socket_roundtrip(tmp_path, monkeypatch):
    from portal_mcp_server import sudo_creds

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sudo_creds.clear_sudo_password()

    thread = sudo_creds.start_control_server()
    assert thread is not None

    sock = sudo_creds.control_socket_path()
    deadline = time.monotonic() + 5
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sock.exists(), "control socket never appeared"
    assert oct(sock.stat().st_mode & 0o777) == oct(0o600)

    resp = sudo_creds.send_sudo_password("web01", "live-secret", ttl=60)
    assert resp.get("status") == "ok", resp

    deadline = time.monotonic() + 2
    while sudo_creds._get_cached("web01") is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sudo_creds._get_cached("web01") == "live-secret"


def test_second_server_defers_when_socket_live(tmp_path, monkeypatch):
    from portal_mcp_server import sudo_creds

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    first = sudo_creds.start_control_server()
    assert first is not None
    sock = sudo_creds.control_socket_path()
    deadline = time.monotonic() + 5
    while not sock.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    # A second start must detect the live socket and decline (return None)
    # instead of clobbering the first server's socket file.
    second = sudo_creds.start_control_server()
    assert second is None
