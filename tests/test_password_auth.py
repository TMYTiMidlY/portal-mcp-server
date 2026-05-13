"""Password authentication: opt-in via hosts.yaml `password_command:`,
never via MCP tool parameters or plaintext yaml fields.

Scope:
  * Pin the LLM-facing safety invariants (the original audit finding):
    - HostConfig has no `password` field
    - ConnectionManager.register_host has no `password` kwarg
    - portal_host MCP tool has no `password` parameter
    - hosts.yaml plaintext `password:` is rejected with an ERROR
    - _build_connect_kwargs never injects a password from a HostConfig
      attribute or a yaml field that did not go through password_command
  * Cover the password_command / passphrase_command happy paths and the
    failure modes (missing command, non-zero exit, timeout, empty output).

The historical name was test_no_password_auth.py; renamed to reflect that
password auth is now supported through a controlled side-channel.
"""
from __future__ import annotations

import inspect
import logging
import textwrap

import pytest


# ════════════════════════════════════════════════════════════════════════════
#  LLM-facing safety invariants — these MUST hold regardless of which
#  password-source side-channel we add. They are the whole point of routing
#  passwords through hosts.yaml + an external command instead of a tool arg.
# ════════════════════════════════════════════════════════════════════════════

def test_hostconfig_has_no_password_field():
    """The dataclass must never grow a `password` attribute. The secret
    lives only inside the `kwargs["password"]` dict that flows directly
    into asyncssh.connect; it is never persisted on HostConfig."""
    from portal_mcp_server.connection_manager import HostConfig
    cfg = HostConfig(name="x", host="1.2.3.4")
    assert not hasattr(cfg, "password"), (
        "HostConfig must not expose a password field — passwords are "
        "fetched on-demand from password_command, never persisted."
    )


def test_connection_manager_register_host_signature_has_no_password():
    from portal_mcp_server.connection_manager import ConnectionManager
    sig = inspect.signature(ConnectionManager.register_host)
    assert "password" not in sig.parameters, (
        "ConnectionManager.register_host must not accept a password kwarg."
    )
    assert "password_command" not in sig.parameters, (
        "register_host is the in-memory dynamic registration path; "
        "password_command must only come from hosts.yaml so the secret is "
        "not handed to a caller (which in production is the MCP tool layer)."
    )


def test_portal_host_register_signature_has_no_password():
    from portal_mcp_server import cli
    sig = inspect.signature(cli.portal_host)
    assert "password" not in sig.parameters, (
        "portal_host MCP tool must not expose a password parameter "
        "(would let LLMs leak credentials into prompt logs)."
    )
    assert "password_command" not in sig.parameters, (
        "portal_host MCP tool must not expose a password_command parameter "
        "either — the command string is itself sensitive (it can name a "
        "secret store entry) and would land in tool-call traces."
    )


def test_hosts_yaml_plaintext_password_field_is_logged_and_ignored(
    tmp_path, caplog,
):
    """Plaintext `password:` in hosts.yaml is the upstream pattern we
    rejected; it leaks credentials into config files, backups and logs.
    The host still loads (so an operator's startup is not bricked), but
    the password value is dropped on the floor and an ERROR fires."""
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(textwrap.dedent("""\
        hosts:
          legacy:
            host: 10.0.0.1
            user: deploy
            password: super-secret
    """))

    with caplog.at_level(logging.ERROR, logger="portal_mcp.connections"):
        m = ConnectionManager(hosts_yaml=yml)

    cfg = m._registry["legacy"]
    assert not hasattr(cfg, "password")
    for value in cfg.__dict__.values():
        assert value != "super-secret"
    assert any(
        "password" in rec.message and "legacy" in rec.message
        for rec in caplog.records
    ), "expected an ERROR log naming the offending host"


@pytest.mark.asyncio
async def test_build_connect_kwargs_no_password_when_no_password_command(
    tmp_path,
):
    """Default (key-based) HostConfig must never produce a password kwarg
    for asyncssh.connect — defensive against accidental regressions in
    the build_connect_kwargs branching."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(name="x", host="1.2.3.4", key="/tmp/no-such-key")
    kwargs = await m._build_connect_kwargs(cfg)
    assert "password" not in kwargs

    cfg2 = HostConfig(name="y", host="1.2.3.4")
    kwargs2 = await m._build_connect_kwargs(cfg2)
    assert "password" not in kwargs2


# ════════════════════════════════════════════════════════════════════════════
#  password_command — the supported password-auth side-channel
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_password_command_happy_path(tmp_path):
    """`auth: password` + `password_command:` runs the command and feeds
    its stdout to asyncssh as the password. client_keys is forced empty
    so a misconfigured command cannot silently fall through to key auth."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="web01", host="10.0.0.10", user="deploy",
        auth="password",
        password_command="printf '%s' 'hunter2'",
    )
    kwargs = await m._build_connect_kwargs(cfg)
    assert kwargs["password"] == "hunter2"
    assert kwargs["client_keys"] == []
    assert kwargs["username"] == "deploy"


@pytest.mark.asyncio
async def test_password_command_strips_single_trailing_newline(tmp_path):
    """`pass show`, `echo`, `cat secret-file` etc. almost always emit a
    trailing newline. Strip exactly one so passwords containing internal
    or trailing whitespace still survive."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="x", host="1.2.3.4",
        auth="password",
        password_command="echo 'hunter2'",
    )
    kwargs = await m._build_connect_kwargs(cfg)
    assert kwargs["password"] == "hunter2"


@pytest.mark.asyncio
async def test_password_command_reads_environment(tmp_path, monkeypatch):
    """The CI-friendly pattern: GitHub Secret → env var → password_command
    `printf %s "$VAR"`. Confirm subprocess inherits parent env."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    monkeypatch.setenv("WEB01_PASSWORD", "from-env-var")
    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="web01", host="1.2.3.4",
        auth="password",
        password_command='printf "%s" "$WEB01_PASSWORD"',
    )
    kwargs = await m._build_connect_kwargs(cfg)
    assert kwargs["password"] == "from-env-var"


@pytest.mark.asyncio
async def test_password_command_failure_does_not_leak_stderr(tmp_path):
    """If the password command exits non-zero, the connection attempt must
    abort with a clear error that includes the host and exit code but
    NEVER the stderr (which often contains the secret on misconfigured
    commands like `printf '%s' actual-password >&2`)."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="web01", host="1.2.3.4",
        auth="password",
        password_command="echo leaked-secret >&2; exit 7",
    )
    with pytest.raises(RuntimeError) as exc:
        await m._build_connect_kwargs(cfg)
    msg = str(exc.value)
    assert "web01" in msg
    assert "7" in msg
    assert "leaked-secret" not in msg


@pytest.mark.asyncio
async def test_password_command_empty_output_rejected(tmp_path):
    """Empty stdout almost certainly means the user's password store
    misfired (entry not found, agent locked). Refuse to attempt auth
    with an empty password — asyncssh would do something undefined."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="x", host="1.2.3.4",
        auth="password",
        password_command="true",  # exit 0, no output
    )
    with pytest.raises(RuntimeError, match="empty output"):
        await m._build_connect_kwargs(cfg)


@pytest.mark.asyncio
async def test_password_command_timeout_is_enforced(tmp_path, monkeypatch):
    """A hanging password_command (locked GPG agent, network-mounted
    secret store, etc.) must NOT wedge the connection pool. Pin that the
    SECRET_COMMAND_TIMEOUT_SEC ceiling fires and the resulting error
    names the host without leaking the command string."""
    from portal_mcp_server import connection_manager as cm
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    # Shrink the ceiling so the test stays fast. The constant is read inside
    # _run_secret_command's worker, so a module-level monkeypatch is enough.
    monkeypatch.setattr(cm, "SECRET_COMMAND_TIMEOUT_SEC", 0.2)

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="slow-host", host="1.2.3.4",
        auth="password",
        password_command="sleep 5",
    )
    with pytest.raises(RuntimeError, match="timed out") as exc:
        await m._build_connect_kwargs(cfg)
    msg = str(exc.value)
    assert "slow-host" in msg
    assert "sleep" not in msg  # don't leak the command string


@pytest.mark.asyncio
async def test_password_command_non_utf8_output_is_rejected(tmp_path):
    """If the command accidentally prints binary (e.g. a private-key file
    dumped by mistake), the decoder must NOT include the offending bytes
    in the error — they may contain the secret. Surface a clean,
    actionable message instead."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="web01", host="1.2.3.4",
        auth="password",
        # Emit raw bytes 0xff 0xfe 0xff — \xff is invalid as the start of
        # a UTF-8 sequence. Shell `printf '\xff'` is not POSIX, so go via
        # python's stdout.buffer to keep the test portable.
        password_command=(
            r"""python3 -c 'import sys; sys.stdout.buffer.write(b"\xff\xfe\xff")'"""
        ),
    )
    with pytest.raises(RuntimeError, match="non-UTF-8 output") as exc:
        await m._build_connect_kwargs(cfg)
    msg = str(exc.value)
    assert "web01" in msg
    assert "\xff" not in msg
    assert "\\xff" not in msg


@pytest.mark.asyncio
async def test_auth_password_without_command_is_refused(tmp_path):
    """Configuration error guard: `auth: password` without
    `password_command` must refuse rather than silently fall through to
    key auth (which would mask the misconfiguration)."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="web01", host="1.2.3.4",
        auth="password",
        password_command=None,
    )
    with pytest.raises(RuntimeError, match="no 'password_command'"):
        await m._build_connect_kwargs(cfg)


def test_hosts_yaml_auth_password_without_command_logs_error(tmp_path, caplog):
    """The same misconfiguration is also surfaced at registry-load time so
    operators see it in startup logs, not only on first connection."""
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(textwrap.dedent("""\
        hosts:
          web01:
            host: 10.0.0.10
            user: deploy
            auth: password
    """))

    with caplog.at_level(logging.ERROR, logger="portal_mcp.connections"):
        ConnectionManager(hosts_yaml=yml)

    assert any(
        "web01" in rec.message and "password_command" in rec.message
        for rec in caplog.records
    )


def test_hosts_yaml_password_command_loads_into_hostconfig(tmp_path):
    """Smoke test for the yaml → HostConfig wiring."""
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(textwrap.dedent("""\
        hosts:
          web01:
            host: 10.0.0.10
            user: deploy
            auth: password
            password_command: pass show ssh/web01
    """))

    m = ConnectionManager(hosts_yaml=yml)
    cfg = m._registry["web01"]
    assert cfg.auth == "password"
    assert cfg.password_command == "pass show ssh/web01"


# ════════════════════════════════════════════════════════════════════════════
#  passphrase_command — same mechanism, applied to encrypted private keys
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_passphrase_command_feeds_asyncssh_passphrase(tmp_path):
    """Encrypted private keys: passphrase_command output flows into
    asyncssh as `passphrase=`. Recommended UX is still ssh-agent (no
    passphrase_command needed); this is the headless / CI fallback."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="x", host="1.2.3.4",
        key="/tmp/no-such-key",
        passphrase_command="printf '%s' 'key-secret'",
    )
    kwargs = await m._build_connect_kwargs(cfg)
    assert kwargs["passphrase"] == "key-secret"
    assert "password" not in kwargs


@pytest.mark.asyncio
async def test_no_passphrase_kwarg_when_no_passphrase_command(tmp_path):
    """Without passphrase_command, asyncssh must be allowed to fall back
    to ssh-agent. This pins that we no longer hard-code
    ``kwargs['passphrase'] = None`` (which used to actively block the
    agent path for encrypted keys)."""
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(name="x", host="1.2.3.4", key="/tmp/no-such-key")
    kwargs = await m._build_connect_kwargs(cfg)
    assert "passphrase" not in kwargs


@pytest.mark.asyncio
async def test_passphrase_command_timeout_is_enforced(tmp_path, monkeypatch):
    """Same timeout discipline as password_command: a hanging
    passphrase_command must abort with a clear, host-named error rather
    than wedging the connection pool."""
    from portal_mcp_server import connection_manager as cm
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    monkeypatch.setattr(cm, "SECRET_COMMAND_TIMEOUT_SEC", 0.2)

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    cfg = HostConfig(
        name="enc-host", host="1.2.3.4",
        key="/tmp/no-such-key",
        passphrase_command="sleep 5",
    )
    with pytest.raises(RuntimeError, match="timed out") as exc:
        await m._build_connect_kwargs(cfg)
    assert "enc-host" in str(exc.value)
