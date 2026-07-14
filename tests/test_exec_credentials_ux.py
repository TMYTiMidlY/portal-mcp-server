"""UX/safety around credentialed exec:
  * sudo / secret exec results are flagged high_risk so the agent reports it;
  * the missing-sudo-password error names both the temporary and permanent
    password sources;
  * `portal <kind> set` auto-installs the credential agent when it's not up;
  * decision-point onboarding: the exec tool docstrings front-load the
    `portal secret set` cue instead of asking the user for plaintext.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from portal_mcp_server import cli, sudo_creds


# ── high-risk marker on credentialed exec ────────────────────────────────────

@pytest.mark.asyncio
async def test_exec_sudo_result_is_flagged_high_risk(monkeypatch):
    async def fake_resolve(host):
        return "pw"

    async def fake_sudo(h, cmd, password, env=None, timeout=0):
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "root", "stderr": ""}

    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", fake_resolve)
    monkeypatch.setattr(cli, "_re_sudo_exec", fake_sudo)
    out = json.loads(await cli.remote_exec("web01", "id", use_sudo=True, timeout=30))
    assert out["high_risk"] is True
    assert "high_risk_note" in out and out["high_risk_note"]


@pytest.mark.asyncio
async def test_exec_sudo_and_secrets_coexist(monkeypatch, tmp_path):
    """remote_exec(use_sudo=True, secrets=[...]) resolves the secret, passes it
    to the sudo exec as env (delivered on stdin inside the elevated shell), and
    redacts the value from the returned stdout/stderr."""
    from portal_mcp_server import secrets_store as ss

    monkeypatch.setenv("PORTAL_SECRETS_YAML", str(tmp_path / "missing.yaml"))
    ss.reload_registry()
    ss.clear_secret()
    ss.cache_secret("github_token", "ghp_SECRET", ttl=60)

    async def fake_resolve(host):
        return "pw"

    captured = {}

    async def fake_sudo(h, cmd, password, env=None, timeout=0):
        captured["env"] = dict(env or {})
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "token=ghp_SECRET", "stderr": ""}

    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", fake_resolve)
    monkeypatch.setattr(cli, "_re_sudo_exec", fake_sudo)
    out = json.loads(await cli.remote_exec("web01", "echo $GITHUB_TOKEN",
                                           use_sudo=True, secrets=["github_token"], timeout=30))
    assert captured["env"] == {"GITHUB_TOKEN": "ghp_SECRET"}
    assert "ghp_SECRET" not in out["stdout"]     # redacted out of the result
    assert "***" in out["stdout"]
    assert out["high_risk"] is True


@pytest.mark.asyncio
async def test_exec_secrets_result_is_flagged_high_risk(monkeypatch):
    async def fake_resolve_secrets(names):
        return ({"X": "v"}, ["v"], None)

    async def fake_exec_env(h, cmd, env, timeout=0):
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(cli, "_resolve_secrets", fake_resolve_secrets)
    monkeypatch.setattr(cli, "_re_exec_env", fake_exec_env)
    out = json.loads(await cli.remote_exec("web01", "echo $X", secrets=["X"], timeout=30))
    assert out["high_risk"] is True


@pytest.mark.asyncio
async def test_plain_exec_is_not_flagged_high_risk(monkeypatch):
    async def fake_exec(h, cmd, timeout=0, login=True):
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "hi", "stderr": ""}

    monkeypatch.setattr(cli, "ssh_exec", fake_exec)
    out = json.loads(await cli.remote_exec("web01", "echo hi", timeout=30))
    assert "high_risk" not in out


# ── missing-sudo-password guidance names both sources ────────────────────────

def test_sudo_missing_message_names_temp_and_permanent_sources():
    msg = cli._sudo_missing_message("web01")
    assert "portal sudo set web01" in msg          # temporary (no-echo)
    assert "sudo_password_command" in msg          # permanent (password manager)
    assert "no-echo" in msg.lower()
    # never invite pasting the password into the conversation
    assert "paste" in msg.lower()


# ── `portal <kind> set` auto-installs the agent when it's not up ──────────────

def test_set_autoinstalls_agent_when_socket_absent(monkeypatch, capsys, tmp_path):
    missing = tmp_path / "credentials.sock"
    calls = {}

    def fake_install(*, socket_path=None, enable_now=False):
        calls["enable_now"] = enable_now
        missing.write_text("x")  # socket activation creates it
        return {"socket_unit": "portal-credential-agent.socket",
                "service_unit": "portal-credential-agent.service",
                "config_path": "/cfg", "socket_path": str(missing)}

    monkeypatch.setattr("portal_mcp_server.credential_agent.install_agent",
                        fake_install)
    monkeypatch.setattr("portal_mcp_server.paths.credential_agent_platform",
                        lambda: "systemd")

    returned = cli._ensure_agent_for_write(lambda: missing)
    assert returned == missing
    assert calls["enable_now"] is True
    out = capsys.readouterr().out
    assert "installing and starting it now" in out
    assert "portal-credential-agent.socket" in out  # install output is included


def test_set_does_not_reinstall_when_socket_present(monkeypatch, tmp_path):
    present = tmp_path / "credentials.sock"
    present.write_text("x")
    called = {"install": False}

    def fake_install(**kwargs):
        called["install"] = True
        return {}

    monkeypatch.setattr("portal_mcp_server.credential_agent.install_agent",
                        fake_install)
    assert cli._ensure_agent_for_write(lambda: present) == present
    assert called["install"] is False


# ── explicit ssh-login-password → sudo cache convenience ────────────────────

def test_ssh_set_does_not_cache_sudo_by_default(monkeypatch):
    calls = []

    def fake_store(kind, key, value, ttl=0):
        calls.append((kind, key, value, ttl))
        return {"status": "ok"}

    monkeypatch.setattr(cli, "_ensure_agent_for_write", lambda _path: None)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    monkeypatch.setattr("portal_mcp_server.credential_agent.store", fake_store)
    monkeypatch.setattr(cli, "_should_cache_ssh_password_as_sudo",
                        lambda _host: False)
    monkeypatch.setattr(cli, "_validate_known_host_or_exit", lambda _a: None)

    args = SimpleNamespace(kind="ssh", key="web01", ttl=60)
    assert cli._kind_set_cli(args) == 0
    assert calls == [("ssh", "web01", "pw", 60)]


def test_ssh_set_caches_sudo_when_host_opts_in(monkeypatch, capsys):
    calls = []

    def fake_store(kind, key, value, ttl=0):
        calls.append((kind, key, value, ttl))
        return {"status": "ok"}

    monkeypatch.setattr(cli, "_ensure_agent_for_write", lambda _path: None)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    monkeypatch.setattr("portal_mcp_server.credential_agent.store", fake_store)
    monkeypatch.setattr(cli, "_should_cache_ssh_password_as_sudo",
                        lambda host: host == "web01")
    monkeypatch.setattr(cli, "_validate_known_host_or_exit", lambda _a: None)

    args = SimpleNamespace(kind="ssh", key="web01", ttl=60)
    assert cli._kind_set_cli(args) == 0
    assert calls == [
        ("ssh", "web01", "pw", 60),
        ("sudo", "web01", "pw", 60),
    ]
    assert "sudo password also cached" in capsys.readouterr().out


def test_ssh_confirm_caches_sudo_when_host_opts_in(monkeypatch):
    calls = []
    prompts = iter(["pw", "pw"])

    def fake_store(kind, key, value, ttl=0):
        calls.append((kind, key, value, ttl))
        return {"status": "ok"}

    monkeypatch.setattr(cli, "_ensure_agent_for_write", lambda _path: None)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(prompts))
    monkeypatch.setattr("portal_mcp_server.credential_agent.store", fake_store)
    monkeypatch.setattr(cli, "_should_cache_ssh_password_as_sudo",
                        lambda _host: True)
    monkeypatch.setattr(cli, "_validate_known_host_or_exit", lambda _a: None)

    args = SimpleNamespace(kind="ssh", key="web01", ttl=60)
    assert cli._kind_confirm_cli(args) == 0
    assert calls == [
        ("ssh", "web01", "pw", 60),
        ("sudo", "web01", "pw", 60),
    ]


# ── unknown-host guard on `set` / `confirm` ─────────────────────────────────

def _fake_manager(known: bool, names=("alpha", "beta")):
    return SimpleNamespace(
        knows_host=lambda _h: known,
        list_hosts=lambda: [{"name": n} for n in names],
    )


def _wire_store(monkeypatch):
    """Common stubs so a `set` that passes validation actually 'stores'."""
    calls = []

    def fake_store(kind, key, value, ttl=0):
        calls.append((kind, key, value, ttl))
        return {"status": "ok"}

    monkeypatch.setattr(cli, "_ensure_agent_for_write", lambda _path: None)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "pw")
    monkeypatch.setattr("portal_mcp_server.credential_agent.store", fake_store)
    monkeypatch.setattr(cli, "_should_cache_ssh_password_as_sudo",
                        lambda _host: False)
    return calls


def test_set_unknown_host_is_rejected(monkeypatch, capsys):
    """`portal ssh set <typo>` for a host in neither hosts.yaml nor ssh config
    aborts BEFORE prompting/storing, and the error names the likely fixes."""
    calls = _wire_store(monkeypatch)
    prompted = {"hit": False}
    monkeypatch.setattr("getpass.getpass",
                        lambda _p: prompted.__setitem__("hit", True))
    monkeypatch.setattr(cli, "get_manager",
                        lambda: _fake_manager(known=False, names=["portaltest"]))

    args = SimpleNamespace(kind="ssh", key="portaltes", ttl=60, force=False)
    with pytest.raises(SystemExit) as exc:
        cli._kind_set_cli(args)
    assert exc.value.code == 1
    assert calls == []                     # nothing cached
    assert prompted["hit"] is False        # never asked for a password
    err = capsys.readouterr().err
    assert "Unknown host 'portaltes'" in err
    assert "hosts.yaml" in err and "ssh" in err.lower()
    assert "portaltest" in err             # typo hint lists the real host


def test_set_known_host_proceeds(monkeypatch):
    """A host present in the registry / ssh config passes the guard and caches."""
    calls = _wire_store(monkeypatch)
    monkeypatch.setattr(cli, "get_manager", lambda: _fake_manager(known=True))

    args = SimpleNamespace(kind="ssh", key="alpha", ttl=60, force=False)
    assert cli._kind_set_cli(args) == 0
    assert calls == [("ssh", "alpha", "pw", 60)]


def test_set_force_bypasses_unknown_host(monkeypatch):
    """--force is the escape hatch: an unknown host still caches (and the guard
    never even consults the registry)."""
    calls = _wire_store(monkeypatch)

    def _boom():
        raise AssertionError("get_manager must not be consulted under --force")

    monkeypatch.setattr(cli, "get_manager", _boom)

    args = SimpleNamespace(kind="ssh", key="ghost", ttl=60, force=True)
    assert cli._kind_set_cli(args) == 0
    assert calls == [("ssh", "ghost", "pw", 60)]


def test_secret_set_skips_host_check(monkeypatch):
    """`secret` keys on a NAME, not a host — the guard must not reject it even
    when it isn't a known host (and no --force attr exists on its args)."""
    calls = _wire_store(monkeypatch)

    def _boom():
        raise AssertionError("secret set must not check the host registry")

    monkeypatch.setattr(cli, "get_manager", _boom)

    args = SimpleNamespace(kind="secret", key="github_token", ttl=60)
    assert cli._kind_set_cli(args) == 0
    assert calls == [("secret", "github_token", "pw", 60)]


def test_set_local_sudo_is_exempt(monkeypatch):
    """`sudo set-local` uses the reserved <local> key, which is deliberately not
    a registry host; the guard must exempt it rather than reject."""
    calls = _wire_store(monkeypatch)

    def _boom():
        raise AssertionError("<local> must be exempt without a registry lookup")

    monkeypatch.setattr(cli, "get_manager", _boom)

    args = SimpleNamespace(kind="sudo", key="<local>", ttl=60, force=False)
    assert cli._kind_set_cli(args) == 0
    assert calls == [("sudo", "<local>", "pw", 60)]


def test_confirm_unknown_host_is_rejected(monkeypatch):
    """The guard also fronts `confirm` (not just `set`)."""
    _wire_store(monkeypatch)
    monkeypatch.setattr(cli, "get_manager", lambda: _fake_manager(known=False))

    args = SimpleNamespace(kind="sudo", key="typo", ttl=60, force=False)
    with pytest.raises(SystemExit) as exc:
        cli._kind_confirm_cli(args)
    assert exc.value.code == 1


# ── multi-step under sudo: commands[] runs separately; newlines never collapsed

@pytest.mark.asyncio
async def test_commands_under_sudo_run_each_separately(monkeypatch):
    """The human-friendly multi-step path: commands=[...] + use_sudo runs each
    line as its own sudo exec, verbatim — no flattening into one arg list."""
    seen = []

    async def fake_resolve(host):
        return "pw"

    async def fake_sudo(h, cmd, password, env=None, timeout=0):
        seen.append(cmd)
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "", "stderr": ""}

    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", fake_resolve)
    monkeypatch.setattr(cli, "_re_sudo_exec", fake_sudo)
    await cli.remote_exec("web01",
                          commands=["systemctl restart caddy", "sleep 4",
                                    "echo ok"],
                          use_sudo=True, timeout=30)
    assert seen == ["systemctl restart caddy", "sleep 4", "echo ok"]


@pytest.mark.asyncio
async def test_multiline_sudo_command_newlines_preserved(monkeypatch):
    """A multi-line `command` string reaches the sudo exec with newlines intact
    (the server never collapses them to spaces); remote_sudo_exec then runs it
    as a `bash -c` script."""
    seen = []

    async def fake_resolve(host):
        return "pw"

    async def fake_sudo(h, cmd, password, env=None, timeout=0):
        seen.append(cmd)
        return {"host": h, "command": cmd, "exit_code": 0,
                "stdout": "", "stderr": ""}

    monkeypatch.setattr(sudo_creds, "resolve_sudo_password", fake_resolve)
    monkeypatch.setattr(cli, "_re_sudo_exec", fake_sudo)
    await cli.remote_exec("web01",
                          command="systemctl restart caddy\nsleep 4\necho ok",
                          use_sudo=True, timeout=30)
    assert seen[0] == "systemctl restart caddy\nsleep 4\necho ok"


# ── decision-point credential onboarding (front-loaded in tool docstrings) ───

def test_exec_tool_docstrings_frontload_secret_onboarding():
    """Both exec tools must surface the `portal secret set` cue at the TOP of
    their description (where an agent reads the tool overview), not only buried
    in the `secrets` parameter detail."""
    for doc in (cli.remote_exec.__doc__, cli.local_exec.__doc__):
        assert doc is not None
        assert "portal secret set <name>" in doc
        assert "paste" in doc.lower()
