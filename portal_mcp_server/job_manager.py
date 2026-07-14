"""job_manager — background ("fire-and-poll") command execution (L1).

remote_job runs a command in the *background* on a remote host and hands the
agent a job_id, so the agent gets control back immediately and can think while
the command runs, poll for incremental output, and cancel at will. This is the
async counterpart to remote_exec (synchronous) and remote_shell (stateful).

Capture strategy (remote tmp files)
-----------------------------------
``submit`` spawns ``nohup bash -c '<cmd>; echo __JOB_DONE__:$? >> <meta>' >
<out> 2>&1 < /dev/null &`` and records the remote PID. Because the work runs
under ``nohup`` writing to a file, the job **survives the SSH connection
dying** — poll/cancel just reconnect over the pool. ``poll`` reads an
incremental byte range of ``<out>`` and the exit code from ``<meta>``;
``cancel`` sends a signal to the PID.

L1 limits (intentional)
-----------------------
* The job table is **best-effort persisted** to ``<state>/jobs.json`` so
  job_ids survive a server restart (the table reloads on startup and a poll
  re-probes the remote PID). It is NOT a durable queue: the file is rewritten
  on each state change and a crash mid-write or a disabled
  (``PORTAL_JOB_PERSIST=0``) store loses the view — the remote nohup process
  keeps running regardless and is recoverable via ``ps``.
* ``use_sudo`` / ``secrets`` are NOT supported in the background (sudo -S wants
  stdin; injecting secrets into a backgrounded ``bash -c`` would put them on
  argv, visible in ``ps``). Use remote_exec for those.
* A bounded number of concurrent live jobs (``PORTAL_JOB_MAX_LIVE``, default
  50); finished jobs are swept after ``PORTAL_JOB_TTL`` (default 3600s) and
  their remote tmp files removed.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .connection_manager import DEFAULT_DECODE_ERRORS, get_manager
from .paths import xdg_state_home
from .safety import quote_shell

logger = logging.getLogger("portal_mcp.jobs")

_TERMINAL = ("done", "failed", "cancelled", "unknown")
_DONE_MARKER = "__JOB_DONE__:"
_CHUNK_SEP = "\n__CHUNK__\n"
# Default per-poll output cap (bytes). Keeps a single poll from dumping a huge
# backlog all at once — the agent pages through with since=new_offset while the
# `more` flag is true. The agent can raise/lower it per call via max_bytes.
DEFAULT_POLL_MAX_BYTES = 64 * 1024


def _pid_alive(pid: int) -> bool:
    """Best-effort: is a local process with this pid running? Conservative — on
    any uncertainty (Windows, permission error) assume alive, so we never adopt
    or delete a state file that a live sibling server still owns."""
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _state_file() -> Optional[Path]:
    """Resolve THIS server process's job-table persistence file.

    ``PORTAL_JOB_PERSIST=0`` disables persistence. ``PORTAL_JOB_STATE_FILE``
    pins one explicit file (tests, or an operator deliberately sharing one).
    Otherwise the default is **per-process** — ``<state>/jobs/<pid>.json`` — so
    two concurrent per-user server processes (e.g. one MCP client each) never
    overwrite each other's table. A dead predecessor's file is adopted on
    startup (see :meth:`JobManager._load`), preserving cross-restart recovery.
    """
    if os.environ.get("PORTAL_JOB_PERSIST", "").lower() in (
            "0", "false", "no", "off"):
        return None
    raw = os.environ.get("PORTAL_JOB_STATE_FILE")
    if raw:
        return Path(raw)
    try:
        return xdg_state_home() / "jobs" / f"{os.getpid()}.json"
    except Exception:  # pragma: no cover - exotic platform
        return None


@dataclass
class JobRecord:
    job_id: str
    host: str
    remote_pid: int
    out_path: str
    meta_path: str
    command: str
    started_at: float
    status: str = "running"          # running | done | failed | cancelled | unknown
    exit_code: Optional[int] = None
    finished_at: Optional[float] = None
    last_offset: int = 0
    cancel_requested: bool = field(default=False)


class JobManager:
    def __init__(self):
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()
        # In-flight submits that passed the max-live check but haven't been
        # inserted yet (their remote spawn runs outside the lock). Counted
        # against the cap so concurrent submits can't overshoot it.
        self._pending = 0
        self._state_file = _state_file()
        # Namespaced = the per-process default path (no explicit override): only
        # then do we scan+adopt sibling files. An explicit PORTAL_JOB_STATE_FILE
        # keeps the old single-file behavior (tests / opt-in shared file).
        self._namespaced = (self._state_file is not None
                            and not os.environ.get("PORTAL_JOB_STATE_FILE"))
        self._load()

    def _load(self) -> None:
        """Best-effort reload of the job table from disk (survives a restart).

        In per-process (namespaced) mode we also adopt the tables of dead
        predecessor processes so a restart still recovers their jobs, while
        never touching a file a concurrent live server still owns. Records are
        reloaded verbatim; liveness is NOT probed here (no remote I/O in
        __init__) — a subsequent poll re-probes the remote PID.
        """
        f = self._state_file
        if not f:
            return
        if self._namespaced:
            self._adopt_dead_siblings(f.parent)
        if f.exists():
            self._load_file(f)

    def _load_file(self, f: Path) -> None:
        try:
            data = json.loads(f.read_text())
        except Exception:  # pragma: no cover - corrupt/partial state file
            logger.debug("job state reload failed for %s", f, exc_info=True)
            return
        n = 0
        for d in data.get("jobs", []):
            try:
                rec = JobRecord(**d)
            except (TypeError, ValueError):
                continue  # schema drift / bad entry — skip it
            self._jobs[rec.job_id] = rec
            n += 1
        if n:
            logger.info("reloaded %d background job(s) from %s", n, f)

    def _adopt_dead_siblings(self, d: Path) -> None:
        """Adopt job tables left by dead predecessor processes (files named
        ``<pid>.json``), then remove them. A file whose pid is still alive
        belongs to a concurrent server and is left untouched."""
        try:
            siblings = list(d.glob("*.json"))
        except OSError:  # pragma: no cover - dir missing
            return
        my_pid = os.getpid()
        for sib in siblings:
            try:
                pid = int(sib.stem)
            except ValueError:
                continue  # not a <pid>.json file
            if pid == my_pid or _pid_alive(pid):
                continue
            self._load_file(sib)
            try:
                sib.unlink()
            except OSError:  # pragma: no cover
                pass

    def _persist(self) -> None:
        """Best-effort atomic write of the job table. Never raises into a job
        operation — persistence is a convenience, not a correctness guarantee."""
        f = self._state_file
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(f.parent, 0o700)  # state dir holds persisted job command lines
            except OSError:
                pass
            payload = {"jobs": [asdict(r) for r in self._jobs.values()]}
            tmp = f.with_name(f.name + ".tmp")
            # Create the temp 0600 so the persisted job table (command lines,
            # hosts) is owner-only regardless of umask; replace() keeps the mode.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(payload))
            tmp.replace(f)
        except Exception:  # pragma: no cover - best effort
            logger.debug("job state persist failed", exc_info=True)

    @property
    def _max_live(self) -> int:
        raw = os.environ.get("PORTAL_JOB_MAX_LIVE", "")
        return int(raw) if raw.isdigit() and int(raw) > 0 else 50

    @property
    def _ttl(self) -> float:
        raw = os.environ.get("PORTAL_JOB_TTL", "")
        try:
            v = float(raw)
            return v if v > 0 else 3600.0
        except (TypeError, ValueError):
            return 3600.0

    # ── submit ──────────────────────────────────────────────────────────────

    async def submit(self, host: str, command: str, login: bool = True) -> dict:
        await self._sweep_expired()
        async with self._lock:
            live = sum(1 for r in self._jobs.values() if r.status not in _TERMINAL)
            # Count reservations: the remote spawn in _spawn_and_record runs
            # OUTSIDE the lock, so without reserving a slot here N concurrent
            # submits could all pass a check that should admit only the first
            # few and overshoot PORTAL_JOB_MAX_LIVE.
            if live + self._pending >= self._max_live:
                raise RuntimeError(
                    f"too many live jobs ({live} >= PORTAL_JOB_MAX_LIVE="
                    f"{self._max_live}); poll/cancel some before submitting more.")
            self._pending += 1
        try:
            return await self._spawn_and_record(host, command, login)
        finally:
            async with self._lock:
                self._pending -= 1

    async def _spawn_and_record(self, host: str, command: str,
                                login: bool = True) -> dict:
        token = uuid.uuid4().hex
        out_path = f"/tmp/portal-job-{token}.out"
        meta_path = f"/tmp/portal-job-{token}.meta"
        # Inner script: run the command, then record its exit status. The inner
        # bash evaluates $? AFTER the command (it is single-quoted into argv by
        # quote_shell, so the OUTER shell does not expand it). A LOGIN shell
        # (bash -lc) loads the user's ~/.profile / ~/.bashrc so long tasks see
        # the same PATH/env as remote_exec; login=False keeps a plain bash -c.
        inner = f"{command}\necho \"{_DONE_MARKER}$?\" >> {quote_shell(meta_path)}\n"
        bash_flag = "-lc" if login else "-c"
        spawn = (f"nohup bash {bash_flag} {quote_shell(inner)} "
                 f"> {quote_shell(out_path)} 2>&1 < /dev/null & echo $!")

        mgr = get_manager()
        conn = await mgr.get_connection(host)
        try:
            try:
                result = await asyncio.wait_for(
                    conn.run(spawn, check=False, errors=DEFAULT_DECODE_ERRORS),
                    timeout=30,
                )
            except asyncio.TimeoutError as exc:
                # The spawn command finishes in milliseconds (nohup detaches and
                # `echo $!` returns immediately) — a 30 s timeout almost always
                # means the SSH link itself stalled. The remote bash may have
                # already forked the nohup child though, in which case the job
                # is running detached but we never recorded its PID, so portal
                # has no way to list / cancel / poll it. Surface the token so
                # the user can find and clean up the orphan by hand.
                raise RuntimeError(
                    f"timed out submitting background job on {host!r} after "
                    f"30s; the remote process may still be running detached. "
                    f"Check with `ssh {host} 'pgrep -f portal-job-{token}'` "
                    f"and clean up /tmp/portal-job-{token}.{{out,meta}} if so."
                ) from exc
        finally:
            mgr.release_connection(host, conn)

        pid_str = (result.stdout or "").strip().splitlines()
        pid_str = pid_str[-1].strip() if pid_str else ""
        if not pid_str.isdigit():
            raise RuntimeError(
                f"could not start background job on {host!r} "
                f"(no PID returned: {(result.stderr or result.stdout or '')[:200]!r})")
        pid = int(pid_str)
        job_id = f"job-{token[:12]}"
        rec = JobRecord(job_id=job_id, host=host, remote_pid=pid,
                        out_path=out_path, meta_path=meta_path,
                        command=command, started_at=time.time())
        async with self._lock:
            self._jobs[job_id] = rec
            self._persist()
        logger.info("job %s submitted on %s (pid %d)", job_id, host, pid)
        return {"job_id": job_id, "host": host, "remote_pid": pid,
                "started_at": _iso(rec.started_at), "status": "running"}

    # ── poll ────────────────────────────────────────────────────────────────

    async def poll(self, job_id: str, since: int = 0, tail: int = 0,
                   max_bytes: int = DEFAULT_POLL_MAX_BYTES) -> dict:
        rec = self._jobs.get(job_id)
        if rec is None:
            return {"job_id": job_id, "status": "unknown",
                    "error": "no such job_id (it may have expired, been swept "
                             "after its TTL, or persistence was disabled)"}
        off = max(0, int(since))
        # >= 4 so a single (max 4-byte) UTF-8 char can always fit in one poll.
        cap = max(4, int(max_bytes))
        q_out, q_meta = quote_shell(rec.out_path), quote_shell(rec.meta_path)
        is_tail = bool(tail and tail > 0)
        if is_tail:
            chunk_cmd = f"tail -n {int(tail)} {q_out} 2>/dev/null"
        else:
            chunk_cmd = (f"tail -c +{off + 1} {q_out} 2>/dev/null | "
                         f'head -c "$N"')
        # The chunk is base64-encoded on the wire so we get the EXACT bytes back
        # (the SSH channel would otherwise decode/mangle them before we can do a
        # clean, boundary-aware UTF-8 decode). base64 wrapping is stripped below.
        poll_cmd = (
            f"M=$(cat {q_meta} 2>/dev/null); "
            f"S=$(wc -c < {q_out} 2>/dev/null || echo 0); "
            f"N=$((S-{off})); [ \"$N\" -lt 0 ] && N=0; "
            f"[ \"$N\" -gt {cap} ] && N={cap}; "
            f"if kill -0 {rec.remote_pid} 2>/dev/null; then A=yes; else A=no; fi; "
            f"printf 'META:%s\\nSIZE:%s\\nALIVE:%s\\n__CHUNK__\\n' \"$M\" \"$S\" \"$A\"; "
            f"{{ {chunk_cmd} ; }} | base64"
        )

        mgr = get_manager()
        try:
            conn = await mgr.get_connection(rec.host)
        except Exception as e:  # host unreachable
            return {"job_id": job_id, "status": "unknown", "error": str(e)}
        try:
            result = await asyncio.wait_for(
                conn.run(poll_cmd, check=False, errors=DEFAULT_DECODE_ERRORS),
                timeout=30,
            )
        except Exception as e:
            return {"job_id": job_id, "status": "unknown", "error": str(e),
                    "host": rec.host}
        finally:
            mgr.release_connection(rec.host, conn)

        meta, size, alive, chunk_b64 = _parse_poll(result.stdout or "")
        raw = _b64decode_loose(chunk_b64)
        status, exit_code = self._classify(rec, meta, alive)
        if is_tail:
            # A snapshot of the end — no offset tracking; resume from EOF after.
            text = raw.decode("utf-8", errors="backslashreplace")
            new_offset = size if size is not None else off
        else:
            text, consumed = _decode_incremental(raw)
            new_offset = off + consumed
            # Once the job is terminal AND we've read to EOF, no further bytes
            # will ever arrive to complete a trailing incomplete/invalid UTF-8
            # sequence. _decode_incremental defers such a tail (consumed <
            # len(raw)) — correct while the job runs, but it would pin
            # new_offset below size forever once terminal, so `more` stays True
            # and an agent's `while more: poll(...)` loop livelocks until the
            # TTL sweep (and the tail bytes are never delivered). Flush the
            # remainder with escapes so new_offset reaches size. The at-EOF
            # guard keeps a multibyte char split at the max_bytes cap deferred,
            # since the next poll has the continuation bytes.
            at_eof = size is not None and off + len(raw) >= size
            if consumed < len(raw) and status in _TERMINAL and at_eof:
                text = raw.decode("utf-8", errors="backslashreplace")
                new_offset = off + len(raw)
        more = size is not None and new_offset < size

        async with self._lock:
            newly_terminal = status in _TERMINAL and rec.finished_at is None
            rec.status = status
            rec.exit_code = exit_code
            rec.last_offset = new_offset
            if newly_terminal:
                rec.finished_at = time.time()
            # Persist only on the meaningful transition (not every offset tick)
            # to avoid churning the state file on a fast poll loop.
            if newly_terminal:
                self._persist()

        out = {"job_id": job_id, "host": rec.host, "status": status,
               "output_chunk": text, "new_offset": new_offset, "more": more}
        if exit_code is not None:
            out["exit_code"] = exit_code
        if rec.finished_at is not None:
            out["finished_at"] = _iso(rec.finished_at)
        return out

    @staticmethod
    def _classify(rec: JobRecord, meta: str, alive: Optional[str]):
        if meta and _DONE_MARKER in meta:
            try:
                code = int(meta.split(_DONE_MARKER, 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                code = None
            if code is None:
                return "unknown", None
            return ("done" if code == 0 else "failed"), code
        if alive == "yes":
            # Still running — even if a cancel was requested, the signal hasn't
            # taken effect yet (trapped SIGTERM, pending KILL). Report the truth
            # rather than prematurely claiming "cancelled".
            return "running", None
        if rec.cancel_requested:
            return "cancelled", rec.exit_code
        # Process gone without recording an exit status (killed externally).
        return "unknown", None

    # ── cancel ──────────────────────────────────────────────────────────────

    async def cancel(self, job_id: str, signal: str = "TERM") -> dict:
        rec = self._jobs.get(job_id)
        if rec is None:
            return {"job_id": job_id, "status_after": "unknown",
                    "signal_sent": False,
                    "error": "no such job_id (expired or server restarted)"}
        if rec.status in _TERMINAL:
            # Never signal a bare PID for a job we already consider finished:
            # the OS may have recycled that PID to an unrelated process.
            return {"job_id": job_id, "signal_sent": False,
                    "status_after": rec.status,
                    "note": f"job already {rec.status}; not signaling (its PID "
                            f"may have been reused by another process)."}
        sig = "KILL" if str(signal).upper() == "KILL" else "TERM"
        pid = rec.remote_pid
        # Signal the whole process group (so children spawned by the job die
        # too), then the pid, then re-probe so the reported status reflects
        # what actually happened instead of assuming success.
        script = (
            f'PGID=$(ps -o pgid= -p {pid} 2>/dev/null | tr -d " "); '
            f'[ -n "$PGID" ] && kill -{sig} -"$PGID" 2>/dev/null; '
            f'kill -{sig} {pid} 2>/dev/null; '
            f'sleep 0.3; '
            f'if kill -0 {pid} 2>/dev/null; then echo ALIVE; else echo DEAD; fi'
        )
        mgr = get_manager()
        try:
            conn = await mgr.get_connection(rec.host)
        except Exception as e:
            return {"job_id": job_id, "signal_sent": False,
                    "status_after": rec.status, "error": str(e)}
        try:
            result = await asyncio.wait_for(
                conn.run(script, check=False, errors=DEFAULT_DECODE_ERRORS),
                timeout=30)
            alive_after = "ALIVE" in (result.stdout or "")
        finally:
            mgr.release_connection(rec.host, conn)

        async with self._lock:
            rec.cancel_requested = True
            if not alive_after and rec.status not in _TERMINAL:
                rec.status = "cancelled"
                rec.finished_at = rec.finished_at or time.time()
            self._persist()
        logger.info("job %s: SIG%s sent to pid %d (alive_after=%s)",
                    job_id, sig, pid, alive_after)
        out = {"job_id": job_id, "signal_sent": True, "signal": sig,
               "status_after": rec.status}
        if alive_after:
            out["note"] = ("signal sent but the process is still alive "
                           "(SIGTERM may be trapped — retry with signal='KILL').")
        return out

    # ── list ────────────────────────────────────────────────────────────────

    async def list_jobs(self) -> list[dict]:
        await self._sweep_expired()
        now = time.time()
        out = []
        for r in self._jobs.values():
            entry = {"job_id": r.job_id, "host": r.host, "status": r.status,
                     "started_at": _iso(r.started_at),
                     "age_s": round(now - r.started_at, 1)}
            if r.exit_code is not None:
                entry["exit_code"] = r.exit_code
            out.append(entry)
        return out

    # ── TTL sweep ───────────────────────────────────────────────────────────

    async def _sweep_expired(self) -> None:
        now = time.time()
        expired: list[JobRecord] = []
        async with self._lock:
            for jid in list(self._jobs):
                r = self._jobs[jid]
                if r.status in _TERMINAL and r.finished_at is not None \
                        and (now - r.finished_at) > self._ttl:
                    expired.append(r)
                    del self._jobs[jid]
            if expired:
                self._persist()
        for r in expired:
            await self._remote_cleanup(r)

    async def _remote_cleanup(self, rec: JobRecord) -> None:
        """Best-effort removal of a finished job's remote tmp files."""
        try:
            mgr = get_manager()
            conn = await mgr.get_connection(rec.host)
            try:
                await asyncio.wait_for(
                    conn.run(f"rm -f {quote_shell(rec.out_path)} "
                             f"{quote_shell(rec.meta_path)}",
                             check=False, errors=DEFAULT_DECODE_ERRORS),
                    timeout=15,
                )
            finally:
                mgr.release_connection(rec.host, conn)
        except Exception:  # pragma: no cover - cleanup is best-effort
            logger.debug("job %s remote cleanup failed", rec.job_id, exc_info=True)


def _parse_poll(stdout: str):
    """Split a poll command's stdout into (meta, size, alive, chunk_b64)."""
    idx = stdout.find(_CHUNK_SEP)
    if idx == -1:
        header, chunk = stdout, ""
    else:
        header, chunk = stdout[:idx], stdout[idx + len(_CHUNK_SEP):]
    meta = ""
    size = None
    alive = None
    for line in header.splitlines():
        if line.startswith("META:"):
            meta = line[5:]
        elif line.startswith("SIZE:"):
            try:
                size = int(line[5:])
            except ValueError:
                size = None
        elif line.startswith("ALIVE:"):
            alive = line[6:]
    return meta, size, alive, chunk


def _b64decode_loose(b64text: str) -> bytes:
    """Decode base64 that may carry line wrapping (GNU/BSD ``base64`` wrap at
    76/64 cols). Whitespace is stripped first. Returns ``b""`` on garbage."""
    cleaned = "".join(b64text.split())
    if not cleaned:
        return b""
    try:
        return base64.b64decode(cleaned)
    except (binascii.Error, ValueError):
        return b""


def _decode_incremental(raw: bytes) -> "tuple[str, int]":
    """Decode UTF-8 ``raw`` -> (text, n_bytes_consumed).

    A trailing *incomplete* multibyte sequence is trimmed and NOT counted in
    ``n_bytes_consumed``, so the next poll re-reads those bytes once their
    continuation has arrived — a chunk boundary therefore never splits a
    character into ``\\xNN`` escape artifacts. ``raw`` is assumed to start on a
    character boundary (every poll advances new_offset only by whole chars).
    """
    if not raw:
        return "", 0
    # A truncated tail is at most 3 bytes short of a 4-byte sequence.
    for trim in range(min(3, len(raw)) + 1):
        end = len(raw) - trim
        try:
            return raw[:end].decode("utf-8"), end
        except UnicodeDecodeError:
            continue
    # Not a clean-tail truncation — genuinely non-UTF-8 bytes (e.g. GBK from a
    # Windows host). Escape and consume all (they won't "complete" on re-read).
    return raw.decode("utf-8", errors="backslashreplace"), len(raw)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


_job_mgr: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    global _job_mgr
    if _job_mgr is None:
        _job_mgr = JobManager()
    return _job_mgr
