"""portal_shell / portal_exec keepalive heartbeat.

A remote command produces no output until it finishes, so without a keepalive a
slow command leaves the MCP client hearing nothing and many clients abort the
request after a fixed idle window (JSON-RPC -32001) while the remote keeps
running and the result is lost. ``_await_with_heartbeat`` emits periodic MCP
progress notifications during execution; each one resets the client's window.

These tests drive the helper with an in-memory fake Context (no live host) and
assert the schema plumbing keeps the injected ``ctx`` out of the client-facing
tool schema.
"""
from __future__ import annotations

import asyncio

import pytest

from portal_mcp_server import cli


class FakeCtx:
    """Records report_progress calls; mimics FastMCP's Context surface."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float | None]] = []

    async def report_progress(self, progress, total=None, message=None) -> None:
        self.calls.append((progress, total))


async def _slow(value, delay):
    await asyncio.sleep(delay)
    return value


# ────────────────────────────────────────────────────────────────────────────
#  _await_with_heartbeat
# ────────────────────────────────────────────────────────────────────────────

async def test_heartbeat_pings_during_slow_op_and_returns_result():
    ctx = FakeCtx()
    res = await cli._await_with_heartbeat(
        _slow({"output": "ok"}, 0.9), ctx, interval=0.2)
    assert res == {"output": "ok"}
    # ~4 pings over 0.9s at 0.2s spacing; allow slack but require >=2.
    assert len(ctx.calls) >= 2
    # Monotonic tick, indeterminate total.
    assert ctx.calls[0] == (1, None)
    assert [c[0] for c in ctx.calls] == list(range(1, len(ctx.calls) + 1))
    assert all(total is None for _, total in ctx.calls)


async def test_heartbeat_no_ctx_is_silent_passthrough():
    res = await cli._await_with_heartbeat(_slow(42, 0.05), None, interval=0.01)
    assert res == 42


async def test_heartbeat_fast_op_emits_no_ping():
    ctx = FakeCtx()
    res = await cli._await_with_heartbeat(_slow("quick", 0.0), ctx, interval=5.0)
    assert res == "quick"
    assert ctx.calls == []


async def test_heartbeat_propagates_exceptions():
    ctx = FakeCtx()

    async def boom():
        await asyncio.sleep(0.05)
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError, match="kaboom"):
        await cli._await_with_heartbeat(boom(), ctx, interval=0.01)


async def test_heartbeat_outer_cancel_cancels_inner_task():
    ctx = FakeCtx()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_running():
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.ensure_future(
        cli._await_with_heartbeat(long_running(), ctx, interval=0.05))
    await started.wait()
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The wrapped coroutine actually received the cancellation (no leak).
    assert cancelled.is_set()


# ────────────────────────────────────────────────────────────────────────────
#  _heartbeat_interval env parsing
# ────────────────────────────────────────────────────────────────────────────

def test_heartbeat_interval_default(monkeypatch):
    monkeypatch.delenv("PORTAL_BASH_HEARTBEAT_INTERVAL", raising=False)
    assert cli._heartbeat_interval() == 5.0


def test_heartbeat_interval_override(monkeypatch):
    monkeypatch.setenv("PORTAL_BASH_HEARTBEAT_INTERVAL", "1.5")
    assert cli._heartbeat_interval() == 1.5


@pytest.mark.parametrize("bad", ["", "abc", "0", "-3"])
def test_heartbeat_interval_invalid_falls_back(monkeypatch, bad):
    monkeypatch.setenv("PORTAL_BASH_HEARTBEAT_INTERVAL", bad)
    assert cli._heartbeat_interval() == 5.0


# ────────────────────────────────────────────────────────────────────────────
#  schema plumbing — injected ctx must not leak to the client
# ────────────────────────────────────────────────────────────────────────────

def test_ctx_detected_as_context_param():
    from mcp.server.fastmcp.utilities.context_injection import (
        find_context_parameter,
    )
    assert find_context_parameter(cli.portal_shell) == "ctx"
    assert find_context_parameter(cli.portal_exec) == "ctx"


async def test_portal_shell_schema_excludes_ctx():
    tools = await cli.mcp.list_tools()
    tool = next(t for t in tools if t.name == "portal_shell")
    props = (tool.inputSchema or {}).get("properties", {})
    assert "ctx" not in props
    # portal_shell is the pure session: host/command/timeout, no sudo/secrets.
    assert {"host", "command", "timeout"} <= set(props)
    assert "use_sudo" not in props
    assert "secrets" not in props


async def test_portal_exec_schema_excludes_ctx():
    tools = await cli.mcp.list_tools()
    tool = next(t for t in tools if t.name == "portal_exec")
    props = (tool.inputSchema or {}).get("properties", {})
    assert "ctx" not in props
    # sudo + secrets moved here (one-shot paths).
    assert {"host", "command", "timeout", "use_sudo", "secrets"} <= set(props)


def test_local_exec_ctx_detected_as_context_param():
    from mcp.server.fastmcp.utilities.context_injection import (
        find_context_parameter,
    )
    assert find_context_parameter(cli.portal_local_exec) == "ctx"


async def test_portal_local_exec_schema_excludes_ctx():
    tools = await cli.mcp.list_tools()
    tool = next(t for t in tools if t.name == "portal_local_exec")
    props = (tool.inputSchema or {}).get("properties", {})
    assert "ctx" not in props
    # timeout is still client-settable, mirroring portal_exec.
    assert {"command", "secrets", "timeout"} <= set(props)
