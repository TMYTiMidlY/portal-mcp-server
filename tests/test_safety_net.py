"""Tests for the opt-in cc-safety-net integration (portal_mcp_server.safety_net).

Covers:
  * SafetyNetChecker.from_config tolerance (defaults, malformed input).
  * Disabled checker is a true no-op (never spawns a subprocess).
  * allowed / blocked verdict parsing from `explain --json`.
  * Fail-closed vs fail-open behaviour when the checker can't produce a verdict
    (binary missing, timeout, empty output, bad JSON, unknown verdict).
  * SecurityPolicy.check_command consults the checker (defense-in-depth, even
    past an allowlist), so every portal_* exec path inherits the gate.

The real ``cc-safety-net`` binary is never invoked — ``subprocess.run`` is
monkeypatched so the suite is fast and offline.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from portal_mcp_server import safety_net as sn
from portal_mcp_server.safety_net import SafetyNetChecker


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["cc-safety-net"], returncode=returncode,
        stdout=stdout, stderr=stderr,
    )


def _verdict(result: str, reason: str = "") -> str:
    payload = {"result": result}
    if reason:
        payload["reason"] = reason
    return json.dumps(payload)


# ── from_config ─────────────────────────────────────────────────────────────

class TestFromConfig:
    def test_none_or_empty_is_disabled(self):
        assert SafetyNetChecker.from_config(None).enabled is False
        assert SafetyNetChecker.from_config({}).enabled is False

    def test_defaults_applied(self):
        c = SafetyNetChecker.from_config({"enabled": True})
        assert c.enabled is True
        assert c.command == ["npx", "-y", "cc-safety-net@latest"]
        assert c.fail_closed is True
        assert c.rulebook_cwd is None
        assert c.timeout_s == 15.0

    def test_string_command_is_wrapped(self):
        c = SafetyNetChecker.from_config({"enabled": True, "command": "cc-safety-net"})
        assert c.command == ["cc-safety-net"]

    def test_malformed_command_falls_back(self):
        c = SafetyNetChecker.from_config({"enabled": True, "command": [1, 2]})
        assert c.command == ["npx", "-y", "cc-safety-net@latest"]

    def test_bad_timeout_falls_back(self):
        assert SafetyNetChecker.from_config(
            {"enabled": True, "timeout_s": "nope"}).timeout_s == 15.0
        assert SafetyNetChecker.from_config(
            {"enabled": True, "timeout_s": -5}).timeout_s == 15.0

    def test_env_coerced_to_str(self):
        c = SafetyNetChecker.from_config(
            {"enabled": True, "env": {"CC_SAFETY_NET_PARANOID_RM": 1}})
        assert c.env == {"CC_SAFETY_NET_PARANOID_RM": "1"}


# ── disabled = no-op ─────────────────────────────────────────────────────────

def test_disabled_never_spawns(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess.run must not be called when disabled")

    monkeypatch.setattr(sn.subprocess, "run", boom)
    c = SafetyNetChecker(enabled=False)
    assert c.check("git reset --hard") is None


def test_blank_command_is_allowed(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not spawn for a blank command")

    monkeypatch.setattr(sn.subprocess, "run", boom)
    c = SafetyNetChecker(enabled=True)
    assert c.check("   ") is None


# ── verdict parsing ──────────────────────────────────────────────────────────

def test_allowed_verdict(monkeypatch):
    monkeypatch.setattr(sn.subprocess, "run",
                        lambda *a, **k: _completed(stdout=_verdict("allowed")))
    assert SafetyNetChecker(enabled=True).check("ls -la") is None


def test_blocked_verdict_returns_reason(monkeypatch):
    reason = "git reset --hard destroys all uncommitted changes permanently."
    monkeypatch.setattr(sn.subprocess, "run",
                        lambda *a, **k: _completed(stdout=_verdict("blocked", reason)))
    err = SafetyNetChecker(enabled=True).check("git reset --hard")
    assert err is not None
    assert "Safety Net blocked" in err
    assert reason in err


def test_passes_explain_json_argv(monkeypatch):
    seen = {}

    def fake_run(argv, **k):
        seen["argv"] = argv
        seen["cwd"] = k.get("cwd")
        return _completed(stdout=_verdict("allowed"))

    monkeypatch.setattr(sn.subprocess, "run", fake_run)
    c = SafetyNetChecker(enabled=True, command=["cc-safety-net"],
                         rulebook_cwd="/work")
    c.check("uptime")
    assert seen["argv"] == ["cc-safety-net", "explain", "--json", "uptime"]
    assert seen["cwd"] == "/work"


# ── fail-closed / fail-open ──────────────────────────────────────────────────

@pytest.mark.parametrize("make_run", [
    pytest.param(lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
                 id="binary-missing"),
    pytest.param(lambda *a, **k: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(cmd="cc", timeout=15)), id="timeout"),
    pytest.param(lambda *a, **k: _completed(stdout=""), id="empty-output"),
    pytest.param(lambda *a, **k: _completed(stdout="not json"), id="bad-json"),
    pytest.param(lambda *a, **k: _completed(stdout=_verdict("maybe")),
                 id="unknown-verdict"),
])
def test_fail_closed_refuses(monkeypatch, make_run):
    monkeypatch.setattr(sn.subprocess, "run", make_run)
    err = SafetyNetChecker(enabled=True, fail_closed=True).check("ls")
    assert err is not None
    assert "NOT executed" in err
    assert "fail-closed" in err


@pytest.mark.parametrize("make_run", [
    lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
    lambda *a, **k: _completed(stdout=""),
    lambda *a, **k: _completed(stdout="not json"),
])
def test_fail_open_allows(monkeypatch, make_run):
    monkeypatch.setattr(sn.subprocess, "run", make_run)
    assert SafetyNetChecker(enabled=True, fail_closed=False).check("ls") is None


# ── SecurityPolicy integration ───────────────────────────────────────────────

def _policy_with_safety_net(tmp_path, *, allowlist: str = ""):
    from portal_mcp_server import security
    body = (
        "policies:\n"
        "  rate_limit_rps: 1000\n"
        "  safety_net:\n"
        "    enabled: true\n"
        "    command: ['cc-safety-net']\n"
    )
    if allowlist:
        body += "  command_allowlist:\n" + allowlist
    pol_yaml = tmp_path / "policies.yaml"
    pol_yaml.write_text(body)
    return security.SecurityPolicy(policies_yaml=pol_yaml)


def test_policy_loads_safety_net(tmp_path):
    pol = _policy_with_safety_net(tmp_path)
    assert pol.safety_net.enabled is True
    assert pol.safety_net.command == ["cc-safety-net"]


def test_check_command_consults_safety_net(tmp_path, monkeypatch):
    pol = _policy_with_safety_net(tmp_path)
    monkeypatch.setattr(
        sn.subprocess, "run",
        lambda *a, **k: _completed(stdout=_verdict(
            "blocked", "rm -rf ~ is extremely dangerous")))
    err = pol.check_command("rm -rf ~")
    assert err is not None
    assert "Safety Net blocked" in err


def test_safety_net_overrides_allowlist(tmp_path, monkeypatch):
    # Even an allowlisted command class is still blocked when semantically
    # destructive — the safety-net check runs before the allowlist verdict.
    pol = _policy_with_safety_net(tmp_path, allowlist="    - 'git *'\n")
    monkeypatch.setattr(
        sn.subprocess, "run",
        lambda *a, **k: _completed(stdout=_verdict(
            "blocked", "git reset --hard destroys uncommitted changes")))
    assert pol.check_command("git reset --hard") is not None


def test_disabled_safety_net_is_transparent(tmp_path, monkeypatch):
    from portal_mcp_server import security
    pol_yaml = tmp_path / "policies.yaml"
    pol_yaml.write_text("policies:\n  rate_limit_rps: 1000\n")
    pol = security.SecurityPolicy(policies_yaml=pol_yaml)

    def boom(*a, **k):
        raise AssertionError("must not spawn when safety_net is absent")

    monkeypatch.setattr(sn.subprocess, "run", boom)
    assert pol.safety_net.enabled is False
    assert pol.check_command("git reset --hard") is None
