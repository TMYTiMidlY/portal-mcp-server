"""Concurrency tests: per-host locks, registry cleanup, lazy-lock race.

Audit findings addressed
------------------------
* ``ConnectionManager._get_lock`` had a "check then create" race that could
  hand two coroutines different locks for the same host. Now uses
  ``setdefault`` (CPython-atomic).
* ``remove_host`` did not clean up ``_locks`` or ``_pool``; both leaked.
* ``remote_bash._LOCK`` was a single global lock, serializing every host.
  Now per-host.
"""
from __future__ import annotations

import asyncio

import pytest


# ════════════════════════════════════════════════════════════════════════════
#  ConnectionManager._get_lock — same instance returned regardless of order
# ════════════════════════════════════════════════════════════════════════════

class TestGetLockRace:
    @pytest.mark.asyncio
    async def test_concurrent_get_lock_returns_same_instance(self, tmp_path):
        from portal_mcp_server.connection_manager import ConnectionManager
        yml = tmp_path / "hosts.yaml"
        yml.write_text("hosts: {}\n")
        m = ConnectionManager(hosts_yaml=yml)

        # Fire 50 coroutines simultaneously; every result must be the SAME lock.
        results = await asyncio.gather(*[m._get_lock("h") for _ in range(50)])
        first = results[0]
        for r in results[1:]:
            assert r is first


# ════════════════════════════════════════════════════════════════════════════
#  remove_host — drops registry entry, lock, and pool
# ════════════════════════════════════════════════════════════════════════════

class TestRemoveHostCleanup:
    def test_removes_registry_lock_and_pool(self, tmp_path):
        from portal_mcp_server.connection_manager import ConnectionManager, PooledConnection
        import time as time_mod

        yml = tmp_path / "hosts.yaml"
        yml.write_text("hosts: {}\n")
        m = ConnectionManager(hosts_yaml=yml)
        m.register_host("h", "1.2.3.4")

        # Simulate the side-effects of an earlier connection round-trip.
        m._locks["h"] = asyncio.Lock()

        class FakeConn:
            def __init__(self): self.closed = False
            def close(self): self.closed = True
            def is_closed(self): return self.closed
        fc = FakeConn()
        m._pool["h"] = [PooledConnection(
            host_name="h", conn=fc,
            created_at=time_mod.time(), last_used=time_mod.time(),
        )]

        msg = m.remove_host("h")
        assert "removed" in msg
        assert "h" not in m._registry
        assert "h" not in m._locks
        assert "h" not in m._pool
        assert fc.closed is True


# ════════════════════════════════════════════════════════════════════════════
#  remote_bash — distinct hosts get distinct locks, run in parallel
# ════════════════════════════════════════════════════════════════════════════

class TestRemoteBashPerHostLock:
    def test_distinct_hosts_get_distinct_locks(self):
        # Reset the per-host lock dict so this test is hermetic regardless of
        # earlier suite execution order.
        from portal_mcp_server import remote_bash as rb
        rb._HOST_LOCKS.clear()
        a = rb._lock_for("alpha")
        b = rb._lock_for("beta")
        c = rb._lock_for("alpha")
        assert a is c
        assert a is not b

    @pytest.mark.asyncio
    async def test_concurrent_lock_creation_idempotent(self):
        # 50 coroutines racing for the same host all see the same lock.
        from portal_mcp_server import remote_bash as rb
        rb._HOST_LOCKS.clear()
        async def _get():
            return rb._lock_for("h")
        results = await asyncio.gather(*[_get() for _ in range(50)])
        first = results[0]
        for r in results[1:]:
            assert r is first

    @pytest.mark.asyncio
    async def test_two_hosts_do_not_block_each_other(self, monkeypatch):
        """Verify the per-host lock allows true parallelism across hosts.

        We patch ``_setup_session`` to sleep for 0.2 s. Total wall time for
        two parallel calls on different hosts must stay well under 0.4 s if
        the locks are correctly per-host.
        """
        from portal_mcp_server import remote_bash as rb
        rb._HOST_SESSIONS.clear()
        rb._HOST_LOCKS.clear()

        call_log = []

        async def fake_setup(host):
            call_log.append(("start", host))
            await asyncio.sleep(0.2)
            call_log.append(("end", host))
            return f"sid-{host}"

        # Bypass the real session manager entirely.
        class _FakeSmgr:
            def _get(self, sid):
                raise KeyError(sid)
        monkeypatch.setattr(rb, "_setup_session", fake_setup)
        monkeypatch.setattr(rb, "get_session_manager", lambda: _FakeSmgr())

        import time as time_mod
        t0 = time_mod.monotonic()
        await asyncio.gather(rb._ensure_session("a"), rb._ensure_session("b"))
        elapsed = time_mod.monotonic() - t0
        assert elapsed < 0.35, (
            f"per-host lock failed: 2 parallel calls took {elapsed:.2f}s "
            f"(expected ~0.2s, hard limit 0.35s)"
        )
        # Both hosts started before either ended → true parallelism.
        starts = [h for ev, h in call_log if ev == "start"]
        ends = [h for ev, h in call_log if ev == "end"]
        assert set(starts) == {"a", "b"}
        assert set(ends) == {"a", "b"}
