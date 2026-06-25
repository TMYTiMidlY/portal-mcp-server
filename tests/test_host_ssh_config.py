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


async def test_register_name_only_uses_ssh_config(home, wired):
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n")
    out = await cli.portal_host(action="register", name="web01")
    assert "~/.ssh/config" in out
    assert wired._registry["web01"].use_ssh_config is True


async def test_register_name_only_without_alias_errors(home, wired):
    _ssh_config(home, "Host other\n  HostName 9.9.9.9\n")
    with pytest.raises(ToolError, match=r"no ~/.ssh/config Host alias"):
        await cli.portal_host(action="register", name="web01")


async def test_register_with_explicit_host_still_works(home, wired):
    out = await cli.portal_host(action="register", name="db", host="10.0.0.5",
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


# ── asyncssh-backed detection: Include is followed, wildcards excluded ────────

def test_alias_detection_follows_include(home, tmp_path):
    """Regression: the old line scan missed `Include`d hosts. The asyncssh
    parser follows them, so a host defined only in an included file is found."""
    incdir = home / ".ssh" / "conf.d"
    incdir.mkdir()
    (incdir / "extra.conf").write_text("Host viainclude\n  HostName 10.9.9.9\n")
    _ssh_config(home, "Include conf.d/*.conf\n"
                      "Host direct\n  HostName 10.0.0.1\n"
                      "Host *\n  User root\n")
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    assert m.has_ssh_config_alias("viainclude")   # via Include — the fix
    assert m.has_ssh_config_alias("direct")
    assert not m.has_ssh_config_alias("never-defined")  # only Host * — excluded


def test_alias_detection_catches_user_only_stanza(home, tmp_path):
    """A stanza that sets only User (no HostName) is still an explicit alias."""
    _ssh_config(home, "Host useronly\n  User deploy\nHost *\n  User root\n")
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    assert m.has_ssh_config_alias("useronly")
    assert not m.has_ssh_config_alias("bare")


def test_alias_detection_regex_fallback(home, tmp_path, monkeypatch):
    """If asyncssh can't parse the config, fall back to the regex scan rather
    than reporting every host as undefined."""
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n")
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))

    def boom(self, ssh_config, name):
        raise RuntimeError("asyncssh parse exploded")

    monkeypatch.setattr(cm.ConnectionManager, "_ssh_config_signature", boom)
    assert m.has_ssh_config_alias("web01")          # regex fallback finds it
    assert not m.has_ssh_config_alias("nope")


# ── list_hosts surfaces ssh-config aliases with a `source` label ────────────

@pytest.fixture
def no_system_ssh(monkeypatch, tmp_path):
    """Neutralise the machine's real /etc/ssh/ssh_config so enumeration tests
    are deterministic (point the system-config path at a nonexistent file)."""
    from portal_mcp_server import paths
    monkeypatch.setattr(paths, "system_ssh_config_path",
                        lambda: tmp_path / "no_such_system_ssh_config")


def test_list_includes_ssh_config_aliases_with_source(home, tmp_path, no_system_ssh):
    """An ssh-config alias that was never registered/connected still appears in
    list_hosts, tagged source='ssh-config', with real resolved fields."""
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n  User deploy\n  Port 2222\n"
                      "Host *\n  User root\n")
    yml = _hosts_yaml(tmp_path, "hosts:\n  db:\n    host: 10.0.0.5\n    user: postgres\n")
    m = cm.ConnectionManager(hosts_yaml=yml)
    hosts = {h["name"]: h for h in m.list_hosts()}
    assert hosts["db"]["source"] == "hosts.yaml"
    assert hosts["db"]["host"] == "10.0.0.5"
    assert hosts["web01"]["source"] == "ssh-config"
    assert hosts["web01"]["host"] == "10.0.0.1"
    assert hosts["web01"]["user"] == "deploy"
    assert hosts["web01"]["port"] == 2222
    assert "*" not in hosts          # Host * wildcard is not a concrete alias


def test_list_overlay_resolves_fields_and_labels_source(home, tmp_path, no_system_ssh):
    """A use_ssh_config overlay shows resolved ssh-config fields (not the
    placeholder alias name) and source='hosts.yaml+ssh-config'."""
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n  User deploy\n  Port 2222\n")
    yml = _hosts_yaml(
        tmp_path, "hosts:\n  web01:\n    use_ssh_config: true\n    tags: [prod]\n")
    m = cm.ConnectionManager(hosts_yaml=yml)
    web01 = next(h for h in m.list_hosts() if h["name"] == "web01")
    assert web01["source"] == "hosts.yaml+ssh-config"
    assert web01["host"] == "10.0.0.1"       # resolved, not the placeholder name
    assert web01["user"] == "deploy"
    assert web01["port"] == 2222
    assert web01["tags"] == ["prod"]         # metadata still from hosts.yaml


def test_list_enumeration_follows_include_and_excludes_patterns(
        home, tmp_path, no_system_ssh):
    incdir = home / ".ssh" / "conf.d"
    incdir.mkdir()
    (incdir / "x.conf").write_text("Host viainc\n  HostName 10.9.9.9\n")
    _ssh_config(home, "Include conf.d/*.conf\n"
                      "Host a1 a2\n  HostName 10.0.0.1\n"
                      "Host !neg pat-*\n  User x\n"
                      "Host *\n  User root\n")
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    names = {h["name"] for h in m.list_hosts()}
    assert {"viainc", "a1", "a2"} <= names         # Include + multi-token harvested
    assert {"neg", "pat-*", "*"} & names == set()  # negation/wildcards excluded


def test_enumerate_ssh_config_aliases_direct(home, tmp_path, no_system_ssh):
    _ssh_config(home, "Host alpha\n  HostName 1.1.1.1\n"
                      "Host beta gamma\n  User u\nHost *\n")
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    assert m.enumerate_ssh_config_aliases() == ["alpha", "beta", "gamma"]


def test_hosts_yaml_entry_shadows_ssh_config_alias_in_list(
        home, tmp_path, no_system_ssh):
    """A name defined in both hosts.yaml and ssh config appears once, sourced
    from hosts.yaml (which takes precedence)."""
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n")
    yml = _hosts_yaml(tmp_path, "hosts:\n  web01:\n    host: 9.9.9.9\n")
    m = cm.ConnectionManager(hosts_yaml=yml)
    entries = [h for h in m.list_hosts() if h["name"] == "web01"]
    assert len(entries) == 1
    assert entries[0]["source"] == "hosts.yaml"
    assert entries[0]["host"] == "9.9.9.9"


def test_list_system_config_fallback(home, tmp_path, monkeypatch):
    """Aliases defined only in the system-wide config are surfaced too."""
    _ssh_config(home, "Host useronly\n  HostName 10.0.0.1\n")
    sysfile = tmp_path / "system_ssh_config"
    sysfile.write_text("Host sysonly\n  HostName 7.7.7.7\n  User sysu\n")
    from portal_mcp_server import paths
    monkeypatch.setattr(paths, "system_ssh_config_path", lambda: sysfile)
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    hosts = {h["name"]: h for h in m.list_hosts()}
    assert hosts["useronly"]["source"] == "ssh-config"
    assert hosts["sysonly"]["source"] == "ssh-config"   # from system fallback
    assert hosts["sysonly"]["host"] == "7.7.7.7"
    assert m.has_ssh_config_alias("sysonly")


def test_portal_ssh_config_override_replaces_and_suppresses_system(
        home, tmp_path, monkeypatch):
    """PORTAL_SSH_CONFIG behaves like `ssh -F`: only that file is read; both the
    user ~/.ssh/config and the system-wide config are suppressed."""
    _ssh_config(home, "Host userhost\n  HostName 10.0.0.1\n")
    sysfile = tmp_path / "system_ssh_config"
    sysfile.write_text("Host syshost\n  HostName 7.7.7.7\n")
    alt = tmp_path / "alt_config"
    alt.write_text("Host althost\n  HostName 5.5.5.5\n  User au\n")
    from portal_mcp_server import paths
    monkeypatch.setattr(paths, "system_ssh_config_path", lambda: sysfile)
    monkeypatch.setenv("PORTAL_SSH_CONFIG", str(alt))
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    names = {h["name"] for h in m.list_hosts()}
    assert "althost" in names
    assert "userhost" not in names and "syshost" not in names
    assert m.has_ssh_config_alias("althost")
    assert not m.has_ssh_config_alias("userhost")


def test_portal_ssh_config_none_disables_ssh_config(home, tmp_path, monkeypatch):
    """PORTAL_SSH_CONFIG=none (ssh -F none): no ssh config is read at all; host
    resolution comes solely from hosts.yaml."""
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n")
    sysfile = tmp_path / "system_ssh_config"
    sysfile.write_text("Host syshost\n  HostName 7.7.7.7\n")
    from portal_mcp_server import paths
    monkeypatch.setattr(paths, "system_ssh_config_path", lambda: sysfile)
    monkeypatch.setenv("PORTAL_SSH_CONFIG", "none")
    yml = _hosts_yaml(tmp_path, "hosts:\n  db:\n    host: 10.0.0.5\n")
    m = cm.ConnectionManager(hosts_yaml=yml)
    assert {h["name"] for h in m.list_hosts()} == {"db"}   # only hosts.yaml
    assert not m.has_ssh_config_alias("web01")
    assert not m.has_ssh_config_alias("syshost")
    assert m.enumerate_ssh_config_aliases() == []


@pytest.mark.asyncio
async def test_none_passes_empty_config_to_asyncssh(home, tmp_path, monkeypatch):
    """A use_ssh_config host under -F none hands asyncssh an empty config list
    (read nothing) instead of letting it fall back to ~/.ssh/config."""
    _ssh_config(home, "Host web01\n  HostName 10.0.0.1\n")
    monkeypatch.setenv("PORTAL_SSH_CONFIG", "none")
    m = cm.ConnectionManager(hosts_yaml=_hosts_yaml(tmp_path, "hosts: {}\n"))
    cfg = cm.HostConfig(name="web01", host="web01", use_ssh_config=True)
    kwargs = await m._build_connect_kwargs(cfg)
    assert kwargs["config"] == []

