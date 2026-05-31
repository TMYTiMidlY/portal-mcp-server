"""Shared pytest configuration / fixtures for portal-mcp-server.

Goals
-----
1. Register the ``ssh`` marker so the existing live-SSH tests can be skipped
   in environments without a reachable SSH server (CI, sandbox).
2. Auto-skip the live tests in ``test_live_ssh.py`` if ``PORTAL_TEST_LIVE`` is
   not set — they require a real SSH server and credentials.
3. Reset module-level singletons between tests so independent tests stay
   independent (the previous suite mutated the global ConnectionManager
   inside its module-level import, which leaked state across tests).
"""
from __future__ import annotations

import os

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "ssh: requires a reachable SSH server "
                  "(set PORTAL_TEST_LIVE=1 to run)"
    )


def pytest_collection_modifyitems(config, items):
    """Auto-skip live SSH tests unless PORTAL_TEST_LIVE is set."""
    if os.environ.get("PORTAL_TEST_LIVE", "").lower() in ("1", "true", "yes"):
        return
    skip = pytest.mark.skip(
        reason="live SSH test — set PORTAL_TEST_LIVE=1 to run"
    )
    for item in items:
        # Anything in the test_live_ssh.py module that exercises a real SSH
        # connection — i.e. all classes EXCEPT TestSecurity which is pure
        # in-memory policy.
        path = str(item.fspath)
        if path.endswith("test_live_ssh.py"):
            cls_name = getattr(item.parent, "name", "")
            if cls_name != "TestSecurity":
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Wipe module-level singletons before each test so tests cannot bleed
    state into one another (e.g. via the global ConnectionManager).
    """
    yield
    # Post-test cleanup: clear anything we know to be a process-wide cache.
    try:
        from portal_mcp_server import remote_bash as rb
        rb._HOST_SESSIONS.clear()
        rb._HOST_LOCKS.clear()
    except Exception:
        pass
    # Clear the three credential caches (sudo / ssh-login / named-secret).
    # Each is consulted on the SSH connect path or by command execution, so a
    # leaked entry from one test can silently override another test's
    # `password_command` / value resolution. Clearing here is cheap and makes
    # test order irrelevant.
    for mod_name, clearer in (
        ("portal_mcp_server.sudo_creds", "clear_sudo_password"),
        ("portal_mcp_server.ssh_creds", "clear_ssh_password"),
        ("portal_mcp_server.secrets_store", "clear_secret"),
    ):
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            getattr(mod, clearer)()  # None -> clear all
        except Exception:
            pass
