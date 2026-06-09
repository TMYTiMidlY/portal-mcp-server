"""W-PASS: SSH key passphrase reuses the SSH-login-password side channel, and
explicit ssh-agent control via use_ssh_agent.

From the user's view "the password for this host" is one idea whether it
unlocks the login or decrypts a key, so it shares the same `portal ssh set
<host>` cache / per-host slot; the connection picks password vs passphrase by
the host's auth mode. use_ssh_agent gives explicit control over the agent.
"""
from __future__ import annotations

import pytest

from portal_mcp_server import connection_manager as cm
from portal_mcp_server import ssh_creds


def _mgr(tmp_path):
    p = tmp_path / "hosts.yaml"
    p.write_text("hosts: {}\n")
    return cm.ConnectionManager(hosts_yaml=p)


# ── _resolve_ssh_passphrase chain ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_passphrase_prefers_cache(tmp_path):
    m = _mgr(tmp_path)
    ssh_creds.clear_ssh_password()
    ssh_creds.cache_ssh_password("web01", "from-cache", ttl=60)
    cfg = cm.HostConfig(name="web01", host="1.2.3.4",
                        passphrase_command="printf '%s' from-command")
    assert await m._resolve_ssh_passphrase(cfg) == "from-cache"


@pytest.mark.asyncio
async def test_passphrase_falls_back_to_command(tmp_path):
    m = _mgr(tmp_path)
    ssh_creds.clear_ssh_password()
    cfg = cm.HostConfig(name="web01", host="1.2.3.4",
                        passphrase_command="printf '%s' from-command")
    assert await m._resolve_ssh_passphrase(cfg) == "from-command"


@pytest.mark.asyncio
async def test_passphrase_none_when_no_source(tmp_path):
    m = _mgr(tmp_path)
    ssh_creds.clear_ssh_password()
    cfg = cm.HostConfig(name="web01", host="1.2.3.4")
    assert await m._resolve_ssh_passphrase(cfg) is None


@pytest.mark.asyncio
async def test_cached_value_used_as_passphrase_on_key_host(tmp_path):
    m = _mgr(tmp_path)
    ssh_creds.clear_ssh_password()
    ssh_creds.cache_ssh_password("web01", "unlock-me", ttl=60)
    cfg = cm.HostConfig(name="web01", host="1.2.3.4", key="/tmp/fake_key")
    kwargs = await m._build_connect_kwargs(cfg)
    assert kwargs["passphrase"] == "unlock-me"
    assert kwargs["client_keys"] == ["/tmp/fake_key"]


# ── use_ssh_agent flag ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_use_ssh_agent_true_omits_client_keys(tmp_path):
    m = _mgr(tmp_path)
    ssh_creds.clear_ssh_password()
    cfg = cm.HostConfig(name="web01", host="1.2.3.4", key="/tmp/fake_key",
                        use_ssh_agent=True)
    kwargs = await m._build_connect_kwargs(cfg)
    # Pure agent: don't pass the key file, and no passphrase resolution.
    assert "client_keys" not in kwargs
    assert "passphrase" not in kwargs


@pytest.mark.asyncio
async def test_use_ssh_agent_false_disables_agent(tmp_path):
    m = _mgr(tmp_path)
    ssh_creds.clear_ssh_password()
    cfg = cm.HostConfig(name="web01", host="1.2.3.4", key="/tmp/fake_key",
                        use_ssh_agent=False)
    kwargs = await m._build_connect_kwargs(cfg)
    assert kwargs["agent_path"] is None
    assert kwargs["client_keys"] == ["/tmp/fake_key"]


@pytest.mark.asyncio
async def test_use_ssh_agent_auto_leaves_agent_alone(tmp_path):
    m = _mgr(tmp_path)
    ssh_creds.clear_ssh_password()
    cfg = cm.HostConfig(name="web01", host="1.2.3.4", key="/tmp/fake_key")
    kwargs = await m._build_connect_kwargs(cfg)
    assert "agent_path" not in kwargs  # asyncssh default (uses SSH_AUTH_SOCK)


def test_use_ssh_agent_read_from_yaml(tmp_path):
    p = tmp_path / "hosts.yaml"
    p.write_text("hosts:\n  web01:\n    host: 1.2.3.4\n    use_ssh_agent: true\n")
    m = cm.ConnectionManager(hosts_yaml=p)
    assert m._registry["web01"].use_ssh_agent is True
