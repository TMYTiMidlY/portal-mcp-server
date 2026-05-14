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

## Overview

`portal-mcp-server` is forked from [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0). The lower-level SSH/asyncssh engine, connection pool, tunnel manager, multi-host orchestrator, and security policy are inherited from the upstream modules. The upper layer is a fresh agent-first 18-tool surface:

- **2** hash-protected remote file editing tools (`portal_read` / `portal_patch`), with the SHA-256 conflict-detection algorithm referenced from [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT), reimplemented for SFTP
- **6** core IO / search / persistent bash tools
- **10** higher-level tools consolidated via a single `mode` parameter (tunnels, file transfer, multi-host orchestration, playbooks, audit, …)

See [`NOTICE`](./NOTICE) and the [Security](#security) section for full provenance and security posture.

## Highlights

- **Cross-tool connection reuse**: every `portal_*` tool shares the same in-process asyncssh pool; one TCP per host gets reused indefinitely, individual calls amortise to channel creation (~10–30 ms).
- **Same speed on Windows**: no dependency on OpenSSH `ControlMaster`; the pool is plain Python objects, so the three major OSes get identical reuse performance.
- **Persistent bash sessions**: `portal_bash` keeps a `bash -i` per host with cwd / env preserved across calls — the agent doesn't have to rebuild context every command.
- **Hash-protected remote edits**: `portal_read` + `portal_patch` use whole-file SHA-256 plus per-range hashes, write through tmp + `posix_rename` (atomic), then re-hash on disk to refuse stale or concurrent overwrites.
- **Agent-first tool budget**: 18 tools instead of the upstream's 57; the tool-list context drops from ~7.5k tokens to ~2.5k, and `mode` parameters collapse semantically overlapping entries.
- **Built-in security policy**: host allowlist, command blocklist/allowlist (fnmatch), per-host rate limit, and an audit log for every state-changing operation, fail-closed by default.
- **OpenSSH-compatible**: native handling of `~/.ssh/config` aliases, `known_hosts`, ssh-agent — no need to re-register hosts.
- **Zero deployment**: MCP clients launch it directly from GitHub via `uvx`, no clone or venv needed.

## Quick start

```bash
# 1. Register with Claude Code (see "Client integration" for other MCP hosts)
claude mcp add portal -- uvx portal-mcp-server

# 2. Make sure the target host is in ~/.ssh/config or config/hosts.yaml

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
│  Cursor ...) │                    │  │ 18 tools │──►│ security gate  │  │
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
| `portal_bash` / `_close` / `_status` | One sticky `bash -i` per host; cwd / env survive across calls; PTY echo + bracketed-paste disabled so sentinel parsing is reliable |
| `portal_cleanup_tmps` | Garbage-collects orphan `*.mcp_tmp.*` files left by interrupted patches |

### 10 high-level tools (mode-switched)

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

### Agent-side conventions

`portal-mcp-server` only provides tools — it does not enforce how the agent uses them. To make agent behaviour on top of these tools predictable and safe, recommend pinning the following rules in `AGENTS.md` / `CLAUDE.md` or your system prompt:

- **Confirm the host alias first** — if the target host is not in `~/.ssh/config` or `hosts.yaml`, ask the user. Don't just register a new host.
- **Writes go through read → patch** — call `portal_read` for `file_hash` (and `range_hash` per region), then `portal_patch` with the same hashes; on conflict, `portal_patch` returns the new hash — re-read and retry.
- **Default sandbox is `/tmp/`** — writes default to remote `/tmp/`. Ask before touching `$HOME` or project source.
- **Don't mix tools within one task** — pick `portal_*` (hash-protected, pool-reused) *or* `ssh`/`scp` from bash, not both. Mixing them bypasses hash checking or breaks sudo flows.
- **Use the multi-host tools** — `portal_multi_exec(mode="parallel")` / `portal_playbook(group_tag=...)`, not a bash loop of `ssh host1; ssh host2; …`.
- **Interactive sudo goes outside MCP** — operations that require an interactive sudo password can't go through `portal_bash`. Have the user run `ssh -t host sudo …` in a terminal instead.

## Design notes

### Tool consolidation: 18 vs. 57

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

If you'd rather not use uv, plain pip editable install works:

```bash
pip install -e ".[dev]"       # prod + dev (pytest etc.)
# or runtime only
pip install -e .
```

## Client integration

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%22%5D%7D) [![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%22%5D%7D&quality=insiders) [![Install in Cursor](https://img.shields.io/badge/Cursor-Install_Server-000000?style=flat-square&logo=cursor&logoColor=white)](https://cursor.com/en/install-mcp?name=portal&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJwb3J0YWwtbWNwLXNlcnZlciJdfQ==)

`portal-mcp-server` is a local stdio MCP server — any MCP-capable host can install it. Each section below gives the minimal config for a popular host. `uvx` pulls from PyPI and caches automatically — no clone or venv required.

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
  "SSH_HOSTS_YAML": "/path/to/hosts.yaml",
  "SSH_POLICIES_YAML": "/path/to/policies.yaml",
  "SSH_MCP_LOG_DIR": "/path/to/logs"
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

## Configuration

All settings are passed as environment variables. Set them in the `env` field of your MCP client config — they only affect the MCP server subprocess, not anything else on your system.

### File paths

| Env var | Meaning | Default |
|---|---|---|
| `SSH_HOSTS_YAML` | Host registry YAML | `./config/hosts.yaml` if present, else `~/.config/portal-mcp-server/hosts.yaml` |
| `SSH_POLICIES_YAML` | Security policy YAML | `./config/policies.yaml` if present, else `~/.config/portal-mcp-server/policies.yaml` |
| `SSH_MCP_LOG_DIR` | Audit + server log directory | `./logs/` if present, else `~/.local/state/portal-mcp-server/logs/` |

Resolution order: env var > `./config/` or `./logs/` in the current directory (developer-checkout layout) > XDG directories. `config/hosts.example.yaml` is the schema template. **`hosts.yaml` contains real credentials and is in `.gitignore` — never commit it.**

### Security & auth

| Env var | Meaning | Default |
|---|---|---|
| `SSH_MCP_AUDIT_FAIL_OPEN` | Set to `1` → audit-write failures are warnings only; unset → **fail-closed**, audit-write failure aborts the operation | _(unset)_ |
| `MCP_AUTH_TOKEN` | Bearer token for HTTP transport (`--transport streamable_http`); not needed for stdio | _(none)_ |

### Connection pool

Controls the in-process asyncssh connection pool. Defaults work well for most setups; tune only under high concurrency or unusual network conditions.

| Env var | Meaning | Default |
|---|---|---|
| `SSH_POOL_SIZE` | Max TCP connections per host. When the pool is full and every connection is at the channel ceiling, the least-loaded connection is reused (with a warning) | `5` |
| `SSH_MAX_CHANNELS_PER_CONN` | Max concurrent channels (SFTP, exec, tunnel, …) multiplexed over one TCP connection. New connections are opened when exceeded, up to `SSH_POOL_SIZE` | `5` |
| `SSH_MAX_IDLE_TIME` | Close idle connections (no active channels) after this many seconds. Set `0` to disable | `600` (10 min) |
| `SSH_MAX_CONN_AGE` | Max connection lifetime in seconds; aged connections with no active channels are closed. Guards against silent firewall / NAT drops | `3600` (1 hour) |

### Full example

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": ["portal-mcp-server"],
      "env": {
        "SSH_HOSTS_YAML": "/home/me/.config/portal-mcp-server/hosts.yaml",
        "SSH_POLICIES_YAML": "/home/me/.config/portal-mcp-server/policies.yaml",
        "SSH_POOL_SIZE": "10",
        "SSH_MAX_CHANNELS_PER_CONN": "8"
      }
    }
  }
}
```

## Authentication

Pick the path for your setup — SSH keys preferred, encrypted keys via ssh-agent; password auth is supported but goes through `password_command`, so plaintext credentials never reach the LLM.

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

### Password auth: opt-in via `password_command`

Provided for legacy hosts that cannot be re-keyed. Two non-negotiable rules:

1. **Never** write `password: <plaintext>` in `hosts.yaml` — startup logs an ERROR and drops the field.
2. **Never** flow a password through an MCP tool — `portal_host` has no password parameter, so credentials cannot land in LLM tool-call traces.

Configure with `auth: password` plus a shell command that prints the password to stdout — same pattern as Borg's `BORG_PASSCOMMAND`, restic's `RESTIC_PASSWORD_COMMAND`, and msmtp's `passwordeval`:

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

Runtime behaviour: the command runs once per new connection (the pool reuses connections, so it triggers rarely), with a 10-second timeout, exactly one trailing newline stripped, stderr never logged (leak defence), and non-zero exit / empty output / non-UTF-8 output all hard-failing. Design rationale (why `shell=True`, why `client_keys=[]` is forced, why stderr never reaches the logs, …) lives in **[`SECURITY.md` § Authentication](./SECURITY.md#authentication)**.

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

## Security

- **Default sandbox**: writes default to remote `/tmp/`; the agent must ask before touching `$HOME` or project source (a prompt-layer convention — see [Agent-side conventions](#agent-side-conventions)).
- **Policy gate**: host allowlist + command blocklist/allowlist + per-host rate limit; every state-changing tool runs through `_gate` with no side doors (`portal_host(register)` gates against the target IP, not the alias; `portal_tunnel_close` is gated; multi-host gates are two-phase).
- **Authentication**: SSH keys are the default and recommended path; password auth is supported but only via `password_command` in `hosts.yaml`, never exposed through any MCP tool — config in [Authentication](#authentication), security design in [`SECURITY.md` § Authentication](./SECURITY.md#authentication).
- **Audit**: every state-changing operation is appended to `logs/audit.jsonl`; fail-closed by default (`SSH_MCP_AUDIT_FAIL_OPEN=1` switches to fail-open).
- **Hash-protected edits**: `portal_read` + `portal_patch` use SHA-256 + per-range hashes + atomic `posix_rename` + post-write rehash to refuse concurrent overwrites.

The full threat model, layer-by-layer defences, operator hygiene, known limitations, and algorithmic provenance live in **[`SECURITY.md`](./SECURITY.md)**.

Vulnerability disclosure: do **not** open a public issue. Use [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new) instead. Targets: acknowledgement within 48 hours, initial assessment within 7 days, resolution within 30 days for critical issues.


## Testing

### Unit + security (no real SSH required)

```bash
pytest tests/ -v
# live SSH tests skip by default (gated by SSH_TEST_LIVE)
```

Coverage: command-injection regression, safety validators, hash-protected editor, concurrency, resource lifecycle, multi-host policy enforcement, password_command / passphrase_command safety invariants, audit fail mode.

### End-to-end live smoke

`tests/live_smoke.py` imports the local working tree and drives a series of real SSH actions: stale `password:` field handling in `hosts.yaml`, basic `ssh_exec`, `portal_multi_exec(mode="parallel", group_tag=...)` against real hosts (verifying both blocked-command and not-in-allowlist hosts get rejected), per-command gating in `portal_bash`, a `portal_bash` + `portal_patch` round-trip in remote `/tmp/` (including the stale-hash rejection path), and audit.jsonl ingestion of the new operation tags.

```bash
SSH_MCP_AUDIT_FAIL_OPEN=1 \
  TEST_HOST=<your-host> TEST_PORT=22 TEST_USER=<user> \
  TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ It writes one file under remote `/tmp/portal-mcp-server-smoke-<pid>.txt` and removes it at the end. Stays inside `/tmp`.

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
- Every new tool needs a docstring (FastMCP uses it as the MCP description) and an entry in `docs/tools.md`
- State-changing tools must call `_gate` and emit `audit_log`
- `pytest tests/ -v` must be green
- Never commit secrets; `config/hosts.example.yaml` is the only schema template
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages

The full development setup, new-tool checklist, PR template, and security & privacy rules are in **[`CONTRIBUTING.en.md`](./CONTRIBUTING.en.md)** ([中文](./CONTRIBUTING.md)).

## License & attribution

Apache License 2.0 (see [`LICENSE`](LICENSE)).

Lineage and third-party algorithmic references are tracked in [`NOTICE`](NOTICE):

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0)** — git ancestor; the lower-level modules (asyncssh engine, connection pool, tunnel manager, orchestrator, security policy) are inherited. The 18-tool `portal_*` upper layer is new.
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT)** — algorithmic reference for the SHA-256 hash-protected edit semantics in `remote_text_editor.py`, reimplemented for AsyncSSH SFTP.

> ⚠️ This tool gives an agent SSH access to remote systems. Use it only on systems you own or are authorised to access.
