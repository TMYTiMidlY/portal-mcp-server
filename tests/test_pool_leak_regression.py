"""Regression tests for the connection-pool leaks identified in code review.

Findings addressed
------------------
1. session_manager.create_session acquired a pooled connection but
   close_session never released it: every portal_bash session permanently
   consumed one ``in_use`` slot, eventually exhausting the pool and
   forcing fresh TCP connects.
2. network_tools.open_*_forward / open_dynamic_proxy did the same — and
   tunnels are typically long-lived, so the leak was even worse in
   practice.

Strategy: install an in-memory recorder for ``get_connection`` /
``release_connection`` and drive each public lifecycle path. Counters
must balance — once on success, once on error.
"""
from __future__ import annotations

import pytest


# ─── Counting helpers ──────────────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.acquired = 0
        self.released = 0

    @property
    def balance(self) -> int:
        return self.acquired - self.released


@pytest.fixture
def conn_balance(monkeypatch):
    """Replace ConnectionManager.{get,release}_connection with counters."""
    from portal_mcp_server import connection_manager

    rec = _Recorder()

    async def fake_get(self, host_name):
        rec.acquired += 1
        # Return a unique sentinel per call so release_connection can find it.
        return object()

    def fake_release(self, host_name, conn):
        rec.released += 1

    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "get_connection", fake_get)
    monkeypatch.setattr(connection_manager.ConnectionManager,
                        "release_connection", fake_release)
    return rec


# ════════════════════════════════════════════════════════════════════════════
#  session_manager — create + close must balance the pool
# ════════════════════════════════════════════════════════════════════════════

class TestSessionManagerLeak:
    @pytest.mark.asyncio
    async def test_close_releases_pool_slot(self, conn_balance, monkeypatch):
        """Happy path: create then close → balance returns to 0."""
        from portal_mcp_server import session_manager

        # Stub create_process so we don't need a real SSH connection.
        class _FakeStdin:
            def write(self, _): pass

        class _FakeProc:
            def __init__(self):
                self.stdin = _FakeStdin()
                self._wait_result = 0
            class _Stdout:
                async def read(self, _): return ""
            stdout = _Stdout()
            async def wait(self): return self._wait_result
            def close(self): pass

        async def fake_create_process(self, *a, **k):
            return _FakeProc()

        # Patch BOTH the bound method on the sentinel object returned by
        # fake_get and provide a generic asyncssh.create_process replacement.
        monkeypatch.setattr(
            "portal_mcp_server.session_manager.asyncio.wait_for",
            lambda coro, timeout: coro,
        )
        # The sentinel object returned by fake_get won't have create_process
        # so we need a smarter fake_get that returns something that does.
        from portal_mcp_server import connection_manager

        class _FakeConn:
            async def create_process(self, *a, **k):
                return _FakeProc()

        rec = _Recorder()

        async def fake_get(self, host_name):
            rec.acquired += 1
            return _FakeConn()

        def fake_release(self, host_name, conn):
            rec.released += 1

        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "get_connection", fake_get)
        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "release_connection", fake_release)

        sm = session_manager.SessionManager()
        sid = await sm.create_session("h")
        assert rec.balance == 1, "creation should hold one slot"
        await sm.close_session(sid)
        assert rec.balance == 0, "close_session must release the pool slot"

    @pytest.mark.asyncio
    async def test_create_failure_releases_pool_slot(self, monkeypatch):
        """If create_process raises, we must NOT leak the pool slot."""
        from portal_mcp_server import session_manager
        from portal_mcp_server import connection_manager

        rec = _Recorder()

        class _BrokenConn:
            async def create_process(self, *a, **k):
                raise RuntimeError("boom")

        async def fake_get(self, host_name):
            rec.acquired += 1
            return _BrokenConn()

        def fake_release(self, host_name, conn):
            rec.released += 1

        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "get_connection", fake_get)
        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "release_connection", fake_release)

        sm = session_manager.SessionManager()
        with pytest.raises(RuntimeError, match="boom"):
            await sm.create_session("h")
        assert rec.balance == 0, "failed creation must release the slot"


# ════════════════════════════════════════════════════════════════════════════
#  network_tools — every open_* + close must balance, even on failure
# ════════════════════════════════════════════════════════════════════════════

class _FakeListener:
    def __init__(self, port=12345):
        self._port = port
        self.closed = False
    def get_port(self):
        return self._port
    def close(self):
        self.closed = True
    async def wait_closed(self):
        pass


class _FakeSshConn:
    def __init__(self, fail: bool = False):
        self._fail = fail
    async def forward_local_port(self, *a, **k):
        if self._fail: raise RuntimeError("forward_local failed")
        return _FakeListener()
    async def forward_remote_port(self, *a, **k):
        if self._fail: raise RuntimeError("forward_remote failed")
        return _FakeListener()
    async def forward_socks(self, *a, **k):
        if self._fail: raise RuntimeError("forward_socks failed")
        return _FakeListener()


class TestTunnelLeak:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("opener,args", [
        ("open_local_forward",  ("h", 0, "1.2.3.4", 80)),
        ("open_remote_forward", ("h", 0, "127.0.0.1", 22)),
        ("open_dynamic_proxy",  ("h", 0)),
    ])
    async def test_open_then_close_balances(self, monkeypatch, opener, args):
        from portal_mcp_server import network_tools, connection_manager

        rec = _Recorder()

        async def fake_get(self, host_name):
            rec.acquired += 1
            return _FakeSshConn(fail=False)

        def fake_release(self, host_name, conn):
            rec.released += 1

        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "get_connection", fake_get)
        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "release_connection", fake_release)

        tm = network_tools.TunnelManager()
        result = await getattr(tm, opener)(*args)
        assert "tunnel_id" in result, result
        assert rec.balance == 1, f"{opener} should hold one slot while open"
        await tm.close_tunnel(result["tunnel_id"])
        assert rec.balance == 0, f"close_tunnel must release {opener}'s slot"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("opener,args", [
        ("open_local_forward",  ("h", 0, "1.2.3.4", 80)),
        ("open_remote_forward", ("h", 0, "127.0.0.1", 22)),
        ("open_dynamic_proxy",  ("h", 0)),
    ])
    async def test_open_failure_releases_slot(self, monkeypatch, opener, args):
        """If asyncssh.forward_* raises, we must NOT leak the pool slot."""
        from portal_mcp_server import network_tools, connection_manager

        rec = _Recorder()

        async def fake_get(self, host_name):
            rec.acquired += 1
            return _FakeSshConn(fail=True)

        def fake_release(self, host_name, conn):
            rec.released += 1

        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "get_connection", fake_get)
        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "release_connection", fake_release)

        tm = network_tools.TunnelManager()
        result = await getattr(tm, opener)(*args)
        assert "error" in result, result
        assert rec.balance == 0, (
            f"{opener} must release the pool slot when forward_* raises"
        )

    @pytest.mark.asyncio
    async def test_close_all_releases_every_slot(self, monkeypatch):
        """close_all opens N tunnels then closes them; balance must be 0."""
        from portal_mcp_server import network_tools, connection_manager

        rec = _Recorder()

        async def fake_get(self, host_name):
            rec.acquired += 1
            return _FakeSshConn(fail=False)

        def fake_release(self, host_name, conn):
            rec.released += 1

        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "get_connection", fake_get)
        monkeypatch.setattr(connection_manager.ConnectionManager,
                            "release_connection", fake_release)

        tm = network_tools.TunnelManager()
        for _ in range(3):
            await tm.open_local_forward("h", 0, "1.2.3.4", 80)
        assert rec.balance == 3
        await tm.close_all()
        assert rec.balance == 0, "close_all must release every tunnel's slot"
