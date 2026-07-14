"""Tunnel bind hardening: listeners are loopback-only unless the operator sets
PORTAL_ALLOW_TUNNEL_EXPOSURE=1, so a tunnel can't become an unauthenticated
off-box proxy into the SSH host by default."""
from __future__ import annotations

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from portal_mcp_server import cli


class _FakeTM:
    def __init__(self):
        self.opened = []

    async def open_local_forward(self, *a, **k):
        self.opened.append(("local", a, k))
        return {"tunnel_id": "x"}

    async def open_remote_forward(self, *a, **k):
        self.opened.append(("reverse", a, k))
        return {"tunnel_id": "x"}

    async def open_dynamic_proxy(self, *a, **k):
        self.opened.append(("socks", a, k))
        return {"tunnel_id": "x"}


@pytest.fixture
def wired(monkeypatch):
    async def ok_gate(*a, **k):
        return None
    monkeypatch.setattr(cli, "_gate", ok_gate)
    tm = _FakeTM()
    monkeypatch.setattr(cli, "get_tunnel_manager", lambda: tm)
    monkeypatch.delenv("PORTAL_ALLOW_TUNNEL_EXPOSURE", raising=False)
    return tm


@pytest.mark.asyncio
async def test_local_nonloopback_refused_without_optin(wired):
    with pytest.raises(ToolError, match="non-loopback"):
        await cli.remote_tunnel(action="open", kind="local", host="h",
                                local_bind="0.0.0.0", remote_host="db",
                                remote_port=5432)
    assert wired.opened == []


@pytest.mark.asyncio
async def test_socks_nonloopback_refused_without_optin(wired):
    with pytest.raises(ToolError, match="non-loopback"):
        await cli.remote_tunnel(action="open", kind="socks", host="h",
                                local_bind="0.0.0.0", local_port=1080)
    assert wired.opened == []


@pytest.mark.asyncio
async def test_local_nonloopback_allowed_with_optin(wired, monkeypatch):
    monkeypatch.setenv("PORTAL_ALLOW_TUNNEL_EXPOSURE", "1")
    await cli.remote_tunnel(action="open", kind="local", host="h",
                            local_bind="0.0.0.0", remote_host="db",
                            remote_port=5432)
    assert wired.opened and wired.opened[0][0] == "local"


@pytest.mark.asyncio
async def test_reverse_binds_remote_loopback_by_default(wired):
    await cli.remote_tunnel(action="open", kind="reverse", host="h",
                            remote_port=8080, local_bind="127.0.0.1",
                            local_port=3000)
    assert wired.opened[0][2].get("listen_bind") == "127.0.0.1"


@pytest.mark.asyncio
async def test_reverse_binds_remote_all_with_optin(wired, monkeypatch):
    monkeypatch.setenv("PORTAL_ALLOW_TUNNEL_EXPOSURE", "1")
    await cli.remote_tunnel(action="open", kind="reverse", host="h",
                            remote_port=8080, local_bind="127.0.0.1",
                            local_port=3000)
    assert wired.opened[0][2].get("listen_bind") == "0.0.0.0"
