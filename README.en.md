<div align="center">

# portal-mcp-server

**Agent-first SSH orchestration MCP server**

Lets coding agents (Claude Code, Copilot CLI, Cursor, …) drive remote machines as fluently as the local one: persistent bash sessions, hash-protected remote file editing, SFTP, SSH tunnels, multi-host orchestration. Built on [AsyncSSH](https://github.com/ronf/asyncssh) + [FastMCP](https://modelcontextprotocol.io/), with an in-process connection pool shared across every tool — identical reuse performance on Windows, macOS, and Linux.

[![CI](https://github.com/TMYTiMidlY/portal-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/TMYTiMidlY/portal-mcp-server/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/portal-mcp-server)](https://pypi.org/project/portal-mcp-server/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen)](https://modelcontextprotocol.io/)
[![Last commit](https://img.shields.io/github/last-commit/TMYTiMidlY/portal-mcp-server)](https://github.com/TMYTiMidlY/portal-mcp-server/commits/main)
[![Issues](https://img.shields.io/github/issues/TMYTiMidlY/portal-mcp-server)](https://github.com/TMYTiMidlY/portal-mcp-server/issues)

[简体中文](./README.md) ｜ English

</div>

---

<details>
<summary>📖 Table of Contents</summary>

- [Overview](#overview)
- [Highlights](#highlights)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Tools](#tools)
- [Design notes](#design-notes)
- [Install](#install)
- [Client integration](#client-integration)
- [Environment variables](#environment-variables)
- [Authentication](#authentication)
- [Security](#security)
- [Testing](#testing)
- [CI / Release](#ci--release)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License & attribution](#license--attribution)

</details>

## Overview

`portal-mcp-server` is forked from [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0). The lower-level SSH/asyncssh engine, connection pool, tunnel manager, multi-host orchestrator, and security policy are inherited from the upstream modules. The upper layer is a fresh agent-first 19-tool surface:

- **2** hash-protected remote file editing tools (`portal_read` / `portal_patch`), with the SHA-256 conflict-detection algorithm referenced from [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT), reimplemented for SFTP
- **6** core IO / search / persistent bash tools
- **10** higher-level tools consolidated via a single `mode` parameter (tunnels, file transfer, multi-host orchestration, playbooks, audit, …)

See [`NOTICE`](./NOTICE) and the [Security](#security) section for full provenance and security posture.

## Highlights

- **Cross-tool connection reuse**: every `portal_*` tool shares the same in-process asyncssh pool; one TCP per host gets reused indefinitely, individual calls amortise to channel creation (~10–30 ms).
- **Same speed on Windows**: no dependency on OpenSSH `ControlMaster`; the pool is plain Python objects, so the three major OSes get identical reuse performance.
- **Persistent bash sessions**: `portal_bash` keeps a `bash -i` per host with cwd / env preserved across calls — the agent doesn't have to rebuild context every command.
- **Hash-protected remote edits**: `portal_read` + `portal_patch` use whole-file SHA-256 plus per-range hashes, write through tmp + `posix_rename` (atomic), then re-hash on disk to refuse stale or concurrent overwrites.
- **Agent-first tool budget**: 19 tools instead of the upstream's 57; the tool-list context drops from ~7.5k tokens to ~2.5k, and `mode` parameters collapse semantically overlapping entries.
- **Built-in security policy**: host allowlist, command blocklist/allowlist (fnmatch), per-host rate limit, and an audit log for every state-changing operation, fail-closed by default.
- **OpenSSH-compatible**: native handling of `~/.ssh/config` aliases, `known_hosts`, ssh-agent — no need to re-register hosts.
- **Zero deployment**: MCP clients launch it directly from GitHub via `uvx`, no clone or venv needed.

## Quick start

```bash
# 1. Register with Claude Code (see "Client integration" for other MCP hosts)
claude mcp add portal -- uvx portal-mcp-server

# 2. Make sure the target host is in ~/.ssh/config or hosts.yaml
#    (hosts.yaml defaults to ~/.config/portal-mcp-server/hosts.yaml;
#     override with PORTAL_HOSTS_YAML — see "Environment variables")

# 3. Use it in an agent conversation
#    "Show me the last 50 lines of /var/log/syslog on myhost"
#    → agent calls portal_bash("myhost", "tail -50 /var/log/syslog")
```

No clone, no venv — `uvx` pulls and runs automatically. For developer setup see [Install](#install).

## Architecture

```
┌──────────────┐    stdio / SSE     ┌─────────────────────────────────────┐
│  MCP Client  │ ◄────────────────► │       portal-mcp-server             │
│ (Claude Code │                    │                                     │
│  Copilot CLI │                    │  ┌──────────┐   ┌────────────────┐  │
│  Cursor ...) │                    │  │ 19 tools │──►│ security gate  │  │
└──────────────┘                    │  └──────────┘   │ + audit log    │  │
                                    │                  └───────┬────────┘  │
                                    │                          │           │
                                    │              ┌───────────▼────────┐  │
                                    │              │  asyncssh pool     │  │
                                    │              │  (in-process,      │  │
                                    │              │   cross-tool reuse)│  │
                                    │              └──┬──────┬──────┬──┘  │
                                    └─────────────────┼──────┼──────┼─────┘
                                                      │      │      │
                                               SSH    │      │      │
                                              ┌───────▼─┐ ┌──▼──┐ ┌─▼──────┐
                                              │ Host A  │ │ ... │ │ Host N │
                                              └─────────┘ └─────┘ └────────┘
```

## Tools

### 8 core tools (preferred entry points)

| Tool | What the agent gets |
|---|---|
| `portal_read` / `portal_patch` | Read remote file with SHA-256 of file + range; patch checks `file_hash` + per-range hash to prevent concurrent overwrite; writes via tmp + `posix_rename` (atomic) and re-hash after write |
| `portal_grep` / `portal_glob` | Remote `rg --json` / `find` with structured output; first-call probe is cached |
| `portal_bash` / `_close` / `_status` | One sticky `bash -i` per host; cwd / env survive across calls; PTY echo + bracketed-paste disabled so sentinel parsing is reliable; `use_sudo=True` runs a one-shot `sudo -S` (password resolved from the per-user broker / `sudo_password_command`, never via the LLM — see [Authentication](#non-interactive-sudo-use_sudo--sudo-login)) |
| `portal_cleanup_tmps` | Garbage-collects orphan `*.mcp_tmp.*` files left by interrupted patches |

### 10 high-level tools (mode-switched)

| Tool | mode / params | Purpose |
|---|---|---|
| `portal_host` | `action=list\|register\|remove` | Host registry (for tag-based grouping; `~/.ssh/config` aliases are auto-resolved without registration) |
| `portal_transfer` | `direction=upload\|download\|sync\|mirror\|upload-list\|download-list` | SFTP file transfer (binary-safe); `sync` pushes a dir, `mirror` pulls one, `upload-list` / `download-list` move an explicit batch of arbitrary local↔remote file pairs given in `paths_json`, all skipping unchanged files by size+mtime (transfers use `preserve=True`, so it's a precise rclone-style equality, not a newer-than heuristic), or sha256 with `checksum=True`; returns structured JSON (bytes / skipped / failed[] / duration), a single file's failure lands in `failed[]` without aborting the batch; MCP progress during transfer doubles as a keepalive against client idle timeouts |
| `portal_tunnel_open` / `_close` / `_list` | `mode=local\|reverse\|socks` | SSH tunnels (port forward / reverse / SOCKS5) |
| `portal_multi_exec` | `mode=parallel\|rolling\|broadcast`, `hosts_json\|group_tag` | Multi-host command orchestration |
| `portal_playbook` | `host\|group_tag` | Multi-step playbook |
| `portal_ping` | optional `hosts_json` | Health check (single host or whole fleet) |
| `portal_audit` | `view=snapshot\|history\|stats\|policy` | Audit log + server introspection |
| `portal_check` | `host`, optional `command` | Security policy dry-run |

### Specific tools vs `portal_bash`: which to use

`portal_bash` can run anything, but **don't reach for it when a purpose-built tool exists** — the specific tools either carry a safety guarantee or return structured output, which makes them more reliable for an agent:

| What you want to do | Use this (**not** a raw `portal_bash` command) | Why |
|---|---|---|
| Read / edit a remote file | `portal_read` → `portal_patch` | SHA-256 + per-range hash against concurrent overwrite, atomic rename, post-write rehash; raw `cat`/`>` has none of that |
| Search content / find files | `portal_grep` / `portal_glob` | `rg --json` / `find` structured output — the agent doesn't parse raw text |
| Transfer files / sync dirs | `portal_transfer` | SFTP binary-safe + incremental skip + progress keepalive; `scp` in a loop has neither incrementality nor idle-survival |
| Run on many hosts | `portal_multi_exec` / `portal_playbook` | parallel / rolling / broadcast + two-phase gating; a bash `for h in …; ssh $h` loop has no gate |
| Open a tunnel | `portal_tunnel_*` | managed lifecycle, visible in `portal_audit`; a bash `ssh -L` runs away unsupervised |

**Everything else** (running processes, tailing logs, systemctl, docker, one-off commands…) is `portal_bash`'s territory — one persistent `bash -i` session covers the 27 tools the upstream project split out. Rule of thumb: **don't mix `portal_*` and bash `ssh`/`scp` in the same task**, or you bypass hash checking or break the sudo flow.

### Agent-side conventions

`portal-mcp-server` only provides tools — it does not enforce how the agent uses them. To make agent behaviour on top of these tools predictable and safe, recommend pinning the following rules in `AGENTS.md` / `CLAUDE.md` or your system prompt:

- **Confirm the host alias first** — if the target host is not in `~/.ssh/config` or `hosts.yaml`, ask the user. Don't just register a new host.
- **Writes go through read → patch** — call `portal_read` for `file_hash` (and `range_hash` per region), then `portal_patch` with the same hashes; on conflict, `portal_patch` returns the new hash — re-read and retry.
- **Default sandbox is `/tmp/`** — writes default to remote `/tmp/`. Ask before touching `$HOME` or project source.
- **Don't mix tools within one task** — pick `portal_*` (hash-protected, pool-reused) *or* `ssh`/`scp` from bash, not both. Mixing them bypasses hash checking or breaks sudo flows.
- **Use the multi-host tools** — `portal_multi_exec(mode="parallel")` / `portal_playbook(group_tag=...)`, not a bash loop of `ssh host1; ssh host2; …`.
- **Sudo, three ways** — when sudo is needed: ① prefer a host-level `sudo_password_command` (pulled from a password manager, fully automatic); ② or have the user pre-seed the password with `portal-mcp-server sudo-login <host>` in the per-user broker from another terminal, then `portal_bash(..., use_sudo=True)`; ③ for genuinely interactive prompts (password change, first-time TTY check), have the user run `ssh -t host sudo …`. `use_sudo` runs a one-shot exec and does **not** inherit `cwd` / env from prior `portal_bash` calls.

<details>
<summary>📋 Full signatures & source map</summary>

> Below are the model-visible signatures of every tool (`ctx` is injected by FastMCP and does not appear in the schema), plus the module each tool lives in.

### Tool signatures

| Tool | Signature |
| --- | --- |
| `portal_read` | `(host, path, start=1, end=None, encoding='utf-8')` |
| `portal_patch` | `(host, path, file_hash, patches_json, encoding='utf-8', auto_newline=False)` |
| `portal_cleanup_tmps` | `(host, directory, max_age_s=3600)` |
| `portal_grep` | `(host, path, pattern, glob='', file_type='', ignore_case=False, max_count=0)` |
| `portal_glob` | `(host, pattern, path='.')` |
| `portal_bash` | `(host, command, timeout=3600.0, use_sudo=False)` |
| `portal_bash_close` | `(host)` |
| `portal_bash_status` | `()` |
| `portal_host` | `(action, name='', host='', user='root', port=22, key_path='', tags='')` |
| `portal_transfer` | `(direction, host, local_path, remote_path, checksum=False, paths_json='')` |
| `portal_tunnel_open` | `(mode, host, local_port=0, local_bind='127.0.0.1', remote_host='', remote_port=0)` |
| `portal_tunnel_close` | `(tunnel_id)` |
| `portal_tunnel_list` | `()` |
| `portal_multi_exec` | `(mode, command='', commands_json='', hosts_json='', group_tag='', timeout=3600, delay_s=2.0, stop_on_error=True)` |
| `portal_playbook` | `(playbook_json, host='', group_tag='')` |
| `portal_ping` | `(hosts_json='')` |
| `portal_check` | `(host, command='')` |
| `portal_audit` | `(view='snapshot', limit=50, host_filter='')` |

### Source map

| Module | Tools / responsibility |
| --- | --- |
| `connection_manager.py` | connection pool shared by every tool |
| `remote_text_editor.py` | `portal_read`, `portal_patch`, `portal_cleanup_tmps` |
| `remote_search.py` | `portal_grep`, `portal_glob` |
| `remote_bash.py` | `portal_bash`, `portal_bash_close`, `portal_bash_status` |
| `file_ops.py` | `portal_transfer` |
| `network_tools.py` | `portal_tunnel_*` |
| `orchestrator.py` | `portal_multi_exec`, `portal_playbook` |
| `security.py` | `_gate()` / `_gate_many()` / `_gate_playbook()` policy gates |
| `audit.py` | `audit_log()` writes + `portal_audit` introspection |

</details>

## Design notes

### Tool consolidation: 19 vs. 57

Anthropic's [_Writing Tools for Agents_](https://www.anthropic.com/engineering/writing-tools-for-agents) is explicit:

> "More tools don't always lead to better outcomes... Tools that merely wrap existing software functionality is a common error... Too many tools or overlapping tools can also distract agents from pursuing efficient strategies."

The upstream `ssh-shell-mcp` exposes one tool per ergonomic — `ssh_run` / `ssh_run_batch` / `ssh_run_script` / `ssh_run_with_env` / `ssh_session_exec` / `ssh_ps` / `ssh_kill` / `ssh_df` / `ssh_free` / `ssh_journalctl` / `ssh_docker` / `ssh_tmux_*` … — **57 tools total**. Most are one-line bash wrappers that **`portal_bash` (a persistent bash session) replaces by itself**.

| Bucket | Count | What we did |
|---|---:|---|
| **Kept and redesigned** | 8 | `portal_read` + `portal_patch` use SHA-256 hashes to fix the concurrency hole in raw cat/write; `portal_grep` / `portal_glob` give structured search output; `portal_bash`(`_close`/`_status`) provide a persistent shell; `portal_cleanup_tmps` handles interrupted writes |
| **Mode-flag merged** | 10 | `portal_tunnel_open(mode=local\|reverse\|socks)` replaces 3 upstream tools; `portal_multi_exec(mode=parallel\|rolling\|broadcast)` replaces 4; `portal_audit(view=...)` collapses 4 introspection endpoints |
| **Removed entirely** | 27 | All trivially expressible as `portal_bash` invocations: 5 exec-family, 6 multi-session-family, 7 sysinfo (ps/df/free/journalctl/info/netstat/service), 5 process-management, 4 tmux |

Result: tool-list context drops from ~7.5k tokens to ~2.5k, and the agent no longer has to disambiguate between semantically overlapping tools.

### In-process connection pool

portal-mcp-server runs an asyncssh connection pool inside its own server process. Every tool invocation (`portal_bash`, `portal_read`, `portal_transfer`, …) shares the same TCP. **Everything except the first connect amortises down to channel creation (~10–30 ms).**

Compared to the best plain-ssh option (`ControlMaster auto / ControlPersist 10m` in `~/.ssh/config`):

| Dimension | portal-mcp-server | plain ssh + ControlMaster |
|---|---|---|
| Reuse mechanism | asyncssh in-process pool (≤ 5 concurrent ops per connection, new ones created on demand) | OpenSSH master process + Unix domain socket |
| Reuse scope | process-level (lives as long as the MCP server) | session-level (default 10-min `ControlPersist`) |
| First connect | TCP + auth (~200–500 ms) | TCP + auth (~200–500 ms) |
| Subsequent commands | reuse pool, open new channel (~10–30 ms) | reuse master, open new channel (~10–30 ms) |
| Cross-tool reuse | ✅ `portal_bash` and `portal_read` share the same TCP | ❌ `ssh` and `scp` only reuse if both have matching `ControlPath` |
| Persistent shell state | ✅ `portal_bash` keeps `bash -i`; cwd/env survive across calls | ❌ each `ssh host cmd` is a fresh shell; cwd/env discarded |
| Concurrency | true asyncio multi-channel parallelism | one ssh process per command, serial startup (sharing master) |
| Windows | ✅ identical performance everywhere Python runs | ❌ Windows OpenSSH does not support ControlMaster |

Anonymised microbenchmark: same LAN (< 1ms RTT), 100× `echo pong`. Plain ssh + ControlMaster averaged 23 ms/call; portal-mcp-server through `portal_bash` averaged 18 ms/call (no ssh client process startup). First-connect both ~280 ms (auth dominated).

### Windows behaviour

`ControlMaster` **doesn't work on Windows OpenSSH** — it relies on Unix-domain-socket-based fd sharing between the master and the child ssh processes, and the default Windows OpenSSH build lacks that primitive (the experimental named-pipe support is also unreliable).

portal-mcp-server **doesn't depend on any OS-level socket sharing**. The pool is plain Python objects in the MCP server's own memory (asyncssh is pure Python). Any platform that runs Python (Windows / macOS / Linux) gets the same reuse performance as Linux.

```text
On Windows:
  plain ssh:        every command opens a new TCP+auth      → ~300 ms × N
  portal-mcp-server: first ~280 ms, then ~20 ms thereafter   → drops to channel-creation floor
```

Side benefit: pool connections live as long as the MCP server (typically hours), not the 10-minute `ControlPersist` default — fewer reconnect spikes inside long sessions.

### Stack choice: asyncssh, not subprocess-wrapped OpenSSH

[asyncssh](https://github.com/ronf/asyncssh) (EPL-2.0 / GPL-2.0 dual-licensed) is an **independent pure-Python SSHv2 implementation**, protocol-equivalent to OpenSSH:

- **One process, many connections, many sessions per connection** — the pool is a Python dict; no process boundaries, no fd sharing required
- **Full protocol coverage** — local/remote/dynamic port forwarding, SFTP, SCP, X11 forwarding, TUN/TAP — anything OpenSSH does at the protocol layer, asyncssh does too
- **OpenSSH-compatible** — natively parses `~/.ssh/config`, `known_hosts`, `authorized_keys`, ssh-agent / Pageant
- **Only depends on PyCA `cryptography`** — install Python and you're done; no C deps, no OS-specific IPC

Compared to "shell out to `ssh` / `scp`":

- No new process per command (saves the ~50–100 ms fork)
- No need to coordinate SSH reuse across multiple OS processes (which is exactly what breaks ControlMaster on Windows)
- Error handling, retries, and timeouts are first-class Python async primitives, not stderr-string parsing

### Feedback channel: warnings ride tool results, not stderr

Every runtime warning or error a **user needs to see** is emitted inside the tool result returned to the agent — never relied on landing in `stderr` or a server log file. This isn't an aesthetic choice; it's forced by how MCP clients actually treat the protocol's diagnostic channels.

**Protocol layer** — [MCP 2025-06-18 spec · transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#stdio):

> The server **MAY** write UTF-8 strings to its standard error (`stderr`) for logging purposes. **Clients MAY capture, forward, or ignore this logging.**

The second candidate, `notifications/message` ([logging capability](https://modelcontextprotocol.io/specification/2025-06-18/server/utilities/logging)), is equally permissive: *"Implementations are free to expose logging through any interface pattern that suits their needs—the protocol itself does not mandate any specific user interaction model."*

**What major clients actually do**:

| Client | Where server stderr goes | User-visible? |
|---|---|---|
| Claude Desktop ([docs](https://modelcontextprotocol.io/docs/develop/connect-local-servers#getting-logs-from-claude-desktop)) | Written to `~/Library/Logs/Claude/mcp-server-<name>.log` | ❌ No in-app indicator; user must `tail -f` the log file |
| Claude Code ([docs](https://docs.anthropic.com/en/docs/claude-code/debug-your-config#check-mcp-servers)) | Discarded by default; official advice: *"run `claude --debug mcp` to see the server's stderr output"* | ❌ Only after a debug-mode relaunch |
| Generic Python MCP SDK client | `errlog: TextIO = sys.stderr` — forwarded to the client process's own stderr | Depends on whatever the client process does with its own stderr |

**The only reliable feedback paths** are the tool result `content` array (the agent always reads it) and JSON-RPC error responses (most clients surface them). So:

- **Important warnings** (misconfigured yaml, missing credentials, ignored fields, …) → collected into the server's `_config_warnings` set and attached to the return value of `portal_host(action="list")` (see `connection_manager.py`)
- **Fatal config errors** → raised inline in the relevant tool result, not just logged at server startup
- **Info-level stderr** → only useful for the server author at debug time; never assumed to reach the user
- **Audit log** → written to `$XDG_STATE_HOME/portal-mcp-server/logs/` ([XDG Base Directory Spec](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html) explicitly places "logs, history" in the state-home tier — persistent but non-critical state); for ops review and post-hoc audit, never assumed to be read live

The rule cuts the other way too: **anything the user should know but the server cannot raise immediately** must be attached to the next relevant tool call's return value. A bare `logger.error()` is a dead letter.

## Install

Two paths depending on what you're doing.

### End user (use the MCP server, never touch the source)

No clone needed — let your MCP client launch it via `uvx` straight from PyPI. See [Client integration](#client-integration). `uvx` caches deps on first run; subsequent restarts are instant.

Manual smoke test in a shell:

```bash
uvx portal-mcp-server --help
```

### Developer (will modify code or run tests)

Recommended: `uv sync` will set up `.venv` from `pyproject.toml` + `uv.lock` in one shot:

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras
source .venv/bin/activate
pytest                        # should be all green (live SSH tests skip by default)
```

To point an MCP client at this local checkout, install it as a fixed executable:

```bash
uv tool install --force .      # --force overwrites the old tool with this checkout
```

If you'd rather not use uv, plain pip editable install works:

```bash
pip install -e ".[dev]"       # -e/--editable points at this source tree; prod + dev
# or runtime only
pip install -e .
```

### Short alias `portal`

After `uv tool install portal-mcp-server` (or the `uv tool install --force .` above), two equivalent entry points are on your `PATH`:

```bash
portal broker-install --now          # install/start the systemd --user broker
portal broker-uninstall              # disable/remove broker user units/config
portal-mcp-server sudo-login web01   # full name
portal sudo-login web01              # short name (recommended for typing)
portal ssh-login web01
portal secret-set GITHUB_TOKEN
```

The `uvx portal-mcp-server xxx` form still requires the full name (`uvx` does not accept aliases). The short name only applies to persistent commands after `uv tool install` / `pip install`.

> **⚠️ Known name collision**: [`SpatiumPortae/portal`](https://github.com/SpatiumPortae/portal) (a P2P file-transfer CLI, packaged in Homebrew core) is also called `portal`. **Homebrew users may collide** — `uv tool install` drops the binary at `~/.local/bin/portal`, Homebrew puts it at `/opt/homebrew/bin/portal` or `/usr/local/bin/portal`, and whichever comes first in `$PATH` wins. To investigate:
>
> ```bash
> which -a portal      # lists every matching executable; the top one is active
> ```
>
> If it collides, fall back to the full `portal-mcp-server`, or reorder your PATH. `uv tool install` will *not* silently overwrite another tool's binary — it errors out and lets you decide.

### systemd --user credential broker

No-echo interactive values from `ssh-login` / `sudo-login` / `secret-set` no longer live in one MCP server process. They go into a per-user, systemd socket-activated broker. Before using those interactive credential commands, explicitly install and start the user socket:

```bash
portal broker-install --now
```

This writes `~/.config/systemd/user/portal-mcp-credential-broker.{socket,service}`. The `.socket` unit listens on the systemd user manager path `%t/portal-mcp-server/credentials.sock`, with creation/removal owned by systemd. The installer also records the resolved absolute socket path in `~/.config/portal-mcp-server/broker.json`, so later MCP clients read that config (or an explicit `PORTAL_CREDENTIAL_BROKER_SOCKET`) instead of deriving the runtime directory themselves.

> **Order of operations**: if you plan to use no-echo input via `secret-set`, `sudo-login`, or `ssh-login`, run `portal broker-install --now` before starting the agent / IDE portal MCP server. If the agent is already running, reload the MCP/plugin integration or restart the agent after installing the broker (for example, reload MCP/plugin in Claude Code, run `/restart` in Copilot CLI, or restart the relevant IDE/agent).

What stays enabled is the systemd socket unit: a same-user local listening endpoint. The broker service is socket-activated on first connection and holds TTL credentials in memory. Stopping the service clears the in-memory credentials while the socket can still activate it again. To remove the units and config:

```bash
portal broker-uninstall
```

## Client integration

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%22%5D%7D) [![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%22%5D%7D&quality=insiders) [![Install in Cursor](https://img.shields.io/badge/Cursor-Install_Server-000000?style=flat-square&logo=cursor&logoColor=white)](https://cursor.com/en/install-mcp?name=portal&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJwb3J0YWwtbWNwLXNlcnZlciJdfQ==)

`portal-mcp-server` is a local stdio MCP server — any MCP-capable host can install it. Each section below gives the minimal config for a popular host. `uvx` pulls from PyPI and caches automatically — no clone or venv required.

> If your MCP client cannot find `uvx`, run `which uvx` (`where uvx` on Windows) and use that absolute path as `command`.

### Generic snippet

> Most hosts accept the `{ "mcpServers": { "<name>": { "command": ..., "args": [...] } } }` top-level schema. VS Code and Codex use their own schemas — see their dedicated sections below.

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": ["portal-mcp-server"]
    }
  }
}
```

To override hosts / policies / log paths, append an `env` block:

```json
"env": {
  "PORTAL_HOSTS_YAML": "/path/to/hosts.yaml",
  "PORTAL_POLICIES_YAML": "/path/to/policies.yaml",
  "PORTAL_LOG_DIR": "/path/to/logs"
}
```

### Claude Code CLI

Edit `<project>/.mcp.json` (same schema as above), or register via CLI / slash command:

```bash
claude mcp add portal -- uvx portal-mcp-server
# or run /mcp inside a Claude Code session; pass --scope user to register globally
```

<details>
<summary><b>GitHub Copilot CLI</b></summary>

Write `<project>/.mcp.json` for project scope, or register at user scope with one command (applies to every project):

```bash
copilot mcp add portal -- uvx portal-mcp-server
# or run /mcp inside a Copilot CLI session for the interactive flow
```

Verify:

```bash
copilot mcp list                # should show portal
copilot mcp get portal          # check Source is Workspace / User
```

</details>

<details>
<summary><b>Cursor</b></summary>

Click the **Install in Cursor** badge above for one-click setup, or write the generic snippet to `~/.cursor/mcp.json` (all projects) or `<project>/.cursor/mcp.json` (this project only). Cursor → Settings → Tools & MCP shows `portal` once added.

</details>

<details>
<summary><b>VS Code (Copilot Chat / Agent mode)</b></summary>

Click the **Install in VS Code** badge above for one-click setup, or write to `<project>/.vscode/mcp.json` manually (VS Code uses its own schema — top-level key is `servers`, not `mcpServers`):

```json
{
  "servers": {
    "portal": {
      "type": "stdio",
      "command": "uvx",
      "args": ["portal-mcp-server"]
    }
  }
}
```

For global scope, place the same `servers` block under the `mcp` field of your VS Code user `settings.json` (path varies by OS).

> Not interchangeable with `mcpServers`. Keep a separate file when you mix VS Code with Copilot CLI / Claude Code / Cursor.

</details>

<details>
<summary><b>Claude Desktop</b></summary>

Paste the generic snippet under `mcpServers` in `claude_desktop_config.json`, then restart Claude Desktop. Config file location:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

</details>

<details>
<summary><b>Windsurf</b></summary>

Windsurf uses the same `mcpServers` schema. In Cascade, click the plugins icon → "Manually configure MCP", then write the generic snippet to `~/.codeium/windsurf/mcp_config.json`. Reload Cascade to enable.

</details>

<details>
<summary><b>OpenAI Codex CLI</b></summary>

Codex uses TOML. Edit `~/.codex/config.toml`:

```toml
[mcp_servers.portal]
command = "uvx"
args = ["portal-mcp-server"]
```

After starting Codex, run `/mcp` in the TUI to confirm `portal` is loaded.

</details>

<details>
<summary><b>Other hosts (Cline / Continue / Roo Code / Zed …)</b></summary>

- **Cline / Continue / Roo Code and other VS Code extensions** — most accept the `{ "mcpServers": ... }` generic snippet; paste it into the extension's MCP settings panel or workspace config
- **Any MCP-compatible host** — paste the generic snippet into the host's MCP config entry; stdio needs no proxy

</details>

## Environment variables

All configurable knobs in portal-mcp-server are passed as environment variables, unified under the `PORTAL_*` prefix to avoid clashes with OpenSSH's own `SSH_*` namespace or other MCP servers. Set them in the `env` field of your MCP client config — they only affect the MCP server subprocess.

> **v1.1.0 rename notice**: the three legacy prefixes from 1.0.x (`SSH_*`, `SSH_MCP_*`, `MCP_*`) have all been consolidated under `PORTAL_*`. **No backward compatibility.** When upgrading from 1.0.x, rename in one pass per the table below. Full migration table in [CHANGELOG](./CHANGELOG.md).

### Overview

| Category | Variable | One-line purpose |
|---|---|---|
| File paths | `PORTAL_HOSTS_YAML` | Host registry YAML |
| File paths | `PORTAL_POLICIES_YAML` | Security policy YAML |
| File paths | `PORTAL_SECRETS_YAML` | Named secrets YAML (source for `secrets=` in `portal_bash` / `portal_local_exec`) |
| File paths | `PORTAL_LOG_DIR` | Audit + server log directory |
| Security & auth | `PORTAL_AUDIT_FAIL_OPEN` | Whether audit-write failure is fail-open |
| Security & auth | `PORTAL_AUTH_TOKEN` | Bearer token for HTTP transport |
| Connection pool | `PORTAL_SSH_POOL_SIZE` | Max TCP connections per host |
| Connection pool | `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | Max concurrent channels per TCP connection |
| Connection pool | `PORTAL_SSH_MAX_IDLE_TIME` | Idle-close timeout in seconds |
| Connection pool | `PORTAL_SSH_MAX_CONN_AGE` | Max connection lifetime in seconds |
| Testing (dev only) | `PORTAL_TEST_LIVE` | Gate for live SSH integration tests |
| Testing (dev only) | `PORTAL_TEST_HOST` / `PORTAL_TEST_PORT` / `PORTAL_TEST_USER` / `PORTAL_TEST_KEY_PATH` | Live test target |

Detailed breakdown below.

### File paths

| Env var | Meaning | Default |
|---|---|---|
| `PORTAL_HOSTS_YAML` | Host registry YAML | `~/.config/portal-mcp-server/hosts.yaml` |
| `PORTAL_POLICIES_YAML` | Security policy YAML | `~/.config/portal-mcp-server/policies.yaml` |
| `PORTAL_SECRETS_YAML` | Named secrets YAML | `~/.config/portal-mcp-server/secrets.yaml` |
| `PORTAL_LOG_DIR` | Audit + server log directory | `~/.local/state/portal-mcp-server/logs/` |

Resolution order: **env var > XDG directory** (`$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` honored per the spec). The current working directory is **not** consulted — `portal-mcp-server` is a long-lived user-level daemon, not a project tool, and a cwd-relative auto-load would let any directory the server happens to be launched from silently override your real config (no mainstream user-level CLI — `ssh`, `gh`, `docker`, `kubectl`, `rclone`, … — does this).

The repo's [`examples/`](./examples/) directory holds schema templates — every `*.yaml` in there is **read-only sample**, never auto-loaded. Bootstrap your real config by copying the templates into the XDG directory:

```bash
mkdir -p ~/.config/portal-mcp-server
cp examples/hosts.yaml    ~/.config/portal-mcp-server/hosts.yaml
cp examples/policies.yaml ~/.config/portal-mcp-server/policies.yaml
cp examples/secrets.yaml  ~/.config/portal-mcp-server/secrets.yaml
# then edit ~/.config/portal-mcp-server/*.yaml with your real values
```

**`~/.config/portal-mcp-server/hosts.yaml` contains real credentials — never commit it.**

> **v1.5.0 breaking changes**:
> - Removed the `./config/hosts.yaml` / `./config/policies.yaml` / `./logs/` cwd-relative fallbacks — resolution is now env > XDG only.
> - Renamed the repo's `config/` directory to `examples/`; files dropped the `.example.` infix (the directory name now carries the "template" semantics).

### Security & auth

| Env var | Meaning | Default |
|---|---|---|
| `PORTAL_AUDIT_FAIL_OPEN` | Set to `1` → audit-write failures are warnings only; unset → **fail-closed**, audit-write failure aborts the operation | _(unset)_ |
| `PORTAL_AUTH_TOKEN` | Bearer token for HTTP transport (`--transport streamable_http`); not needed for stdio | _(none)_ |

### Connection pool

Controls the in-process asyncssh connection pool. Defaults work well for most setups; tune only under high concurrency or unusual network conditions. Pool behaviour is documented in [§ In-process connection pool](#in-process-connection-pool).

| Env var | Meaning | Default |
|---|---|---|
| `PORTAL_SSH_POOL_SIZE` | Max TCP connections per host. When the pool is full and every connection is at the channel ceiling, the least-loaded connection is reused (with a warning) | `5` |
| `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | Max concurrent channels (SFTP, exec, tunnel, …) multiplexed over one TCP connection. New connections are opened when exceeded, up to `PORTAL_SSH_POOL_SIZE` | `5` |
| `PORTAL_SSH_MAX_IDLE_TIME` | Close idle connections (no active channels) after this many seconds. Set `0` to disable | `600` (10 min) |
| `PORTAL_SSH_MAX_CONN_AGE` | Max connection lifetime in seconds; aged connections with no active channels are closed. Guards against silent firewall / NAT drops | `3600` (1 hour) |

### Testing (dev only)

Only relevant when running `tests/`; regular MCP deployments do not need these. See [§ Testing](#testing) for full usage.

| Env var | Meaning | Default |
|---|---|---|
| `PORTAL_TEST_LIVE` | Set to `1` / `true` / `yes` to actually run the real-SSH tests in `tests/test_live_ssh.py`; otherwise they are all skipped | _(unset)_ |
| `PORTAL_TEST_HOST` | Live-test target host | `127.0.0.1` |
| `PORTAL_TEST_PORT` | Live-test target port | `22` |
| `PORTAL_TEST_USER` | Live-test SSH user | `$USER` or `root` |
| `PORTAL_TEST_KEY_PATH` | Private key for live tests | `~/.ssh/id_ed25519` |

### Full example

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": ["portal-mcp-server"],
      "env": {
        "PORTAL_HOSTS_YAML": "/home/me/.config/portal-mcp-server/hosts.yaml",
        "PORTAL_POLICIES_YAML": "/home/me/.config/portal-mcp-server/policies.yaml",
        "PORTAL_SSH_POOL_SIZE": "10",
        "PORTAL_SSH_MAX_CHANNELS_PER_CONN": "8"
      }
    }
  }
}
```

## Authentication

Pick the path for your setup — SSH keys preferred, encrypted keys via ssh-agent; password auth is supported but goes through `password_command`, so plaintext credentials never reach the LLM.

### Credential-flow overview

There are four credential flows, each with a "password-manager style" (command source) and/or a "no-echo interactive style" (getpass + systemd --user credential broker). **As currently implemented:**

| Credential flow | Command source (password-manager style) | No-echo interactive entry (getpass style) | Cache key | Cache semantics | Trigger |
|---|---|---|---|---|---|
| **A. Remote SSH login password** | `password_command` (hosts.yaml) | ✅ `ssh-login <host>` | host | broker in-memory TTL (default 900s, interactive entry only; command source fetched per connection) | on connect for `auth: password` / auto fallback when key auth refused |
| **B. Remote sudo execution** | `sudo_password_command` (hosts.yaml) | ✅ `sudo-login <host>` | host | broker in-memory TTL (default 900s) | `portal_bash(use_sudo=True)` |
| **C. Secret injection · remote** | `command` in `secrets.yaml` (fetched each time) | ✅ `secret-set <name>` | name | broker in-memory TTL (default 900s, `--ttl` configurable) | `portal_bash(secrets=[…])` |
| **D. Secret injection · local** | same as C (shares `secrets.yaml`) | same as C (shares `secret-set`) | same as C | same as C | `portal_local_exec(secrets=[…])` |

Things to know:

- **C and D are one and the same credential pipeline** — they share `secrets.yaml` + `secret-set` + the same per-user broker + the same name-keyed TTL cache; only the consuming tool differs (remote injects via SSH stdin, local via subprocess env).
- **A, B, and C share one per-user broker socket**, but the broker keeps separate `ssh` / `sudo` / `secret` key spaces. A's password goes into `asyncssh.connect()` during the SSH handshake; B's password is fed to `sudo -S` after the handshake; C/D are injected as environment variables.
- **A's resolution chain**: explicit `auth: password` login goes `cache (ssh-login) → password_command → error`. Pure key hosts retry that same chain *once* when asyncssh raises `PermissionDenied`, but **only when a source is available**; with no cache and no `password_command` the original `PermissionDenied` propagates — so a stale config never masks the real "your key is rejected" failure.
- **Interactive entries (getpass style) = per-user broker in-memory TTL cache**: default 900s, reusable within the TTL, auto-cleared on expiry, gone on broker restart, never written to disk. **Command sources (password-manager style) = fetched each time**, no TTL.

### SSH keys (preferred)

Use ed25519:

```bash
ssh-keygen -t ed25519 -C "you@example.com"
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@your-host
```

The same key works with GitHub — see the official guides: [Generating a new SSH key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent) and [Adding a new SSH key to your GitHub account](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account).

### Encrypted private keys: ssh-agent

Unlock once, reuse for the session — asyncssh picks the unlocked key up via `$SSH_AUTH_SOCK` automatically:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519        # passphrase prompted once
```

For headless / CI environments where ssh-agent is impractical, configure `passphrase_command:` in `hosts.yaml` (see below).

### Password auth: `password_command` or `ssh-login`

Provided for legacy hosts that cannot be re-keyed. Two non-negotiable rules:

1. **Never** write `password: <plaintext>` in `hosts.yaml` — startup logs an ERROR and drops the field.
2. **Never** flow a password through an MCP tool — `portal_host` has no password parameter, so credentials cannot land in LLM tool-call traces.

Two sources (order: broker cache → `password_command` → error), same shape as sudo / secret:

1. **Password manager (1a, fully automatic)** — in `hosts.yaml` set `auth: password` plus a shell command that prints the password to stdout, same pattern as Borg's `BORG_PASSCOMMAND`, restic's `RESTIC_PASSWORD_COMMAND`, and msmtp's `passwordeval`:

   ```yaml
   hosts:
     legacy-host:
       host: 10.0.0.40
       user: admin
       auth: password
       # CI / env-var pattern (GitHub Secrets, Vault inject into env, then read):
       password_command: printf '%s' "$LEGACY_HOST_PASSWORD"
       # Or pull from a password manager:
       # password_command: pass show ssh/legacy-host
       # password_command: bw get password legacy-host
       # password_command: op read "op://Private/legacy-host/password"
   ```

2. **Seed it once (1b, `ssh-login`, interactive)** — in a **separate terminal** (not the agent chat):

   ```bash
   portal-mcp-server ssh-login legacy-host        # getpass, no echo
   portal-mcp-server ssh-login legacy-host --ttl 1800   # custom TTL (seconds); default 900 (15 min)
   ```

   The password travels over the systemd --user managed local unix socket into the per-user broker memory cache: the `.socket` unit listens on `%t/portal-mcp-server/credentials.sock`, the installer records the resolved absolute path in `broker.json`, the directory is `0700`, the socket is `0600`, and the broker performs an `SO_PEERCRED` same-uid check. It is **never written to disk, never sent to the LLM**, and is dropped automatically when the TTL expires. Works even when the host has no `password_command` in `hosts.yaml`, or is a key-mode host (the default — no `auth:` field in hosts.yaml).

#### Auto-fallback: key failure → password

When asyncssh raises `PermissionDenied` for a key-mode host (the default — no `auth:` field in hosts.yaml), the server retries *once* via the password chain (broker cache → `password_command`), but **only when a source is available**. With no cache and no `password_command` the original `PermissionDenied` propagates — so a missing config never masks the real "your key is rejected" failure. Keys remain the preferred path; password is an opt-in safety net.

Runtime behaviour: `password_command` runs with a 10-second timeout, exactly one trailing newline stripped, stderr never logged (leak defence), and non-zero exit / empty output / non-UTF-8 output all hard-failing. Design rationale (why `shell=True`, why `client_keys=[]` is forced, why stderr never reaches the logs, …) lives in **[`SECURITY.md` § Authentication](./SECURITY.md#authentication)**.

### Encrypted-key passphrases: `passphrase_command`

The same mechanism, applied to private-key passphrases:

```yaml
hosts:
  encrypted-key-host:
    host: 10.0.0.30
    user: deploy
    key: ~/.ssh/encrypted_key
    passphrase_command: pass show ssh/encrypted_key
```

Prefer ssh-agent when you have a usable terminal — UX is better. Use `passphrase_command:` only in headless / CI environments.

### Non-interactive sudo: `use_sudo` + `sudo-login`

`portal_bash(host, cmd, use_sudo=True)` lets the agent run root commands, but **the sudo password never reaches the LLM** — `portal_bash` has no password parameter; the password is resolved server-side. Two sources (same philosophy as the SSH password):

1. **Password manager (automatic)** — set `sudo_password_command` on the host in `hosts.yaml`, fully symmetric with `password_command`:

   ```yaml
   hosts:
     prod-box:
       host: 10.0.0.50
       user: deploy
       sudo_password_command: pass show sudo/prod-box   # or op read / bw get / printf "$ENV"
   ```

2. **Seed it once (interactive)** — in a **separate terminal** (not the agent chat):

   ```bash
   portal-mcp-server sudo-login prod-box        # getpass, no echo
   portal-mcp-server sudo-login prod-box --ttl 1800   # custom TTL (seconds); default 900 (15 min)
   ```

   The password travels over the systemd --user managed local unix socket into the per-user broker memory cache: the `.socket` unit listens on `%t/portal-mcp-server/credentials.sock`, the installer records the resolved absolute path in `broker.json`, the directory is `0700`, and the socket is `0600` / same-user only. It is **never written to disk, never sent to the LLM**, and is dropped automatically when the TTL expires.

Resolution order: **broker memory cache (2) → `sudo_password_command` (1) → error** (telling you to `sudo-login` or configure `sudo_password_command`).

Implementation notes: `use_sudo` runs a one-shot `conn.run(input=pw, ...)` executing `sudo -S -k -p '' -- bash -c <cmd>`; it does **not** reuse the persistent `bash -i` session (`sudo -S` reads stdin, which collides with the sentinel protocol). Consequently a sudo command does **not** inherit `cd` / `export` state from prior `portal_bash` calls — bake any `cd … && …` into the same command. `-k` forces fresh auth each time; `-p ''` suppresses the prompt. Genuinely interactive sudo (needs a TTY, or a password change) still can't go through `portal_bash` — have the user run `ssh -t host sudo …`.

### Named-secret injection: `secrets=[…]` + `secret-set`

Use this to hand a command an API token (a GitHub token, a deploy key, …) **without it entering the session history or being sent to the third-party LLM backend**. Same threat model as the sudo password: the agent passes only the secret's **name**, the server resolves the value and injects it as an **environment variable** into a one-shot command. The value travels via the process environment / SSH stdin (never on argv, so `ps` and the audit log can't see it), and any echo of it in the command output is redacted to `***` before the result reaches the agent.

> **Why not just `export`?** The pain point: a throwaway `export TOKEN=…` never reaches the agent's execution context — it only affects the new terminal *you* opened, while the agent runs commands in the MCP server process's environment, which can't see it. The only way to make the agent use it was to `vim` a `.env` / secrets file for it to source — which puts the secret back on disk and is easy to forget to delete. This design turns "hand over a key once" into a **native no-echo CLI prompt** (`secret-set` uses `getpass`, just like typing a password), with the value living only in per-user broker memory and auto-expiring on a TTL — never on disk, never to the LLM.

- Remote: `portal_bash(host, cmd, secrets=["github_token"])`, referencing `$GITHUB_TOKEN` (the uppercased name) in `cmd`.
- Local: `portal_local_exec(cmd, secrets=["github_token"])` runs on the **MCP server host** (not over SSH). Local execution is a larger threat surface, so it is **disabled** unless the server process has `PORTAL_ALLOW_LOCAL_EXEC=1`.

Two sources (order: broker memory cache → `secrets.yaml`):

1. **Secret manager (secrets.yaml)** — symmetric to `password_command`; a command that prints the secret to stdout:

   ```yaml
   secrets:
     github_token:
       command: pass show api/github      # or op read / printf "$ENV"
   ```

2. **Live input (`secret-set`, interactive once)** — in a *separate* terminal:

   ```bash
   portal-mcp-server secret-set github_token            # getpass, no echo
   portal-mcp-server secret-set github_token --ttl 1800 # custom TTL (s), default 900
   ```

   The value is pushed over the systemd --user managed local unix socket into the per-user broker memory cache: the `.socket` unit listens on `%t/portal-mcp-server/credentials.sock`, the installer records the resolved absolute path in `broker.json`, the directory is 0700, and the socket is 0600 / same-user only. It is never written to disk, never seen by the LLM, and is cleared on TTL expiry.

See [`examples/secrets.yaml`](./examples/secrets.yaml). `secrets` and `use_sudo` are mutually exclusive in a single `portal_bash` call.

#### Wait semantics: fail-fast → `ask_user` → retry

No-echo input inherently means "wait for the human to type it," but **that wait is never put on the agent's critical path** — the MCP server is usually headless with no access to the user's tty, so it can neither pop a `getpass` prompt nor block the tool call until it times out. The contract is therefore:

1. **Fail-fast**: when the secret (or sudo password) isn't ready, the tool **returns an error immediately and does not run the command**; the error never contains the value.
2. **Bounce it back to the user**: the error explicitly nudges the agent to use an interactive input/choice tool (e.g. `ask_user`) to ask the user to run `secret-set <name>` / `sudo-login <host>` in a *separate* terminal and reply "ok" when done; the agent then retries the call.
3. **No such tool → end the turn**: if the agent has no `ask_user`-style tool, it should **tell the user what to run and end its turn**, waiting for the user's next prompt to retry — rather than busy-waiting or polling.

So "waiting" surfaces only as a normal conversational turn handoff: the `getpass` block lives in the user's own terminal, while the agent side is always "check cache → run on hit / fail-fast with instructions on miss." **Never ask the user to paste the value into the conversation** — that would feed it straight to the third-party LLM and defeat the entire design.

## Security

- **Default sandbox**: writes default to remote `/tmp/`; the agent must ask before touching `$HOME` or project source (a prompt-layer convention — see [Agent-side conventions](#agent-side-conventions)).
- **Policy gate**: host allowlist + command blocklist/allowlist + per-host rate limit; every state-changing tool runs through `_gate` with no side doors (`portal_host(register)` gates against the target IP, not the alias; `portal_tunnel_close` is gated; multi-host gates are two-phase).
- **Authentication**: SSH keys are the default and recommended path; password auth is supported but only via `password_command` in `hosts.yaml`, never exposed through any MCP tool — config in [Authentication](#authentication), security design in [`SECURITY.md` § Authentication](./SECURITY.md#authentication).
- **Audit**: every state-changing operation is appended to `logs/audit.jsonl`; fail-closed by default (`PORTAL_AUDIT_FAIL_OPEN=1` switches to fail-open).
- **Hash-protected edits**: `portal_read` + `portal_patch` use SHA-256 + per-range hashes + atomic `posix_rename` + post-write rehash to refuse concurrent overwrites.

The full threat model, layer-by-layer defences, operator hygiene, known limitations, and algorithmic provenance live in **[`SECURITY.md`](./SECURITY.md)**.

Vulnerability disclosure: do **not** open a public issue. Use [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new) instead. Targets: acknowledgement within 48 hours, initial assessment within 7 days, resolution within 30 days for critical issues.


## Testing

### Unit + security (no real SSH required)

```bash
pytest tests/ -v
# live SSH tests skip by default (gated by PORTAL_TEST_LIVE)
```

Coverage: command-injection regression, safety validators, hash-protected editor, concurrency, resource lifecycle, multi-host policy enforcement, password_command / passphrase_command safety invariants, audit fail mode.

### End-to-end live smoke

`tests/live_smoke.py` imports the local working tree and drives a series of real SSH actions: stale `password:` field handling in `hosts.yaml`, basic `ssh_exec`, `portal_multi_exec(mode="parallel", group_tag=...)` against real hosts (verifying both blocked-command and not-in-allowlist hosts get rejected), per-command gating in `portal_bash`, a `portal_bash` + `portal_patch` round-trip in remote `/tmp/` (including the stale-hash rejection path), and audit.jsonl ingestion of the new operation tags.

```bash
PORTAL_AUDIT_FAIL_OPEN=1 \
  PORTAL_TEST_HOST=<your-host> PORTAL_TEST_PORT=22 PORTAL_TEST_USER=<user> \
  PORTAL_TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ It writes one file under remote `/tmp/portal-mcp-server-smoke-<pid>.txt` and removes it at the end. Stays inside `/tmp`.

## CI / Release

GitHub Actions handles both testing and publishing — you never need a local `python -m build`:

- **CI** ([`ci.yml`](.github/workflows/ci.yml)): every PR / push to `main` runs `ruff check portal_mcp_server/` + `pytest tests/` on Python **3.10 / 3.11 / 3.12 / 3.13**; all four must be green to merge.
- **Release** ([`release.yml`](.github/workflows/release.yml)): pushing a `v*.*.*` tag triggers a three-stage pipeline — `python -m build` produces wheel + sdist → the matching `CHANGELOG.md` section is awk-extracted into the [GitHub Release](https://github.com/TMYTiMidlY/portal-mcp-server/releases) body → the artifacts are published to [PyPI](https://pypi.org/project/portal-mcp-server/) via [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC short-lived tokens, no static `PYPI_API_TOKEN`).

The full release procedure, CHANGELOG format constraint, and recovery
playbook live in [`CONTRIBUTING.en.md` § CI & Release automation](./CONTRIBUTING.en.md#ci--release-automation).

## FAQ

### Local edits don't show up in the agent

`uvx portal-mcp-server` launches from PyPI cache. If you modified local code, the agent won't see it — it uses the published PyPI version.

| Where you edited | Will the agent see it? |
|---|---|
| Local working tree | ❌ No. uvx pulls from PyPI, not a local path |
| New version published to PyPI | ✅ Use `uvx portal-mcp-server@latest` or `--refresh` to update the cache |

For local debugging without publishing, point your `.mcp.json`'s `args` at your working tree:

```json
"args": ["--from", "/absolute/path/to/portal-mcp-server", "portal-mcp-server"]
```

(Path must be absolute.) **Don't commit this local path into a shared project-level `.mcp.json`.**

### Connection timeout / Permission denied (publickey)

1. Confirm that `ssh user@host` works from a terminal first
2. Check key permissions: `chmod 600 ~/.ssh/id_ed25519`
3. If using `~/.ssh/config`, verify the `Host` alias, `HostName`, `User`, and `IdentityFile` are correct
4. Jump hosts (ProxyJump): asyncssh natively supports `ProxyJump` from `~/.ssh/config` — make sure the jump host itself is reachable via `ssh`

### Connections drop after MCP client restart

This is expected. The connection pool lives inside the MCP server process. When the MCP client restarts, it stops the server process and the pool is released. The next `portal_*` tool call will automatically reconnect.

### How to update to the latest version

```bash
# Clear uvx cache and re-fetch
uvx portal-mcp-server@latest --help
```

Then restart the MCP client.

## Contributing

Issues and PRs welcome. Quick rules:

- Python 3.10+, all I/O `async/await`, no blocking calls
- No hardcoded hostnames / usernames / IPs / paths
- Every new tool needs a docstring (FastMCP uses it as the MCP description) and an entry in the README "Tools" section (including the collapsible full-signature + source-map tables)
- State-changing tools must call `_gate` and emit `audit_log`
- `pytest tests/ -v` must be green
- Never commit secrets; `examples/hosts.yaml` is the only schema template
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages

The full development setup, new-tool checklist, PR template, and security & privacy rules are in **[`CONTRIBUTING.en.md`](./CONTRIBUTING.en.md)** ([中文](./CONTRIBUTING.md)).

## License & attribution

Apache License 2.0 (see [`LICENSE`](LICENSE)).

Lineage and third-party algorithmic references are tracked in [`NOTICE`](NOTICE):

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0)** — git ancestor; the lower-level modules (asyncssh engine, connection pool, tunnel manager, orchestrator, security policy) are inherited. The 19-tool `portal_*` upper layer is new.
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT)** — algorithmic reference for the SHA-256 hash-protected edit semantics in `remote_text_editor.py`, reimplemented for AsyncSSH SFTP.

> ⚠️ This tool gives an agent SSH access to remote systems. Use it only on systems you own or are authorised to access.
