"""The FastMCP lifespan must close shell sessions + the connection pool on
shutdown (wiring the previously-dead close_all paths). Best-effort: a failure
in one closer must not stop the other or raise out of shutdown."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from portal_mcp_server import cli


@pytest.mark.asyncio
async def test_lifespan_closes_sessions_and_pool(monkeypatch):
    called = {"sess": False, "pool": False}

    class FakeSessions:
        async def close_all(self):
            called["sess"] = True
            return 0

    class FakePool:
        async def close_all(self):
            called["pool"] = True

    monkeypatch.setattr(
        "portal_mcp_server.session_manager.get_session_manager",
        lambda: FakeSessions(),
    )
    monkeypatch.setattr(cli, "get_manager", lambda: FakePool())

    async with cli._server_lifespan(cli.mcp):
        pass

    assert called["sess"] is True
    assert called["pool"] is True


@pytest.mark.asyncio
async def test_lifespan_swallows_closer_errors(monkeypatch):
    """A raising session-closer must not stop the pool close or escape."""
    pool_closed = {"v": False}

    class BoomSessions:
        async def close_all(self):
            raise RuntimeError("boom")

    class FakePool:
        async def close_all(self):
            pool_closed["v"] = True

    monkeypatch.setattr(
        "portal_mcp_server.session_manager.get_session_manager",
        lambda: BoomSessions(),
    )
    monkeypatch.setattr(cli, "get_manager", lambda: FakePool())

    async with cli._server_lifespan(cli.mcp):
        pass

    assert pool_closed["v"] is True


def test_first_import_has_no_incomplete_lifespan_warning(tmp_path):
    """A fresh process catches the first Settings construction, not a cached import."""
    code = """
import warnings
warnings.filterwarnings('error', message="Field 'lifespan' has an incomplete definition")
from portal_mcp_server import cli
assert cli.mcp.settings.lifespan is cli._server_lifespan
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_server_constructed_with_lifespan():
    assert cli.mcp.settings.lifespan is cli._server_lifespan
