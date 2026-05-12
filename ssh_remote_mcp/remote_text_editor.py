"""Remote text editor with hash-based concurrent-modification detection.

Algorithm reference: tumf/mcp-text-editor (MIT). See SECURITY.md
§ "References & Algorithmic Provenance" for the full design diff and the
reasons we did not vendor the upstream library.

Key design decisions a source reader needs to know
--------------------------------------------------

* **Patches are applied bottom-to-top** (sorted by ``start`` descending) so
  earlier patches' line-number changes do not invalidate later patches.
  Overlap detection runs after the sort: each upper patch's start must be
  strictly greater than the lower patch's end.

* **Atomic write** = ``<path>.mcp_tmp.<uuid12>`` opened via SFTP, then
  ``posix_rename`` into place; falls back to ``remove + rename`` for SFTP
  servers that do not advertise the posix-rename extension. Tmp files
  carry a recognizable suffix so :func:`cleanup_orphan_tmps` can find
  leftovers without false positives.

* **Three hash checks** all use :func:`_hash_eq` (constant-time
  comparison via :func:`hmac.compare_digest`):
  1. whole-file hash before patching — detects concurrent overwrites,
  2. per-patch ``range_hash`` — detects the case where the file hash
     stayed the same but the targeted region changed,
  3. post-write rehash — detects partial SFTP writes (transport closed
     mid-stream) before we report success.

* **Connection-pool safety**: every SFTP session is released in a
  ``finally`` block with ``await sftp.wait_closed()``, so a thrown
  asyncssh exception cannot leak a pooled session.

* **Path validation** via :func:`safety.validate_remote_path` rejects NUL
  bytes and ASCII control characters.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

import asyncssh

from .connection_manager import get_manager
from .safety import validate_remote_path

logger = logging.getLogger("ssh_mcp.remote_editor")

# A stable, recognizable suffix so a janitor can later identify orphans we
# left behind (e.g. when a connection died after `sftp.open(tmp)` but before
# `posix_rename`). The suffix is wide enough (12 hex chars) that there is no
# practical chance of a real user file matching it.
_TMP_SUFFIX_RE = re.compile(r"\.mcp_tmp\.[0-9a-f]{12}$")


def _make_tmp_path(target_path: str) -> str:
    """Return a tmp path in the same directory as ``target_path``.

    Putting the tmp file beside the target is what makes ``posix_rename``
    atomic on POSIX filesystems (rename is only atomic *within* a single
    filesystem). Using a recognizable suffix lets :func:`cleanup_orphan_tmps`
    find leftovers without false positives.
    """
    return f"{target_path}.mcp_tmp.{uuid.uuid4().hex[:12]}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_eq(a: str, b: str) -> bool:
    """Constant-time hash comparison via :func:`hmac.compare_digest`.

    Inherited design intent from upstream ``utils.secure_compare_hash``;
    cheap insurance against timing oracles even though the threat surface
    is small for an SSH-tunneled local edit tool.
    """
    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


@dataclass
class Patch:
    start: int  # 1-based inclusive
    end: Optional[int]  # 1-based inclusive; None = end of file
    contents: str  # new content (must end with newline if you want one)
    range_hash: str  # SHA-256 of the slice being replaced (empty for pure inserts)


class RemoteEditError(Exception):
    """Raised by portal_patch on hash mismatch or unrecoverable I/O failure."""

    def __init__(self, message: str, current_hash: Optional[str] = None,
                 current_content: Optional[str] = None):
        super().__init__(message)
        self.current_hash = current_hash
        self.current_content = current_content


async def _read_full(host: str, path: str, encoding: str) -> str:
    validate_remote_path(path)
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        sftp = await conn.start_sftp_client()
        try:
            async with sftp.open(path, "r", encoding=encoding) as f:
                return await f.read()
        finally:
            sftp.exit()
            try:
                await sftp.wait_closed()
            except Exception:  # pragma: no cover
                pass
    finally:
        mgr.release_connection(host, conn)


async def _atomic_write(host: str, path: str, new_content: str, encoding: str) -> None:
    """Write atomically via tmp + rename on the remote host.

    Failure-mode contract
    ---------------------
    1. If we never got far enough to create the tmp file, we never call
       ``sftp.remove(tmp)``. (This is why we use ``created`` below: calling
       remove on a path we never opened risks deleting an unrelated file
       whose name happened to collide — exceedingly unlikely thanks to the
       12-hex suffix, but we still guard.)
    2. If creation succeeded but anything *after* it raises (including
       :class:`asyncio.CancelledError`), we make a best-effort cleanup and
       re-raise.
    3. If the connection itself dies mid-cleanup, the orphan tmp will be
       picked up by :func:`cleanup_orphan_tmps` on the next maintenance run.
    """
    validate_remote_path(path)
    tmp_path = _make_tmp_path(path)
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    created = False
    try:
        sftp = await conn.start_sftp_client()
        try:
            try:
                async with sftp.open(tmp_path, "w", encoding=encoding) as f:
                    created = True
                    await f.write(new_content)
                try:
                    await sftp.posix_rename(tmp_path, path)
                except (asyncssh.SFTPOpUnsupported, AttributeError):
                    try:
                        await sftp.remove(path)
                    except asyncssh.SFTPNoSuchFile:
                        pass
                    await sftp.rename(tmp_path, path)
                # Rename succeeded — tmp_path no longer exists at its old name.
                created = False
            except BaseException:
                # Best-effort cleanup. We catch BaseException explicitly so a
                # CancelledError propagating from asyncio.shield-style code
                # does not bypass cleanup.
                if created:
                    try:
                        await sftp.remove(tmp_path)
                    except Exception:
                        logger.warning(
                            "remote_text_editor: failed to remove orphan tmp "
                            "%s on %s; cleanup_orphan_tmps will pick it up",
                            tmp_path, host,
                        )
                raise
        finally:
            sftp.exit()
            try:
                await sftp.wait_closed()
            except Exception:  # pragma: no cover
                pass
    finally:
        mgr.release_connection(host, conn)


async def cleanup_orphan_tmps(host: str, directory: str,
                               max_age_s: int = 3600) -> Dict[str, Any]:
    """Find and remove orphan tmp files left by a failed :func:`_atomic_write`.

    Args:
        host:        registered host alias.
        directory:   absolute remote directory to scan (non-recursive).
        max_age_s:   only remove files older than this many seconds. Defaults
                     to 1 hour, which is more than enough headroom over any
                     realistic edit but small enough that a leftover from a
                     legitimate concurrent edit will *not* be touched. Pass
                     ``0`` to remove every match unconditionally.

    Returns:
        ``{"scanned": int, "removed": [str, ...], "skipped": [(str, str), ...]}``
        — ``skipped`` entries carry a per-file reason (too young / remove
        failed) so the caller can surface them.
    """
    validate_remote_path(directory)
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    sftp = None
    removed: list[str] = []
    skipped: list[tuple[str, str]] = []
    scanned = 0
    try:
        sftp = await conn.start_sftp_client()
        entries = await sftp.readdir(directory)
        now = time.time()
        for e in entries:
            name = e.filename
            if not _TMP_SUFFIX_RE.search(name):
                continue
            scanned += 1
            full = str(PurePosixPath(directory) / name)
            mtime = getattr(e.attrs, "mtime", None)
            if max_age_s > 0 and mtime is not None and (now - mtime) < max_age_s:
                skipped.append((full, f"younger than {max_age_s}s"))
                continue
            try:
                await sftp.remove(full)
                removed.append(full)
            except Exception as exc:
                skipped.append((full, f"remove failed: {exc}"))
    finally:
        if sftp is not None:
            try:
                sftp.exit()
                await sftp.wait_closed()
            except Exception:  # pragma: no cover
                pass
        mgr.release_connection(host, conn)
    return {"scanned": scanned, "removed": removed, "skipped": skipped}


async def remote_read(host: str, path: str, start: int = 1,
                      end: Optional[int] = None, encoding: str = "utf-8") -> Dict[str, Any]:
    """Read a file (or a line range) from a remote host.

    Returns:
        {
          content: str,            # the requested slice
          file_hash: str,          # SHA-256 of the WHOLE file
          range_hash: str,         # SHA-256 of the returned slice
          start: int, end: int,    # 1-based inclusive line range actually returned
          total_lines: int,
          encoding: str,
        }
    """
    full = await _read_full(host, path, encoding)
    lines = full.splitlines(keepends=True)
    total = len(lines)

    s_idx = max(0, start - 1)
    e_idx = total if end is None else min(total, end)
    if s_idx >= total:
        slice_text = ""
        actual_end = start
    else:
        slice_lines = lines[s_idx:e_idx]
        slice_text = "".join(slice_lines)
        actual_end = s_idx + len(slice_lines)

    return {
        "content": slice_text,
        "file_hash": _sha256(full),
        "range_hash": _sha256(slice_text),
        "start": s_idx + 1 if total else 1,
        "end": actual_end,
        "total_lines": total,
        "encoding": encoding,
    }


async def remote_patch(host: str, path: str, file_hash: str,
                       patches: List[Dict[str, Any]],
                       encoding: str = "utf-8",
                       auto_newline: bool = False) -> Dict[str, Any]:
    """Apply patches to a remote file with hash-based conflict detection.

    Each patch dict must include: start, end (or null), contents, range_hash.

    Args:
        auto_newline: When ``True``, ensure each ``contents`` ends with
            ``\\n`` if (a) the existing slice it replaces ended with one and
            (b) ``contents`` is non-empty. This catches the common LLM
            mistake of passing ``"new line"`` when the surrounding file uses
            POSIX line endings — the silent corruption the upstream
            ``mcp-text-editor`` design notes warn about. Default is
            ``False`` for byte-for-byte compatibility with the upstream
            "callers manage their own line endings" contract; we still
            surface a ``warnings`` field whenever a patch *would* have been
            normalized so the agent can diagnose mysterious "two lines
            merged into one" reports without re-running.

    Returns on success:  ``{"result": "ok", "file_hash": <new sha256>,
                           "warnings": [str, ...]}``
    Returns on conflict: ``{"result": "error", "reason": ..., ...}``
    """
    if not patches:
        return {"result": "error", "reason": "patches list is empty"}

    # 1) Re-read remote file and verify whole-file hash
    full = await _read_full(host, path, encoding)
    current_hash = _sha256(full)
    if not _hash_eq(current_hash, file_hash):
        return {
            "result": "error",
            "reason": "Content hash mismatch — file was modified after you read it",
            "current_file_hash": current_hash,
            "current_content": full,
            "suggestion": "call portal_read again to get the current hash, then retry",
        }

    lines = full.splitlines(keepends=True)
    total = len(lines)

    # 2) Validate patches and convert to 0-based indices
    parsed: List[Patch] = []
    for raw in patches:
        try:
            p = Patch(
                start=int(raw["start"]),
                end=None if raw.get("end") is None else int(raw["end"]),
                contents=str(raw["contents"]),
                range_hash=str(raw.get("range_hash", "")),
            )
        except (KeyError, TypeError, ValueError) as e:
            return {"result": "error", "reason": f"invalid patch: {e}"}
        if p.start < 1:
            return {"result": "error", "reason": f"patch start must be >= 1 (got {p.start})"}
        parsed.append(p)

    # 3) Sort bottom-to-top so earlier patches' line numbers stay valid
    parsed.sort(key=lambda p: p.start, reverse=True)

    # 4) Detect overlapping patches (after sort: each prev.start should be > current.end)
    for i in range(len(parsed) - 1):
        upper = parsed[i]
        lower = parsed[i + 1]
        upper_start_0 = upper.start - 1
        lower_end_0 = total if lower.end is None else lower.end
        if lower_end_0 > upper_start_0:
            return {
                "result": "error",
                "reason": f"overlapping patches: [{lower.start},{lower.end}] vs [{upper.start},{upper.end}]",
            }

    # 5) Apply each patch with per-range hash check
    new_lines = list(lines)
    warnings: list[str] = []
    for p in parsed:
        s_idx = p.start - 1
        e_idx = total if p.end is None else min(total, p.end)
        if s_idx > total:
            return {
                "result": "error",
                "reason": f"patch start {p.start} is beyond end of file ({total} lines)",
            }
        existing_slice = "".join(lines[s_idx:e_idx])
        if p.range_hash != "" and not _hash_eq(_sha256(existing_slice), p.range_hash):
            return {
                "result": "error",
                "reason": f"range_hash mismatch for [{p.start},{p.end}]",
                "current_range_hash": _sha256(existing_slice),
                "current_range_content": existing_slice,
            }

        # Newline normalization. Two cases trigger a warning / fix:
        #   * ``contents`` is non-empty and lacks a trailing \n
        #   * the slice it replaces *did* end with \n
        # Without the trailing newline, ``splitlines(keepends=True)`` produces
        # a final element with no \n, which when joined with the rest of the
        # file causes the next surviving line to glue onto the patched line.
        contents = p.contents
        needs_newline = (
            contents
            and not contents.endswith("\n")
            and existing_slice.endswith("\n")
        )
        if needs_newline:
            note = (
                f"patch [{p.start},{p.end}] contents missing trailing newline "
                f"but the replaced slice ended with one"
            )
            if auto_newline:
                contents = contents + "\n"
                warnings.append(f"{note}; auto-appended")
            else:
                warnings.append(
                    f"{note}; pass auto_newline=true or include the newline "
                    f"yourself to avoid line gluing"
                )

        new_content_lines = contents.splitlines(keepends=True)
        new_lines[s_idx:e_idx] = new_content_lines

    new_full = "".join(new_lines)

    # 6) Atomic write
    await _atomic_write(host, path, new_full, encoding)

    # 7) Read back and re-hash to confirm write integrity
    written = await _read_full(host, path, encoding)
    written_hash = _sha256(written)
    expected_hash = _sha256(new_full)
    if not _hash_eq(written_hash, expected_hash):
        return {
            "result": "error",
            "reason": "post-write hash verification failed (write may be partial)",
            "expected_file_hash": expected_hash,
            "actual_file_hash": written_hash,
            "warnings": warnings,
        }

    out: Dict[str, Any] = {"result": "ok", "file_hash": written_hash}
    if warnings:
        out["warnings"] = warnings
    return out
