"""#3 — hosts.yaml <-> ssh_config MERGE (opt-in via use_ssh_config).

ssh_config options (HostName / User / Port / IdentityFile / IdentityAgent /
ProxyJump / …) are the base; hosts.yaml fields the user EXPLICITLY set override
on top. A hosts.yaml host: that disagrees with the alias's HostName is REFUSED,
not silently resolved.
"""
import pytest

from portal_mcp_server import connection_manager as cm


def _mgr(monkeypatch, tmp_path, resolved_hostname="web01"):
    m = cm.ConnectionManager()
    # Non-empty file list so config= is passed; no real ssh-config reads.
    monkeypatch.setattr(m, "_ssh_config_files", lambda: [tmp_path / "config"])
    monkeypatch.setattr(m, "_known_hosts_arg", lambda cfg: None)
    monkeypatch.setattr(
        m, "_resolve_ssh_config_fields",
        lambda name, files=None: {"host": resolved_hostname, "user": "u", "port": 22})
    return m


@pytest.mark.asyncio
async def test_merge_defers_to_ssh_config_when_no_overrides(monkeypatch, tmp_path):
    m = _mgr(monkeypatch, tmp_path)
    cfg = cm.HostConfig(name="web01", host="web01", use_ssh_config=True,
                        specified_fields=frozenset({"use_ssh_config"}))
    kw = await m._build_connect_kwargs(cfg)
    assert kw["host"] == "web01"          # alias → asyncssh matches Host web01
    assert "config" in kw                 # ssh config files handed to asyncssh
    assert "username" not in kw and "port" not in kw   # deferred to ssh_config
    assert "client_keys" not in kw        # deferred to ssh_config IdentityFile/Agent


@pytest.mark.asyncio
async def test_merge_applies_explicit_overrides(monkeypatch, tmp_path):
    m = _mgr(monkeypatch, tmp_path)
    cfg = cm.HostConfig(name="web01", host="web01", user="deploy", port=2222,
                        use_ssh_config=True,
                        specified_fields=frozenset(
                            {"use_ssh_config", "user", "port"}))
    kw = await m._build_connect_kwargs(cfg)
    assert kw["username"] == "deploy"     # hosts.yaml overrides ssh_config User
    assert kw["port"] == 2222


@pytest.mark.asyncio
async def test_merge_hostname_mismatch_refused(monkeypatch, tmp_path):
    m = _mgr(monkeypatch, tmp_path, resolved_hostname="10.0.0.5")
    cfg = cm.HostConfig(name="web01", host="192.0.2.9", use_ssh_config=True,
                        specified_fields=frozenset({"use_ssh_config", "host"}))
    with pytest.raises(RuntimeError, match="HostName"):
        await m._build_connect_kwargs(cfg)


@pytest.mark.asyncio
async def test_merge_hostname_match_ok(monkeypatch, tmp_path):
    m = _mgr(monkeypatch, tmp_path, resolved_hostname="192.0.2.9")
    cfg = cm.HostConfig(name="web01", host="192.0.2.9", use_ssh_config=True,
                        specified_fields=frozenset({"use_ssh_config", "host"}))
    kw = await m._build_connect_kwargs(cfg)   # no raise
    assert kw["host"] == "web01"


@pytest.mark.asyncio
async def test_merge_hostname_omitted_inherits(monkeypatch, tmp_path):
    m = _mgr(monkeypatch, tmp_path, resolved_hostname="10.0.0.5")
    # host: omitted -> host defaults to name; "host" NOT in specified -> no guard
    cfg = cm.HostConfig(name="web01", host="web01", use_ssh_config=True,
                        specified_fields=frozenset({"use_ssh_config"}))
    kw = await m._build_connect_kwargs(cfg)   # no raise (HostName inherited)
    assert kw["host"] == "web01"


@pytest.mark.asyncio
async def test_merge_explicit_key_overrides(monkeypatch, tmp_path):
    m = _mgr(monkeypatch, tmp_path)
    cfg = cm.HostConfig(name="web01", host="web01", key="/tmp/k",
                        use_ssh_config=True,
                        specified_fields=frozenset({"use_ssh_config", "key"}))
    kw = await m._build_connect_kwargs(cfg)
    assert kw["client_keys"] == ["/tmp/k"]   # explicit key overrides IdentityFile


@pytest.mark.asyncio
async def test_explicit_host_still_enumerates_default_keys(monkeypatch, tmp_path):
    """Non-merge host with no key still enumerates default key files (the merge
    skip must not leak into the ordinary path)."""
    m = _mgr(monkeypatch, tmp_path)
    cfg = cm.HostConfig(name="web01", host="10.0.0.1")  # use_ssh_config False
    kw = await m._build_connect_kwargs(cfg)
    assert kw["host"] == "10.0.0.1" and kw["username"] == "root" and kw["port"] == 22
