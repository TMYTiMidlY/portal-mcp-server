"""inspect's new view='server' + the `server` block embedded in
view='snapshot'. Covers the diagnostic surface added so an agent can ask
"which version of portal-mcp-server am I talking to?" without needing
PORTAL_ALLOW_LOCAL_EXEC or shell access.
"""
from __future__ import annotations

import json

from portal_mcp_server import cli, server_info as si


def test_view_server_returns_metadata_dict():
    out = cli.inspect(view="server")
    data = json.loads(out)
    # Required keys
    for key in ("version", "python_version", "pid", "started_at",
                "uptime_s", "transport", "config"):
        assert key in data, f"missing {key!r}: {data}"
    # Types we care about
    assert isinstance(data["version"], str) and data["version"]
    assert isinstance(data["pid"], int) and data["pid"] > 0
    assert isinstance(data["uptime_s"], (int, float)) and data["uptime_s"] >= 0
    assert isinstance(data["config"], dict)
    # Config paths exist as keys (values are stringified Paths)
    for cfg_key in ("hosts_yaml", "policies_yaml", "secrets_yaml", "log_dir"):
        assert cfg_key in data["config"], f"missing config.{cfg_key}"
        assert isinstance(data["config"][cfg_key], str)


def test_view_snapshot_embeds_server_block():
    out = cli.inspect(view="snapshot")
    data = json.loads(out)
    assert "server" in data, f"snapshot missing 'server' block: {data}"
    assert data["server"]["version"] == json.loads(
        cli.inspect(view="server"))["version"]


def test_view_snapshot_embeds_bash_sessions():
    """The host→session_id map (formerly portal_bash_status) is folded into
    the snapshot so introspection lives in one place."""
    data = json.loads(cli.inspect(view="snapshot"))
    assert "bash_sessions" in data, f"snapshot missing 'bash_sessions': {data}"
    assert isinstance(data["bash_sessions"], dict)


def test_view_sessions_returns_host_session_map():
    """view='sessions' replaces the deleted portal_bash_status tool."""
    out = cli.inspect(view="sessions")
    data = json.loads(out)
    assert isinstance(data, dict)  # host -> session_id (empty when no warm shells)


def test_portal_bash_status_tool_removed():
    """The standalone portal_bash_status tool was folded into inspect."""
    assert not hasattr(cli, "portal_bash_status")


def test_set_transport_records_value():
    si.set_transport("stdio")
    assert json.loads(cli.inspect(view="server"))["transport"] == "stdio"
    si.set_transport("streamable_http")
    assert json.loads(cli.inspect(view="server"))["transport"] == "streamable_http"
    # Reset to a sensible-ish default so other tests aren't surprised.
    si.set_transport(None)


def test_unknown_view_raises_with_new_view_listed():
    from mcp.server.fastmcp.exceptions import ToolError
    import pytest
    with pytest.raises(ToolError, match=r"snapshot, server, sessions, history, stats, policy"):
        cli.inspect(view="bogus")
