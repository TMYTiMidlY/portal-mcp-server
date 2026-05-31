"""
File Operations — SFTP-based file management on remote hosts.

This module deliberately funnels every SFTP call through ``_sftp_session``,
an :class:`asynccontextmanager` that:

  1. acquires a pooled SSH connection,
  2. opens an SFTP client,
  3. yields the SFTP client,
  4. *always* closes the SFTP client (``exit`` + ``wait_closed``) and
     releases the pooled connection back to the manager — even on exception.

Before this refactor (see git blame) the helper returned ``(conn, sftp)`` and
relied on the caller to clean up. Every public function leaked the connection
back to the pool: ``in_use`` was incremented in ``get_connection`` but never
decremented, so ``ConnectionManager`` would refuse new sessions after roughly
``pool_size`` calls per host. Tests for this regression live in
``tests/test_resource_lifecycle.py``.

Every remote_path argument is validated with :func:`safety.validate_remote_path`
which rejects empty strings, NUL bytes, and ASCII control characters — the
classic shell-truncation smuggling vectors.
"""
import asyncio
import hashlib
import logging
import stat as _stat
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Callable, Optional

import asyncssh

from .connection_manager import DEFAULT_DECODE_ERRORS, get_manager
from .safety import quote_shell, validate_remote_path

logger = logging.getLogger("portal_mcp.files")

# Type of the optional sync progress callback: (bytes_done, bytes_total) -> None.
ProgressCB = Optional[Callable[[int, int], None]]

# Window for mtime equality when deciding to skip a file (rclone's SFTP
# "modify-window" default is 1s). Transfers use preserve=True so a file we
# copied carries the source mtime; the tolerance only absorbs the second-level
# truncation some SFTP servers apply. Widening this trades correctness (missing
# an out-of-band update) for speed, so keep it small.
MTIME_TOLERANCE_SEC = 1.0


def _mtime_match(a: float, b: float) -> bool:
    """True when two mtimes are equal within MTIME_TOLERANCE_SEC."""
    return abs(a - b) <= MTIME_TOLERANCE_SEC


def _make_handler(progress_cb: ProgressCB):
    """Adapt a (done, total) callback to asyncssh's progress_handler signature.

    asyncssh calls ``progress_handler(srcpath, dstpath, bytes_copied,
    total_bytes)`` synchronously from inside the transfer coroutine. We forward
    just the byte counts; the cli layer throttles and schedules the actual MCP
    progress notification (which doubles as a keepalive against client timeouts).
    """
    if progress_cb is None:
        return None

    def _handler(_srcpath, _dstpath, bytes_copied: int, total_bytes: int) -> None:
        try:
            progress_cb(bytes_copied, total_bytes)
        except Exception:  # pragma: no cover - never let progress break a transfer
            pass

    return _handler


@asynccontextmanager
async def _sftp_session(host_name: str) -> AsyncIterator[asyncssh.SFTPClient]:
    """Acquire an SFTP client and guarantee connection release.

    Use as::

        async with _sftp_session(host) as sftp:
            await sftp.put(local, remote)
    """
    mgr = get_manager()
    conn = await mgr.get_connection(host_name)
    sftp = None
    try:
        sftp = await conn.start_sftp_client()
        yield sftp
    finally:
        if sftp is not None:
            try:
                sftp.exit()
                await sftp.wait_closed()
            except Exception as e:  # pragma: no cover - log-only
                logger.debug(f"sftp close error on {host_name}: {e}")
        mgr.release_connection(host_name, conn)


@asynccontextmanager
async def _conn_and_sftp(host_name: str):
    """Like :func:`_sftp_session` but also yields the underlying SSH connection.

    Needed by ``sync``/``mirror`` whose ``checksum=True`` path runs
    ``sha256sum`` on the remote via ``conn.run`` while reusing the same SFTP
    channel for stats and transfers.
    """
    mgr = get_manager()
    conn = await mgr.get_connection(host_name)
    sftp = None
    try:
        sftp = await conn.start_sftp_client()
        yield conn, sftp
    finally:
        if sftp is not None:
            try:
                sftp.exit()
                await sftp.wait_closed()
            except Exception as e:  # pragma: no cover - log-only
                logger.debug(f"sftp close error on {host_name}: {e}")
        mgr.release_connection(host_name, conn)


async def ssh_upload_file(host_name: str, local_path: str, remote_path: str,
                          progress_cb: ProgressCB = None) -> dict:
    """Upload a local file to the remote host via SFTP. Returns a status dict."""
    res = {"status": "error", "direction": "upload", "host": host_name,
           "local_path": local_path, "remote_path": remote_path,
           "bytes": 0, "duration_s": 0.0}
    try:
        validate_remote_path(remote_path)
    except ValueError as e:
        res["error"] = f"Invalid remote_path: {e}"
        return res
    p = Path(local_path)
    if not p.exists():
        res["error"] = f"Local file not found: {local_path}"
        return res
    t0 = time.monotonic()
    try:
        async with _sftp_session(host_name) as sftp:
            await sftp.put(local_path, remote_path, preserve=True,
                           progress_handler=_make_handler(progress_cb))
        res["status"] = "ok"
        res["bytes"] = p.stat().st_size
        res["duration_s"] = round(time.monotonic() - t0, 3)
        return res
    except Exception as e:
        res["error"] = f"Upload failed: {e}"
        return res


async def ssh_download_file(host_name: str, remote_path: str, local_path: str,
                            progress_cb: ProgressCB = None) -> dict:
    """Download a remote file to local disk via SFTP. Returns a status dict."""
    res = {"status": "error", "direction": "download", "host": host_name,
           "local_path": local_path, "remote_path": remote_path,
           "bytes": 0, "duration_s": 0.0}
    try:
        validate_remote_path(remote_path)
    except ValueError as e:
        res["error"] = f"Invalid remote_path: {e}"
        return res
    t0 = time.monotonic()
    try:
        async with _sftp_session(host_name) as sftp:
            await sftp.get(remote_path, local_path, preserve=True,
                           progress_handler=_make_handler(progress_cb))
        res["status"] = "ok"
        try:
            res["bytes"] = Path(local_path).stat().st_size
        except OSError:
            pass
        res["duration_s"] = round(time.monotonic() - t0, 3)
        return res
    except Exception as e:
        res["error"] = f"Download failed: {e}"
        return res


async def ssh_list_directory(host_name: str, remote_path: str = ".") -> list[dict]:
    """List contents of a remote directory."""
    try:
        validate_remote_path(remote_path)
    except ValueError as e:
        return [{"error": f"Invalid remote_path: {e}"}]
    try:
        async with _sftp_session(host_name) as sftp:
            entries = await sftp.readdir(remote_path)
            result = []
            for e in entries:
                a = e.attrs
                result.append({
                    "name": e.filename,
                    "size": a.size,
                    "permissions": oct(a.permissions) if a.permissions else None,
                    "mtime": a.mtime,
                    "is_dir": asyncssh.SFTP_TYPE_DIRECTORY ==
                              (a.permissions >> 12 & 0xf if a.permissions else 0),
                })
            return result
    except Exception as e:
        return [{"error": str(e)}]


async def ssh_read_file(host_name: str, remote_path: str, max_bytes: int = 524288) -> str:
    """Read a remote file's contents (up to max_bytes)."""
    try:
        validate_remote_path(remote_path)
    except ValueError as e:
        return f"Invalid remote_path: {e}"
    try:
        async with _sftp_session(host_name) as sftp:
            async with sftp.open(remote_path, "r") as f:
                content = await f.read(max_bytes)
        return content if isinstance(content, str) else content.decode("utf-8", errors="replace")
    except Exception as e:
        return f"Read failed: {e}"


async def ssh_write_file(host_name: str, remote_path: str, content: str) -> str:
    """Write content to a remote file."""
    try:
        validate_remote_path(remote_path)
    except ValueError as e:
        return f"Invalid remote_path: {e}"
    try:
        async with _sftp_session(host_name) as sftp:
            async with sftp.open(remote_path, "w") as f:
                await f.write(content)
        return f"Written {len(content)} bytes to {host_name}:{remote_path}"
    except Exception as e:
        return f"Write failed: {e}"


async def ssh_delete_file(host_name: str, remote_path: str) -> str:
    """Delete a remote file."""
    try:
        validate_remote_path(remote_path)
    except ValueError as e:
        return f"Invalid remote_path: {e}"
    try:
        async with _sftp_session(host_name) as sftp:
            await sftp.remove(remote_path)
        return f"Deleted {host_name}:{remote_path}"
    except Exception as e:
        return f"Delete failed: {e}"


async def _local_sha256(path: str) -> str:
    def _hash() -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    return await asyncio.get_event_loop().run_in_executor(None, _hash)


async def _remote_sha256(conn, remote_path: str) -> Optional[str]:
    """sha256 of a remote file via ``sha256sum``; None if unavailable."""
    try:
        result = await conn.run(f"sha256sum -b -- {quote_shell(remote_path)}",
                                check=False, errors=DEFAULT_DECODE_ERRORS)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    parts = (result.stdout or "").strip().split()
    return parts[0] if parts else None


async def _files_match(conn, sftp, local_path: str, remote_path: str,
                       local_size: int, local_mtime: float,
                       checksum: bool) -> bool:
    """Decide whether a transfer can be skipped.

    size+mtime mode (default): same size AND the destination's mtime matches the
    source within ``MTIME_TOLERANCE_SEC``. Because transfers use
    ``preserve=True``, a file we previously copied carries the source's mtime, so
    this is a precise equality check (rclone-style) rather than a "newer-than"
    heuristic — a remote file modified out-of-band no longer false-matches.
    checksum mode: same size AND identical sha256 (requires ``sha256sum`` on the
    remote; if it's missing we conservatively re-transfer).
    """
    try:
        rstat = await sftp.stat(remote_path)
    except Exception:
        return False
    if rstat.size != local_size:
        return False
    if checksum:
        rhash = await _remote_sha256(conn, remote_path)
        if rhash is None:
            return False
        lhash = await _local_sha256(local_path)
        return rhash == lhash
    if rstat.mtime is None:
        return False
    return _mtime_match(rstat.mtime, local_mtime)


async def ssh_sync_directory(host_name: str, local_dir: str, remote_dir: str,
                             checksum: bool = False,
                             progress_cb: ProgressCB = None) -> dict:
    """Recursively sync a local directory to a remote directory (upload).

    Skips files already present with a matching size+mtime (or sha256 when
    ``checksum=True``). Returns a structured status dict.
    """
    res = {"status": "error", "direction": "sync", "host": host_name,
           "uploaded": 0, "skipped": 0, "failed": [],
           "bytes_total": 0, "bytes_transferred": 0, "duration_s": 0.0}
    try:
        validate_remote_path(remote_dir)
    except ValueError as e:
        res["error"] = f"Invalid remote_dir: {e}"
        return res
    local = Path(local_dir)
    if not local.is_dir():
        res["error"] = f"Local directory not found: {local_dir}"
        return res
    t0 = time.monotonic()
    remote_root = str(PurePosixPath(remote_dir))
    try:
        async with _conn_and_sftp(host_name) as (conn, sftp):
            for local_file in sorted(local.rglob("*")):
                if not local_file.is_file():
                    continue
                st = local_file.stat()
                size = st.st_size
                res["bytes_total"] += size
                rel = local_file.relative_to(local)
                remote_file = str(PurePosixPath(remote_root, *rel.parts))
                remote_parent = str(PurePosixPath(remote_file).parent)
                try:
                    if await _files_match(conn, sftp, str(local_file), remote_file,
                                          size, st.st_mtime, checksum):
                        res["skipped"] += 1
                        continue
                    await sftp.makedirs(remote_parent, exist_ok=True)
                    await sftp.put(str(local_file), remote_file, preserve=True,
                                   progress_handler=_make_handler(progress_cb))
                    res["uploaded"] += 1
                    res["bytes_transferred"] += size
                except Exception as e:
                    res["failed"].append({"path": str(rel), "error": str(e)})
    except Exception as e:
        res["error"] = f"Sync failed: {e}"
        res["duration_s"] = round(time.monotonic() - t0, 3)
        return res
    res["status"] = "ok" if not res["failed"] else "partial"
    res["duration_s"] = round(time.monotonic() - t0, 3)
    return res


async def _walk_remote(sftp, root: str) -> list[tuple[str, int, Optional[float]]]:
    """Recursively list regular files under a remote dir.

    Returns ``(posix_path, size, mtime)`` tuples. Symlinks and special files are
    skipped (their permission bits aren't S_ISREG), avoiding loops.
    """
    out: list[tuple[str, int, Optional[float]]] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = await sftp.readdir(d)
        except Exception:
            continue
        for entry in entries:
            name = entry.filename
            if name in (".", ".."):
                continue
            full = str(PurePosixPath(d) / name)
            perms = entry.attrs.permissions or 0
            if _stat.S_ISDIR(perms):
                stack.append(full)
            elif _stat.S_ISREG(perms):
                out.append((full, entry.attrs.size or 0, entry.attrs.mtime))
    return out


async def ssh_mirror_directory(host_name: str, remote_dir: str, local_dir: str,
                               checksum: bool = False,
                               progress_cb: ProgressCB = None) -> dict:
    """Recursively mirror a remote directory to local disk (download).

    The remote→local counterpart of ``sync``: same skip logic (size+mtime, or
    sha256 when ``checksum=True``) and structured status dict.
    """
    res = {"status": "error", "direction": "mirror", "host": host_name,
           "downloaded": 0, "skipped": 0, "failed": [],
           "bytes_total": 0, "bytes_transferred": 0, "duration_s": 0.0}
    try:
        validate_remote_path(remote_dir)
    except ValueError as e:
        res["error"] = f"Invalid remote_dir: {e}"
        return res
    local = Path(local_dir)
    t0 = time.monotonic()
    remote_root = str(PurePosixPath(remote_dir))
    try:
        async with _conn_and_sftp(host_name) as (conn, sftp):
            try:
                rstat = await sftp.stat(remote_root)
            except Exception:
                res["error"] = f"Remote directory not found: {remote_dir}"
                return res
            if not _stat.S_ISDIR(rstat.permissions or 0):
                res["error"] = f"Remote path is not a directory: {remote_dir}"
                return res
            remote_files = await _walk_remote(sftp, remote_root)
            for rpath, size, rmtime in remote_files:
                res["bytes_total"] += size
                rel = PurePosixPath(rpath).relative_to(remote_root)
                local_file = local.joinpath(*rel.parts)
                try:
                    if local_file.is_file():
                        lst = local_file.stat()
                        skip = lst.st_size == size
                        if skip and checksum:
                            rhash = await _remote_sha256(conn, rpath)
                            skip = rhash is not None and rhash == await _local_sha256(str(local_file))
                        elif skip:
                            skip = rmtime is not None and _mtime_match(lst.st_mtime, rmtime)
                        if skip:
                            res["skipped"] += 1
                            continue
                    local_file.parent.mkdir(parents=True, exist_ok=True)
                    await sftp.get(rpath, str(local_file), preserve=True,
                                   progress_handler=_make_handler(progress_cb))
                    res["downloaded"] += 1
                    res["bytes_transferred"] += size
                except Exception as e:
                    res["failed"].append({"path": str(rel), "error": str(e)})
    except Exception as e:
        res["error"] = f"Mirror failed: {e}"
        res["duration_s"] = round(time.monotonic() - t0, 3)
        return res
    res["status"] = "ok" if not res["failed"] else "partial"
    res["duration_s"] = round(time.monotonic() - t0, 3)
    return res
