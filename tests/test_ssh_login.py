"""SSH login password provisioning — out-of-band sources, never via the LLM.

Mirrors :mod:`tests.test_sudo_auth` for the SSH-login password (the value
asyncssh feeds during connection setup), and covers the new key→password
fallback path in ``ConnectionManager.get_connection``: a key-auth-only host
that hits ``asyncssh.PermissionDenied`` retries once with a side-channel
password (cache from ``portal ssh set`` or a ``password_command``), and is
unaffected when no such password is configured.
"""
from __future__ import annotations

import inspect

import asyncssh
import pytest


# ────────────────────────────────────────────────────────────────────────────
#  LLM-facing safety invariants
# ────────────────────────────────────────────────────────────────────────────

def test_no_module_function_returns_or_logs_passwords():
    """The ssh_creds module's public API must not have any helper that takes
    a password and writes it somewhere observable. Smoke-check by name: a
    function called ``log_*`` or ``print_*`` taking a password kwarg would
    be a leak surface."""
    from portal_mcp_server import ssh_creds
    leaky = [
        name for name, fn in vars(ssh_creds).items()
        if callable(fn)
        and name.lower().startswith(("log_", "print_", "dump_"))
        and any("password" in p.lower() for p in
                inspect.signature(fn).parameters)
    ]
    assert leaky == [], (
        f"ssh_creds exposes potentially leaky helpers: {leaky}"
    )


def test_cli_set_verb_uses_getpass_and_has_no_password_arg():
    """`portal ssh set <host>` (and the matching sudo/secret verbs) must
    read the value via ``getpass.getpass`` on stdin — never as a positional
    or option, since process argv lands in shell history and ``ps``. The
    shared verb implementation is :func:`cli._kind_set_cli`; pin both
    invariants on its source."""
    from portal_mcp_server import cli
    assert hasattr(cli, "_kind_set_cli")
    src = inspect.getsource(cli._kind_set_cli)
    assert "getpass.getpass" in src
    # The shared verb factory must not register an inline password argument.
    factory_src = inspect.getsource(cli._build_kind_subparser)
    assert 'add_argument("password"' not in factory_src
    assert "add_argument('password'" not in factory_src


# ────────────────────────────────────────────────────────────────────────────
#  In-memory TTL cache
# ────────────────────────────────────────────────────────────────────────────

def test_cache_set_get_clear():
    from portal_mcp_server import ssh_creds
    ssh_creds.clear_ssh_password()
    ssh_creds.cache_ssh_password("web01", "s3cret", ttl=60)
    assert ssh_creds._get_cached("web01") == "s3cret"
    assert ssh_creds.get_cached_password("web01") == "s3cret"
    ssh_creds.clear_ssh_password("web01")
    assert ssh_creds._get_cached("web01") is None


def test_cache_ttl_expiry():
    from portal_mcp_server import ssh_creds
    ssh_creds.clear_ssh_password()
    ssh_creds.cache_ssh_password("web01", "s3cret", ttl=-1)  # already expired
    assert ssh_creds._get_cached("web01") is None
    assert ssh_creds.get_cached_password("web01") is None


def test_clear_all():
    from portal_mcp_server import ssh_creds
    ssh_creds.cache_ssh_password("a", "x", ttl=60)
    ssh_creds.cache_ssh_password("b", "y", ttl=60)
    ssh_creds.clear_ssh_password()
    assert ssh_creds._get_cached("a") is None
    assert ssh_creds._get_cached("b") is None


def test_caches_are_independent_of_sudo_and_secrets():
    """The three side-channels (`portal ssh set`, `portal sudo set`,
    `portal secret set`) must keep independent caches so clearing one
    cannot accidentally drop another, and so a value pushed under the
    same key on one channel is not visible on another. Pin that the three
    modules each hold their own dict."""
    from portal_mcp_server import ssh_creds, sudo_creds, secrets_store
    ssh_creds.clear_ssh_password()
    sudo_creds.clear_sudo_password()
    secrets_store.clear_secret()

    ssh_creds.cache_ssh_password("web01", "ssh-value", ttl=60)
    sudo_creds.cache_sudo_password("web01", "sudo-value", ttl=60)
    secrets_store.cache_secret("web01", "secret-value", ttl=60)

    assert ssh_creds._get_cached("web01") == "ssh-value"
    assert sudo_creds._get_cached("web01") == "sudo-value"
    assert secrets_store._get_cached("web01") == "secret-value"

    ssh_creds.clear_ssh_password()
    assert ssh_creds._get_cached("web01") is None
    # The other two are untouched.
    assert sudo_creds._get_cached("web01") == "sudo-value"
    assert secrets_store._get_cached("web01") == "secret-value"

    sudo_creds.clear_sudo_password()
    secrets_store.clear_secret()


# ────────────────────────────────────────────────────────────────────────────
#  ConnectionManager._resolve_ssh_password — cache first, then command
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_prefers_cache_over_command(tmp_path):
    """When both the `portal ssh set` cache and a `password_command` are
    available the cache value wins — explicit out-of-band push is the
    operator's override of whatever the password manager has stored."""
    from portal_mcp_server import ssh_creds
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    ssh_creds.clear_ssh_password()
    ssh_creds.cache_ssh_password("web01", "from-cache", ttl=60)
    try:
        cfg = HostConfig(
            name="web01", host="1.2.3.4",
            password_command="printf '%s' 'from-command'",
        )
        pw = await m._resolve_ssh_password(cfg)
        assert pw == "from-cache"
    finally:
        ssh_creds.clear_ssh_password()


@pytest.mark.asyncio
async def test_resolve_falls_back_to_command_when_cache_empty(tmp_path):
    from portal_mcp_server import ssh_creds
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    ssh_creds.clear_ssh_password()
    cfg = HostConfig(
        name="web01", host="1.2.3.4",
        password_command="printf '%s' 'from-command'",
    )
    pw = await m._resolve_ssh_password(cfg)
    assert pw == "from-command"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_no_source(tmp_path):
    from portal_mcp_server import ssh_creds
    from portal_mcp_server.connection_manager import ConnectionManager, HostConfig

    yml = tmp_path / "hosts.yaml"
    yml.write_text("hosts: {}\n")
    m = ConnectionManager(hosts_yaml=yml)

    ssh_creds.clear_ssh_password()
    cfg = HostConfig(name="web01", host="1.2.3.4")
    assert await m._resolve_ssh_password(cfg) is None


# ────────────────────────────────────────────────────────────────────────────
#  Key auth → password fallback inside ConnectionManager.get_connection
# ────────────────────────────────────────────────────────────────────────────

class _FakeConn:
    """Minimal asyncssh.SSHClientConnection stand-in for pool bookkeeping."""

    def __init__(self):
        self._closed = False

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True

    async def wait_closed(self):
        return None


@pytest.mark.asyncio
async def test_key_auth_falls_back_to_ssh_set_cache(tmp_path, monkeypatch):
    """Key-mode host: asyncssh raises PermissionDenied (key refused).
    The manager retries once with a side-channel password from the
    `portal ssh set` cache; that retry succeeds and the connection is pooled.
    The second connect call must omit client_keys and carry the cached
    password."""
    from portal_mcp_server import ssh_creds
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts:\n"
        "  keyhost:\n"
        "    host: 1.2.3.4\n"
        "    user: deploy\n"
    )
    m = ConnectionManager(hosts_yaml=yml)

    ssh_creds.clear_ssh_password()
    ssh_creds.cache_ssh_password("keyhost", "fallback-secret", ttl=60)

    call_log: list[dict] = []
    fake = _FakeConn()

    async def fake_connect(**kwargs):
        call_log.append(kwargs)
        if len(call_log) == 1:
            raise asyncssh.PermissionDenied(reason="permission denied")
        return fake

    monkeypatch.setattr("asyncssh.connect", fake_connect)
    try:
        conn = await m.get_connection("keyhost")
        assert conn is fake
        assert len(call_log) == 2, "expected exactly one retry on PermissionDenied"

        # First attempt: key-mode kwargs (no password, no forced empty client_keys).
        assert "password" not in call_log[0]

        # Retry attempt: side-channel password, client_keys forced empty so a
        # later default-key probe cannot silently mask a bad password.
        assert call_log[1]["password"] == "fallback-secret"
        assert call_log[1]["client_keys"] == []
        assert "passphrase" not in call_log[1]
    finally:
        ssh_creds.clear_ssh_password()


@pytest.mark.asyncio
async def test_key_auth_falls_back_to_password_command(tmp_path, monkeypatch):
    """Same fallback path, sourced from `password_command` instead of cache."""
    from portal_mcp_server import ssh_creds
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts:\n"
        "  keyhost:\n"
        "    host: 1.2.3.4\n"
        "    user: deploy\n"
        "    password_command: \"printf '%s' from-command\"\n"
    )
    m = ConnectionManager(hosts_yaml=yml)

    ssh_creds.clear_ssh_password()

    call_log: list[dict] = []
    fake = _FakeConn()

    async def fake_connect(**kwargs):
        call_log.append(kwargs)
        if len(call_log) == 1:
            raise asyncssh.PermissionDenied(reason="permission denied")
        return fake

    monkeypatch.setattr("asyncssh.connect", fake_connect)
    conn = await m.get_connection("keyhost")
    assert conn is fake
    assert call_log[1]["password"] == "from-command"
    assert call_log[1]["client_keys"] == []


@pytest.mark.asyncio
async def test_key_auth_without_password_source_propagates_error(
    tmp_path, monkeypatch,
):
    """Pure key host with no `portal ssh set` cache entry and no password_command:
    the manager must NOT retry — the original PermissionDenied propagates
    so the operator gets the real reason (wrong key, agent not running,
    etc.) instead of a misleading "no password configured" rewrite."""
    from portal_mcp_server import ssh_creds
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts:\n"
        "  keyhost:\n"
        "    host: 1.2.3.4\n"
        "    user: deploy\n"
    )
    m = ConnectionManager(hosts_yaml=yml)
    ssh_creds.clear_ssh_password()

    call_log: list[dict] = []

    async def fake_connect(**kwargs):
        call_log.append(kwargs)
        raise asyncssh.PermissionDenied(reason="permission denied")

    monkeypatch.setattr("asyncssh.connect", fake_connect)
    with pytest.raises(asyncssh.PermissionDenied):
        await m.get_connection("keyhost")
    assert len(call_log) == 1, (
        "must NOT retry when no side-channel password is available"
    )


@pytest.mark.asyncio
async def test_key_auth_fallback_drops_passphrase_kwarg(tmp_path, monkeypatch):
    """When the manager retries a key-host with the side-channel password
    after asyncssh raises PermissionDenied, the per-key passphrase
    (resolved from `passphrase_command`) must NOT be carried into the
    retry kwargs — asyncssh would otherwise try to apply it to the new
    password-mode connection and fail in confusing ways."""
    from portal_mcp_server import ssh_creds
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts:\n"
        "  encryptedkeyhost:\n"
        "    host: 1.2.3.4\n"
        "    user: deploy\n"
        "    passphrase_command: \"printf '%s' key-unlock\"\n"
        "    password_command: \"printf '%s' login-pw\"\n"
    )
    m = ConnectionManager(hosts_yaml=yml)

    ssh_creds.clear_ssh_password()

    call_log: list[dict] = []
    fake = _FakeConn()

    async def fake_connect(**kwargs):
        call_log.append(kwargs)
        if len(call_log) == 1:
            raise asyncssh.PermissionDenied(reason="permission denied")
        return fake

    monkeypatch.setattr("asyncssh.connect", fake_connect)
    conn = await m.get_connection("encryptedkeyhost")
    assert conn is fake
    assert len(call_log) == 2

    # First attempt is key-mode and DOES carry the passphrase (asyncssh
    # uses it to unlock the encrypted private key). This proves the test
    # is actually exercising the pop() path on retry, not a no-op.
    assert call_log[0].get("passphrase") == "key-unlock"

    # Retry attempt: the SSH *login* password (from password_command) is
    # supplied, client_keys forced empty, and the key passphrase MUST be gone
    # — otherwise asyncssh would treat it as a password-mode auth with a stray
    # key-passphrase, which is meaningless and version-dependent in its error.
    assert call_log[1]["password"] == "login-pw"
    assert call_log[1]["client_keys"] == []
    assert "passphrase" not in call_log[1]


@pytest.mark.asyncio
async def test_auth_password_does_not_double_try_on_failure(
    tmp_path, monkeypatch,
):
    """`auth: password` hosts already exhausted the password chain inside
    _build_connect_kwargs. A subsequent PermissionDenied must propagate
    without a second connect — otherwise the manager could connect with
    `client_keys=[]` for a host that was meant to be password-only."""
    from portal_mcp_server import ssh_creds
    from portal_mcp_server.connection_manager import ConnectionManager

    yml = tmp_path / "hosts.yaml"
    yml.write_text(
        "hosts:\n"
        "  pwhost:\n"
        "    host: 1.2.3.4\n"
        "    user: deploy\n"
        "    auth: password\n"
        "    password_command: \"printf '%s' wrong-pw\"\n"
    )
    m = ConnectionManager(hosts_yaml=yml)
    ssh_creds.clear_ssh_password()

    call_log: list[dict] = []

    async def fake_connect(**kwargs):
        call_log.append(kwargs)
        raise asyncssh.PermissionDenied(reason="permission denied")

    monkeypatch.setattr("asyncssh.connect", fake_connect)
    with pytest.raises(asyncssh.PermissionDenied):
        await m.get_connection("pwhost")
    assert len(call_log) == 1, (
        "auth=password host must not retry after PermissionDenied"
    )


# ────────────────────────────────────────────────────────────────────────────
#  Live agent round trip: `portal ssh set` client → per-user agent cache
# ────────────────────────────────────────────────────────────────────────────

def test_control_socket_roundtrip(agent_socket):
    from portal_mcp_server import ssh_creds

    ssh_creds.clear_ssh_password()

    assert ssh_creds.control_socket_path() == agent_socket
    assert oct(agent_socket.stat().st_mode & 0o777) == oct(0o600)

    resp = ssh_creds.send_ssh_password("web01", "live-secret", ttl=60)
    assert resp.get("status") == "ok", resp
    assert ssh_creds.fetch_ssh_password_from_agent("web01") == "live-secret"


def test_live_credentials_share_one_agent_socket(agent_socket):
    """The three side-channels share the per-user systemd agent socket."""
    from portal_mcp_server import ssh_creds, sudo_creds, secrets_store

    paths = {
        ssh_creds.control_socket_path(),
        sudo_creds.control_socket_path(),
        secrets_store.control_secrets_socket_path(),
    }
    assert paths == {agent_socket}


# ────────────────────────────────────────────────────────────────────────────
#  Peer-credential (same-uid) check on the control socket
# ────────────────────────────────────────────────────────────────────────────

def test_send_refuses_when_peer_uid_does_not_match(agent_socket, monkeypatch):
    """Defence-in-depth: even if a hostile local user managed to land a
    listener at our expected socket path (e.g. on a system where the
    /tmp fallback dir was pre-created with weaker permissions), the
    client side of `portal ssh set` must NOT hand them the password.
    Verified by stubbing :func:`is_same_uid_peer` to ``False`` on the
    client side while the server (on the loopback path) accepts."""
    from portal_mcp_server import ssh_creds

    ssh_creds.clear_ssh_password()
    assert agent_socket.exists()

    # Simulate the peer-uid check failing on the client side: pretend the
    # socket we just connected to belongs to someone else's uid.
    monkeypatch.setattr(
        "portal_mcp_server.credential_agent.is_same_uid_peer",
        lambda _sock: False,
    )

    with pytest.raises(RuntimeError, match="peer uid"):
        ssh_creds.send_ssh_password("web01", "should-never-be-sent")

    monkeypatch.setattr(
        "portal_mcp_server.credential_agent.is_same_uid_peer",
        lambda _sock: True,
    )
    assert ssh_creds.fetch_ssh_password_from_agent("web01") is None
