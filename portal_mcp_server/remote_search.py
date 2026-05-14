"""Remote grep / glob using ripgrep (fallback: grep -rn / find) over AsyncSSH.

Tools:
  - remote_grep(host, path, pattern, glob?, type?, ignore_case?, max_count?) -> list of matches
  - remote_glob(host, pattern, path?) -> list of matching file paths

A per-host probe caches whether `rg` is available; everything is structured.
"""
from __future__ import annotations

import json
import logging
import shlex
from typing import Any, Dict, Optional

from .connection_manager import get_manager

logger = logging.getLogger("portal_mcp.remote_search")

# Cache: host -> {"rg": bool, "find": bool}
_TOOL_CACHE: Dict[str, Dict[str, bool]] = {}


async def _probe_tools(host: str) -> Dict[str, bool]:
    if host in _TOOL_CACHE:
        return _TOOL_CACHE[host]
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        r = await conn.run("command -v rg >/dev/null && echo yes || echo no")
        has_rg = r.stdout.strip() == "yes"
        r2 = await conn.run("command -v find >/dev/null && echo yes || echo no")
        has_find = r2.stdout.strip() == "yes"
    finally:
        mgr.release_connection(host, conn)
    _TOOL_CACHE[host] = {"rg": has_rg, "find": has_find}
    logger.info(f"Probed {host}: rg={has_rg} find={has_find}")
    return _TOOL_CACHE[host]


def _q(s: str) -> str:
    return shlex.quote(s)


async def remote_grep(
    host: str,
    path: str,
    pattern: str,
    glob: Optional[str] = None,
    type: Optional[str] = None,
    ignore_case: bool = False,
    max_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Search for pattern under path on remote host.

    Returns:
        {
          "engine": "rg" | "grep",
          "matches": [
            {"file": str, "line": int, "text": str},
            ...
          ],
          "truncated": bool,   # True if max_count cut things off
        }
    """
    tools = await _probe_tools(host)
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        if tools["rg"]:
            cmd_parts = ["rg", "--json", "--no-config"]
            if ignore_case:
                cmd_parts.append("-i")
            if glob:
                cmd_parts += ["--glob", _q(glob)]
            if type:
                cmd_parts += ["--type", _q(type)]
            if max_count is not None:
                cmd_parts += ["--max-count", str(int(max_count))]
            cmd_parts += [_q(pattern), _q(path)]
            cmd = " ".join(cmd_parts)
            r = await conn.run(cmd, check=False)
            matches = []
            for line in r.stdout.splitlines():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "match":
                    continue
                d = obj["data"]
                file_path = d["path"].get("text") or d["path"].get("bytes", "")
                line_no = d.get("line_number")
                text = d["lines"].get("text", "").rstrip("\n")
                matches.append({"file": file_path, "line": line_no, "text": text})
            return {"engine": "rg", "matches": matches,
                    "truncated": max_count is not None and len(matches) >= max_count}
        # Fallback: grep -rn
        cmd_parts = ["grep", "-rn"]
        if ignore_case:
            cmd_parts.append("-i")
        if glob:
            cmd_parts += ["--include", _q(glob)]
        if max_count is not None:
            cmd_parts += [f"-m{int(max_count)}"]
        cmd_parts += ["--", _q(pattern), _q(path)]
        cmd = " ".join(cmd_parts)
        r = await conn.run(cmd, check=False)
        matches = []
        for line in r.stdout.splitlines():
            # format: file:line:text
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            try:
                line_no = int(parts[1])
            except ValueError:
                continue
            matches.append({"file": parts[0], "line": line_no, "text": parts[2]})
        return {"engine": "grep", "matches": matches,
                "truncated": max_count is not None and len(matches) >= max_count}
    finally:
        mgr.release_connection(host, conn)


async def remote_glob(host: str, pattern: str, path: Optional[str] = None) -> Dict[str, Any]:
    """List files matching glob pattern on remote host.

    pattern: glob expression (e.g. "**/*.py")
    path: directory to search under (default: cwd via shell)

    Implementation: prefer `rg --files | rg <pattern>` (fast); else `find`.
    """
    tools = await _probe_tools(host)
    base = path or "."
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        if tools["rg"]:
            # rg --files lists every file rg would search; pipe into rg with the
            # provided pattern (anchored as a substring/glob match).
            cmd = (
                f"cd {_q(base)} && "
                f"rg --files --no-config 2>/dev/null | "
                f"rg --no-config --fixed-strings -- {_q(pattern)} || true"
            )
            # If user passes a real glob (with * or ?) prefer find for accuracy
            if any(ch in pattern for ch in "*?["):
                cmd = (
                    f"cd {_q(base)} && "
                    f"find . -type f -path {_q(pattern)} 2>/dev/null"
                )
                engine = "find"
            else:
                engine = "rg-files"
        else:
            cmd = f"cd {_q(base)} && find . -type f -name {_q(pattern)} 2>/dev/null"
            engine = "find"
        r = await conn.run(cmd, check=False)
        files = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        return {"engine": engine, "files": files, "base": base}
    finally:
        mgr.release_connection(host, conn)
