"""Remote text editor with hash-based concurrent-modification detection.

Design pattern (read → hash → patch → re-check → atomic-write) is adapted from
tumf/mcp-text-editor (MIT). This module re-implements it on top of AsyncSSH
SFTP so all I/O happens against the remote host through a single connection.

Tools exposed:
  - remote_read(host, path, start?, end?, encoding?) -> {content, hash, range_hash, total_lines}
  - remote_patch(host, path, file_hash, patches[], encoding?) -> {result, hash} | error

Patches are applied bottom-to-top to keep line numbers stable. File writes use
``<path>.tmp.<uuid>`` followed by SFTP ``rename``. The new content is read back
and re-hashed before reporting success, providing a defense-in-depth check
against silently truncated writes.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncssh

from .connection_manager import get_manager

logger = logging.getLogger("ssh_mcp.remote_editor")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Patch:
    start: int  # 1-based inclusive
    end: Optional[int]  # 1-based inclusive; None = end of file
    contents: str  # new content (must end with newline if you want one)
    range_hash: str  # SHA-256 of the slice being replaced (empty for pure inserts)


class RemoteEditError(Exception):
    """Raised by remote_patch on hash mismatch or unrecoverable I/O failure."""

    def __init__(self, message: str, current_hash: Optional[str] = None,
                 current_content: Optional[str] = None):
        super().__init__(message)
        self.current_hash = current_hash
        self.current_content = current_content


async def _read_full(host: str, path: str, encoding: str) -> str:
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        sftp = await conn.start_sftp_client()
        try:
            async with sftp.open(path, "r", encoding=encoding) as f:
                return await f.read()
        finally:
            sftp.exit()
    finally:
        mgr.release_connection(host, conn)


async def _atomic_write(host: str, path: str, new_content: str, encoding: str) -> None:
    """Write atomically via tmp + rename on the remote host."""
    tmp_path = f"{path}.tmp.{uuid.uuid4().hex[:12]}"
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        sftp = await conn.start_sftp_client()
        try:
            async with sftp.open(tmp_path, "w", encoding=encoding) as f:
                await f.write(new_content)
            try:
                await sftp.posix_rename(tmp_path, path)
            except (asyncssh.SFTPOpUnsupported, AttributeError):
                try:
                    await sftp.remove(path)
                except asyncssh.SFTPNoSuchFile:
                    pass
                await sftp.rename(tmp_path, path)
        except Exception:
            try:
                await sftp.remove(tmp_path)
            except Exception:
                pass
            raise
        finally:
            sftp.exit()
    finally:
        mgr.release_connection(host, conn)


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
                       encoding: str = "utf-8") -> Dict[str, Any]:
    """Apply patches to a remote file with hash-based conflict detection.

    Each patch dict must include: start, end (or null), contents, range_hash.

    Returns on success:  {"result": "ok", "file_hash": <new sha256>}
    Returns on conflict: {"result": "error", "reason": ..., "current_file_hash": ..., "current_content": ...}
    """
    if not patches:
        return {"result": "error", "reason": "patches list is empty"}

    # 1) Re-read remote file and verify whole-file hash
    full = await _read_full(host, path, encoding)
    current_hash = _sha256(full)
    if current_hash != file_hash:
        return {
            "result": "error",
            "reason": "Content hash mismatch — file was modified after you read it",
            "current_file_hash": current_hash,
            "current_content": full,
            "suggestion": "call remote_read again to get the current hash, then retry",
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
    for p in parsed:
        s_idx = p.start - 1
        e_idx = total if p.end is None else min(total, p.end)
        if s_idx > total:
            return {
                "result": "error",
                "reason": f"patch start {p.start} is beyond end of file ({total} lines)",
            }
        existing_slice = "".join(lines[s_idx:e_idx])
        if p.range_hash != "" and _sha256(existing_slice) != p.range_hash:
            return {
                "result": "error",
                "reason": f"range_hash mismatch for [{p.start},{p.end}]",
                "current_range_hash": _sha256(existing_slice),
                "current_range_content": existing_slice,
            }
        new_content_lines = p.contents.splitlines(keepends=True)
        # If contents was non-empty but lacks trailing newline, do not silently add one;
        # callers should manage their own line endings. This matches mcp-text-editor.
        new_lines[s_idx:e_idx] = new_content_lines

    new_full = "".join(new_lines)

    # 6) Atomic write
    await _atomic_write(host, path, new_full, encoding)

    # 7) Read back and re-hash to confirm write integrity
    written = await _read_full(host, path, encoding)
    written_hash = _sha256(written)
    expected_hash = _sha256(new_full)
    if written_hash != expected_hash:
        return {
            "result": "error",
            "reason": "post-write hash verification failed (write may be partial)",
            "expected_file_hash": expected_hash,
            "actual_file_hash": written_hash,
        }

    return {"result": "ok", "file_hash": written_hash}
