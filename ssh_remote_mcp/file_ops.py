"""
File Operations — SFTP-based file management on remote hosts.
"""
import asyncio
import os
import logging
from pathlib import Path
from typing import Optional

from .connection_manager import get_manager

logger = logging.getLogger("ssh_mcp.files")


async def _sftp(host_name: str):
    """Context manager helper returning (conn, sftp)."""
    mgr = get_manager()
    conn = await mgr.get_connection(host_name)
    return conn, await conn.start_sftp_client()


async def ssh_upload_file(host_name: str, local_path: str, remote_path: str) -> str:
    """Upload a local file to the remote host via SFTP."""
    if not Path(local_path).exists():
        return f"Local file not found: {local_path}"
    conn, sftp = await _sftp(host_name)
    try:
        async with sftp:
            await sftp.put(local_path, remote_path)
        return f"Uploaded {local_path} → {host_name}:{remote_path}"
    except Exception as e:
        return f"Upload failed: {e}"


async def ssh_download_file(host_name: str, remote_path: str, local_path: str) -> str:
    """Download a remote file to local disk via SFTP."""
    conn, sftp = await _sftp(host_name)
    try:
        async with sftp:
            await sftp.get(remote_path, local_path)
        return f"Downloaded {host_name}:{remote_path} → {local_path}"
    except Exception as e:
        return f"Download failed: {e}"


async def ssh_list_directory(host_name: str, remote_path: str = ".") -> list[dict]:
    """List contents of a remote directory."""
    conn, sftp = await _sftp(host_name)
    try:
        async with sftp:
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

import asyncssh  # noqa: F811

async def ssh_read_file(host_name: str, remote_path: str, max_bytes: int = 524288) -> str:
    """Read a remote file's contents (up to max_bytes)."""
    conn, sftp = await _sftp(host_name)
    try:
        async with sftp:
            async with sftp.open(remote_path, "r") as f:
                content = await f.read(max_bytes)
        return content if isinstance(content, str) else content.decode("utf-8", errors="replace")
    except Exception as e:
        return f"Read failed: {e}"


async def ssh_write_file(host_name: str, remote_path: str, content: str) -> str:
    """Write content to a remote file."""
    conn, sftp = await _sftp(host_name)
    try:
        async with sftp:
            async with sftp.open(remote_path, "w") as f:
                await f.write(content)
        return f"Written {len(content)} bytes to {host_name}:{remote_path}"
    except Exception as e:
        return f"Write failed: {e}"


async def ssh_delete_file(host_name: str, remote_path: str) -> str:
    """Delete a remote file."""
    conn, sftp = await _sftp(host_name)
    try:
        async with sftp:
            await sftp.remove(remote_path)
        return f"Deleted {host_name}:{remote_path}"
    except Exception as e:
        return f"Delete failed: {e}"


async def ssh_sync_directory(host_name: str, local_dir: str, remote_dir: str) -> str:
    """Recursively sync a local directory to a remote directory (upload)."""
    local = Path(local_dir)
    if not local.is_dir():
        return f"Local directory not found: {local_dir}"
    conn, sftp = await _sftp(host_name)
    uploaded = 0
    errors = []
    try:
        async with sftp:
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
