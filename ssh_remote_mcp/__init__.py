# ssh_remote_mcp/__init__.py
"""portal-mcp-server — Agent-feels-local SSH orchestration MCP server.

The Python package is still importable as ``ssh_remote_mcp`` for
backward compatibility (and because renaming a Python package on PyPI
is more disruptive than renaming the CLI/repo).
"""
from .cli import main, mcp

__all__ = ["main", "mcp"]

