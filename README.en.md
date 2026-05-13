# portal-mcp-server

> **Now you're thinking with portals.**
> Agent-feels-local SSH orchestration MCP server for coding agents (Claude Code / Copilot CLI / Cursor …).
> 18 tools, AsyncSSH + FastMCP, SHA-256 conflict-protected remote edits, cross-tool connection reuse, identical performance on Windows / macOS / Linux.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen)](https://modelcontextprotocol.io/)

🌐 **Language**: [中文](./README.md)（默认） · **English**

---

## Contents

- [What it is](#what-it-is)
- [Why 18 tools instead of 57](#why-18-tools-instead-of-57)
- [The 18 tools](#the-18-tools)
- [Why this design](#why-this-design)
- [Install](#install)
- [Register with your agent](#register-with-your-agent)
- [Configuration](#configuration)
- [Security](#security)
- [Testing](#testing)
- ["Why doesn't my local change take effect?"](#why-doesnt-my-local-change-take-effect)
- [Attribution & license](#attribution--license)

---

## What it is

`portal-mcp-server` is forked from [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0). The lower-level SSH/asyncssh engine, connection pool, tunnel manager, multi-host orchestrator, and security policy are inherited from the upstream modules. The upper layer is a fresh, **agent-first 18-tool surface**:

- **2** hash-protected remote file editing tools (`portal_read` / `portal_patch`), with the SHA-256 conflict-detection algorithm referenced from [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT, clean-room reimplementation)
- **6** core IO / search / persistent bash tools
- **10** higher-level tools consolidated via a single `mode` parameter

See [`NOTICE`](./NOTICE) and [`SECURITY.md`](./SECURITY.md) for the full provenance and security posture.

---

## Why 18 tools instead of 57

Anthropic's [_Writing Tools for Agents_](https://www.anthropic.com/engineering/writing-tools-for-agents) is explicit:
> "More tools don't always lead to better outcomes... Tools that merely wrap existing software functionality is a common error... Too many tools or overlapping tools can also distract agents from pursuing efficient strategies."

The upstream `ssh-shell-mcp` exposes one tool per ergonomic — `ssh_run` / `ssh_run_batch` / `ssh_run_script` / `ssh_run_with_env` / `ssh_session_exec` / `ssh_ps` / `ssh_kill` / `ssh_df` / `ssh_free` / `ssh_journalctl` / `ssh_docker` / `ssh_tmux_*` … — **57 tools total**. Most are one-line bash wrappers that **`portal_bash` (a persistent bash session) replaces by itself**.

The portal-mcp-server tradeoff:

| Bucket | Count | What we did |
|---|---:|---|
| **Kept and redesigned** | 8 portal core | `portal_read` + `portal_patch` use SHA-256 hashes to fix the concurrency hole in raw cat/write; `portal_grep` / `portal_glob` give structured search output; `portal_bash`(`_close`/`_status`) provide a persistent shell; `portal_cleanup_tmps` handles interrupted writes |
| **Mode-flag merged** | 10 portal high-level | `portal_tunnel_open(mode=local\|reverse\|socks)` replaces 3 upstream tools; `portal_multi_exec(mode=parallel\|rolling\|broadcast)` replaces 4; `portal_audit(view=...)` collapses 4 introspection endpoints; etc. |
| **Removed entirely** | 27 | All trivially expressible as `portal_bash` invocations: 5 exec-family, 6 multi-session-family, 7 sysinfo (ps/df/free/journalctl/info/netstat/service), 5 process-management, 4 tmux |

Result: tool-list context drops from ~7.5k tokens to ~2.5k, and the agent no longer has to disambiguate between semantically overlapping tools.

---

## The 18 tools

### 8 portal core (preferred entry points)

| Tool | What the agent gets |
|---|---|
| `portal_read` / `portal_patch` | Read remote file with SHA-256 of file + range; patch checks `file_hash` + per-range hash to prevent concurrent overwrite; writes via tmp + `posix_rename` (atomic) and re-hash after write |
| `portal_grep` / `portal_glob` | Remote `rg --json` / `find` with structured output; first-call probe is cached |
| `portal_bash` / `_close` / `_status` | One sticky `bash -i` per host; cwd / env survive across calls; PTY echo + bracketed-paste disabled so sentinel parsing is reliable |
| `portal_cleanup_tmps` | Garbage-collects orphan `*.mcp_tmp.*` files left by interrupted patches |

### 10 portal high-level (mode-switched)

| Tool | mode / params | Purpose |
|---|---|---|
| `portal_host` | `action=list\|register\|remove` | Host registry (for tag-based grouping; `~/.ssh/config` aliases are auto-resolved without registration) |
| `portal_transfer` | `direction=upload\|download\|sync` | SFTP file transfer (binary-safe) |
| `portal_tunnel_open` / `_close` / `_list` | `mode=local\|reverse\|socks` | SSH tunnels (port forward / reverse / SOCKS5) |
| `portal_multi_exec` | `mode=parallel\|rolling\|broadcast`, `hosts_json\|group_tag` | Multi-host command orchestration |
| `portal_playbook` | `host\|group_tag` | Multi-step playbook |
| `portal_ping` | optional `hosts_json` | Health check (single host or whole fleet) |
| `portal_audit` | `view=snapshot\|history\|stats\|policy` | Audit log + server introspection |
| `portal_check` | `host`, optional `command` | Security policy dry-run |

> `~/.ssh/config` aliases are **auto-resolved** — when `get_connection("1810")` doesn't find the host in the registry it auto-registers from `~/.ssh/config`; asyncssh natively handles HostName / User / Port / IdentityFile / ProxyJump.
>
> The companion [`remote` skill](https://github.com/TMYTiMidlY/skills) teaches the agent the read → patch flow, when to default to `/tmp` as a sandbox, and when it should ask first.

---

## Why this design

### A cross-tool, in-process connection pool

portal-mcp-server runs an asyncssh connection pool inside its own server process. Every tool invocation (`portal_bash`, `portal_read`, `portal_transfer`, …) shares the same TCP. **Everything except the first connect amortises down to channel creation (~10–30 ms).**

Compared to the best plain-ssh option (`ControlMaster auto / ControlPersist 10m` in `~/.ssh/config`):

| Dimension | portal-mcp-server | plain ssh + ControlMaster |
|---|---|---|
| Reuse mechanism | asyncssh in-process pool (≤ 5 TCP per host) | OpenSSH master process + Unix domain socket |
| Reuse scope | **process-level** (lives as long as the MCP server) | session-level (default 10-min `ControlPersist`) |
| First connect | TCP + auth (~200–500 ms) | TCP + auth (~200–500 ms) |
| Subsequent commands | reuse pool, open new channel (**~10–30 ms**) | reuse master, open new channel (**~10–30 ms**) |
| Cross-tool reuse | ✅ `portal_bash` and `portal_read` share the same TCP | ❌ `ssh` and `scp` only reuse if both have matching `ControlPath` |
| Persistent shell state | ✅ `portal_bash` keeps `bash -i`; cwd/env survive across calls | ❌ each `ssh host cmd` is a fresh shell; cwd/env discarded |
| Concurrency | true asyncio multi-channel parallelism | one ssh process per command, serial startup (sharing master) |
| Windows | ✅ identical performance everywhere Python runs | ❌ Windows OpenSSH does not support ControlMaster |

> Anonymised microbenchmark: same LAN (< 1ms RTT), 100× `echo pong`. Plain ssh + ControlMaster averaged 23 ms/call; portal-mcp-server through `portal_bash` averaged 18 ms/call (no ssh client process startup). First-connect both ~280 ms (auth dominated).

### Why this matters more on Windows

`ControlMaster` **doesn't work on Windows OpenSSH** — it relies on Unix-domain-socket-based fd sharing between the master and the child ssh processes, and the default Windows OpenSSH build lacks that primitive (the experimental named-pipe support is also unreliable).

portal-mcp-server **doesn't depend on any OS-level socket sharing**. The pool is plain Python objects in the MCP server's own memory (asyncssh is pure Python). Any platform that runs Python (Windows / macOS / Linux) gets **the same reuse performance as Linux**.

```text
On Windows:
  plain ssh:        every command opens a new TCP+auth      → ~300 ms × N
  portal-mcp-server: first ~280 ms, then ~20 ms thereafter   → drops to channel-creation floor
```

Side benefit: pool connections live as long as the MCP server (typically hours), not the 10-minute `ControlPersist` default — fewer reconnect spikes inside long sessions.

### Why asyncssh, not subprocess-wrapped OpenSSH

[asyncssh](https://github.com/ronf/asyncssh) (EPL-2.0 / GPL-2.0 dual-licensed) is an **independent pure-Python SSHv2 implementation**, protocol-equivalent to OpenSSH:

- **One process, many connections, many sessions per connection** — the pool is a Python dict; no process boundaries, no fd sharing required
- **Full protocol coverage** — local/remote/dynamic port forwarding, SFTP, SCP, X11 forwarding, TUN/TAP — anything OpenSSH does at the protocol layer, asyncssh does too
- **OpenSSH-compatible** — natively parses `~/.ssh/config`, `known_hosts`, `authorized_keys`, ssh-agent / Pageant
- **Only depends on PyCA `cryptography`** — install Python and you're done; no C deps, no OS-specific IPC

Compared to "shell out to `ssh` / `scp`":
- No new process per command (saves the ~50–100 ms fork)
- No need to coordinate SSH reuse across multiple OS processes (which is exactly what breaks ControlMaster on Windows)
- Error handling, retries, and timeouts are first-class Python async primitives, not stderr-string parsing

---

## Install

Two paths depending on what you're doing.

### Agent / end user (use the MCP server, never touch the source)

No clone needed — let your MCP client launch it via `uvx` straight from GitHub. See [Register with your agent](#register-with-your-agent) below. `uvx` caches deps on first run; subsequent restarts are instant.

Manual smoke test in a shell:
```bash
uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git portal-mcp-server --help
```

### Developer (will modify code or run tests)

Recommended: `uv sync` will set up `.venv` from `pyproject.toml` + `uv.lock` in one shot:

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras
source .venv/bin/activate
pytest                        # 129 passed, 22 skipped
```

If you'd rather not use uv, plain pip editable install works:
```bash
pip install -e ".[dev]"       # prod + dev (pytest etc.)
# or runtime only
pip install -e .
```

---

## Register with your agent

### Copilot CLI (workspace-level `.mcp.json`)

Copilot CLI natively supports a workspace-level `.mcp.json` (same format as Claude Code / Cursor):

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/TMYTiMidlY/portal-mcp-server.git",
        "portal-mcp-server"
      ]
    }
  }
}
```

Verify:
```bash
cd <project>
copilot mcp list                # → Workspace servers: portal (local)
copilot mcp get portal          # → Source: Workspace (<project>/.mcp.json)
```

> ⚠️ Don't use `copilot mcp add portal -- ...` — it writes to user-level `~/.copilot/mcp-config.json` by default, which leaks into every project. Edit `.mcp.json` directly to keep it project-scoped.

### VS Code (`.vscode/mcp.json`)

VS Code uses a different schema (top-level key is `servers`, not `mcpServers`):

```json
{
  "servers": {
    "portal": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/TMYTiMidlY/portal-mcp-server.git",
        "portal-mcp-server"
      ]
    }
  }
}
```

> The two formats are not interchangeable. If you use both Copilot CLI and VS Code you'll need to maintain both files.

### Companion skill

Install the `remote` skill from [TMYTiMidlY/skills](https://github.com/TMYTiMidlY/skills) (follow the `manage-skills` flow to symlink it into `<target>/.agents/skills/`). The agent will then automatically follow the read → hash-check → patch flow and the `/tmp`-by-default sandbox rule when it sees instructions like "do X on host 1810…".

---

## Configuration

| Env var | Meaning | Default |
|---|---|---|
| `SSH_HOSTS_YAML` | Host registry YAML | `./config/hosts.yaml` if present, else `$XDG_CONFIG_HOME/portal-mcp-server/hosts.yaml` |
| `SSH_POLICIES_YAML` | Security policy YAML | `./config/policies.yaml` if present, else `$XDG_CONFIG_HOME/portal-mcp-server/policies.yaml` |
| `SSH_MCP_LOG_DIR` | Audit + server log directory | `./logs/` if present, else `$XDG_STATE_HOME/portal-mcp-server/logs/` |
| `SSH_MCP_AUDIT_FAIL_OPEN` | Set to `1` → audit-write failures are warnings only; unset (default) → **fail-closed**, audit-write failure raises and aborts the operation | _(unset)_ |
| `MCP_AUTH_TOKEN` | Bearer token for HTTP transport | _(none)_ |

`config/hosts.example.yaml` is the schema template. **`hosts.yaml` contains real credentials and is in `.gitignore` — never commit it.**

---

## Security

### Default constraints

portal-mcp-server does not enforce a path allowlist — that's the job of the companion `remote` skill at the prompt layer:
> **Writes default to remote `/tmp/`. Always ask before touching `$HOME` or project source directories.**

For machine-level enforcement, add rules to `command_blocklist` in `config/policies.yaml` (e.g. `"rm -rf /home/*"`).

### Policy gate

`SecurityPolicy` checks: host allowlist (fnmatch), command blocklist/allowlist (fnmatch), per-host rate limit (sliding window). Every command-execution tool goes through `_gate(host, command)`; multi-host orchestration (`portal_multi_exec` parallel/rolling/broadcast and `portal_playbook` group path) goes through `_gate_many(hosts, command)`, and `playbook` additionally walks every `step` through the blocklist. `portal_bash` gates each command too — a persistent session does **not** authorise arbitrary commands.

### Authentication

**Key-based only.** `HostConfig` has no `password` field; `portal_host(action="register", ...)` has no `password` parameter. Stale `password:` keys in `hosts.yaml` are detected at startup, logged at ERROR level, and ignored.

### Audit

All state-changing tools write `logs/audit.jsonl` (exec / file write / patch / register / tunnel / playbook / multi-host orchestration). Read-only tools (`portal_read` / `portal_grep` / `portal_glob` / `portal_audit` / `portal_check` / `portal_tunnel_list`) explicitly do not audit, to keep the log readable.

**Default is fail-closed** — audit-write failure raises and aborts the operation. Set `SSH_MCP_AUDIT_FAIL_OPEN=1` to switch to fail-open (warning only, suitable for dev / test).

See [`SECURITY.md`](./SECURITY.md) for the algorithmic provenance and design diff. Vulnerability disclosures: GitHub Security Advisories.

---

## Testing

### Unit + security (no real SSH required)

```bash
pytest tests/ -v
# 129 passed, 22 skipped (live SSH tests gated by SSH_TEST_LIVE)
```

Coverage: command-injection regression, safety validators, hash-protected editor, concurrency, resource lifecycle, multi-host policy enforcement, no-password-auth invariants, audit fail mode.

### End-to-end live smoke

`tests/live_smoke.py` imports the local working tree and drives a series of real SSH actions: stale `password:` handling in `hosts.yaml`, basic `ssh_exec`, `portal_multi_exec(mode="parallel", group_tag=...)` against real hosts (verifying both blocked-command and not-in-allowlist hosts get rejected), per-command gating in `portal_bash`, a `portal_bash` + `portal_patch` round-trip in remote `/tmp/` (including the stale-hash rejection path), and audit.jsonl ingestion of the new operation tags.

```bash
SSH_MCP_AUDIT_FAIL_OPEN=1 \
  TEST_HOST=<your-host> TEST_PORT=22 TEST_USER=<user> \
  TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ It writes one file under remote `/tmp/portal-mcp-server-smoke-<pid>.txt` and removes it at the end. Stays inside `/tmp`.

> The repo also contains `examples/phase6_acceptance.py`, a developer-era end-to-end demo. It **hard-codes host alias `1810` and paths under `~/SU2-Quantum/`** and is kept only as an internal regression script — adapt the host and paths before running.

---

## "Why doesn't my local change take effect?"

`uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git portal-mcp-server` re-fetches the latest commit on `main` from GitHub at the moment the MCP client launches the subprocess. So:

| Where you edited | Will the agent see it? |
|---|---|
| Local working tree (`/home/.../portal-mcp-server/`) | ❌ No. uvx pulls remote git, not a local path |
| Committed but not pushed | ❌ No |
| Committed + pushed to `TMYTiMidlY/portal-mcp-server` main | ✅ Yes, but you must restart the MCP client (uvx fetches at process start; it does not refetch within the running process) |

To verify which version uvx actually loaded:

```bash
# cd somewhere outside the repo, otherwise uvx prefers the local working tree
cd /tmp && uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git \
  --refresh python -c "
import portal_mcp_server.audit as a
print('audit env var:', getattr(a, '_FAIL_OPEN_ENV',
      'NOT SET — running an OLD/published version'))
"
```

- `audit env var: SSH_MCP_AUDIT_FAIL_OPEN` → already on the new, security-tightened version
- `NOT SET — running an OLD/published version` → uvx pulled an old commit (push didn't reach the upstream, or uvx cache is stale — `--refresh` clears it)

For local debugging without pushing, point `mcp-config.example.json`'s `args` at your working tree:
```json
"args": ["--from", "/absolute/path/to/portal-mcp-server", "portal-mcp-server"]
```
(Path must be absolute.) **Don't commit this local path back into the example.**

---

## Attribution & license

Apache License 2.0 (see [`LICENSE`](LICENSE)).

Lineage and third-party algorithmic references are tracked in [`NOTICE`](NOTICE):

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0)** — git ancestor; the lower-level modules (asyncssh engine, connection pool, tunnel manager, orchestrator, security policy) are inherited. The 18-tool `portal_*` upper layer is new.
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT)** — algorithmic reference for the SHA-256 hash-protected edit semantics in `remote_text_editor.py` (clean-room reimplementation, no source code copied).

> ⚠️ This tool gives an agent programmatic SSH access to remote systems. **Use only on systems you own or have explicit written authorisation to access.** Unauthorised access is illegal in most jurisdictions.
