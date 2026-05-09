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
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import asyncssh

from .connection_manager import get_manager
from .safety import validate_remote_path

logger = logging.getLogger("ssh_mcp.files")


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


async def ssh_upload_file(host_name: str, local_path: str, remote_path: str) -> str:
    """Upload a local file to the remote host via SFTP."""
    try:
        validate_remote_path(remote_path)
    except ValueError as e:
        return f"Invalid remote_path: {e}"
    if not Path(local_path).exists():
        return f"Local file not found: {local_path}"
    try:
        async with _sftp_session(host_name) as sftp:
            await sftp.put(local_path, remote_path)
        return f"Uploaded {local_path} → {host_name}:{remote_path}"
    except Exception as e:
        return f"Upload failed: {e}"


async def ssh_download_file(host_name: str, remote_path: str, local_path: str) -> str:
    """Download a remote file to local disk via SFTP."""
    try:
        validate_remote_path(remote_path)
    except ValueError as e:
        return f"Invalid remote_path: {e}"
    try:
        async with _sftp_session(host_name) as sftp:
            await sftp.get(remote_path, local_path)
        return f"Downloaded {host_name}:{remote_path} → {local_path}"
    except Exception as e:
        return f"Download failed: {e}"


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


async def ssh_sync_directory(host_name: str, local_dir: str, remote_dir: str) -> str:
    """Recursively sync a local directory to a remote directory (upload)."""
    try:
        validate_remote_path(remote_dir)
    except ValueError as e:
        return f"Invalid remote_dir: {e}"
    local = Path(local_dir)
    if not local.is_dir():
        return f"Local directory not found: {local_dir}"
    uploaded = 0
    errors = []
    try:
        async with _sftp_session(host_name) as sftp:
            for local_file in local.rglob("*"):
                if local_file.is_file():
                    rel = local_file.relative_to(local)
                    remote_file = f"{remote_dir}/{rel}".replace("\\", "/")
                    remote_parent = str(Path(remote_file).parent).replace("\\", "/")
                    try:
                        await sftp.makedirs(remote_parent, exist_ok=True)
                        await sftp.put(str(local_file), remote_file)
                        uploaded += 1
                    except Exception as e:
                        errors.append(f"{rel}: {e}")
    except Exception as e:
        return f"Sync failed: {e}"
    msg = f"Synced {uploaded} files to {host_name}:{remote_dir}"
    if errors:
        msg += f"\nErrors ({len(errors)}): " + "; ".join(errors[:5])
    return msg
