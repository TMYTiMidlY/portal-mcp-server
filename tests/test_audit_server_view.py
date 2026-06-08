"""portal_audit's new view='server' + the `server` block embedded in
view='snapshot'. Covers the diagnostic surface added so an agent can ask
"which version of portal-mcp-server am I talking to?" without needing
PORTAL_ALLOW_LOCAL_EXEC or shell access.
"""
from __future__ import annotations

import json

from portal_mcp_server import cli, server_info as si


def test_view_server_returns_metadata_dict():
    out = cli.portal_audit(view="server")
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
    out = cli.portal_audit(view="snapshot")
    data = json.loads(out)
    assert "server" in data, f"snapshot missing 'server' block: {data}"
    assert data["server"]["version"] == json.loads(
        cli.portal_audit(view="server"))["version"]


def test_set_transport_records_value():
    si.set_transport("stdio")
    assert json.loads(cli.portal_audit(view="server"))["transport"] == "stdio"
    si.set_transport("streamable_http")
    assert json.loads(cli.portal_audit(view="server"))["transport"] == "streamable_http"
    # Reset to a sensible-ish default so other tests aren't surprised.
    si.set_transport(None)


def test_unknown_view_raises_with_new_view_listed():
    from mcp.server.fastmcp.exceptions import ToolError
    import pytest
    with pytest.raises(ToolError, match=r"snapshot, server, history, stats, policy"):
        cli.portal_audit(view="bogus")
