"""Same-uid peer-credential check for Unix credential agent sockets.

This module enforces the assertion documented in SECURITY.md: every inbound
*and* outbound connection on the portal-mcp-server credential agent socket
must run under the same uid as the agent process. The check is a defence-in-depth
layer on top of the permissions enforced by the systemd user socket unit and
the socket itself: even if a hostile local user manages to make their own socket
land at the expected path, this layer refuses the exchange before any secret is
read or written.

On Linux we use ``SO_PEERCRED``, which returns the peer's ``struct ucred``
``(pid, uid, gid)``. On platforms that do not expose a comparable API in a
straightforward way (notably macOS, where ``LOCAL_PEERCRED`` returns an
``xucred`` with a layout that varies by SDK version), we degrade to "allow"
and rely on the underlying filesystem permissions — which is the exact
behaviour the codebase had before this check existed, so this is no
regression.

On Windows the agent speaks over a named pipe instead of a Unix socket;
:func:`is_same_user_named_pipe_peer` resolves the peer process's user SID
(via ``GetNamedPipe{Client,Server}ProcessId`` → token → SID) and compares it
to the current user's. It **fails open** (returns ``True``) on any error, so a
bug in the Win32 plumbing can never break the same-user happy path — it only
adds a reject when it can positively prove a cross-user peer.
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import sys
from typing import Optional

logger = logging.getLogger("portal_mcp.peer_creds")

# struct ucred { pid_t pid; uid_t uid; gid_t gid; } on Linux — three 4-byte ints.
_UCRED_FMT = "3i"
_UCRED_SIZE = struct.calcsize(_UCRED_FMT)


def peer_uid(sock: socket.socket) -> Optional[int]:
    """Return the peer process's effective uid on a connected Unix socket,
    or ``None`` if the platform does not expose peer credentials.
    """
    if sys.platform == "linux":
        try:
            buf = sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED_SIZE
            )
            _pid, uid, _gid = struct.unpack(_UCRED_FMT, buf)
            return uid
        except OSError:
            return None
    return None


def is_same_uid_peer(sock: socket.socket) -> bool:
    """Return True iff the peer runs under this process's uid (or the OS does
    not expose peer credentials, in which case the caller falls back to
    filesystem-permission enforcement on the socket path).
    """
    uid = peer_uid(sock)
    if uid is None:
        return True
    return uid == os.getuid()


# ── Windows named-pipe peer verification ─────────────────────────────────────
# The agent uses a named pipe on Windows (no SO_PEERCRED). We resolve the peer
# process's user SID and compare it to ours. Everything here FAILS OPEN: any
# Win32 error degrades to "allow", so this can only ever *add* a reject for a
# provably cross-user peer — it can never break the same-user happy path.

def is_same_user_named_pipe_peer(handle: int, *, role: str) -> bool:
    """True iff the named pipe's peer runs as the current Windows user.

    ``role="server"`` checks the connected client (``GetNamedPipeClientProcessId``);
    ``role="client"`` checks the server (``GetNamedPipeServerProcessId``).
    Non-Windows or any failure → ``True`` (degrade to ACL / name scoping).
    ``handle`` is the OS pipe HANDLE as an int.
    """
    if sys.platform != "win32":
        return True
    try:
        peer_pid = _win_named_pipe_peer_pid(handle, role)
        if peer_pid is None:
            return True
        peer_sid = _win_process_user_sid(peer_pid)
        my_sid = _win_process_user_sid(None)  # None => current process
        if not peer_sid or not my_sid:
            return True
        return peer_sid == my_sid
    except Exception:  # pragma: no cover - win32-only; fail open
        logger.debug("named-pipe peer check failed; degrading to allow",
                     exc_info=True)
        return True


def _win_named_pipe_peer_pid(handle: int, role: str):  # pragma: no cover - win32 only
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = (k32.GetNamedPipeClientProcessId if role == "server"
          else k32.GetNamedPipeServerProcessId)
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    fn.restype = wintypes.BOOL
    pid = wintypes.DWORD(0)
    if not fn(wintypes.HANDLE(handle), ctypes.byref(pid)):
        return None
    return pid.value


def _win_process_user_sid(pid):  # pragma: no cover - win32 only
    """Return the user SID string for ``pid`` (or the current process if pid is
    None), or ``None`` on any failure."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL

    if pid is None:
        hproc = k32.GetCurrentProcess()
        close_proc = False
    else:
        hproc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        close_proc = True
    if not hproc:
        return None
    try:
        htok = wintypes.HANDLE()
        if not advapi.OpenProcessToken(hproc, TOKEN_QUERY, ctypes.byref(htok)):
            return None
        try:
            length = wintypes.DWORD(0)
            advapi.GetTokenInformation(htok, TokenUser, None, 0, ctypes.byref(length))
            if length.value == 0:
                return None
            buf = ctypes.create_string_buffer(length.value)
            if not advapi.GetTokenInformation(
                    htok, TokenUser, buf, length, ctypes.byref(length)):
                return None
            # TOKEN_USER's first member is SID_AND_ATTRIBUTES { PSID Sid; ... };
            # the PSID is the first pointer in the buffer.
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            return _win_sid_to_string(psid)
        finally:
            k32.CloseHandle(htok)
    finally:
        if close_proc:
            k32.CloseHandle(hproc)


def _win_sid_to_string(psid):  # pragma: no cover - win32 only
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    k32.LocalFree.argtypes = [wintypes.HANDLE]
    out = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(psid, ctypes.byref(out)):
        return None
    try:
        return out.value
    finally:
        k32.LocalFree(out)
