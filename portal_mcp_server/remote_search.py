"""Remote grep / glob over AsyncSSH, modelled on Claude Code's Grep/Glob tools.

The schema, defaults, and output shapes are ported from Claude Code so an agent
already trained on those tools uses these correctly zero-shot — but the cryptic
CC flag names (``-A``/``-B``/``-C``/``-i``/``-n``) are replaced with clear,
self-describing parameter names (``before_context`` / ``after_context`` /
``context`` / ``ignore_case`` …). Execution shells out to ``rg`` on the remote
host (fallback ``grep`` / ``find``); everything is returned as structured JSON.

Tools
-----
* ``remote_grep`` — regex search with three output modes:
    - ``files_with_matches`` (default): matching file paths, newest first.
    - ``content``: matching lines (+ optional context), with a TOTAL output cap
      (``head_limit``) and ``offset`` pagination.
    - ``count``: per-file match-line counts plus a grand total.
  Respects ``.gitignore`` (rg's default), like CC Grep.
* ``remote_glob`` — list files matching a glob, newest-first, hard-capped at
  100 with a ``truncated`` flag. Does NOT respect ``.gitignore`` (like CC Glob).

A per-host probe caches whether ``rg`` is available.
"""
from __future__ import annotations

import json
import logging
import shlex
import time
from typing import Any, Dict, List, Optional

from .connection_manager import DEFAULT_DECODE_ERRORS, get_manager

logger = logging.getLogger("portal_mcp.remote_search")

# Cache: host -> {"rg": bool, "find": bool}
_TOOL_CACHE: Dict[str, Dict[str, bool]] = {}

# Defaults ported from Claude Code.
DEFAULT_GREP_HEAD_LIMIT = 250
GLOB_HARD_CAP = 100


async def _probe_tools(host: str) -> Dict[str, bool]:
    if host in _TOOL_CACHE:
        return _TOOL_CACHE[host]
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        r = await conn.run("command -v rg >/dev/null && echo yes || echo no",
                           errors=DEFAULT_DECODE_ERRORS)
        has_rg = r.stdout.strip() == "yes"
        r2 = await conn.run("command -v find >/dev/null && echo yes || echo no",
                            errors=DEFAULT_DECODE_ERRORS)
        has_find = r2.stdout.strip() == "yes"
    finally:
        mgr.release_connection(host, conn)
    _TOOL_CACHE[host] = {"rg": has_rg, "find": has_find}
    logger.info(f"Probed {host}: rg={has_rg} find={has_find}")
    return _TOOL_CACHE[host]


def _q(s: str) -> str:
    return shlex.quote(s)


def _relativize(file_path: str, base: str) -> str:
    """Strip a leading ``base/`` (or ``./``) prefix so paths stay short."""
    for prefix in (base.rstrip("/") + "/", "./"):
        if prefix != "/" and file_path.startswith(prefix):
            return file_path[len(prefix):]
    return file_path


def _rg_filters(glob: Optional[str], file_type: Optional[str],
                ignore_case: bool, multiline: bool) -> List[str]:
    parts: List[str] = []
    if ignore_case:
        parts.append("-i")
    if multiline:
        parts.append("--multiline")
    if glob:
        parts += ["--glob", _q(glob)]
    if file_type:
        parts += ["--type", _q(file_type)]
    return parts


async def remote_grep(
    host: str,
    pattern: str,
    path: str = ".",
    *,
    glob: Optional[str] = None,
    file_type: Optional[str] = None,
    output_mode: str = "files_with_matches",
    ignore_case: bool = False,
    before_context: int = 0,
    after_context: int = 0,
    context: int = 0,
    head_limit: int = DEFAULT_GREP_HEAD_LIMIT,
    offset: int = 0,
    multiline: bool = False,
) -> Dict[str, Any]:
    """Search ``pattern`` under ``path`` on the remote host.

    Returns a dict whose shape depends on ``output_mode``:

      files_with_matches -> {output_mode, engine, files:[str], num_files,
                             truncated}
      content            -> {output_mode, engine, matches:[{file,line,text,
                             context?}], total, returned, offset, truncated}
      count              -> {output_mode, engine, counts:[{file,count}],
                             total_matches, num_files, truncated}
    """
    tools = await _probe_tools(host)
    base = path or "."
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    try:
        if tools["rg"]:
            return await _rg_grep(
                conn, pattern, base, glob=glob, file_type=file_type,
                output_mode=output_mode, ignore_case=ignore_case,
                before_context=before_context, after_context=after_context,
                context=context, head_limit=head_limit, offset=offset,
                multiline=multiline,
            )
        return await _grep_fallback(
            conn, pattern, base, glob=glob, output_mode=output_mode,
            ignore_case=ignore_case, before_context=before_context,
            after_context=after_context, context=context,
            head_limit=head_limit, offset=offset,
        )
    finally:
        mgr.release_connection(host, conn)


async def _rg_grep(conn, pattern, base, *, glob, file_type, output_mode,
                   ignore_case, before_context, after_context, context,
                   head_limit, offset, multiline) -> Dict[str, Any]:
    filters = _rg_filters(glob, file_type, ignore_case, multiline)

    if output_mode == "files_with_matches":
        # rg has no descending file sort, so we sort ascending by mtime and
        # reverse to put the newest file first.
        parts = ["rg", "--no-config", "-l", "--sort", "modified", *filters,
                 "--", _q(pattern), _q(base)]
        r = await conn.run(" ".join(parts), check=False,
                           errors=DEFAULT_DECODE_ERRORS)
        files = [_relativize(ln.strip(), base)
                 for ln in r.stdout.splitlines() if ln.strip()]
        files.reverse()  # newest first
        total = len(files)
        page = files[offset:offset + head_limit] if head_limit else files[offset:]
        return {"output_mode": output_mode, "engine": "rg",
                "files": page, "num_files": total,
                "truncated": offset + len(page) < total}

    if output_mode == "count":
        parts = ["rg", "--no-config", "-c", *filters,
                 "--", _q(pattern), _q(base)]
        r = await conn.run(" ".join(parts), check=False,
                           errors=DEFAULT_DECODE_ERRORS)
        counts: List[dict] = []
        for ln in r.stdout.splitlines():
            sep = ln.rfind(":")  # format: file:count
            if sep == -1:
                continue
            try:
                n = int(ln[sep + 1:])
            except ValueError:
                continue
            counts.append({"file": _relativize(ln[:sep], base), "count": n})
        total_matches = sum(c["count"] for c in counts)
        page = counts[offset:offset + head_limit] if head_limit else counts[offset:]
        return {"output_mode": output_mode, "engine": "rg",
                "counts": page, "total_matches": total_matches,
                "num_files": len(counts),
                "truncated": offset + len(page) < len(counts)}

    # content: parse rg --json, treat head_limit as a TOTAL output-line cap.
    ctx_flags: List[str] = []
    if context:
        ctx_flags += ["-C", str(int(context))]
    else:
        if before_context:
            ctx_flags += ["-B", str(int(before_context))]
        if after_context:
            ctx_flags += ["-A", str(int(after_context))]
    parts = ["rg", "--no-config", "--json", *ctx_flags, *filters,
             "--", _q(pattern), _q(base)]
    r = await conn.run(" ".join(parts), check=False,
                       errors=DEFAULT_DECODE_ERRORS)
    entries = _parse_rg_json(r.stdout, base)
    total = len(entries)
    page = entries[offset:offset + head_limit] if head_limit else entries[offset:]
    return {"output_mode": "content", "engine": "rg",
            "matches": page, "total": total, "returned": len(page),
            "offset": offset, "truncated": offset + len(page) < total}


def _parse_rg_json(stdout: str, base: str) -> List[dict]:
    entries: List[dict] = []
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = obj.get("type")
        if kind not in ("match", "context"):
            continue
        d = obj["data"]
        fp = d["path"].get("text") or d["path"].get("bytes", "")
        entry = {"file": _relativize(fp, base),
                 "line": d.get("line_number"),
                 "text": (d["lines"].get("text", "") or "").rstrip("\n")}
        if kind == "context":
            entry["context"] = True
        entries.append(entry)
    return entries


async def _grep_fallback(conn, pattern, base, *, glob, output_mode,
                         ignore_case, before_context, after_context, context,
                         head_limit, offset) -> Dict[str, Any]:
    """Best-effort grep fallback when ripgrep is absent (no mtime sort)."""
    flags = ["-r"]
    if ignore_case:
        flags.append("-i")
    if glob:
        flags += ["--include", _q(glob)]

    if output_mode == "files_with_matches":
        parts = ["grep", *flags, "-l", "--", _q(pattern), _q(base)]
        r = await conn.run(" ".join(parts), check=False,
                           errors=DEFAULT_DECODE_ERRORS)
        files = [_relativize(ln.strip(), base)
                 for ln in r.stdout.splitlines() if ln.strip()]
        total = len(files)
        page = files[offset:offset + head_limit] if head_limit else files[offset:]
        return {"output_mode": output_mode, "engine": "grep",
                "files": page, "num_files": total,
                "truncated": offset + len(page) < total}

    if output_mode == "count":
        parts = ["grep", *flags, "-c", "--", _q(pattern), _q(base)]
        r = await conn.run(" ".join(parts), check=False,
                           errors=DEFAULT_DECODE_ERRORS)
        counts: List[dict] = []
        for ln in r.stdout.splitlines():
            sep = ln.rfind(":")
            if sep == -1:
                continue
            try:
                n = int(ln[sep + 1:])
            except ValueError:
                continue
            if n > 0:
                counts.append({"file": _relativize(ln[:sep], base), "count": n})
        total_matches = sum(c["count"] for c in counts)
        page = counts[offset:offset + head_limit] if head_limit else counts[offset:]
        return {"output_mode": output_mode, "engine": "grep",
                "counts": page, "total_matches": total_matches,
                "num_files": len(counts),
                "truncated": offset + len(page) < len(counts)}

    # content
    if context:
        flags += [f"-C{int(context)}"]
    else:
        if before_context:
            flags += [f"-B{int(before_context)}"]
        if after_context:
            flags += [f"-A{int(after_context)}"]
    parts = ["grep", *flags, "-n", "--", _q(pattern), _q(base)]
    r = await conn.run(" ".join(parts), check=False,
                       errors=DEFAULT_DECODE_ERRORS)
    entries: List[dict] = []
    for ln in r.stdout.splitlines():
        parts3 = ln.split(":", 2)  # file:line:text
        if len(parts3) < 3:
            continue
        try:
            line_no = int(parts3[1])
        except ValueError:
            continue
        entries.append({"file": _relativize(parts3[0], base),
                        "line": line_no, "text": parts3[2]})
    total = len(entries)
    page = entries[offset:offset + head_limit] if head_limit else entries[offset:]
    return {"output_mode": "content", "engine": "grep",
            "matches": page, "total": total, "returned": len(page),
            "offset": offset, "truncated": offset + len(page) < total}


async def remote_glob(host: str, pattern: str, path: str = ".") -> Dict[str, Any]:
    """List files matching ``pattern`` (a glob) under ``path``, newest first.

    Modelled on Claude Code's Glob: returns ``{filenames, num_files,
    truncated, duration_ms}`` with a hard cap of 100 files and a ``truncated``
    flag. Does NOT respect ``.gitignore`` (CC Glob's behaviour). ``rg``'s ``-g``
    understands ``**``; ``--sort modified`` (reversed) gives newest-first.
    """
    tools = await _probe_tools(host)
    base = path or "."
    mgr = get_manager()
    conn = await mgr.get_connection(host)
    t0 = time.monotonic()
    try:
        if tools["rg"]:
            # --no-ignore: a glob should not be filtered by .gitignore.
            cmd = (f"cd {_q(base)} && rg --files --no-config --no-ignore "
                   f"--sort modified -g {_q(pattern)} 2>/dev/null || true")
            engine = "rg"
        elif tools["find"]:
            # Best-effort: translate the glob to a find -path match.
            cmd = (f"cd {_q(base)} && find . -type f -path {_q('*' + pattern)} "
                   f"2>/dev/null || true")
            engine = "find"
        else:
            return {"filenames": [], "num_files": 0, "truncated": False,
                    "duration_ms": 0, "engine": "none",
                    "error": "neither rg nor find available on host"}
        r = await conn.run(cmd, check=False, errors=DEFAULT_DECODE_ERRORS)
        files = [ln.strip().lstrip("./") for ln in r.stdout.splitlines()
                 if ln.strip()]
        if engine == "rg":
            files.reverse()  # rg sorts ascending mtime; we want newest first
        total = len(files)
        truncated = total > GLOB_HARD_CAP
        page = files[:GLOB_HARD_CAP]
        return {"filenames": page, "num_files": total, "truncated": truncated,
                "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                "engine": engine, "base": base}
    finally:
        mgr.release_connection(host, conn)
