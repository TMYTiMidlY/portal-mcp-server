"""Fail-closed decision matrix for the Windows named-pipe peer-SID check.

The real Win32 SID resolution is exercised by the windows-latest CI job (and
the manual Windows smoke test); here we validate the pure decision logic on any
platform by forcing win32 and stubbing the two resolver helpers. The invariant:
only a *proven* same-user peer is allowed; anything that prevents proving it
(peer pid or token SID unreadable) denies — except failing to read our OWN SID,
which degrades to allow (a local bug, not a cross-user signal).
"""
from __future__ import annotations

import pytest

from portal_mcp_server import _peer_creds as pc


@pytest.mark.parametrize("my_sid,peer_pid,peer_sid,expected", [
    ("S-1-5-21-me", 1234, "S-1-5-21-me", True),      # same user -> allow
    ("S-1-5-21-me", 1234, "S-1-5-21-other", False),  # different user -> deny
    ("S-1-5-21-me", 1234, None, False),              # peer token unreadable -> deny
    ("S-1-5-21-me", None, "ignored", False),         # peer pid unresolved -> deny
    (None, 1234, "ignored", True),                   # own SID unresolved -> allow
])
def test_peer_check_fail_closed(monkeypatch, my_sid, peer_pid, peer_sid, expected):
    monkeypatch.setattr(pc.sys, "platform", "win32")
    monkeypatch.setattr(pc, "_win_named_pipe_peer_pid", lambda h, role: peer_pid)
    monkeypatch.setattr(
        pc, "_win_process_user_sid",
        lambda pid: my_sid if pid is None else peer_sid)
    assert pc.is_same_user_named_pipe_peer(0, role="server") is expected


def test_peer_check_noop_off_windows(monkeypatch):
    monkeypatch.setattr(pc.sys, "platform", "linux")
    assert pc.is_same_user_named_pipe_peer(0, role="server") is True
