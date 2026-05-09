"""Tests for SSH host-key verification policy.

Audit finding: ``known_hosts=None`` was hard-coded for every host, totally
disabling MITM protection. The fix makes strict checking the default and
requires opt-in to disable it per host.

We don't open real SSH connections — we inspect the kwargs that
``_build_connect_kwargs`` produces, which is what gets handed to
``asyncssh.connect``.
"""
from __future__ import annotations

import pytest

from ssh_remote_mcp.connection_manager import ConnectionManager, HostConfig


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    """Empty manager bound to a tmp hosts.yaml so we don't load real config."""
    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    return ConnectionManager(hosts_yaml=yml)


class TestHostKeyDefaults:
    @pytest.mark.asyncio
    async def test_default_strict_yields_default_known_hosts(self, mgr):
        cfg = HostConfig(name="x", host="1.2.3.4")
        # Sanity: dataclass default is strict.
        assert cfg.strict_host_key_checking is True
        kwargs = await mgr._build_connect_kwargs(cfg)
        # Empty tuple = "use default ~/.ssh/known_hosts and be strict",
        # NOT None (which would disable verification).
        assert kwargs["known_hosts"] == ()

    @pytest.mark.asyncio
    async def test_explicit_known_hosts_path_used(self, mgr, tmp_path):
        kh = tmp_path / "known_hosts"
        kh.write_text("")
        cfg = HostConfig(name="x", host="1.2.3.4", known_hosts=str(kh))
        kwargs = await mgr._build_connect_kwargs(cfg)
        assert kwargs["known_hosts"] == str(kh)

    @pytest.mark.asyncio
    async def test_strict_false_disables_verification(self, mgr, caplog):
        cfg = HostConfig(name="x", host="1.2.3.4",
                         strict_host_key_checking=False)
        with caplog.at_level("WARNING"):
            kwargs = await mgr._build_connect_kwargs(cfg)
        # Only this branch is allowed to produce known_hosts=None.
        assert kwargs["known_hosts"] is None
        assert any("DISABLED" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_use_ssh_config_still_strict(self, mgr):
        # The shortcut path that defers to ~/.ssh/config used to be
        # unconditionally permissive (known_hosts=None). Now it follows the
        # same policy.
        cfg = HostConfig(name="alias", host="alias", use_ssh_config=True)
        kwargs = await mgr._build_connect_kwargs(cfg)
        assert kwargs["known_hosts"] == ()


class TestRegistryYAMLParsing:
    def test_yaml_loads_strict_field(self, tmp_path):
        yml = tmp_path / "hosts.yaml"
        yml.write_text(
            """
hosts:
  alpha:
    host: 1.2.3.4
    user: deploy
  beta:
    host: 5.6.7.8
    user: deploy
    strict_host_key_checking: false
  gamma:
    host: 9.9.9.9
    user: deploy
    known_hosts: /etc/ssh/ssh_known_hosts
"""
        )
        m = ConnectionManager(hosts_yaml=yml)
        assert m._registry["alpha"].strict_host_key_checking is True
        assert m._registry["beta"].strict_host_key_checking is False
        assert m._registry["gamma"].known_hosts == "/etc/ssh/ssh_known_hosts"
        assert m._registry["gamma"].strict_host_key_checking is True

    def test_register_host_default_is_strict(self, mgr):
        mgr.register_host("z", "10.0.0.1")
        assert mgr._registry["z"].strict_host_key_checking is True

    def test_register_host_can_opt_out(self, mgr):
        mgr.register_host("z", "10.0.0.1", strict_host_key_checking=False)
        assert mgr._registry["z"].strict_host_key_checking is False
