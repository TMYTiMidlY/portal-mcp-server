"""#2 — host / credential-key identifiers are strip-normalized at boundaries so
a stray trailing space (tab-complete / copy-paste) can't split store vs fetch
(the reported ``sudo password for host 'LisaHost '`` bug).
"""
import pytest

from portal_mcp_server import connection_manager, credential_agent, security
from portal_mcp_server.safety import normalize_host_name


@pytest.mark.parametrize("raw,expect", [
    ("web01 ", "web01"), (" web01", "web01"), ("  web01  ", "web01"),
    ("web01", "web01"), ("<local>", "<local>"), ("   ", ""), ("", ""),
])
def test_normalize_host_name(raw, expect):
    assert normalize_host_name(raw) == expect


def test_normalize_host_name_non_str_passthrough():
    assert normalize_host_name(None) is None
    assert normalize_host_name(5) == 5


# ── credential agent server: the reported sudo-key mismatch ──────────────────
def test_credential_agent_key_from_msg_strips():
    ag = credential_agent.CredentialAgent()
    assert ag._key_from_msg("sudo", {"host": "LisaHost "}) == "LisaHost"
    assert ag._key_from_msg("secret", {"name": "  tok  "}) == "tok"
    assert ag._key_from_msg("sudo", {"host": "   "}) is None
    assert ag._key_from_msg("sudo", {}) is None


@pytest.mark.asyncio
async def test_credential_agent_store_spaced_fetch_clean():
    """store 'LisaHost ' then get 'LisaHost' must hit the SAME cache entry."""
    ag = credential_agent.CredentialAgent()
    r = await ag._set("sudo", {"host": "LisaHost ", "password": "sekret", "ttl": 60})
    assert r["status"] == "ok"
    g = await ag._get("sudo", {"host": "LisaHost"})
    assert g["status"] == "ok" and g["password"] == "sekret"
    # and the reverse direction
    r2 = await ag._set("sudo", {"host": "Web", "password": "pw2", "ttl": 60})
    assert r2["status"] == "ok"
    g2 = await ag._get("sudo", {"host": " Web "})
    assert g2["status"] == "ok" and g2["password"] == "pw2"


# ── security allowlist tolerates surrounding whitespace ──────────────────────
def test_check_host_normalizes(tmp_path):
    pol = security.SecurityPolicy(policies_yaml=tmp_path / "p.yaml")
    pol.host_allowlist = ["web01"]
    assert pol.check_host("web01 ") is None       # trailing space still allowed
    assert pol.check_host("  web01") is None
    assert pol.check_host("web02") is not None     # genuinely not allowed


# ── registry keys normalized on register ─────────────────────────────────────
def test_register_host_key_normalized(monkeypatch):
    monkeypatch.setenv("PORTAL_SSH_CONFIG", "none")  # don't read real ~/.ssh/config
    mgr = connection_manager.ConnectionManager()
    mgr.register_host(name="Spacey ", host="10.0.0.1")
    assert "Spacey" in mgr._registry
    assert "Spacey " not in mgr._registry
    # lookups by the clean name resolve the same entry
    assert mgr.login_shell_for("Spacey") is None  # not set, but no KeyError
