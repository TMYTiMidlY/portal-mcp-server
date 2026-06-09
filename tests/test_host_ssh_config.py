"""hosts.yaml <-> ~/.ssh/config interaction: conflict detection + overlay.

hosts.yaml fully overrides ssh config (no field-level merge). These tests pin
the warnings that surface the two footguns, the use_ssh_config overlay recipe
(connection from ssh config, metadata from hosts.yaml), and the
register-by-name-only path that auto-detects an ssh config alias.
"""
from __future__ import annotations

import pytest

from mcp.server.fastmcp.exceptions import ToolError

from portal_mcp_server import connection_manager as cm
from portal_mcp_server import cli, security


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _ssh_config(home, content):
    (home / ".ssh" / "config").write_text(content)


def _hosts_yaml(tmp_path, body):
    p = tmp_path / "hosts.yaml"
    p.write_text(body)
    return p


# ── alias parsing ───────────────────────────────────────────────────────────

def test_alias_set_excludes_wildcards(home, tmp_path):
    _ssh_config(home, "Host web01 web01-alt\n  HostName 10.0.0.1\n"
                      "Host *\n  User root\n")
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    assert m.has_ssh_config_alias("web01")
    assert m.has_ssh_config_alias("web01-alt")
    assert not m.has_ssh_config_alias("nope")
    assert not m.has_ssh_config_alias("*")


# ── overlay warnings ────────────────────────────────────────────────────────

def test_hosts_yaml_overlapping_ssh_config_warns(home, tmp_path):
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n")
    yml = _hosts_yaml(tmp_path, "hosts:\n  web01:\n    host: 1.2.3.4\n")
    m = cm.ConnectionManager(hosts_yaml=yml)
    warns = m.config_warnings().get("web01", [])
    assert any("BOTH hosts.yaml and" in w and "use_ssh_config" in w for w in warns)


def test_use_ssh_config_without_alias_warns(home, tmp_path):
    _ssh_config(home, "Host other\n  HostName 9.9.9.9\n")
    yml = _hosts_yaml(
        tmp_path,
        "hosts:\n  web01:\n    use_ssh_config: true\n    tags: [prod]\n")
    m = cm.ConnectionManager(hosts_yaml=yml)
    warns = m.config_warnings().get("web01", [])
    assert any("no matching Host alias" in w for w in warns)
    # Loaded anyway, with use_ssh_config honoured and host defaulting to name.
    assert m._registry["web01"].use_ssh_config is True
    assert m._registry["web01"].host == "web01"


def test_use_ssh_config_with_alias_is_clean(home, tmp_path):
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n  User deploy\n")
    yml = _hosts_yaml(
        tmp_path,
        "hosts:\n  web01:\n    use_ssh_config: true\n    tags: [prod]\n"
        "    sudo_password_command: 'pass show sudo/web01'\n")
    m = cm.ConnectionManager(hosts_yaml=yml)
    assert "web01" not in m.config_warnings()
    cfg = m._registry["web01"]
    assert cfg.use_ssh_config is True
    assert cfg.tags == ["prod"]
    assert cfg.sudo_password_command == "pass show sudo/web01"


def test_use_ssh_config_true_is_actually_read(home, tmp_path):
    """Regression: hosts.yaml use_ssh_config was previously ignored, and a
    host stanza without `host:` raised KeyError at load."""
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n")
    yml = _hosts_yaml(tmp_path, "hosts:\n  web01:\n    use_ssh_config: true\n")
    m = cm.ConnectionManager(hosts_yaml=yml)  # must not raise
    assert m._registry["web01"].use_ssh_config is True


# ── register-by-name-only auto-detects an ssh config alias ──────────────────

@pytest.fixture
def wired(home, tmp_path, monkeypatch):
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    monkeypatch.setattr(cm, "_manager", m)
    monkeypatch.setattr(cli, "get_manager", lambda: m)
    pol = security.SecurityPolicy(policies_yaml=tmp_path / "none.yaml")
    monkeypatch.setattr(cli, "get_policy", lambda: pol)
    return m


def test_register_name_only_uses_ssh_config(home, wired):
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n")
    out = cli.portal_host(action="register", name="web01")
    assert "~/.ssh/config" in out
    assert wired._registry["web01"].use_ssh_config is True


def test_register_name_only_without_alias_errors(home, wired):
    _ssh_config(home, "Host other\n  HostName 9.9.9.9\n")
    with pytest.raises(ToolError, match=r"no ~/.ssh/config Host alias"):
        cli.portal_host(action="register", name="web01")


def test_register_with_explicit_host_still_works(home, wired):
    out = cli.portal_host(action="register", name="db", host="10.0.0.5",
                          user="postgres")
    assert "10.0.0.5" in out
    assert wired._registry["db"].use_ssh_config is False


# ── hosts.yaml ssh_config-style connection fields (W-MAIN.11c) ──────────────

def test_extra_connection_fields_read_from_yaml(home, tmp_path):
    yml = _hosts_yaml(
        tmp_path,
        "hosts:\n"
        "  web01:\n"
        "    host: 10.0.0.1\n"
        "    proxy_jump: 'bastion.example.com'\n"
        "    keepalive_interval: 30\n"
        "    forward_agent: true\n")
    m = cm.ConnectionManager(hosts_yaml=yml)
    cfg = m._registry["web01"]
    assert cfg.proxy_jump == "bastion.example.com"
    assert cfg.keepalive_interval == 30
    assert cfg.forward_agent is True


@pytest.mark.asyncio
async def test_extra_fields_forwarded_to_asyncssh_kwargs(tmp_path):
    cfg = cm.HostConfig(
        name="web01", host="10.0.0.1", key="/tmp/fake_key",
        proxy_jump="user@bastion:2222", keepalive_interval=15,
        forward_agent=True)
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    kwargs = await m._build_connect_kwargs(cfg)
    assert kwargs["tunnel"] == "user@bastion:2222"
    assert kwargs["keepalive_interval"] == 15
    assert kwargs["agent_forwarding"] is True


@pytest.mark.asyncio
async def test_extra_fields_absent_by_default(tmp_path):
    cfg = cm.HostConfig(name="web01", host="10.0.0.1", key="/tmp/fake_key")
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    kwargs = await m._build_connect_kwargs(cfg)
    assert "tunnel" not in kwargs
    assert "keepalive_interval" not in kwargs
    assert "agent_forwarding" not in kwargs
