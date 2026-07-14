"""Tests for ConnectionManager pool lifecycle: size enforcement, idle
pruning, max-age pruning, connection() context manager, pool_config.

These tests use a fake SSH connection class to exercise pool logic
without any real network I/O.
"""
from __future__ import annotations

import time as time_mod

import pytest

from portal_mcp_server.connection_manager import (
    ConnectionManager,
    PooledConnection,
)


# ── Helpers ──────────────────────────────────────────────────────────────

class FakeConn:
    """Minimal stand-in for asyncssh.SSHClientConnection."""
    def __init__(self):
        self._closed = False

    def close(self):
        self._closed = True

    def is_closed(self):
        return self._closed


def _make_manager(tmp_path, **kwargs) -> ConnectionManager:
    """Create a ConnectionManager with an empty hosts.yaml."""
    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    return ConnectionManager(hosts_yaml=yml, **kwargs)


def _inject_conn(mgr: ConnectionManager, host: str, *,
                 in_use: int = 0,
                 age: float = 0,
                 idle: float = 0) -> FakeConn:
    """Inject a fake PooledConnection into the pool for testing."""
    now = time_mod.time()
    fc = FakeConn()
    pc = PooledConnection(
        host_name=host,
        conn=fc,
        created_at=now - age,
        last_used=now - idle,
        in_use=in_use,
    )
    mgr._pool.setdefault(host, []).append(pc)
    return fc


# ── Pool size enforcement ────────────────────────────────────────────────

class TestPoolSizeClamp:
    def test_pool_size_zero_is_clamped_to_one(self, tmp_path):
        """pool_size=0 (e.g. PORTAL_SSH_POOL_SIZE="0") must not leave the
        overload branch calling min() on an empty pool — an opaque
        ValueError. The size is clamped to >= 1 at construction. Regression."""
        mgr = _make_manager(tmp_path, pool_size=0)
        assert mgr._pool_size == 1


class TestPoolSizeEnforcement:
    """pool_size caps the number of TCP connections per host."""

    @pytest.mark.asyncio
    async def test_overload_when_at_capacity(self, tmp_path, monkeypatch):
        """When pool_size=2 and both connections are fully loaded,
        get_connection should reuse the least-busy one instead of creating
        a third connection."""
        mgr = _make_manager(tmp_path, pool_size=2, max_channels_per_conn=2)
        mgr.register_host("h", "1.2.3.4")

        # Inject 2 connections, both fully loaded
        fc1 = _inject_conn(mgr, "h", in_use=2)
        _inject_conn(mgr, "h", in_use=3)  # more loaded (2nd conn, ref unused)

        # Prevent real asyncssh.connect from being called
        connect_called = False

        async def fake_connect(**kw):
            nonlocal connect_called
            connect_called = True
            return FakeConn()

        import asyncssh
        monkeypatch.setattr(asyncssh, "connect", fake_connect)

        conn = await mgr.get_connection("h")

        # Should NOT have created a new connection
        assert not connect_called
        # Should have picked the least-loaded (fc1, was in_use=2)
        assert conn is fc1
        # in_use should have incremented
        assert mgr._pool["h"][0].in_use == 3  # was 2, now 3

    @pytest.mark.asyncio
    async def test_creates_new_conn_under_capacity(self, tmp_path, monkeypatch):
        """When pool_size has room but all existing connections are fully
        loaded, a new connection should be created."""
        mgr = _make_manager(tmp_path, pool_size=3, max_channels_per_conn=1)
        mgr.register_host("h", "1.2.3.4")

        _inject_conn(mgr, "h", in_use=1)  # fully loaded

        new_conn = FakeConn()

        async def fake_connect(**kw):
            return new_conn

        import asyncssh
        monkeypatch.setattr(asyncssh, "connect", fake_connect)

        conn = await mgr.get_connection("h")
        assert conn is new_conn
        assert len(mgr._pool["h"]) == 2  # now 2 connections


# ── Idle pruning ─────────────────────────────────────────────────────────

class TestIdlePruning:
    """Connections idle beyond max_idle_time are pruned."""

    @pytest.mark.asyncio
    async def test_idle_conn_pruned(self, tmp_path, monkeypatch):
        mgr = _make_manager(tmp_path, max_idle_time=60)
        mgr.register_host("h", "1.2.3.4")

        # Inject an idle connection that's been idle for 120s
        fc_old = _inject_conn(mgr, "h", in_use=0, idle=120)
        assert not fc_old._closed

        new_conn = FakeConn()

        async def fake_connect(**kw):
            return new_conn

        import asyncssh
        monkeypatch.setattr(asyncssh, "connect", fake_connect)

        conn = await mgr.get_connection("h")

        # Old connection should be closed and pruned
        assert fc_old._closed
        assert conn is new_conn
        assert len(mgr._pool["h"]) == 1

    @pytest.mark.asyncio
    async def test_active_conn_not_pruned_even_if_old(self, tmp_path, monkeypatch):
        """A connection with in_use > 0 is never pruned for idleness."""
        mgr = _make_manager(tmp_path, max_idle_time=60)
        mgr.register_host("h", "1.2.3.4")

        # in_use=1, so even though idle=120 it should survive
        fc = _inject_conn(mgr, "h", in_use=1, idle=120)

        conn = await mgr.get_connection("h")
        assert conn is fc
        assert not fc._closed


# ── Max age pruning ──────────────────────────────────────────────────────

class TestMaxAgePruning:
    """Connections older than max_conn_age are pruned when idle."""

    @pytest.mark.asyncio
    async def test_aged_conn_pruned(self, tmp_path, monkeypatch):
        mgr = _make_manager(tmp_path, max_conn_age=300)
        mgr.register_host("h", "1.2.3.4")

        # Connection that's 600s old, but was used recently (idle=5s)
        fc_old = _inject_conn(mgr, "h", in_use=0, age=600, idle=5)

        new_conn = FakeConn()

        async def fake_connect(**kw):
            return new_conn

        import asyncssh
        monkeypatch.setattr(asyncssh, "connect", fake_connect)

        conn = await mgr.get_connection("h")
        assert fc_old._closed
        assert conn is new_conn

    @pytest.mark.asyncio
    async def test_aged_but_active_conn_survives(self, tmp_path):
        """A connection with in_use > 0 survives even past max_conn_age."""
        mgr = _make_manager(tmp_path, max_conn_age=300)
        mgr.register_host("h", "1.2.3.4")

        fc = _inject_conn(mgr, "h", in_use=2, age=600)

        conn = await mgr.get_connection("h")
        assert conn is fc
        assert not fc._closed


# ── Dead connection pruning ──────────────────────────────────────────────

class TestDeadPruning:

    @pytest.mark.asyncio
    async def test_dead_conns_pruned_on_get(self, tmp_path, monkeypatch):
        mgr = _make_manager(tmp_path)
        mgr.register_host("h", "1.2.3.4")

        # Inject a dead connection
        fc_dead = _inject_conn(mgr, "h", in_use=0)
        fc_dead._closed = True  # mark as dead

        # And an alive one
        fc_alive = _inject_conn(mgr, "h", in_use=0)

        conn = await mgr.get_connection("h")
        assert conn is fc_alive
        # Pool should only have the alive connection
        assert len(mgr._pool["h"]) == 1


# ── connection() context manager ─────────────────────────────────────────

class TestConnectionContextManager:

    @pytest.mark.asyncio
    async def test_auto_release(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.register_host("h", "1.2.3.4")

        fc = _inject_conn(mgr, "h", in_use=0)

        async with mgr.connection("h") as conn:
            assert conn is fc
            # During the block, in_use should be 1
            assert mgr._pool["h"][0].in_use == 1

        # After exit, in_use should be back to 0
        assert mgr._pool["h"][0].in_use == 0

    @pytest.mark.asyncio
    async def test_release_on_exception(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.register_host("h", "1.2.3.4")

        _inject_conn(mgr, "h", in_use=0)

        with pytest.raises(RuntimeError):
            async with mgr.connection("h"):
                raise RuntimeError("boom")

        # Connection should still be released despite the exception
        assert mgr._pool["h"][0].in_use == 0


# ── release_connection updates last_used ─────────────────────────────────

class TestReleaseUpdatesTimestamp:

    def test_release_updates_last_used(self, tmp_path):
        mgr = _make_manager(tmp_path)
        fc = FakeConn()
        pc = PooledConnection(
            host_name="h", conn=fc,
            created_at=time_mod.time() - 100,
            last_used=time_mod.time() - 100,
            in_use=1,
        )
        mgr._pool["h"] = [pc]

        before = time_mod.time()
        mgr.release_connection("h", fc)
        after = time_mod.time()

        assert pc.in_use == 0
        assert before <= pc.last_used <= after


# ── pool_config property ─────────────────────────────────────────────────

class TestPoolConfig:

    def test_returns_all_settings(self, tmp_path):
        mgr = _make_manager(
            tmp_path,
            pool_size=3,
            max_channels_per_conn=10,
            max_idle_time=120.0,
            max_conn_age=1800.0,
        )
        cfg = mgr.pool_config
        assert cfg == {
            "pool_size": 3,
            "max_channels_per_conn": 10,
            "max_idle_time": 120.0,
            "max_conn_age": 1800.0,
        }


# ── Reuse prefers least-loaded ───────────────────────────────────────────

class TestReuseLeastLoaded:

    @pytest.mark.asyncio
    async def test_reuses_first_conn_with_capacity(self, tmp_path):
        """Pool iterates in order and picks the first connection with
        in_use < max_channels_per_conn."""
        mgr = _make_manager(tmp_path, max_channels_per_conn=5)
        mgr.register_host("h", "1.2.3.4")

        fc1 = _inject_conn(mgr, "h", in_use=3)  # has capacity
        _inject_conn(mgr, "h", in_use=1)  # also has capacity, but comes second
        _inject_conn(mgr, "h", in_use=4)

        conn = await mgr.get_connection("h")
        # Should pick fc1 (first with in_use < 5)
        assert conn is fc1

    @pytest.mark.asyncio
    async def test_skips_fully_loaded_conns(self, tmp_path):
        mgr = _make_manager(tmp_path, max_channels_per_conn=2)
        mgr.register_host("h", "1.2.3.4")

        _inject_conn(mgr, "h", in_use=2)  # fully loaded
        _inject_conn(mgr, "h", in_use=2)  # fully loaded
        fc3 = _inject_conn(mgr, "h", in_use=1)  # has room

        conn = await mgr.get_connection("h")
        assert conn is fc3


# ── register/remove invalidate pooled connections (config-generation guard) ──
class TestRegisterInvalidatesPool:
    def test_reregister_closes_idle_pooled_conn(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.register_host("h", "1.2.3.4")
        fc = _inject_conn(mgr, "h", in_use=0)      # idle conn to the OLD address
        assert not fc._closed
        mgr.register_host("h", "5.6.7.8")           # re-register with a new addr
        assert fc._closed                            # stale idle conn invalidated
        assert mgr._pool.get("h", []) == []

    def test_reregister_marks_inuse_stale_then_closes_on_release(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.register_host("h", "1.2.3.4")
        fc = _inject_conn(mgr, "h", in_use=1)       # a channel is in flight
        mgr.register_host("h", "5.6.7.8")
        pcs = mgr._pool.get("h", [])
        assert len(pcs) == 1 and pcs[0].stale and not fc._closed  # kept, not yanked
        mgr.release_connection("h", fc)             # last channel released
        assert fc._closed

    def test_remove_host_invalidates_idle_conn(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr.register_host("h", "1.2.3.4")
        fc = _inject_conn(mgr, "h", in_use=0)
        mgr.remove_host("h")
        assert fc._closed
        assert "h" not in mgr._pool

    @pytest.mark.asyncio
    async def test_reregister_during_connect_discards_and_retries(self, tmp_path, monkeypatch):
        """If the host is reconfigured while a connect is in flight, the
        connection built against the old config is discarded and get_connection
        retries against the new one (no stale conn pooled, no KeyError)."""
        import asyncssh
        mgr = _make_manager(tmp_path, pool_size=2, max_channels_per_conn=2)
        mgr.register_host("h", "1.2.3.4")
        conns: list = []
        first = {"done": False}

        async def fake_connect(**kw):
            fc = FakeConn()
            conns.append((kw.get("host"), fc))
            if not first["done"]:
                first["done"] = True
                mgr.register_host("h", "9.9.9.9")   # re-register mid-connect
            return fc

        monkeypatch.setattr(asyncssh, "connect", fake_connect)
        conn = await mgr.get_connection("h")
        assert len(conns) == 2                       # first attempt + retry
        assert conns[0][1]._closed                    # superseded conn closed
        assert conn is conns[1][1]                    # returned the retry conn
        assert conn in [pc.conn for pc in mgr._pool.get("h", [])]
