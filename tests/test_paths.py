"""Tests for ``portal_mcp_server.paths``.

Focus on the absolute-vs-relative override policy: every ``PORTAL_*`` env
override must be absolute, mirroring the XDG-spec policy applied to
``XDG_CONFIG_HOME`` / ``XDG_STATE_HOME``. Relative values are warned and
ignored — the function falls back to the platform default.
"""
from __future__ import annotations

import logging
import re

import pytest

from portal_mcp_server import paths


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip every env var the resolver looks at, so each test starts clean."""
    for var in (
        "PORTAL_HOSTS_YAML",
        "PORTAL_POLICIES_YAML",
        "PORTAL_SECRETS_YAML",
        "PORTAL_LOG_DIR",
        "PORTAL_CREDENTIAL_AGENT_SOCKET",
        "PORTAL_SSH_CONFIG",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize(
    "env_var, resolver",
    [
        ("PORTAL_HOSTS_YAML", paths.hosts_yaml_path),
        ("PORTAL_POLICIES_YAML", paths.policies_yaml_path),
        ("PORTAL_SECRETS_YAML", paths.secrets_yaml_path),
        ("PORTAL_LOG_DIR", paths.default_log_dir),
    ],
)
def test_portal_override_absolute_is_honoured(monkeypatch, tmp_path, env_var, resolver):
    target = tmp_path / "abs"
    monkeypatch.setenv(env_var, str(target))
    assert resolver() == target


@pytest.mark.parametrize(
    "env_var, resolver",
    [
        ("PORTAL_HOSTS_YAML", paths.hosts_yaml_path),
        ("PORTAL_POLICIES_YAML", paths.policies_yaml_path),
        ("PORTAL_SECRETS_YAML", paths.secrets_yaml_path),
        ("PORTAL_LOG_DIR", paths.default_log_dir),
    ],
)
def test_portal_override_relative_is_rejected(
    monkeypatch, caplog, env_var, resolver,
):
    monkeypatch.setenv(env_var, "relative/path")
    caplog.set_level(logging.WARNING, logger="portal_mcp_server.paths")
    result = resolver()
    # Must NOT have been interpreted as cwd-relative — the result is an
    # absolute path resolved from the platform default (or XDG override),
    # not the literal relative override.
    assert result.is_absolute(), f"{env_var}: relative override leaked through"
    assert "relative/path" not in str(result)
    assert any(
        env_var in rec.message and "non-absolute" in rec.message
        for rec in caplog.records
    ), f"expected a warning naming {env_var}; got {[r.message for r in caplog.records]}"


def test_portal_override_user_expansion(monkeypatch, tmp_path):
    """``~`` is expanded before the absolute check (the resolved path *is* absolute)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PORTAL_LOG_DIR", "~/logs")
    assert paths.default_log_dir() == tmp_path / "logs"


def test_relative_xdg_env_is_sanitized(monkeypatch, caplog):
    """Relative XDG_CONFIG_HOME / XDG_STATE_HOME are dropped + warned + cleared."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel/cfg")
    monkeypatch.setenv("XDG_STATE_HOME", "rel/state")
    caplog.set_level(logging.WARNING, logger="portal_mcp_server.paths")
    # Bust the _warn_relative_xdg lru_cache so warnings fire even when other
    # tests in the same process already triggered the same env_var+raw pair.
    paths._warn_relative_xdg.cache_clear()
    config = paths.xdg_config_home()
    state = paths.xdg_state_home()
    assert config.is_absolute()
    assert state.is_absolute()
    assert "rel/cfg" not in str(config)
    assert "rel/state" not in str(state)
    assert any(
        "XDG_CONFIG_HOME" in r.message and "non-absolute" in r.message
        for r in caplog.records
    )


def test_credential_agent_socket_override_relative_falls_through(
    monkeypatch, tmp_path, caplog,
):
    """A relative PORTAL_CREDENTIAL_AGENT_SOCKET warns + falls through to agent.json."""
    # No agent.json configured + no env -> raises CredentialAgentNotConfigured.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PORTAL_CREDENTIAL_AGENT_SOCKET", "rel/sock")
    caplog.set_level(logging.WARNING, logger="portal_mcp_server.paths")
    with pytest.raises(paths.CredentialAgentNotConfigured):
        paths.credential_agent_socket_path()
    assert any(
        "PORTAL_CREDENTIAL_AGENT_SOCKET" in r.message
        and "non-absolute" in r.message
        for r in caplog.records
    )


def test_credential_agent_socket_override_absolute_is_honoured(
    monkeypatch, tmp_path,
):
    target = tmp_path / "creds.sock"
    monkeypatch.setenv("PORTAL_CREDENTIAL_AGENT_SOCKET", str(target))
    assert paths.credential_agent_socket_path() == target


def test_resolved_paths_are_under_platform_dirs(monkeypatch, tmp_path):
    """Without any override, resolved paths sit under platformdirs locations."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # We don't pin a platform here — just assert each resolver returns an
    # absolute path rooted at the test HOME (platformdirs picks the right
    # base for the current OS).
    for resolver in (
        paths.hosts_yaml_path,
        paths.policies_yaml_path,
        paths.secrets_yaml_path,
        paths.default_log_dir,
    ):
        p = resolver()
        assert p.is_absolute()
        # On Linux/macOS the resolved path will start with $HOME; on
        # Windows the test HOME would still appear in %LOCALAPPDATA% only
        # if the runner sets it that way, so we use a permissive check.
        match = re.search(re.escape(str(tmp_path)), str(p))
        assert match, f"{resolver.__name__} -> {p!r} does not contain {tmp_path!r}"


# ── OpenSSH client config discovery ──────────────────────────────────────────

def test_ssh_config_path_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.ssh_config_path() == tmp_path / ".ssh" / "config"


def test_ssh_config_path_override_absolute_is_honoured(monkeypatch, tmp_path):
    target = tmp_path / "myconfig"
    monkeypatch.setenv("PORTAL_SSH_CONFIG", str(target))
    assert paths.ssh_config_path() == target


def test_ssh_config_override_relative_is_rejected(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PORTAL_SSH_CONFIG", "relative/config")
    caplog.set_level(logging.WARNING, logger="portal_mcp_server.paths")
    result = paths.ssh_config_path()
    assert result == tmp_path / ".ssh" / "config"     # fell back to the default
    assert any(
        "PORTAL_SSH_CONFIG" in r.message and "non-absolute" in r.message
        for r in caplog.records
    ), f"expected a warning; got {[r.message for r in caplog.records]}"


def test_ssh_config_files_user_then_system_in_order(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    user = home / ".ssh" / "config"
    user.write_text("Host u\n")
    monkeypatch.setenv("HOME", str(home))
    system = tmp_path / "etc_ssh_config"
    system.write_text("Host s\n")
    monkeypatch.setattr(paths, "system_ssh_config_path", lambda: system)
    assert paths.ssh_config_files() == [user, system]


def test_ssh_config_files_skips_missing(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)        # no ~/.ssh/config written
    monkeypatch.setenv("HOME", str(home))
    system = tmp_path / "etc_ssh_config"
    system.write_text("Host s\n")
    monkeypatch.setattr(paths, "system_ssh_config_path", lambda: system)
    assert paths.ssh_config_files() == [system]   # only the existing file


def test_ssh_config_override_suppresses_system_like_dash_F(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host u\n")
    monkeypatch.setenv("HOME", str(home))
    system = tmp_path / "etc_ssh_config"
    system.write_text("Host s\n")
    monkeypatch.setattr(paths, "system_ssh_config_path", lambda: system)
    alt = tmp_path / "alt"
    alt.write_text("Host a\n")
    monkeypatch.setenv("PORTAL_SSH_CONFIG", str(alt))
    assert paths.ssh_config_files() == [alt]      # -F semantics: only the override


def test_ssh_config_none_reads_nothing(monkeypatch, tmp_path):
    """PORTAL_SSH_CONFIG=none == `ssh -F none`: no config file at all, even
    though both the user and system files exist."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host u\n")
    monkeypatch.setenv("HOME", str(home))
    system = tmp_path / "etc_ssh_config"
    system.write_text("Host s\n")
    monkeypatch.setattr(paths, "system_ssh_config_path", lambda: system)
    monkeypatch.setenv("PORTAL_SSH_CONFIG", "none")
    assert paths.ssh_config_files() == []
    # ssh_config_path (display only) still reports the conventional default.
    assert paths.ssh_config_path() == home / ".ssh" / "config"


def test_ssh_config_none_is_case_insensitive(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host u\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PORTAL_SSH_CONFIG", "NONE")
    assert paths.ssh_config_files() == []


def test_system_ssh_config_path_posix(monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert paths.system_ssh_config_path() == Path("/etc/ssh/ssh_config")


def test_system_ssh_config_path_windows(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", r"C:\ProgramData")
    p = paths.system_ssh_config_path()
    assert p.name == "ssh_config"
    assert "ssh" in str(p) and "ProgramData" in str(p)

