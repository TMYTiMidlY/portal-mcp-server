"""End-to-end acceptance test (Phase 6).

Performs the 7-step demo from plan.md against host '1810' (must be a Host
alias in ~/.ssh/config). All writes are confined to /tmp/ssh-remote-mcp-test/
on the remote. Cleans up at the end.
"""
import asyncio
import json
import sys

from server.connection_manager import get_manager
from server.remote_bash import remote_bash, remote_bash_close
from server.remote_search import remote_grep, remote_glob
from server.remote_text_editor import remote_read, remote_patch

HOST = "1810"
SANDBOX = "/tmp/ssh-remote-mcp-test"
SAMPLE = f"{SANDBOX}/runner.py"
SOURCE = "~/SU2-Quantum/tgv/runner.py"
SU2_ROOT = "~/SU2-Quantum"


def banner(n: int, title: str) -> None:
    print(f"\n{'=' * 60}\nStep {n}: {title}\n{'=' * 60}")


async def main() -> int:
    mgr = get_manager()
    conn = await mgr.get_connection(HOST)

    banner(0, "prepare sandbox + copy SU2-Quantum runner.py into it")
    setup_cmd = f"mkdir -p {SANDBOX} && cp {SOURCE} {SAMPLE} && wc -l {SAMPLE}"
    r = await conn.run(setup_cmd)
    print(r.stdout.strip())
    mgr.release_connection(HOST, conn)

    banner(1, "remote_read sandbox file lines 1..30 → returns content + sha256")
    rd = await remote_read(HOST, SAMPLE, start=1, end=30)
    print(f"file_hash:   {rd['file_hash']}")
    print(f"range_hash:  {rd['range_hash']}")
    print(f"total_lines: {rd['total_lines']}")
    print(f"first 3 lines:\n{chr(10).join(rd['content'].splitlines()[:3])}")

    banner(2, "remote_grep AsyncHttpProgressReporter under real SU2-Quantum (read-only)")
    gr = await remote_grep(HOST, "/home/timidly/SU2-Quantum", "AsyncHttpProgressReporter", glob="*.py")
    print(f"engine: {gr['engine']}")
    for m in gr["matches"]:
        print(f"  {m['file']}:{m['line']}  {m['text'][:78]}")

    banner(3, "remote_patch sandbox file: change line 1 from `# Usage: ...` style to a marker")
    line1 = await remote_read(HOST, SAMPLE, start=1, end=1)
    print(f"original line 1 (first 60c): {line1['content'][:60]!r}")
    new_line1 = "# PHASE 6 ACCEPTANCE: line modified by remote_patch\n"
    res = await remote_patch(
        HOST,
        SAMPLE,
        file_hash=rd["file_hash"],
        patches=[{"start": 1, "end": 1, "contents": new_line1, "range_hash": line1["range_hash"]}],
    )
    print(f"patch result: {res}")
    assert res["result"] == "ok", f"patch FAILED: {res}"

    banner(4, "NEGATIVE: external process modifies file, agent's stale-hash patch must be rejected")
    conn = await mgr.get_connection(HOST)
    await conn.run(f"echo '# tampered by another agent' >> {SAMPLE}")
    mgr.release_connection(HOST, conn)
    bad = await remote_patch(
        HOST,
        SAMPLE,
        file_hash=res["file_hash"],  # now stale
        patches=[{"start": 1, "end": 1, "contents": "# would clobber\n", "range_hash": ""}],
    )
    print(f"bad patch result: {bad['result']}  reason: {bad.get('reason')}")
    print(f"current_file_hash returned: {bad.get('current_file_hash', '')[:20]}...")
    assert bad["result"] == "error", "STALE-HASH PATCH WAS NOT REJECTED!"
    assert "hash mismatch" in bad.get("reason", "").lower()
    print("✅ stale-hash patch correctly rejected")

    banner(5, "remote_bash persistent: cd then pwd then ls — same session, cwd preserved")
    b1 = await remote_bash(HOST, f"cd {SANDBOX} && pwd")
    b2 = await remote_bash(HOST, "pwd && ls")
    b3 = await remote_bash(HOST, "echo $$")  # same shell PID
    print(f"after cd:     {b1['output']!r}  session={b1['session_id']}")
    print(f"pwd && ls:    {b2['output']!r}  session={b2['session_id']}")
    print(f"shell PID:    {b3['output']!r}")
    assert b1["session_id"] == b2["session_id"] == b3["session_id"], "sessions diverged!"
    assert SANDBOX in b2["output"], "cwd did not persist!"

    banner(6, "verify single TCP connection to 1810")
    conn = await mgr.get_connection(HOST)
    r = await conn.run(
        "ss -tn 'src 10.144.18.10' state established | wc -l", check=False
    )
    print(f"established connections seen by 1810 (incl. header line): {r.stdout.strip()}")
    pool = mgr.pool_status()
    print(f"local connection pool: {json.dumps(pool, indent=2)}")
    assert len(pool) == 1, f"expected 1 pooled connection, got {pool}"
    mgr.release_connection(HOST, conn)

    banner(7, "cleanup sandbox")
    conn = await mgr.get_connection(HOST)
    await conn.run(f"rm -rf {SANDBOX}")
    mgr.release_connection(HOST, conn)
    await remote_bash_close(HOST)
    await mgr.close_all()
    print("\n🎉 ALL 7 STEPS PASSED — Phase 6 acceptance complete")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
