"""Tests for the opt-in cc-safety-net integration (portal_mcp_server.safety_net).

Covers:
  * SafetyNetChecker.from_config tolerance (defaults, malformed input).
  * Disabled checker is a true no-op (never spawns a subprocess).
  * allowed / blocked verdict parsing from `explain --json`.
  * Fail-closed vs fail-open behaviour when the checker can't produce a verdict
    (binary missing, timeout, empty output, bad JSON, unknown verdict).
  * SecurityPolicy.check_command consults the checker (defense-in-depth, even
    past an allowlist), so every portal_* exec path inherits the gate.

The real ``cc-safety-net`` binary is never invoked — we monkeypatch
``asyncio.create_subprocess_exec`` and feed in a tiny fake ``Process`` so the
suite stays fast and offline. The checker itself is async (the MCP server's
event loop must not be blocked while the npx subprocess runs).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from portal_mcp_server import safety_net as sn
from portal_mcp_server.safety_net import SafetyNetChecker


# ── fake process plumbing ───────────────────────────────────────────────────

class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process`` returning a fixed result
    from ``.communicate()``. Use ``_SleepingProc`` when the test needs to
    drive the wait_for timeout branch instead.
    """

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"",
                 returncode: int = 0):
        self._stdout, self._stderr = stdout, stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr

    def terminate(self):  # pragma: no cover - never hit in the happy path
        pass

    def kill(self):  # pragma: no cover
        pass

    async def wait(self):  # pragma: no cover
        return self.returncode


class _SleepingProc:
    """Fake proc whose ``communicate`` hangs forever. Pair with a small
    ``SafetyNetChecker.timeout_s`` to exercise the timeout branch quickly.
    """

    def __init__(self):
        self.returncode = None

    async def communicate(self):
        await asyncio.sleep(60)
        return b"", b""  # unreachable in tests (wait_for fires first)

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _factory(*, proc=None, raise_on_create=None):
    """Build a replacement for ``asyncio.create_subprocess_exec``.

    ``proc`` → returned from the (awaited) call.
    ``raise_on_create`` → raised inside the await (simulates the spawn itself
    failing, e.g. FileNotFoundError when the binary is missing).
    """
    async def fake(*_a, **_k):
        if raise_on_create is not None:
            raise raise_on_create
        return proc
    return fake


def _verdict(result: str, reason: str = "") -> bytes:
    payload = {"result": result}
    if reason:
        payload["reason"] = reason
    return json.dumps(payload).encode("utf-8")


# ── from_config (synchronous: it never touches the subprocess) ──────────────

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


# ── disabled / blank → no spawn ─────────────────────────────────────────────

async def test_disabled_never_spawns(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError(
            "create_subprocess_exec must not be called when disabled")

    monkeypatch.setattr(sn.asyncio, "create_subprocess_exec", boom)
    c = SafetyNetChecker(enabled=False)
    assert await c.check("git reset --hard") is None


async def test_blank_command_is_allowed(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("must not spawn for a blank command")

    monkeypatch.setattr(sn.asyncio, "create_subprocess_exec", boom)
    c = SafetyNetChecker(enabled=True)
    assert await c.check("   ") is None


# ── verdict parsing ─────────────────────────────────────────────────────────

async def test_allowed_verdict(monkeypatch):
    monkeypatch.setattr(
        sn.asyncio, "create_subprocess_exec",
        _factory(proc=_FakeProc(stdout=_verdict("allowed"))))
    assert await SafetyNetChecker(enabled=True).check("ls -la") is None


async def test_blocked_verdict_returns_reason(monkeypatch):
    reason = "git reset --hard destroys all uncommitted changes permanently."
    monkeypatch.setattr(
        sn.asyncio, "create_subprocess_exec",
        _factory(proc=_FakeProc(stdout=_verdict("blocked", reason))))
    err = await SafetyNetChecker(enabled=True).check("git reset --hard")
    assert err is not None
    assert "Safety Net blocked" in err
    assert reason in err


async def test_passes_explain_json_argv(monkeypatch):
    seen = {}

    async def fake(*argv, **kw):
        seen["argv"] = list(argv)
        seen["cwd"] = kw.get("cwd")
        return _FakeProc(stdout=_verdict("allowed"))

    monkeypatch.setattr(sn.asyncio, "create_subprocess_exec", fake)
    c = SafetyNetChecker(enabled=True, command=["cc-safety-net"],
                         rulebook_cwd="/work")
    await c.check("uptime")
    assert seen["argv"] == ["cc-safety-net", "explain", "--json", "uptime"]
    assert seen["cwd"] == "/work"


# ── fail-closed / fail-open ─────────────────────────────────────────────────
#
# Each parameter is (factory-builder, timeout_s). The timeout case needs a
# tiny timeout_s so we actually trip wait_for instead of slow-walking the
# suite; the rest run at the default.

def _binary_missing():
    return _factory(raise_on_create=FileNotFoundError())


def _timeout_proc():
    return _factory(proc=_SleepingProc())


def _empty_output():
    return _factory(proc=_FakeProc(stdout=b""))


def _bad_json():
    return _factory(proc=_FakeProc(stdout=b"not json"))


def _unknown_verdict():
    return _factory(proc=_FakeProc(stdout=_verdict("maybe")))


_FAILURE_FACTORIES = [
    pytest.param(_binary_missing, 15.0, id="binary-missing"),
    pytest.param(_timeout_proc, 0.05, id="timeout"),
    pytest.param(_empty_output, 15.0, id="empty-output"),
    pytest.param(_bad_json, 15.0, id="bad-json"),
    pytest.param(_unknown_verdict, 15.0, id="unknown-verdict"),
]


@pytest.mark.parametrize("make_factory,timeout_s", _FAILURE_FACTORIES)
async def test_fail_closed_refuses(monkeypatch, make_factory, timeout_s):
    monkeypatch.setattr(sn.asyncio, "create_subprocess_exec", make_factory())
    err = await SafetyNetChecker(
        enabled=True, fail_closed=True, timeout_s=timeout_s).check("ls")
    assert err is not None
    assert "NOT executed" in err
    assert "fail-closed" in err


@pytest.mark.parametrize("make_factory", [
    _binary_missing, _empty_output, _bad_json,
])
async def test_fail_open_allows(monkeypatch, make_factory):
    monkeypatch.setattr(sn.asyncio, "create_subprocess_exec", make_factory())
    assert await SafetyNetChecker(
        enabled=True, fail_closed=False).check("ls") is None


# ── SecurityPolicy integration ──────────────────────────────────────────────

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


async def test_check_command_consults_safety_net(tmp_path, monkeypatch):
    pol = _policy_with_safety_net(tmp_path)
    monkeypatch.setattr(
        sn.asyncio, "create_subprocess_exec",
        _factory(proc=_FakeProc(
            stdout=_verdict("blocked", "rm -rf ~ is extremely dangerous"))))
    err = await pol.check_command("rm -rf ~")
    assert err is not None
    assert "Safety Net blocked" in err


async def test_safety_net_overrides_allowlist(tmp_path, monkeypatch):
    # Even an allowlisted command class is still blocked when semantically
    # destructive — the safety-net check runs before the allowlist verdict.
    pol = _policy_with_safety_net(tmp_path, allowlist="    - 'git *'\n")
    monkeypatch.setattr(
        sn.asyncio, "create_subprocess_exec",
        _factory(proc=_FakeProc(stdout=_verdict(
            "blocked", "git reset --hard destroys uncommitted changes"))))
    assert await pol.check_command("git reset --hard") is not None


async def test_disabled_safety_net_is_transparent(tmp_path, monkeypatch):
    from portal_mcp_server import security
    pol_yaml = tmp_path / "policies.yaml"
    pol_yaml.write_text("policies:\n  rate_limit_rps: 1000\n")
    pol = security.SecurityPolicy(policies_yaml=pol_yaml)

    def boom(*_a, **_k):
        raise AssertionError("must not spawn when safety_net is absent")

    monkeypatch.setattr(sn.asyncio, "create_subprocess_exec", boom)
    assert pol.safety_net.enabled is False
    assert await pol.check_command("git reset --hard") is None
