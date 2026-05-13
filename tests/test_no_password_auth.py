"""Verify password authentication has been removed (alignment with README
and SECURITY.md, both of which state "key-based auth only").

Original audit finding
----------------------
README/SECURITY.md said password auth was unsupported, but the code fully
supported it via ``HostConfig.password``, ``ssh_register_host(password=...)``
and a ``cfg.password`` branch in ``_build_connect_kwargs``. The fork has
since deleted all three; these tests pin that decision so it cannot regress.
"""
from __future__ import annotations

import inspect
import logging

import pytest


# ════════════════════════════════════════════════════════════════════════════
#  HostConfig dataclass no longer carries a password field
# ════════════════════════════════════════════════════════════════════════════

def test_hostconfig_has_no_password_field():
    from portal_mcp_server.connection_manager import HostConfig
    cfg = HostConfig(name="x", host="1.2.3.4")
    assert not hasattr(cfg, "password"), (
        "HostConfig must not expose a password field; password auth was "
        "intentionally removed (see README 'Key-based auth only')."
    )


# ════════════════════════════════════════════════════════════════════════════
#  ConnectionManager.register_host has no password parameter
# ════════════════════════════════════════════════════════════════════════════

def test_connection_manager_register_host_signature_has_no_password():
    from portal_mcp_server.connection_manager import ConnectionManager
    sig = inspect.signature(ConnectionManager.register_host)
    assert "password" not in sig.parameters, (
        "ConnectionManager.register_host must not accept a password kwarg."
    )


# ════════════════════════════════════════════════════════════════════════════
#  portal_host(action="register") MCP tool has no password parameter
# ════════════════════════════════════════════════════════════════════════════

def test_portal_host_register_signature_has_no_password():
    from portal_mcp_server import cli
    sig = inspect.signature(cli.portal_host)
    assert "password" not in sig.parameters, (
        "portal_host MCP tool must not expose a password parameter "
        "(would let LLMs leak credentials into prompt logs)."
    )


# ════════════════════════════════════════════════════════════════════════════
#  hosts.yaml containing a 'password:' key is loaded with an ERROR log
#  and the password field is silently dropped (no crash, no honoring).
# ════════════════════════════════════════════════════════════════════════════

def test_hosts_yaml_password_field_is_logged_and_ignored(tmp_path, caplog):
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts:\n"
        "  legacy:\n"
        "    host: 10.0.0.1\n"
        "    user: deploy\n"
        "    password: super-secret\n"
    )

    with caplog.at_level(logging.ERROR, logger="portal_mcp.connections"):
        m = ConnectionManager(hosts_yaml=yml)

    # The host loaded fine, but the password field never reached HostConfig.
    cfg = m._registry["legacy"]
    assert not hasattr(cfg, "password")
    # The secret value is also nowhere on the dataclass.
    for value in cfg.__dict__.values():
        assert value != "super-secret"

    # And we logged a clear error so the operator knows.
    assert any(
        "password" in rec.message and "legacy" in rec.message
        for rec in caplog.records
    ), "expected an ERROR log mentioning the offending host and 'password'"


# ════════════════════════════════════════════════════════════════════════════
#  _build_connect_kwargs never injects a 'password' kwarg into asyncssh
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_build_connect_kwargs_never_emits_password(tmp_path):
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(name="x", host="1.2.3.4", key="/tmp/no-such-key")
    kwargs = await m._build_connect_kwargs(cfg)
    assert "password" not in kwargs, (
        "_build_connect_kwargs must never set a 'password' kwarg on "
        "asyncssh.connect — password auth has been removed."
    )

    cfg2 = HostConfig(name="y", host="1.2.3.4")
    kwargs2 = await m._build_connect_kwargs(cfg2)
    assert "password" not in kwargs2
