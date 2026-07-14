"""HTTP transport bind hardening: loopback by default; a non-loopback bind
without PORTAL_AUTH_TOKEN is refused rather than silently exposing every MCP
tool unauthenticated."""
from __future__ import annotations

import sys

import pytest

from portal_mcp_server import cli


def test_is_loopback_host():
    for h in ("127.0.0.1", "::1", "localhost", "127.0.0.5", "LOCALHOST"):
        assert cli._is_loopback_host(h), h
    for h in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.1", ""):
        assert not cli._is_loopback_host(h), h


def test_default_http_bind_is_loopback():
    # the --host default must be loopback so an accidental HTTP start is local
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    assert cli._is_loopback_host(p.parse_args([]).host)


def test_http_refuses_nonloopback_without_token(monkeypatch):
    monkeypatch.delenv("PORTAL_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        sys, "argv",
        ["portal-mcp-server", "--transport", "streamable_http", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert "non-loopback" in str(ei.value)
