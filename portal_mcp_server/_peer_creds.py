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
