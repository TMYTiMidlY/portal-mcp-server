"""Dispatch parameters must carry a machine-readable ``enum`` in the schema.

Before this change every dispatch/selector parameter (``action`` / ``mode`` /
``direction`` / ``view``) was annotated as a bare ``str``, so FastMCP emitted
``{"type": "string"}`` with no ``enum``. The agent then had to infer the valid
values from the natural-language docstring alone. Annotating them with
``typing.Literal[...]`` makes Pydantic/FastMCP emit ``enum`` so MCP clients can
validate (and reject) invalid values at the schema layer.
"""
from __future__ import annotations

import pytest

from portal_mcp_server import cli


# (tool name, param name, expected enum values)
_DISPATCH_ENUMS = [
    ("hosts", "action", {"list", "register", "remove"}),
    ("remote_transfer", "direction",
     {"upload", "download", "sync", "mirror", "upload-list", "download-list"}),
    ("remote_tunnel", "action", {"open", "close", "list"}),
    ("remote_tunnel", "kind", {"local", "reverse", "socks"}),
    ("remote_grep", "output_mode",
     {"files_with_matches", "content", "count"}),
    ("remote_job", "action", {"submit", "poll", "cancel", "list"}),
    ("remote_job", "signal", {"TERM", "KILL"}),
    ("inspect", "view",
     {"snapshot", "server", "sessions", "history", "stats", "policy"}),
]


@pytest.mark.parametrize("tool_name,param,expected", _DISPATCH_ENUMS)
async def test_dispatch_param_has_enum(tool_name, param, expected):
    tools = await cli.mcp.list_tools()
    tool = next(t for t in tools if t.name == tool_name)
    props = (tool.inputSchema or {}).get("properties", {})
    assert param in props, f"{tool_name} missing param {param!r}"
    schema = props[param]
    assert "enum" in schema, (
        f"{tool_name}.{param} should carry an enum (got {schema}); annotate it "
        "with typing.Literal[...] so the valid values reach the client schema"
    )
    assert set(schema["enum"]) == expected


async def test_framework_rejects_invalid_dispatch_value():
    """The MCP layer (Pydantic) rejects an out-of-enum value before the tool
    body runs — a real guard, not just documentation."""
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await cli.mcp.call_tool("inspect", {"view": "not-a-real-view"})
