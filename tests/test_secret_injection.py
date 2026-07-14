"""named-secret injection — API tokens reach the command, never the LLM.

Mirrors test_sudo_auth.py: a secret value must reach the executed command only
through the in-memory cache (populated by the ``portal secret set`` side-channel)
or a secrets.yaml ``command`` — never as an MCP tool parameter, never in the
returned output, never in the audit log.
"""
from __future__ import annotations

import inspect

import pytest

from mcp.server.fastmcp.exceptions import ToolError


# ────────────────────────────────────────────────────────────────────────────
#  LLM-facing safety invariants
# ────────────────────────────────────────────────────────────────────────────

def test_tools_take_secret_names_not_values():
    """portal_exec / portal_local_exec accept a list of secret NAMES via
    `secrets`, never a token/value/secret parameter."""
    from portal_mcp_server.cli import portal_exec, portal_local_exec
    for fn in (portal_exec, portal_local_exec):
        params = inspect.signature(fn).parameters
        assert "secrets" in params
        assert not any(
            bad in p.lower()
            for p in params
            for bad in ("token", "value", "apikey", "api_key")
        ), f"{fn.__name__} must not take a secret value as a parameter"


def test_env_var_name_mapping():
    from portal_mcp_server import secrets_store as ss
    assert ss.env_var_name("github_token") == "GITHUB_TOKEN"
    assert ss.env_var_name("openai-api-key") == "OPENAI_API_KEY"
    assert ss.env_var_name("1starts.digit") == "_1STARTS_DIGIT"


def test_redact_masks_every_value_longest_first():
    from portal_mcp_server import secrets_store as ss
    assert ss.redact("a SECRET b", ["SECRET"]) == "a *** b"
    # overlapping: the longer value must be fully masked
    assert ss.redact("ABCDEF", ["AB", "ABCDEF"]) == "***"
    assert ss.redact("", ["x"]) == ""
    assert ss.redact("nothing", [""]) == "nothing"


# ────────────────────────────────────────────────────────────────────────────
#  In-memory TTL cache
# ────────────────────────────────────────────────────────────────────────────

def test_cache_set_get_clear():
    from portal_mcp_server import secrets_store as ss
    ss.clear_secret()
    ss.cache_secret("github_token", "tok", ttl=60)
    assert ss._get_cached("github_token") == "tok"
    ss.clear_secret("github_token")
    assert ss._get_cached("github_token") is None


def test_cache_ttl_expiry():
    from portal_mcp_server import secrets_store as ss
    ss.clear_secret()
    ss.cache_secret("github_token", "tok", ttl=-1)
    assert ss._get_cached("github_token") is None


# ────────────────────────────────────────────────────────────────────────────
#  resolve_secret: cache first, then secrets.yaml command
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_prefers_cache(monkeypatch, tmp_path):
    from portal_mcp_server import secrets_store as ss
    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(tmp_path / "nope.yaml"))
    ss.reload_registry()
    ss.clear_secret()
    ss.cache_secret("github_token", "from-cache", ttl=60)
    assert await ss.resolve_secret("github_token") == "from-cache"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_command(monkeypatch, tmp_path):
    from portal_mcp_server import secrets_store as ss
    p = tmp_path / "secrets.yaml"
    p.write_text("secrets:\n  github_token:\n    command: \"printf %s tok-from-cmd\"\n")
    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(p))
    ss.reload_registry()
    ss.clear_secret()
    assert await ss.resolve_secret("github_token") == "tok-from-cmd"


@pytest.mark.asyncio
async def test_resolve_shorthand_command_string(monkeypatch, tmp_path):
    from portal_mcp_server import secrets_store as ss
    p = tmp_path / "secrets.yaml"
    p.write_text("secrets:\n  k: \"printf %s vvv\"\n")
    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(p))
    ss.reload_registry()
    ss.clear_secret()
    assert await ss.resolve_secret("k") == "vvv"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_source(monkeypatch, tmp_path):
    from portal_mcp_server import secrets_store as ss
    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(tmp_path / "missing.yaml"))
    ss.reload_registry()
    ss.clear_secret()
    assert await ss.resolve_secret("unknown") is None


@pytest.mark.asyncio
async def test_resolve_propagates_command_failure_without_value(monkeypatch, tmp_path):
    from portal_mcp_server import secrets_store as ss
    p = tmp_path / "secrets.yaml"
    p.write_text("secrets:\n  bad:\n    command: \"exit 3\"\n")
    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(p))
    ss.reload_registry()
    ss.clear_secret()
    with pytest.raises(RuntimeError) as ei:
        await ss.resolve_secret("bad")
    # error mentions the name + exit code, never the command body / a value
    assert "bad" in str(ei.value)
    assert "exit 3" not in str(ei.value)


# ────────────────────────────────────────────────────────────────────────────
#  secrets.yaml registry: warnings, never a plaintext value
# ────────────────────────────────────────────────────────────────────────────

def test_registry_warns_on_plaintext_value(monkeypatch, tmp_path):
    from portal_mcp_server import secrets_store as ss
    p = tmp_path / "secrets.yaml"
    p.write_text("secrets:\n  leaky:\n    value: hunter2\n")
    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(p))
    ss.reload_registry()
    warns = ss.registry_warnings()
    assert "leaky" in warns
    assert any("plaintext" in w.lower() for w in warns["leaky"])
    # a plaintext value must NOT become a usable command source
    assert ss.secret_command_for("leaky") is None


def test_registry_warns_on_invalid_name(monkeypatch, tmp_path):
    from portal_mcp_server import secrets_store as ss
    p = tmp_path / "secrets.yaml"
    p.write_text("secrets:\n  \"bad name!\":\n    command: \"echo x\"\n")
    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(p))
    ss.reload_registry()
    assert "bad name!" in ss.registry_warnings()


# ────────────────────────────────────────────────────────────────────────────
#  Live agent round trip: `portal secret set` client → per-user agent cache
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_secrets_control_socket_roundtrip(agent_socket):
    from portal_mcp_server import secrets_store as ss
    ss.clear_secret()

    assert ss.control_secrets_socket_path() == agent_socket
    assert oct(agent_socket.stat().st_mode & 0o777) == oct(0o600)

    resp = ss.send_secret("github_token", "live-token", ttl=60)
    assert resp.get("status") == "ok", resp
    assert await ss.resolve_secret("github_token") == "live-token"


# ────────────────────────────────────────────────────────────────────────────
#  portal_local_exec: disabled by default, injects env, redacts, no value in audit
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_local_exec_disabled_by_default(monkeypatch):
    from portal_mcp_server import cli
    monkeypatch.delenv("PORTAL_ALLOW_LOCAL_EXEC", raising=False)
    with pytest.raises(ToolError, match="disabled"):
        await cli.portal_local_exec("echo hi", timeout=30)


@pytest.mark.asyncio
async def test_local_exec_injects_env_and_redacts(monkeypatch):
    import json
    from portal_mcp_server import cli, secrets_store as ss
    monkeypatch.setenv("PORTAL_ALLOW_LOCAL_EXEC", "1")
    ss.clear_secret()
    ss.cache_secret("my_token", "VALUE-XYZ-789", ttl=60)

    # env injected correctly: command checks equality, prints MATCH not the value
    out = await cli.portal_local_exec(
        'test "$MY_TOKEN" = VALUE-XYZ-789 && echo MATCH', secrets=["my_token"], timeout=30)
    res = json.loads(out)
    assert res["output"] == "MATCH"

    # echo of the secret is redacted
    out2 = await cli.portal_local_exec('echo "$MY_TOKEN"', secrets=["my_token"], timeout=30)
    assert "VALUE-XYZ-789" not in out2
    assert "***" in out2


@pytest.mark.asyncio
async def test_local_exec_value_never_in_audit(monkeypatch):
    from portal_mcp_server import cli, secrets_store as ss
    from portal_mcp_server.audit import get_history
    monkeypatch.setenv("PORTAL_ALLOW_LOCAL_EXEC", "1")
    ss.clear_secret()
    ss.cache_secret("my_token", "VALUE-XYZ-789", ttl=60)
    # command references the secret by env var only — the value is injected via
    # the environment, so the mechanism must not leak it into the audit entry.
    await cli.portal_local_exec('echo "$MY_TOKEN"', secrets=["my_token"], timeout=30)
    latest = get_history(limit=1)[0]
    assert "VALUE-XYZ-789" not in str(latest)
    assert "my_token" in latest["command"]  # the NAME is fine to record


@pytest.mark.asyncio
async def test_local_exec_unknown_secret_errors(monkeypatch, tmp_path):
    from portal_mcp_server import cli, secrets_store as ss
    monkeypatch.setenv("PORTAL_ALLOW_LOCAL_EXEC", "1")
    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(tmp_path / "missing.yaml"))
    ss.reload_registry()
    ss.clear_secret()
    with pytest.raises(ToolError, match="not available"):
        await cli.portal_local_exec("echo hi", secrets=["nope"], timeout=30)
