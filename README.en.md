<div align="center">

# portal-mcp-server

**Agent-first SSH orchestration MCP server**

Lets coding agents (Claude Code, Copilot CLI, Cursor, …) drive remote machines as fluently as the local one: persistent bash sessions, hash-protected remote file editing, SFTP, SSH tunnels, multi-host orchestration. Built on [AsyncSSH](https://github.com/ronf/asyncssh) + [FastMCP](https://modelcontextprotocol.io/), with an in-process connection pool shared across every tool — identical reuse performance on Windows, macOS, and Linux.

[![CI](https://github.com/TMYTiMidlY/portal-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/TMYTiMidlY/portal-mcp-server/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen)](https://modelcontextprotocol.io/)
[![Last commit](https://img.shields.io/github/last-commit/TMYTiMidlY/portal-mcp-server)](https://github.com/TMYTiMidlY/portal-mcp-server/commits/main)
[![Issues](https://img.shields.io/github/issues/TMYTiMidlY/portal-mcp-server)](https://github.com/TMYTiMidlY/portal-mcp-server/issues)

[简体中文](./README.md) ｜ English

</div>

---

## Contents

- [Overview](#overview)
- [Highlights](#highlights)
- [Tools](#tools)
- [Design notes](#design-notes)
- [Install](#install)
- [Client integration](#client-integration)
- [Configuration](#configuration)
- [Security](#security)
- [Testing](#testing)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License & attribution](#license--attribution)

## Overview

`portal-mcp-server` is forked from [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0). The lower-level SSH/asyncssh engine, connection pool, tunnel manager, multi-host orchestrator, and security policy are inherited from the upstream modules. The upper layer is a fresh agent-first 18-tool surface:

- **2** hash-protected remote file editing tools (`portal_read` / `portal_patch`), with the SHA-256 conflict-detection algorithm referenced from [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT, clean-room reimplementation)
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

> The companion [`remote` skill](https://github.com/TMYTiMidlY/skills) teaches the agent the read → patch flow, when to default to `/tmp` as a sandbox, and when it should ask first.

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

No clone needed — let your MCP client launch it via `uvx` straight from GitHub. See [Client integration](#client-integration). `uvx` caches deps on first run; subsequent restarts are instant.

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
pytest                        # 144 passed, 22 skipped
```

If you'd rather not use uv, plain pip editable install works:

```bash
pip install -e ".[dev]"       # prod + dev (pytest etc.)
# or runtime only
pip install -e .
```

## Client integration

### Copilot CLI / Claude Code / Cursor

These all share the same `.mcp.json` schema. Drop this into `<project>/.mcp.json`:

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

To override hosts / policies / log paths, add an `env` block:

```json
"env": {
  "SSH_HOSTS_YAML": "/path/to/hosts.yaml",
  "SSH_POLICIES_YAML": "/path/to/policies.yaml",
  "SSH_MCP_LOG_DIR": "/path/to/logs"
}
```

Verify under Copilot CLI:

```bash
cd <project>
copilot mcp list                # → Workspace servers: portal (local)
copilot mcp get portal          # → Source: Workspace (<project>/.mcp.json)
```

> ⚠️ Don't use `copilot mcp add portal -- ...` — it writes to user-level `~/.copilot/mcp-config.json` by default, which leaks into every project. Edit `.mcp.json` directly to keep it project-scoped.

**Claude Code** can use the same `.mcp.json`, but you can also let its CLI / in-session slash command register the server for you (everything still ends up in the same config file):

```bash
claude mcp add portal -- uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git portal-mcp-server
# or, inside a Claude Code session, just type /mcp for the interactive flow
```

**Claude Desktop** uses the same `mcpServers` top-level schema — paste the JSON snippet above under `mcpServers` in `claude_desktop_config.json`.

### VS Code

VS Code uses a different schema (top-level key is `servers`, not `mcpServers`). Write into `.vscode/mcp.json`:

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

## Configuration

| Env var | Meaning | Default |
|---|---|---|
| `SSH_HOSTS_YAML` | Host registry YAML | `./config/hosts.yaml` if present, else `$XDG_CONFIG_HOME/portal-mcp-server/hosts.yaml` |
| `SSH_POLICIES_YAML` | Security policy YAML | `./config/policies.yaml` if present, else `$XDG_CONFIG_HOME/portal-mcp-server/policies.yaml` |
| `SSH_MCP_LOG_DIR` | Audit + server log directory | `./logs/` if present, else `$XDG_STATE_HOME/portal-mcp-server/logs/` |
| `SSH_MCP_AUDIT_FAIL_OPEN` | Set to `1` → audit-write failures are warnings only; unset (default) → **fail-closed**, audit-write failure raises and aborts the operation | _(unset)_ |
| `MCP_AUTH_TOKEN` | Bearer token for HTTP transport | _(none)_ |

`config/hosts.example.yaml` is the schema template. **`hosts.yaml` contains real credentials and is in `.gitignore` — never commit it.**

## Security

### Default constraints

portal-mcp-server does not enforce a path allowlist — that's the job of the companion `remote` skill at the prompt layer:

> **Writes default to remote `/tmp/`. Always ask before touching `$HOME` or project source directories.**

For machine-level enforcement, add rules to `command_blocklist` in `config/policies.yaml` (e.g. `"rm -rf /home/*"`).

### Policy gate

`SecurityPolicy` checks: host allowlist (fnmatch), command blocklist/allowlist (fnmatch), per-host rate limit (sliding window). Every command-execution tool goes through `_gate(host, command)`; multi-host orchestration (`portal_multi_exec` parallel/rolling/broadcast and `portal_playbook` group path) goes through `_gate_many(hosts, command)`, and `playbook` additionally walks every `step` through the blocklist. `portal_bash` gates each command too — a persistent session does **not** authorise arbitrary commands.

Every state-changing entry point is gated; there are no side doors:

- `portal_host(action="register")` gates against the **target host** (the actual IP / DNS the connection will reach), so an agent cannot launder a non-allowlisted target through an alias whose name happens to match `safe-*`. `action="remove"` gates against the alias.
- `portal_tunnel_open` and `portal_tunnel_close` both gate the originating host (the close path resolves it from the active-tunnel record).
- `portal_bash` and `portal_bash_close` both gate the host (and the bash command, for `portal_bash`).
- Multi-host gates are **two-phase**: every host is validated first, only then are per-host rate-limit tokens consumed — a single failing host cannot burn quota on the others.

### Authentication

**Key-based only.** `HostConfig` has no `password` field; `portal_host(action="register", ...)` has no `password` parameter. Stale `password:` keys in `hosts.yaml` are detected at startup, logged at ERROR level, and ignored.

### Audit

All state-changing tools write `logs/audit.jsonl` (exec / file write / patch / register / tunnel / playbook / multi-host orchestration). Read-only tools (`portal_read` / `portal_grep` / `portal_glob` / `portal_audit` / `portal_check` / `portal_tunnel_list`) explicitly do not audit, to keep the log readable.

**Default is fail-closed** — audit-write failure raises and aborts the operation. Set `SSH_MCP_AUDIT_FAIL_OPEN=1` to switch to fail-open (warning only, suitable for dev / test).

> ⚠️ **Honest disclosure on fail-closed semantics**: audit entries are written *after* the underlying operation completes (we need its result to know what to log). So if the disk write fails right after a successful operation, the agent sees a `RuntimeError` even though the remote patch / exec / register has already happened. Fail-closed prevents *subsequent* operations; it cannot roll back the one that just succeeded. For strict transactional auditing, fan out to an OS-level facility (rsyslog, central log collector) downstream.

### Operator hygiene

- Keep SSH private keys at `chmod 600`. Never commit `hosts.yaml` or any file that contains hostnames, usernames, or key paths.
- Run remote targets behind a VPN (e.g. Tailscale) where possible. The MCP server itself only speaks stdio; it opens no network ports.
- Create dedicated SSH users for automated access rather than reusing `root` or personal accounts. Lock them down with `AllowUsers` / `Match` / `ForceCommand` in `sshd_config`.

### Known limitations

- Password-based SSH authentication is not supported by design.
- Host key verification uses the system `known_hosts` by default; disabling it weakens MITM protection.

### Algorithmic provenance

The hash-protected file editing in `portal_mcp_server.remote_text_editor` (`remote_read` / `remote_patch`) is a deliberate port of the safe-edit pattern from [tumf/mcp-text-editor](https://github.com/tumf/mcp-text-editor) (MIT):

| Upstream (`mcp-text-editor`) | Here (`remote_text_editor`) |
|---|---|
| Whole-file SHA-256 conflict detection | identical algorithm, runs over SFTP |
| Line-range patch model | identical model, plus per-patch `range_hash` |
| Single-shot file overwrite | replaced with tmp-file + `posix_rename` (atomic) |
| Local `open(...)` + `portalocker` advisory lock (calls `fcntl.flock` on Linux) | replaced with AsyncSSH SFTP + connection-pool release |

The upstream library is **not** a Python dependency because its `TextEditorService` directly calls `with open(file_path, "r")` and exposes no file-backend interface — it cannot be retargeted to SFTP without forking. The test suite in `tests/test_remote_text_editor.py` mirrors the upstream test matrix (hash mismatch, overlap, beyond-EOF, multi-patch ordering, …) and adds SFTP-specific coverage (`posix_rename` fall-back, post-write rehash, connection release on every exit path).

### Reporting a vulnerability

Please do not open a public GitHub issue for security vulnerabilities. Use [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new) instead. Targets: acknowledgement within 48 hours, initial assessment within 7 days, resolution within 30 days for critical issues.

### Supported versions

| Version | Supported |
|---|---|
| `main` branch | ✅ Active |
| Older tags | ❌ No backports |

## Testing

### Unit + security (no real SSH required)

```bash
pytest tests/ -v
# 144 passed, 22 skipped (live SSH tests gated by SSH_TEST_LIVE)
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

## FAQ

### Local edits don't show up in the agent

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

For local debugging without pushing, point your `.mcp.json`'s `args` at your working tree:

```json
"args": ["--from", "/absolute/path/to/portal-mcp-server", "portal-mcp-server"]
```

(Path must be absolute.) **Don't commit this local path into a shared project-level `.mcp.json`.**

## Contributing

Issues and PRs welcome. Before opening a PR, please:

- Target Python 3.10+; add type hints where practical and keep all I/O `async/await` — no blocking calls
- Don't hardcode hostnames / usernames / IPs / paths — always read from config
- Write docstrings for every new tool (FastMCP uses the docstring as the MCP description) and update `docs/tools.md` when relevant
- Cover the new path with tests; `pytest tests/ -v` must pass
- Never commit secrets, real hostnames, or personal data; `config/hosts.example.yaml` is the only schema template
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages (`feat:` / `fix:` / `docs:` / `chore:` …)

## License & attribution

Apache License 2.0 (see [`LICENSE`](LICENSE)).

Lineage and third-party algorithmic references are tracked in [`NOTICE`](NOTICE):

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0)** — git ancestor; the lower-level modules (asyncssh engine, connection pool, tunnel manager, orchestrator, security policy) are inherited. The 18-tool `portal_*` upper layer is new.
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT)** — algorithmic reference for the SHA-256 hash-protected edit semantics in `remote_text_editor.py` (clean-room reimplementation, no source code copied).

> ⚠️ This tool gives an agent programmatic SSH access to remote systems. **Use only on systems you own or have explicit written authorisation to access.** Unauthorised access is illegal in most jurisdictions.
