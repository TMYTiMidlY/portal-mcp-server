"""Live end-to-end smoke test for the security/audit hardening changes.

Targets host alias '1810' (or whatever TEST_HOST/PORT/USER point to).

Verifies:
  1. ssh_exec basic round-trip still works (no regression).
  2. Multi-host policy gate actually fires against a real registered host.
  3. ssh_session_exec is gated per-command (was a bypass before).
  4. remote_bash + remote_patch round-trip on /tmp succeeds AND emits audit.
  5. hosts.yaml containing 'password:' is loaded without crash and password
     value never reaches HostConfig (one more belt-and-braces against regression).
  6. The new audit entries appear in audit.jsonl with the new operation tags.

Run:
  SSH_MCP_AUDIT_FAIL_OPEN=1 uv run --with-editable . --with pytest \
    --with pytest-asyncio python tests/live_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEST_HOST = os.environ.get("TEST_HOST", "10.144.18.10")
TEST_PORT = int(os.environ.get("TEST_PORT", "2222"))
TEST_USER = os.environ.get("TEST_USER", "timidly")
TEST_KEY = os.environ.get("TEST_KEY_PATH", os.path.expanduser("~/.ssh/id_ed25519"))


def sect(title):
    print(f"\n{'═' * 70}\n  {title}\n{'═' * 70}")


async def main() -> int:
    failures: list[str] = []

    # ── (1) hosts.yaml password-field handling ────────────────────────────
    sect("1. hosts.yaml with 'password:' is loaded, value is dropped, ERROR logged")
    import logging
    log_buffer: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, rec):
            log_buffer.append(rec)

    cap = _Capture(level=logging.ERROR)
    logging.getLogger("ssh_mcp.connections").addHandler(cap)

    with tempfile.TemporaryDirectory() as td:
        yml = Path(td) / "hosts.yaml"
        yml.write_text(
            "hosts:\n"
            "  legacy:\n"
            "    host: 10.0.0.1\n"
            "    user: deploy\n"
            "    password: super-secret\n"
        )
        from ssh_remote_mcp.connection_manager import ConnectionManager
        m = ConnectionManager(hosts_yaml=yml)
        cfg = m._registry["legacy"]
        if hasattr(cfg, "password"):
            failures.append("HostConfig.password field still present!")
        if "super-secret" in str(cfg.__dict__):
            failures.append("password value leaked into HostConfig!")
        if not any("legacy" in r.message and "password" in r.message
                   for r in log_buffer):
            failures.append("expected ERROR log mentioning legacy/password")
        else:
            print("  ✓ hosts.yaml password field ignored, ERROR logged, "
                  f"value not in HostConfig (cfg keys: {list(cfg.__dict__)})")

    # ── (2) Real ssh_exec round-trip against 1810 ─────────────────────────
    sect(f"2. ssh_exec round-trip against {TEST_USER}@{TEST_HOST}:{TEST_PORT}")
    from ssh_remote_mcp.connection_manager import get_manager
    mgr = get_manager()
    # Use a deterministic alias so subsequent steps can find it.
    mgr.register_host(
        name="live-1810",
        host=TEST_HOST,
        port=TEST_PORT,
        user=TEST_USER,
        key=TEST_KEY if os.path.exists(TEST_KEY) else None,
        tags=["smoke-fleet"],
    )
    from ssh_remote_mcp.shell_engine import ssh_exec
    res = await ssh_exec("live-1810", "echo hello-from-smoke && hostname")
    if res.get("exit_code") != 0:
        failures.append(f"ssh_exec failed: {res}")
    elif "hello-from-smoke" not in res.get("stdout", ""):
        failures.append(f"unexpected stdout: {res}")
    else:
        print(f"  ✓ exit_code=0, stdout={res['stdout'].strip()!r}")

    # ── (3) Policy gate on multi-host orchestration ────────────────────────
    sect("3. Multi-host policy gate (rejects blocked command + disallowed host)")
    # Install a restrictive policy programmatically.
    from ssh_remote_mcp import security, cli
    with tempfile.TemporaryDirectory() as td:
        pol_yml = Path(td) / "policies.yaml"
        pol_yml.write_text(
            "policies:\n"
            "  host_allowlist:\n"
            "    - 'live-*'\n"
            "  command_blocklist:\n"
            "    - 'rm -rf*'\n"
            "  rate_limit_rps: 1000\n"
        )
        pol = security.SecurityPolicy(policies_yaml=pol_yml)
        security._policy = pol  # rebind module-level singleton

        # 3a. blocked command on real host → BLOCKED, no exec
        out = await cli.ssh_group_exec("smoke-fleet", "rm -rf /tmp/x", timeout=5)
        if "BLOCKED" not in out:
            failures.append(f"ssh_group_exec did NOT block 'rm -rf': {out}")
        else:
            print(f"  ✓ ssh_group_exec blocked rm -rf  →  {out[:80]}")

        # 3b. allowed command on real host → runs
        out = await cli.ssh_group_exec("smoke-fleet", "uptime", timeout=5)
        try:
            arr = json.loads(out)
            if not arr or arr[0].get("exit_code") != 0:
                failures.append(f"ssh_group_exec uptime did not succeed: {out}")
            else:
                print(f"  ✓ ssh_group_exec uptime OK on live-1810")
        except json.JSONDecodeError:
            failures.append(f"ssh_group_exec output not JSON: {out}")

        # 3c. disallowed host alias → blocked
        mgr.register_host(name="bad-host", host="127.0.0.1",
                          tags=["smoke-fleet"])
        out = await cli.ssh_group_exec("smoke-fleet", "uptime", timeout=5)
        if "BLOCKED" not in out or "bad-host" not in out:
            failures.append(
                f"ssh_group_exec should block bad-host (not in allowlist): {out}"
            )
        else:
            print(f"  ✓ ssh_group_exec blocked unallowed bad-host  →  {out[:80]}")
        mgr.remove_host("bad-host")

        # 3d. session_exec gate
        from ssh_remote_mcp.session_manager import get_session_manager
        sm = get_session_manager()
        sid = await sm.create_session("live-1810")
        out = await cli.ssh_session_exec(sid, "rm -rf /tmp/whatever", timeout=2)
        if "BLOCKED" not in out:
            failures.append(f"ssh_session_exec did NOT block rm -rf: {out}")
        else:
            print(f"  ✓ ssh_session_exec blocked rm -rf inside session  →  {out[:80]}")

        # 3e. session_exec allowed command runs
        out = await cli.ssh_session_exec(sid, "echo session-ok && pwd", timeout=5)
        if "session-ok" not in out:
            failures.append(f"ssh_session_exec allowed cmd failed: {out}")
        else:
            print(f"  ✓ ssh_session_exec allowed cmd ran inside session")
        await sm.close_session(sid)

        # Reset policy back to permissive defaults so step 4 isn't blocked.
        security._policy = security.SecurityPolicy()

    # ── (4) remote_bash + remote_patch round-trip on /tmp ─────────────────
    sect("4. remote_bash + remote_patch round-trip on /tmp/ on live-1810")
    # Drive through the cli.* MCP wrappers so audit_log fires.
    target = f"/tmp/ssh-remote-mcp-smoke-{os.getpid()}.txt"
    try:
        # 4a. remote_bash creates the file
        out = await cli.remote_bash(
            "live-1810",
            f"printf 'line-1\\nline-2\\nline-3\\n' > {target}",
            timeout=10,
        )
        d = json.loads(out)
        if d.get("exit_code", 0) not in (0, None):
            failures.append(f"remote_bash write returned non-zero: {d}")
        else:
            print(f"  ✓ remote_bash wrote {target}")

        # 4b. remote_read JUST line 2 so range_hash applies to line 2 alone
        out = await cli.remote_read("live-1810", target, start=2, end=2)
        rd = json.loads(out)
        if "file_hash" not in rd or "range_hash" not in rd:
            failures.append(f"remote_read unexpected: {rd}")
        elif "line-2" not in rd.get("content", ""):
            failures.append(f"remote_read content unexpected: {rd}")
        else:
            print(f"  ✓ remote_read line 2 → file_hash {rd['file_hash'][:12]}…, "
                  f"range_hash {rd['range_hash'][:12]}…")

        # 4c. remote_patch replaces line 2
        patches = json.dumps([{
            "start": 2, "end": 2,
            "contents": "LINE-2-PATCHED\n",
            "range_hash": rd["range_hash"],
        }])
        out = await cli.remote_patch("live-1810", target,
                                      file_hash=rd["file_hash"],
                                      patches_json=patches)
        wr = json.loads(out)
        if wr.get("result") != "ok":
            failures.append(f"remote_patch failed: {wr}")
        else:
            print(f"  ✓ remote_patch applied, new file_hash "
                  f"{wr.get('file_hash','?')[:12]}…")

        # 4d. verify contents via remote_bash
        out = await cli.remote_bash("live-1810", f"cat {target}", timeout=5)
        d = json.loads(out)
        body = d.get("output", d.get("stdout", ""))
        if "LINE-2-PATCHED" not in body:
            failures.append(f"patched content not visible: body={body!r}")
        else:
            print(f"  ✓ patched content visible via remote_bash")

        # 4e. patch with stale hash MUST be refused (hash conflict path)
        out = await cli.remote_patch("live-1810", target,
                                      file_hash="0" * 64,
                                      patches_json=patches)
        wr2 = json.loads(out)
        if wr2.get("result") == "ok":
            failures.append(f"stale-hash patch wrongly succeeded: {wr2}")
        else:
            print(f"  ✓ stale-hash patch correctly refused: "
                  f"{wr2.get('reason','?')[:60]}")
    finally:
        await cli.remote_bash("live-1810", f"rm -f {target}", timeout=5)
        await cli.remote_bash_close("live-1810")

    # ── (5) audit.jsonl received the new operation types ─────────────────
    sect("5. audit.jsonl recorded new operation tags")
    # Trigger register_host + create_session via the MCP wrappers so the
    # newly-added audit hooks fire.
    cli.ssh_register_host(name="live-1810-audit", host=TEST_HOST,
                           port=TEST_PORT, user=TEST_USER,
                           key_path=TEST_KEY if os.path.exists(TEST_KEY) else "",
                           tags="smoke-audit")
    sid_str = await cli.ssh_create_session("live-1810-audit")
    # ssh_create_session returns "Session created: <sid>"; close via cli wrapper.
    sid_id = sid_str.split(":")[-1].strip()
    await cli.ssh_close_session(sid_id)

    from ssh_remote_mcp.audit import _audit_file
    if not _audit_file.exists():
        failures.append(f"audit file missing: {_audit_file}")
    else:
        recent = _audit_file.read_text().splitlines()[-300:]
        ops_seen = set()
        for line in recent:
            try:
                ops_seen.add(json.loads(line).get("operation"))
            except json.JSONDecodeError:
                pass
        expected = {
            "remote_bash", "file_patch", "session_exec",
            "group_exec", "register_host", "session",
        }
        missing = expected - ops_seen
        if missing:
            failures.append(
                f"audit missing operation tags: {missing}; saw {ops_seen}"
            )
        else:
            print(f"  ✓ audit.jsonl saw all new operation tags: "
                  f"{sorted(expected & ops_seen)}")

    # ── Summary ───────────────────────────────────────────────────────────
    sect("SUMMARY")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"\n  {len(failures)} failure(s)")
        return 1
    print("  ALL CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
