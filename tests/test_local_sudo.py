"""Local sudo for portal_local_exec — the LOCAL counterpart of the remote
``portal_exec(use_sudo=True)`` path.

The reserved identity is ``<local>`` (sudo_creds.LOCAL_SUDO_KEY): illegal as a
hostname, so it can never collide with an SSH host named ``local`` /
``localhost``. The sudo password reaches ``sudo -S`` only through the in-memory
cache / per-user agent (``portal sudo set-local``) or a TOP-LEVEL ``<local>:``
section's ``sudo_password_command`` in hosts.yaml — never as a tool parameter.
"""
from __future__ import annotations

import inspect

import pytest

from portal_mcp_server.cli import ToolError


# ────────────────────────────────────────────────────────────────────────────
#  LLM-facing safety invariant
# ────────────────────────────────────────────────────────────────────────────

def test_portal_local_exec_exposes_use_sudo_bool_not_password():
    from portal_mcp_server import cli
    params = inspect.signature(cli.portal_local_exec).parameters
    assert "use_sudo" in params
    assert params["use_sudo"].annotation is bool
    assert not any("password" in p.lower() or "passwd" in p.lower() for p in params), (
        "portal_local_exec must not take a password parameter"
    )


def test_local_sudo_key_is_hostname_illegal():
    """The reserved local identity must be un-representable as a hostname so it
    can never shadow a real remote named ``local`` / ``localhost``."""
    from portal_mcp_server.sudo_creds import LOCAL_SUDO_KEY
    assert LOCAL_SUDO_KEY == "<local>"
    assert "<" in LOCAL_SUDO_KEY and ">" in LOCAL_SUDO_KEY


# ────────────────────────────────────────────────────────────────────────────
#  Top-level ``<local>:`` section parsing (NOT a host under ``hosts:``)
# ────────────────────────────────────────────────────────────────────────────

def test_top_level_local_section_parsed(tmp_path):
    from portal_mcp_server.connection_manager import ConnectionManager
    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts:\n"
        "  web01:\n"
        "    host: 1.2.3.4\n"
        "<local>:\n"
        "  sudo_password_command: printf '%s' s3cret\n"
    )
    m = ConnectionManager(hosts_yaml=yml)
    # The local section feeds local sudo, but is NOT a connectable host.
    assert "<local>" not in m._registry
    assert "web01" in m._registry
    assert m._local_sudo_password_command == "printf '%s' s3cret"


@pytest.mark.asyncio
async def test_sudo_password_command_for_local_runs_command(tmp_path):
    from portal_mcp_server.connection_manager import ConnectionManager
    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts: {}\n"
        "<local>:\n"
        "  sudo_password_command: printf '%s' loc-pw\n"
    )
    m = ConnectionManager(hosts_yaml=yml)
    assert await m.sudo_password_command_for("<local>") == "loc-pw"
    # An unknown remote name still resolves to None (no host, no command).
    assert await m.sudo_password_command_for("nope") is None


def test_top_level_local_absent_leaves_command_none(tmp_path):
    from portal_mcp_server.connection_manager import ConnectionManager
    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts:\n  web01:\n    host: 1.2.3.4\n")
    m = ConnectionManager(hosts_yaml=yml)
    assert m._local_sudo_password_command is None


# ────────────────────────────────────────────────────────────────────────────
#  resolve_sudo_password("<local>"): cache → agent → top-level command
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_local_prefers_cache():
    from portal_mcp_server import sudo_creds
    sudo_creds.clear_sudo_password()
    sudo_creds.cache_sudo_password("<local>", "loc-cache-pw", ttl=60)
    assert await sudo_creds.resolve_sudo_password("<local>") == "loc-cache-pw"


@pytest.mark.asyncio
async def test_resolve_local_falls_back_to_top_level_command(monkeypatch, tmp_path):
    from portal_mcp_server import sudo_creds, connection_manager
    sudo_creds.clear_sudo_password()
    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts: {}\n"
        "<local>:\n"
        "  sudo_password_command: printf '%s' loc-cmd-pw\n"
    )
    m = connection_manager.ConnectionManager(hosts_yaml=yml)
    monkeypatch.setattr(connection_manager, "get_manager", lambda: m)
    assert await sudo_creds.resolve_sudo_password("<local>") == "loc-cmd-pw"


@pytest.mark.asyncio
async def test_resolve_local_none_when_no_source(monkeypatch, tmp_path):
    from portal_mcp_server import sudo_creds, connection_manager
    sudo_creds.clear_sudo_password()
    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")  # no local section
    m = connection_manager.ConnectionManager(hosts_yaml=yml)
    monkeypatch.setattr(connection_manager, "get_manager", lambda: m)
    assert await sudo_creds.resolve_sudo_password("<local>") is None


# ────────────────────────────────────────────────────────────────────────────
#  portal_local_exec(use_sudo=True) behaviour
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_use_sudo_and_secrets_mutually_exclusive(monkeypatch, tmp_path):
    from portal_mcp_server import cli, security
    monkeypatch.setenv("PORTAL_ALLOW_LOCAL_EXEC", "1")
    pol = security.SecurityPolicy(policies_yaml=tmp_path / "none.yaml")
    monkeypatch.setattr(cli, "get_policy", lambda: pol)
    with pytest.raises(ToolError, match="cannot be combined"):
        await cli.portal_local_exec(command="id", secrets=["x"], use_sudo=True)


@pytest.mark.asyncio
async def test_use_sudo_missing_password_message(monkeypatch, tmp_path):
    from portal_mcp_server import cli, security, sudo_creds, connection_manager
    monkeypatch.setenv("PORTAL_ALLOW_LOCAL_EXEC", "1")
    pol = security.SecurityPolicy(policies_yaml=tmp_path / "none.yaml")
    monkeypatch.setattr(cli, "get_policy", lambda: pol)
    sudo_creds.clear_sudo_password()
    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = connection_manager.ConnectionManager(hosts_yaml=yml)
    monkeypatch.setattr(connection_manager, "get_manager", lambda: m)
    with pytest.raises(ToolError) as ei:
        await cli.portal_local_exec(command="id", use_sudo=True)
    msg = str(ei.value)
    # Names BOTH out-of-band sources, never asks for a pasted password.
    assert "set-local" in msg
    assert "<local>:" in msg
    assert "paste" in msg.lower()


@pytest.mark.asyncio
async def test_local_sudo_password_never_in_output_or_audit(monkeypatch, tmp_path):
    """Password reaches the sudo mechanism (stdin) only — never the result JSON
    nor the audit log; the audit records the command name as ``sudo: <cmd>``."""
    from portal_mcp_server import cli, security, sudo_creds
    from portal_mcp_server.audit import get_history

    monkeypatch.setenv("PORTAL_ALLOW_LOCAL_EXEC", "1")
    pol = security.SecurityPolicy(policies_yaml=tmp_path / "none.yaml")
    monkeypatch.setattr(cli, "get_policy", lambda: pol)

    PW = "LOCAL-SUP3R-PW"
    sudo_creds.clear_sudo_password()
    sudo_creds.cache_sudo_password("<local>", PW, ttl=60)

    captured = {}

    async def fake_local_sudo(cmd, password, env, timeout=600.0):
        captured["password"] = password   # real impl feeds this on stdin only
        captured["cmd"] = cmd
        return {"output": "uid=0(root)", "exit_code": 0}

    monkeypatch.setattr(cli, "_local_sudo_exec_env", fake_local_sudo)

    out = await cli.portal_local_exec(command="id", use_sudo=True)
    assert captured["password"] == PW         # mechanism got it (for sudo -S)
    assert captured["cmd"] == "id"
    assert PW not in out                       # ...never surfaced to the agent
    assert '"high_risk": true' in out          # privileged action is flagged
    latest = get_history(limit=1)[0]
    assert PW not in str(latest)               # ...nor to the audit log
    assert latest["command"] == "sudo: id"     # command name is fine to record


# ────────────────────────────────────────────────────────────────────────────
#  CLI: `portal sudo set-local` maps to the reserved <local> key
# ────────────────────────────────────────────────────────────────────────────

def test_set_local_dispatches_with_local_key(monkeypatch):
    from portal_mcp_server import cli
    captured = {}

    def fake_set(args):
        captured["kind"] = args.kind
        captured["key"] = args.key
        return 0

    monkeypatch.setattr(cli, "_kind_set_cli", fake_set)
    rc = cli._credential_main(["sudo", "set-local"])
    assert rc == 0
    assert captured["kind"] == "sudo"
    assert captured["key"] == "<local>"


def test_set_local_only_on_sudo_kind(monkeypatch):
    """set-local is sudo-specific; other kinds must not gain it."""
    from portal_mcp_server import cli
    for kind in ("ssh", "passphrase", "secret"):
        with pytest.raises(SystemExit):
            cli._credential_main([kind, "set-local"])
