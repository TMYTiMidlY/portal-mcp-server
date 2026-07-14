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

> ℹ️ **The Chinese [`README.md`](./README.md) is canonical.** This English
> translation may lag behind it for the latest tool-surface changes; for the
> authoritative 14-tool reference see [`README.md`](./README.md). (Tool count and
> signatures here are current; some narrative prose may still describe
> pre-refactor tool names.)

---

<details>
<summary>📖 Table of Contents</summary>

- [Overview](#overview)
- [Highlights](#highlights)
- [Why portal-mcp-server vs. plain SSH](#why-portal-mcp-server-vs-plain-ssh)
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

`portal-mcp-server` is forked from [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0): the lower-level SSH/asyncssh engine, connection pool, tunnel manager, multi-host orchestrator, and security policy are inherited from the upstream modules. The upper layer is a fresh agent-first `portal_*` tool surface — built around three execution paths (a persistent bash session, one-shot exec, and background jobs) plus primitives for hash-protected remote editing, structured search, SFTP transfer, tunnels, and auditing. The double-hash conflict-detection algorithm behind remote editing (`portal_read` / `portal_patch`) is referenced from [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT) and reimplemented for SFTP.

See [`NOTICE`](./NOTICE) and the [Security](#security) section for full provenance and security posture.

## Highlights

- **Cross-tool connection reuse**: every `portal_*` tool shares the same in-process asyncssh pool; one TCP per host gets reused indefinitely, individual calls amortise to channel creation (~10–30 ms).
- **Same speed on Windows**: no dependency on OpenSSH `ControlMaster`; the pool is plain Python objects, so the three major OSes get identical reuse performance.
- **Persistent shell sessions**: `portal_shell` keeps an interactive shell (bash/zsh) per host with cwd / env preserved across calls, and can run a `commands=[…]` sequence in the same session — the agent doesn't have to rebuild context every command.
- **Hash-protected remote edits**: `portal_read` + `portal_patch` use whole-file SHA-256 plus per-range hashes, write through tmp + `posix_rename` (atomic), then re-hash on disk to refuse stale or concurrent overwrites.
- **Agent-first, minimal tool surface**: `action` / `mode` parameters collapse semantically overlapping entries, and each tool earns its place only by offering a guarantee bash can't cheaply synthesize — so the agent has fewer look-alike tools to choose between. The tool schemas (name + description + inputSchema) total **~8k tokens** (≈ **4%** of a 200k context window; `tiktoken o200k_base` estimate).
- **Built-in security policy**: host allowlist, command blocklist/allowlist (fnmatch), per-host rate limit, and an audit log for every state-changing operation, fail-closed by default; plus an optional [cc-safety-net](https://github.com/kenryu42/cc-safety-net) semantic command gate (opt-in — unwraps `bash -c` / interpreter one-liners, blocks destructive git/rm, and covers `portal_exec`/`local_exec`/`shell`/`job`, which bypass the agent's own `bash` PreToolUse hook; fail-closed by default).
- **OpenSSH-compatible**: native handling of `~/.ssh/config` aliases, `known_hosts`, ssh-agent — no need to re-register hosts.
- **Zero deployment**: MCP clients launch it directly from GitHub via `uvx`, no clone or venv needed.

## Why portal-mcp-server vs. plain SSH

The naive way to give an agent remote access is to let it shell out to `ssh` / `scp` / `rsync`. That "plain" path is barely workable on Linux/macOS with `ControlMaster`, **effectively broken on Windows**, and missing essential affordances around file editing, sudo, multi-host orchestration, and audit. The table below puts the key differences in one place — each row is a concrete pitfall an agent hits with the plain approach and how portal-mcp-server addresses it.

| Dimension | Plain (bash + `ssh` / `scp` / `rsync`) | portal-mcp-server |
|---|---|---|
| **SSH reuse · Linux/macOS** | OpenSSH `ControlMaster auto` + Unix socket; default `ControlPersist 10m`, master dies after that | asyncssh **in-process pool**; reused for as long as the MCP server lives (hours) |
| **SSH reuse · Windows** | ❌ **Doesn't work** — Microsoft's Win32-OpenSSH port has had `ControlMaster` broken since v0.0.3.0 (`muxclient socket(): Unknown error`), [issue #405](https://github.com/PowerShell/Win32-OpenSSH/issues/405) open since 2017 (the implementation needs Unix-domain-socket fd sharing, which Windows lacks) | ✅ **Identical to Linux** — the pool is a plain Python dict; asyncssh needs no OS-level socket sharing |
| **First connect / subsequent latency** | First ~200–500ms; **without reuse every command is a fresh TCP+auth, ~300ms each** (the default on Windows); with ControlMaster, ~10–30ms after the first | First ~200–500ms, **then ~10–30ms (same on all three OSes)** — just channel creation |
| **Cross-"tool" reuse** | `ssh` and `scp` only reuse a master if `ControlPath` matches exactly; in practice each binary opens its own connection | ✅ Every `portal_*` tool (bash / read / patch / transfer / tunnel …) naturally shares the same TCP |
| **Persistent shell state** | Every `ssh host cmd` is a new shell; `cd` / `export` / venv activation **all reset**; the agent has to prepend `cd /path && source venv/bin/activate && ...` to every command | ✅ `portal_shell` keeps a sticky interactive shell (bash/zsh); cwd / env / venv survive across calls |
| **Remote file editing (safe edit)** | All three options are unsafe: ① `scp` down → edit → `scp` up (no concurrency check, concurrent writer's changes silently lost, non-atomic); ② `ssh host "sed -i ..."` (no dry-run, no rollback, line numbers brittle); ③ `ssh host "cat > file"` (concurrent overwrite, half-written file if the connection drops mid-write) | ✅ `portal_read` returns SHA-256 + per-range hashes; `portal_patch` checks the hashes → writes to `*.mcp_tmp.*` → atomic `posix_rename` → re-hashes after write. **Concurrent edits / interrupted writes / line-number drift all fail instead of silently corrupting** |
| **File / directory transfer** | `scp` has no incremental skip and a single failure kills the batch; `rsync` is better but forks a new process per invocation, and **its progress never reaches the agent** — MCP clients drop the connection on idle timeout during long transfers | ✅ `portal_transfer` does size+mtime (or sha256) incremental skipping, **emits MCP progress as a keepalive against idle timeout**, lands per-file failures in `failed[]` without aborting the batch, and `paths_json` supports arbitrary local↔remote pair batches |
| **sudo password ergonomics** | All options are bad: ① `ssh -t host sudo cmd` **prompts every time** — the agent can't drive it; ② `echo $PASS \| ssh host "sudo -S cmd"` — **password lands in the LLM context**; ③ `sshpass -p $PASS ssh ...` — **password ends up in `ps` argv and the LLM**; ④ NOPASSWD sudoers — give up on auth entirely | ✅ `portal_exec(use_sudo=True)`: password source is either ① `sudo_password_command` (pulled from `pass` / `op` / `bw` on demand, fully automatic) or ② `portal sudo set <host>` (user types it once in another terminal via `getpass`, stored in the systemd `--user` credential agent's in-memory TTL cache). **Password never reaches the LLM, never appears in `ps` argv, never hits disk** |
| **Multi-host parallel execution** | `for h in $hosts; do ssh $h cmd; done` — **serial** startup (one fork+auth per host), no policy gate, a single failure depends on `set -e` or hand-rolled error handling | ✅ `portal_exec(host=[...])` runs in true parallel with a two-phase safety gate (check every host *first*, then execute); `serialize=True`+`delay_s` does a rolling rollout, and a `commands` sequence covers multi-step playbooks with `stop_on_error` |
| **SSH tunnel lifecycle** | `ssh -L 8080:db:5432 host -fN` runs unsupervised — **no one tracks when to close it**, who opened it, or whether it's still alive; you need `pgrep` to find it | ✅ `portal_tunnel(action=open)` returns a `tunnel_id`, `action=list` enumerates live tunnels, `action=close` shuts one down; everything is auditable |
| **Command audit** | None — you'd have to wrap shell history with `script(1)` or a custom logger; agent calls are invisible | ✅ State-changing tools first pass the `_gate` policy check (blocked → not executed, not logged), then write a structured line to `audit.jsonl` (host, operation, command, result, timestamp); audit-write failure is fail-closed by default (operation aborts), relax with `PORTAL_AUDIT_FAIL_OPEN=1` |
| **Structured search** | `ssh host "grep -rn ... \| head"` returns **raw text the agent must parse**; degrades gracefully only if you remember to install rg | ✅ `portal_grep` / `portal_glob` prefer `rg --json`, fall back to `grep -rn` / `find` automatically, and return `{file, line, text}` structured output |

> **Windows users, this matters**: the "SSH reuse · Windows" row above isn't a footnote — it's a **fundamental gap**. The default Windows OpenSSH client has no ControlMaster, so every remote command an agent issues pays the ~300 ms TCP+auth tax; fifty calls is fifteen seconds of pure overhead. portal-mcp-server is ~280 ms first call and ~20 ms thereafter on Windows, identical to Linux — which is why we recommend it over a `ssh` subprocess approach by default.

## Quick start

```bash
# 1. Register with Claude Code (see "Client integration" for other MCP hosts)
claude mcp add portal -- uvx portal-mcp-server@latest

# 2. Make sure the target host is in ~/.ssh/config or hosts.yaml
#    (hosts.yaml defaults to ~/.config/portal-mcp-server/hosts.yaml;
#     override with PORTAL_HOSTS_YAML — see "Environment variables")

# 3. Use it in an agent conversation
#    "Show me the last 50 lines of /var/log/syslog on myhost"
#    → agent calls portal_shell("myhost", "tail -50 /var/log/syslog")
```

No clone, no venv — `uvx` pulls and runs automatically. For developer setup see [Install](#install).

## Architecture

```
┌──────────────┐    stdio / SSE     ┌─────────────────────────────────────┐
│  MCP Client  │ ◄────────────────► │       portal-mcp-server             │
│ (Claude Code │                    │                                     │
│  Copilot CLI │                    │  ┌──────────┐   ┌────────────────┐  │
│  Cursor ...) │                    │  │ 14 tools │──►│ security gate  │  │
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

14 tools. The keep/drop test: **only keep guarantees the agent can't cheaply synthesize itself** (concurrency, atomic/hash anti-conflict, no credential leakage, the security gate, real structured output); anything that "just packages a script/state" (playbook, ping, rolling-as-a-tool, standalone tmp cleanup) is deleted or folded into a primitive.

### Running commands: the exec family (pick by "stateful / local / sync vs async")

| Tool | When to use it |
|---|---|
| `portal_exec` | **Default workhorse.** Stateless one-shot, result immediately (**split** stdout/stderr + exit code). `host` can be one host / a list / a `group_tag`; one `command` or a `commands` sequence; multi-host is parallel by default, `serialize=True` (+`delay_s`) does a rolling rollout; `use_sudo` / `secrets` inject credentials out-of-band. Reuses the connection pool — fast. |
| `portal_shell` | Only when you need **cwd/env to persist across calls** (`cd`/`export`/venv) — one sticky interactive shell (bash/zsh) per host, with optional `commands=[…]` multi-step (state carried across steps). Output is the **combined** stream (a PTY merges stdout/stderr). Otherwise use `portal_exec` (faster, multi-host). |
| `portal_job` | **Background** long tasks. `submit` returns a `job_id` instantly (remote `nohup` + tmp files, **keeps running even if the connection drops**), `poll` fetches incremental output / status, `cancel` kills, `list` lists. Job table is best-effort persisted, bounded, TTL-swept; the background path does **not** support sudo/secrets (use `portal_exec`). |
| `portal_local_exec` | Run on the **MCP server's own machine** (not over SSH). OFF by default — an off-target derivative of the project's remote-driving goal — so the operator must explicitly set `PORTAL_ALLOW_LOCAL_EXEC=1`. Only for tasks that genuinely belong on the server host. `use_sudo=True` runs it under local `sudo -S -k` (reserved `<local>` identity; password from `portal sudo set-local` or a top-level `<local>:` section's `sudo_password_command`), and may be combined with `secrets`. |
| `portal_close_shell` | Close a host's sticky `portal_shell` session (the next `portal_shell` reopens). Rarely needed — only to reset a dirtied session. |

> **★ `use_sudo` and `secrets` can be combined** — both `portal_exec` (remote) and `portal_local_exec` (local) support elevating privileges and injecting a secret in the same call; behavior is consistent across both paths.

> **★ Two layers of "reuse" — don't conflate them**: *connection reuse* = asyncssh's TCP/channel pool, shared by **every** tool, purely for **speed** (~280ms first connect, ~10-30ms per call after); *session reuse* = the one sticky interactive shell (bash/zsh) per host that only `portal_shell` uses, for **state continuity**. That shell session rides on a pooled channel; the two are orthogonal. And because the session is implicit plumbing, its state table lives in `portal_audit(view="sessions")` rather than carrying its own `list` the way tunnel/host/job do.

### File editing / search / transfer

| Tool | What the agent gets |
|---|---|
| `portal_read` / `portal_patch` | Read a remote file and get its SHA-256; patch uses `file_hash` + per-range hash to prevent concurrent overwrite, writes via tmp + `posix_rename` (atomic) and re-hashes after write. **After a successful patch it opportunistically sweeps orphan `*.mcp_tmp.*` files >1h old in the same directory** (free-riding the already-open SFTP session, fully exception-isolated so it never affects the patch result) — which is why there is no standalone cleanup tool. |
| `portal_grep` | A faithful port of Claude Code's Grep: `output_mode=files_with_matches` (default, paths newest-first by mtime) / `content` (matching lines + optional context, `head_limit` caps the **total** line count, `offset` paginates) / `count`. Clear parameter names (`before_context`/`after_context`/`context`/`ignore_case` instead of CC's `-B`/`-A`/`-C`/`-i`), respects `.gitignore`, every result carries a `truncated` flag. **Don't run raw `rg` through `portal_exec`.** |
| `portal_glob` | A faithful port of CC's Glob: `rg --files --no-ignore --sort modified -g`, **newest-first by mtime**, hard cap 100, with `truncated`, returns `{filenames, num_files, truncated, duration_ms}`. Does NOT respect `.gitignore` (CC Glob's default). **Don't run raw `find` through `portal_exec`.** |
| `portal_transfer` | `direction=upload\|download\|sync\|mirror\|upload-list\|download-list`. Binary-safe SFTP; `sync` pushes a dir, `mirror` pulls one, `*-list` moves a batch of arbitrary local↔remote file pairs from `paths_json`, all skipping unchanged files by size+mtime (`checksum=True` switches to sha256); a single file's failure lands in `failed[]` without aborting the batch; big transfers use MCP progress as a keepalive against client idle timeouts. |

### Resources (agent-managed, so `list` rides with the tool)

| Tool | action / params | Purpose |
|---|---|---|
| `portal_host` | `action=list\|register\|remove` | Host registry. `register` needs `name`+`host` — or just `name` (if `~/.ssh/config` has a matching Host alias, it's registered with `use_ssh_config` overlay). `tags` feed `portal_exec`'s `group_tag`. `list` also enumerates `Host` aliases from ssh config (resolving real `HostName`/`User`/`Port`); every entry carries a `source` field (`hosts.yaml`/`runtime`/`ssh-config`/`…+ssh-config`) and may carry per-host `warnings` (e.g. a hosts.yaml↔ssh-config conflict) — relay them to the user. **No password parameter.** |
| `portal_tunnel` | `action=open\|close\|list`, `kind=local\|reverse\|socks` | Single-entry SSH tunnels (mirrors `portal_host`). `action` picks the operation, `kind` the tunnel type. `open` goes through the host gate; `close` takes a `tunnel_id` (gated on the source host). |

### Introspection / policy

| Tool | view / params | Purpose |
|---|---|---|
| `portal_check` | `host`, optional `command` | Security-policy dry-run, doesn't execute. Returns `ALLOWED` / `BLOCKED: <reason>`. ⚠️ The default policy is **permissive** — `ALLOWED` only means "no rule currently blocks it", not "this is safe". |
| `portal_audit` | `view=snapshot\|server\|sessions\|history\|stats\|policy` | The read-only introspection **hub**: server metadata + connection pool + bash sessions + audit stats + policy. **hosts/tunnels are NOT here** — they're resources, listed by `portal_host(action=list)` / `portal_tunnel(action=list)`. The `sessions` view is plumbing diagnostics (the host→session_id sticky-session table). |

### Which to use: a purpose-built tool vs `portal_exec`/`portal_shell`

`portal_exec` can run anything, but **don't reach for a raw command when a purpose-built tool exists** — the specific tools either carry a safety guarantee or return structured output:

| What you want to do | Use this (**not** a raw command) | Why |
|---|---|---|
| Read / edit a remote file | `portal_read` → `portal_patch` | SHA-256 + per-range hash against concurrent overwrite, atomic rename, post-write rehash |
| Search content / find files | `portal_grep` / `portal_glob` | structured JSON + token guardrails; don't run raw `rg`/`find` through `portal_exec` |
| Transfer files / sync dirs | `portal_transfer` | binary-safe SFTP + incremental skip + progress keepalive |
| Run on many hosts | `portal_exec(host=[...])` / `group_tag=` | parallel / rolling + two-phase gating; a bash `for h; ssh $h` loop has no gate |
| Open a tunnel | `portal_tunnel` | managed lifecycle, listable; a bash `ssh -L` runs away unsupervised |
| Background a long task | `portal_job` | exposes state + hands back control, poll/cancel-able; a raw `nohup &` is lost once it detaches |

Rule of thumb: **don't mix `portal_*` and bash `ssh`/`scp` in the same task**, or you bypass hash checking or break the sudo flow.

### Agent-side conventions

`portal-mcp-server` only provides tools — it does not enforce how the agent uses them. To make agent behaviour on top of these tools predictable and safe, recommend pinning the following rules in `AGENTS.md` / `CLAUDE.md` or your system prompt:

- **Confirm the host alias first** — if the target host is not in `~/.ssh/config` or `hosts.yaml`, ask the user. Don't just register a new host.
- **Writes go through read → patch** — call `portal_read` for `file_hash` (and `range_hash` per region), then `portal_patch` with the same hashes; on conflict, `portal_patch` returns the new hash — re-read and retry.
- **Default sandbox is `/tmp/`** — writes default to remote `/tmp/`. Ask before touching `$HOME` or project source.
- **Don't mix tools within one task** — pick `portal_*` (hash-protected, pool-reused) *or* `ssh`/`scp` from bash, not both. Mixing them bypasses hash checking or breaks sudo flows.
- **Use the multi-host tool** — `portal_exec(host=[...])` / `group_tag=...`, not a bash loop of `ssh host1; ssh host2; …`.
- **Sudo, three ways** — when sudo is needed: ① prefer a host-level `sudo_password_command` (pulled from a password manager, fully automatic); ② or have the user pre-seed the password with `portal sudo set <host>` into the per-user credential agent from another terminal, then `portal_exec(..., use_sudo=True)`; ③ for genuinely interactive prompts (password change, first-time TTY check), have the user run `ssh -t host sudo …`. If a password-auth host's sudo password is explicitly the same as its SSH login password, set `sudo_password_same_as_ssh: true` in `hosts.yaml`; after that, `portal ssh set <host>` also seeds the sudo cache. The default remains separate prompts. `use_sudo` runs a one-shot exec and does **not** inherit `cwd` / env from prior `portal_shell` calls.
- **Edit root-owned files with `portal_patch(use_sudo=True)` / `portal_read(use_sudo=True)`** — plain SFTP writes as the login user and can't touch a root file. The `use_sudo` implementation: `sudo cat` to read (exact bytes, hash-valid), content staged over ordinary SFTP into the user's **private home** (not /tmp → no TOCTOU), then one sudo step (`cp` → temp beside the target → restore owner/group/mode → atomic rename). It keeps the patch's **full double-hash safety + atomicity and restores the original owner/mode**; the target must already exist; the result is flagged `high_risk`.
- **For work that must survive the agent closing, use `portal_job`, not `portal_exec`** — this is the exec-vs-job **design distinction**: `portal_exec` / `portal_shell` are foreground/synchronous and run inside the server process, so when the agent (stdio client) stops the server dies with it and the in-flight command is cancelled; `portal_job` `nohup`s the command on the **remote** and persists `jobs.json` across restarts, so it keeps running after the agent stops and stays pollable/cancellable on reconnect. Foreground exec/transfer do not self-persist; an interrupted large upload is cheaply recovered by re-issuing it (`resume`, see `portal_transfer`).

<details>
<summary>📋 Full per-tool reference (signatures · return shapes · source map)</summary>

> The model-visible signature of every tool (`ctx` is the MCP progress / keepalive context, injected by FastMCP on the async tools — it **never** appears in the schema below). Every host-targeting tool takes a `host`, resolved hosts.yaml / runtime registry → OpenSSH client config (reusing asyncssh's `SSHClientConfig`, Include-aware) — see [Environment variables → File paths](#file-paths). State-changing tools write to `audit.jsonl`; read-only tools (`portal_read` / `portal_grep` / `portal_glob` / `portal_check` / `portal_audit` and the read actions of `portal_tunnel` / `portal_job`) are **intentionally not audited**.

### Running commands: the exec family

| Tool | Signature | Return shape / key behavior |
| --- | --- | --- |
| `portal_exec` | `(host='' \| [host…], command='', commands=None, group_tag='', *, timeout, login=None, use_sudo=False, secrets=None, serialize=False, delay_s=0.0, stop_on_error=True)` | Stateless one-shot over the connection pool. **Single host + single command → one dict** (**split** stdout/stderr + exit code); multiple hosts / a `commands` sequence → a **list** (a multi-command host carries `{host, results:[…]}`). `timeout` is **required** (no default; a value over the `PORTAL_MAX_TIMEOUT` ceiling is refused → use `portal_job`); `login` defaults to a login shell (`bash -lc`). Parallel / rolling fan-out and `use_sudo` / `secrets` semantics: see the tool list above and [Authentication](#authentication). |
| `portal_shell` | `(host, command='', commands=None, stop_on_error=True, *, timeout)` | One persistent interactive shell per host. Single command → `{host, session_id, command, exit_code, output, duration_s}` (`output` is the PTY-merged stream, capped + flagged `truncated` when oversize); `commands=[…]` runs in the SAME session → `{host, session_id, results:[…], duration_s}`, stopping at the first failure with `stopped_at` when `stop_on_error`. A command wedged on an interactive prompt is auto-Ctrl-C'd → `exit_code:-1` + `error:"interactive_prompt_blocked"` + `session_preserved:true`. `timeout` is **required**. Boundary protocol: see **Design notes · Persistent shell sessions** below. |
| `portal_job` | `(action=submit\|poll\|cancel\|list, host='', command='', job_id='', since=0, tail=0, max_bytes=65536, signal=TERM\|KILL, use_sudo=False, secrets=None)` | `submit` returns a `job_id` instantly (remote `nohup` + tmp files, survives a dropped connection); `poll` pages on demand (`since=<offset>` returns only newer bytes, capped at `max_bytes` — default 64 KiB — with a `more` flag; or `tail=N`), base64-transferred + boundary-aware UTF-8 decoded; `cancel` signals it, `list` shows all. Job table is best-effort persisted across restarts, bounded, TTL-swept (`PORTAL_JOB_*`). `use_sudo` / `secrets` are **background-unsafe — passing them is rejected** with a redirect to `portal_exec`. |
| `portal_local_exec` | `(command, secrets=None, use_sudo=False, *, timeout)` | Runs on the **MCP server's own machine** (**not** over SSH); OFF by default unless `PORTAL_ALLOW_LOCAL_EXEC=1`. `timeout` is **required** (same `PORTAL_MAX_TIMEOUT` ceiling; over-ceiling is refused with no background fallback). `use_sudo=True` runs under local `sudo -S -k` with the reserved **`<local>`** identity (≠ the ordinary SSH host `local` / `localhost`; password from `portal sudo set-local` or a top-level `<local>:` section in hosts.yaml), may be combined with `secrets`, flags `high_risk`. |
| `portal_close_shell` | `(host)` | Close a host's cached `portal_shell` session (the next `portal_shell` reopens). Rarely needed — only to reset a dirtied session. |

### File editing (hash-protected)

| Tool | Signature | Return shape / key behavior |
| --- | --- | --- |
| `portal_read` | `(host, path, start=1, end=None, limit=None, encoding='utf-8', use_sudo=False)` | → `{content, file_hash, range_hash, start, end, total_lines, truncated}`; the two SHA-256 hashes are required by `portal_patch`. **Paged**: at most `limit` lines per call (default `PORTAL_READ_MAX_LINES=2000`) + `PORTAL_READ_MAX_BYTES` (default 16384) of content; a page cut short sets `truncated=true` and `next_start` gives the continuation line. Pages cut on line boundaries, so `range_hash` stays valid for a follow-up patch. `use_sudo=True` reads a root-only (600) file via `sudo cat`, preserving exact bytes (hash stays valid); flags `high_risk`. |
| `portal_patch` | `(host, path, file_hash, patches_json, encoding='utf-8', auto_newline=False, use_sudo=False)` | Hash-protected line-range patch: if the file changed since `portal_read` it's rejected (returns `current_file_hash`) and left untouched; patches apply bottom-to-top, overlap is rejected, writes go through `*.mcp_tmp.<12hex>` + `posix_rename` (atomic) and re-hash after write. After a successful write it opportunistically sweeps orphan tmp files >1h old in the same dir (free-riding the open SFTP session, isolated; swept paths under an optional `swept` key). `use_sudo=True` edits a root-owned file: `sudo cat` read + SFTP-stage into the user's private home + one sudo step (`cp` → temp beside target → restore owner/group/mode → atomic rename), keeping the same hash safety + atomicity; target must pre-exist; flags `high_risk`. `patches_json` = `[{"start":int,"end":int\|null,"contents":str,"range_hash":str}, …]`. |

### Remote search (Claude-Code-faithful)

| Tool | Signature | Return shape / key behavior |
| --- | --- | --- |
| `portal_grep` | `(host, pattern, path='.', glob='', file_type='', output_mode=files_with_matches\|content\|count, ignore_case=False, before_context=0, after_context=0, context=0, head_limit=250, offset=0, multiline=False)` | Regex content search (`rg`, fallback `grep`). `output_mode`: `files_with_matches` (default, paths newest-first) / `content` (matching lines + optional context, `head_limit` caps the **total** line count, `offset` paginates) / `count`. Respects `.gitignore`, every result carries `truncated`. Clear parameter names instead of CC's `-A`/`-B`/`-C`/`-i`. |
| `portal_glob` | `(host, pattern, path='.')` | Find files by glob, `rg --files --no-ignore --sort modified -g`, **newest-first**, hard cap 100 + `truncated` → `{filenames, num_files, truncated, duration_ms}`. Does NOT respect `.gitignore` (matches CC Glob). |

### File transfer (SFTP)

| Tool | Signature | Return shape / key behavior |
| --- | --- | --- |
| `portal_transfer` | `(direction=upload\|download\|sync\|mirror\|upload-list\|download-list, host, local_path, remote_path, checksum=False, paths_json='', resume=True)` | Binary-safe, atomic SFTP. Single-file modes (`upload`/`download`) → `{status, direction, host, bytes, duration_s, …}`; incremental modes (`sync` pushes a dir / `mirror` pulls one / `*-list`) skip files matching size+mtime (`checksum=True` switches to sha256) → `{status, uploaded\|downloaded, skipped, failed[], bytes_total, bytes_transferred, duration_s}`, a single file's failure landing in `failed[]` without aborting. **upload resume** (`resume=True`, default): when a smaller partial exists remotely, only the missing tail is appended, then the whole file's sha256 is verified and re-uploaded fresh once on mismatch (`resumed`/`restarted_after_mismatch` in the result); `resume=False` forces a fresh overwrite. `*-list` needs `paths_json` = `[{"local":…,"remote":…}, …]`; copies regular files only. |

### Resources (agent-managed)

| Tool | Signature | Return shape / key behavior |
| --- | --- | --- |
| `portal_tunnel` | `(action=open\|close\|list, kind=local\|reverse\|socks, host='', tunnel_id='', local_port=0, local_bind='127.0.0.1', remote_host='', remote_port=0)` | `open` opens a tunnel through `host`: `local` forwards `localhost:local_port → remote_host:remote_port`, `reverse` exposes `local_bind:local_port` as `host:remote_port`, `socks` is a SOCKS5 proxy. `close` by `tunnel_id` (gated on the source host); `list` shows all active tunnels. |
| `portal_host` | `(action=list\|register\|remove, name='', host='', user='root', port=22, key_path='', tags='')` | Runtime host registry. `register` needs `name`+`host` — or just `name` (if `~/.ssh/config` has a matching `Host` alias it auto-registers a `use_ssh_config` overlay). `tags` (comma-separated) feed `group_tag`. `list` also enumerates ssh-config aliases (resolving real `HostName`/`User`/`Port`), each entry carrying a `source` (`hosts.yaml`/`runtime`/`ssh-config`/`…+ssh-config`) + optional per-host `warnings` — relay them to the user. **No password parameter.** |

### Introspection & policy

| Tool | Signature | Return shape / key behavior |
| --- | --- | --- |
| `portal_check` | `(host, command='')` | Security-policy dry-run, doesn't execute → `"ALLOWED"` or `"BLOCKED: <reason>"`. ⚠️ The default policy is **permissive** — `ALLOWED` only means "no rule currently blocks it", not "this is safe". |
| `portal_audit` | `(view=snapshot\|server\|sessions\|history\|stats\|policy, limit=50, host_filter='')` | Read-only introspection of server **plumbing** + history. `snapshot` (metadata + pool + bash sessions + audit stats + policy) / `server` (version / metadata only) / `sessions` (the `host→session_id` map of persistent bash sessions) / `history` (last `limit` entries, filterable) / `stats` (counts by operation) / `policy`. **hosts / tunnels are NOT here** — they're resources, listed by `portal_host` / `portal_tunnel`'s own `list`. |

> **Credential CLIs (out-of-band, not MCP tools).** The agent never sees a credential value. Passwords / passphrases / secrets are provisioned by a human in a separate terminal with `portal {ssh,sudo,passphrase,secret} set` and held by a per-user agent; `show` / `list` return a sha256[:16] fingerprint + TTL, `confirm` re-prompts and compares. Full mechanism, cross-platform auto-install and the "plaintext never leaves the agent" principle: see [Authentication](#authentication) and [Credential agent](#credential-agent-linux-systemd--macos-launchd--windows-scheduled-task).

### Source map

| Module | Tools / responsibility |
| --- | --- |
| `connection_manager.py` | connection pool + host registry shared by every tool |
| `shell_engine.py` | `portal_exec` (one-shot `ssh_exec`) |
| `remote_bash.py` | `portal_shell` / `portal_close_shell` + the one-shot sudo/secrets paths |
| `session_manager.py` | persistent interactive-shell sessions (bash/zsh; cwd/env, exit codes, OSC 133 boundary protocol, soft-cancel) |
| `job_manager.py` | `portal_job` (background submit/poll/cancel/list) |
| `local_exec.py` | `portal_local_exec` |
| `remote_text_editor.py` | `portal_read`, `portal_patch` (+ orphan tmp sweep) |
| `remote_search.py` | `portal_grep`, `portal_glob` |
| `file_ops.py` | `portal_transfer` |
| `network_tools.py` | `portal_tunnel` |
| `credential_agent.py` | the per-user socket-activated TTL cache for `portal {ssh,sudo,secret} set` |
| `ssh_creds.py` / `sudo_creds.py` / `secrets_store.py` | credential resolution + output redaction |
| `security.py` | `_gate()` / `_gate_exec()` policy gates |
| `audit.py` | `audit_log()` writes + `portal_audit` introspection |

</details>

<details>
<summary>🔀 Migrating from old tool names</summary>

> This refactor renamed / merged / deleted a batch of tools (breaking, but commits deliberately don't mark `!` — the version is hand-controlled). Mapping:

| Old | New |
|---|---|
| `portal_bash(host, cmd)` | `portal_shell(host, cmd)` (persistent session) or `portal_exec(host, cmd)` (one-shot, faster) |
| `portal_bash(..., use_sudo=True / secrets=[…])` | `portal_exec(..., use_sudo=True / secrets=[…])` |
| `portal_bash_close` | `portal_close_shell` |
| `portal_multi_exec(mode=parallel, hosts_json=…)` | `portal_exec(host=[…])` |
| `portal_multi_exec(mode=rolling, …)` | `portal_exec(host=[…], serialize=True, delay_s=N)` |
| `portal_multi_exec(mode=broadcast, commands_json=…)` | `portal_exec(host=[…], commands=[…])` |
| `portal_playbook(host=…/group_tag=…)` | `portal_exec(host=…/group_tag=…, commands=[…])` |
| `portal_ping(hosts_json=…)` | `portal_exec(host=[…], command="echo pong")` |
| `portal_tunnel_open/_close/_list` | `portal_tunnel(action=open\|close\|list, kind=…)` |
| `portal_cleanup_tmps` | removed — `portal_patch` sweeps same-directory orphan tmps on success |
| `portal_bash_status` | `portal_audit(view="sessions")` |
| — | **new** `portal_job(action=submit\|poll\|cancel\|list)` for background tasks |

</details>

## Design notes

### Tool consolidation: few and orthogonal

Anthropic's [_Writing Tools for Agents_](https://www.anthropic.com/engineering/writing-tools-for-agents) is explicit:

> "More tools don't always lead to better outcomes... Tools that merely wrap existing software functionality is a common error... Too many tools or overlapping tools can also distract agents from pursuing efficient strategies."

So portal-mcp-server collapses the tool surface to a **few orthogonal** primitives. The test is a single one: **a tool is kept only when it provides a guarantee bash can't cheaply synthesize** (concurrency safety, atomic / hash-protected writes, credential non-leakage, the security gate, real structured output). Anything that's just a one-line bash wrapper with overlapping semantics gets no tool of its own — it's covered by `portal_shell` (a persistent bash session) and `portal_exec` (one-shot, incl. multi-host fan-out / sudo / secrets). Each remaining tool holds exactly one such guarantee:

| Tool surface | The "bash can't cheaply synthesize this" guarantee |
|---|---|
| `portal_read` + `portal_patch` | Whole-file SHA-256 + per-range hashes, replacing the concurrent-overwrite and mid-stream-disconnect holes of raw `cat` / `sed` / `> file` |
| `portal_grep` / `portal_glob` | Structured output faithfully ported from Claude Code's search schema (`rg --json` first, auto-fallback to `grep` / `find`) — the agent never parses raw text |
| `portal_shell`(`_close`) / `portal_exec` | Persistent shell + exit codes; one-shot + true parallel multi-host fan-out + a two-phase security gate + credential non-leakage |
| `portal_transfer` | SFTP incremental short-circuit (size+mtime or sha256) + progress heartbeat against idle timeouts + per-file fault tolerance |
| `portal_job` | Background submit/poll/cancel/list — giving the agent the ability to background work, think, and interrupt at will |
| `portal_tunnel` / `portal_host` / `portal_audit` | An `action` / `view` field folds a resource's several actions into one tool instead of one tool per action |

All dispatch parameters (`action` / `view` / `output_mode` / ...) are annotated with `typing.Literal`, so the schema carries `enum` and clients can validate — the agent never has to disambiguate between semantically overlapping tools. **Tool-schema context footprint**: the tools' name + description + inputSchema total **~8k tokens** (`tiktoken o200k_base` estimate, ≈ **4%** of a 200k context window; the descriptions carry sudo / secrets / safety-convention guardrail prose, so they run thick).

### In-process connection pool

portal-mcp-server runs an asyncssh connection pool inside its own server process. Every tool invocation (`portal_shell`, `portal_read`, `portal_transfer`, …) shares the same TCP. **Everything except the first connect amortises down to channel creation (~10–30 ms).** The full comparison against `ControlMaster` / Windows OpenSSH / `ssh`↔`scp` reuse / persistent shell / cross-platform behaviour is already laid out in [§ Why portal-mcp-server vs. plain SSH](#why-portal-mcp-server-vs-plain-ssh) above; below are just the mechanism-level details:

- **Pool shape**: `PORTAL_SSH_POOL_SIZE` caps TCP connections per host (default 5); `PORTAL_SSH_MAX_CHANNELS_PER_CONN` caps channels per TCP (default 5). When all connections hit the channel ceiling, the least-loaded one is reused with a warning. asyncio gives **true multi-channel parallelism over a single TCP**, unlike plain ssh where each parallel command requires a separate ssh process (fork + auth per channel).
- **Idle / age**: `PORTAL_SSH_MAX_IDLE_TIME` defaults to 600 s, `PORTAL_SSH_MAX_CONN_AGE` defaults to 3600 s — idle-expired or aged-out connections close once they have no active channels, guarding against silent NAT / firewall drops.
- **Long-session stability**: pool connections live as long as the MCP server (typically hours), not the 10-minute `ControlPersist` default — fewer reconnect spikes inside long sessions.
- **Anonymised microbenchmark**: same LAN (< 1 ms RTT), 100× `echo pong`. Plain ssh + ControlMaster averaged 23 ms/call; portal-mcp-server through `portal_shell` averaged 18 ms/call (no ssh client process startup). First connect ~280 ms on both (auth dominated).
- **What this looks like on Windows**: plain ssh pays ~300 ms × N (no reuse — and the experimental named-pipe fallback is also unreliable); portal-mcp-server is ~280 ms for the first call and ~20 ms thereafter, dropping to the channel-creation floor — because asyncssh is pure Python and the pool lives in the MCP server's own memory with zero OS-level socket-sharing dependency (which is exactly where Windows OpenSSH's ControlMaster falls over).

### Persistent shell sessions: command boundaries borrowed from OSC 133 (the iTerm2 / VS Code scheme)

`portal_shell` hands the agent a **per-host, cross-call-persistent** `bash -i` / `zsh -i` — cwd, env, and shell functions survive between calls automatically (it's literally the same process). This is the **second layer of reuse** beyond the connection pool above: the pool reuses TCP channels for **speed**, the persistent session reuses one interactive shell for **state continuity** (the ★ note in the tool list above calls these two layers out explicitly as "don't conflate them").

The catch: one `bash -i` runs many commands over **a single SSH channel**, and SSH only reports an exit status when the channel **closes**. To recover each command's `$?` without tearing the channel down (which would lose cwd/env), we have to draw the command boundaries ourselves. The old way was an in-band sentinel (`echo <sentinel>:$?` after each command, then scan stdout) — mixing a control signal into the data stream, fragile at the root (details below).

**The current design borrows OSC 133 (FinalTerm) Shell Integration — the scheme iTerm2 / VS Code's integrated terminal / Kitty / WezTerm use**: let the **shell itself emit the command boundary**. On first use a tiny integration script is injected over stdin (**stdin only, never written to disk**), registering a `PROMPT_COMMAND` / `precmd` hook that prints `\x1b]133;D;<exit>\x07` after every command; we collapse to a **pure parser**. The sequence starts with an ESC byte, so ordinary text — **even output that literally spells `]133;D;0`** — can't forge it, `$?` is read straight from the marker, and the whole class of sentinel fragilities disappears at the root.

Two capabilities come for free: a command wedged on an interactive prompt (sudo / ssh first-connect / `mysql -p` / gpg passphrase) is **auto-Ctrl-C'd and the session kept** (soft-cancel — cwd/env survive, the next command runs immediately), while the one-shot path `portal_exec` is untouched (it opens a fresh channel per command and gets a native exit code straight from asyncssh).

> **Why the old sentinel was fragile at the root** (the motivation to drop it): ① `sudo` (or anything that grabs stdin) **swallows the sentinel as a bogus password** and the command hangs until `timeout` (default 3600 s); ② large output bloats an unbounded buffer; ③ a command whose stdout literally contains the sentinel string is mistaken for completion. An ESC-led OSC 133 marker kills all three at once.

> **Pitfalls surfaced by real-hardware spikes** (all recorded in `session_manager.py`, commits `466108b` / `46b1440`): the shell must start `--noprofile --norc` / `--no-rcs`, or a user's rc clobbers the hook and silently breaks the protocol; **zsh needs `unsetopt zle`** — the ZLE line editor echoes the command line back regardless of `stty -echo`, leaking command text into the output (only zsh 5.9 tripped this on real hardware; bash's readline honours `stty -echo`, so bash was clean from the start); **multi-line commands are wrapped in `{ … }`** — an interactive shell fires the marker once per top-level input line, so without wrapping a multi-line command emits one D per line and desyncs later calls, and it's a brace group rather than a `( … )` subshell so `cd`/`export` persist; **fish is deferred** — `fish_postexec` wasn't validated on real hardware, so fish falls back to bash.

### Stack choice: asyncssh, not subprocess-wrapped OpenSSH

[asyncssh](https://github.com/ronf/asyncssh) (EPL-2.0 / GPL-2.0 dual-licensed) is an **independent pure-Python SSHv2 implementation**, protocol-equivalent to OpenSSH:

- **One process, many connections, many sessions per connection** — the pool is a Python dict; no process boundaries, no fd sharing required. That's also why portal gets the same reuse performance on Windows as on Linux (the OpenSSH master/child model just doesn't work on Windows).
- **Full protocol coverage** — local/remote/dynamic port forwarding, SFTP, SCP, X11 forwarding, TUN/TAP — anything OpenSSH does at the protocol layer, asyncssh does too.
- **OpenSSH-compatible** — natively parses `~/.ssh/config`, `known_hosts`, `authorized_keys`, ssh-agent / Pageant. portal's host-alias detection and the ssh-config enumeration behind `portal_host(action=list)` are built **directly on asyncssh's `SSHClientConfig` parser** (Include-aware, cross-platform), not a hand-rolled scanner.
- **Only depends on PyCA `cryptography`** — install Python and you're done; no C deps, no OS-specific IPC.

Versus "shell out to `ssh` / `scp`": no ~50–100 ms fork per command, no need to coordinate SSH reuse across OS processes (the root cause of the Windows ControlMaster failure), and error handling / retries / timeouts are first-class Python async primitives rather than stderr-string parsing.

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
- **Audit log** → written to `$XDG_STATE_HOME/portal-mcp-server/log/` ([XDG Base Directory Spec](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html) explicitly places "logs, history" in the state-home tier — persistent but non-critical state); for ops review and post-hoc audit, never assumed to be read live

The rule cuts the other way too: **anything the user should know but the server cannot raise immediately** must be attached to the next relevant tool call's return value. A bare `logger.error()` is a dead letter.

## Install

Two paths depending on what you're doing.

### End user (use the MCP server, never touch the source)

No clone needed — let your MCP client launch it via `uvx` straight from PyPI. See [Client integration](#client-integration). `uvx` caches deps on first run; subsequent restarts are instant.

Manual smoke test in a shell:

```bash
uvx portal-mcp-server@latest --help
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
uv tool install --force --editable .  # point at this checkout; source edits apply immediately
```

If you'd rather not use uv, plain pip editable install works:

```bash
pip install -e ".[dev]"       # -e/--editable points at this source tree; prod + dev
# or runtime only
pip install -e .
```

### Short alias `portal`

After `uv tool install portal-mcp-server` (or the `uv tool install --force --editable .` above), two equivalent entry points are on your `PATH`:

```bash
portal agent install --now           # install/start the systemd --user credential agent
portal agent uninstall               # disable/remove agent user units/config
portal-mcp-server sudo set web01     # full name
portal sudo set web01                # short name (recommended for typing)
portal ssh set web01                 # SSH login password
portal passphrase set web01          # SSH private-key passphrase
portal secret set GITHUB_TOKEN
```

The `uvx portal-mcp-server xxx` form still requires the full name (`uvx` does not accept aliases). The short name only applies to persistent commands after `uv tool install` / `pip install`.

> **⚠️ Known name collision**: [`SpatiumPortae/portal`](https://github.com/SpatiumPortae/portal) (a P2P file-transfer CLI, packaged in Homebrew core) is also called `portal`. **Homebrew users may collide** — `uv tool install` drops the binary at `~/.local/bin/portal`, Homebrew puts it at `/opt/homebrew/bin/portal` or `/usr/local/bin/portal`, and whichever comes first in `$PATH` wins. To investigate:
>
> ```bash
> which -a portal      # lists every matching executable; the top one is active
> ```
>
> If it collides, fall back to the full `portal-mcp-server`, or reorder your PATH. `uv tool install` will *not* silently overwrite another tool's binary — it errors out and lets you decide.

### Credential agent (Linux systemd / macOS launchd / Windows scheduled task)

> **⚠️ Auto-install: Linux + macOS + Windows, all per-user.** `portal agent install` dispatches by OS and every backend runs the agent **as you, in your own session**: **Linux** ships a pair of **systemd user units** (`.socket` + `.service` under `~/.config/systemd/user/`, lazily started via socket activation); **macOS** ships a **launchd LaunchAgent** (`~/Library/LaunchAgents/com.tmytimidly.portal-credential-agent.plist`, run-and-keepalive — the agent self-binds its AF_UNIX socket, sidestepping the `launch_activate_socket` ctypes dance); **Windows** registers a **per-user logon scheduled task** (Task Scheduler, **InteractiveToken** principal — runs as you, only while you're logged on, **never as SYSTEM**, no stored password; the XML sets `ExecutionTimeLimit=PT0S` to dodge the 72h default kill and a `RestartOnFailure` keepalive) with a **named-pipe** IPC transport (no AF_UNIX). Windows's named pipe + scheduled-task install are both exercised on a real kernel by the `windows-latest` CI job.
>
> Where there's no agent at all (other platforms), the alternative: use the `password_command` / `passphrase_command` / `sudo_password_command` fields in `hosts.yaml`, or the `command:` field in `secrets.yaml`, to pull credentials on demand from the system password manager (Keychain, `pass`, `secret-tool`, `gopass`, 1Password CLI, etc.) — see [Authentication](#authentication) below. The MCP server itself (`portal_shell` and every remote tool) runs fine on Windows / macOS / Linux.

No-echo interactive values from `portal ssh set` / `portal passphrase set` / `portal sudo set` / `portal secret set` no longer live in one MCP server process. They go into a per-user, systemd socket-activated **credential agent**. Before using those interactive credential commands, explicitly install and start the user socket:

```bash
portal agent install --now
```

This writes `~/.config/systemd/user/portal-credential-agent.{socket,service}`. The `.socket` and `.service` units are paired by default: when the socket unit receives its first connection, systemd starts the same-named service and hands it the listening fd via `LISTEN_PID` / `LISTEN_FDS` (socket activation). The `.socket` listens on the systemd user manager path `%t/portal-mcp-server/credentials.sock`, with creation/removal owned by systemd. The installer also records the systemd-specifier-expanded absolute socket path in `~/.config/portal-mcp-server/agent.json`, so MCP clients can read it directly (or honour an explicit `PORTAL_CREDENTIAL_AGENT_SOCKET`) instead of guessing the runtime directory — a `XDG_RUNTIME_DIR` derived from a GUI app's child process isn't always correct, so this cache is necessary.

> **Order of operations**: `portal {secret,sudo,ssh,passphrase} set` auto-installs and starts the credential agent on first use (it runs the equivalent of `portal agent install --now`, prints the install output, then takes the no-echo input), so you can usually just run `set` directly. **But** for the MCP server (the one inside your IDE/agent) to read the credentials, the agent must be reloaded once relative to the MCP server's start: if your IDE was already running when you first `set`, reload the MCP/plugin integration or restart it afterwards (reload MCP/plugin in Claude Code, `/restart` in Copilot CLI, or restart the IDE/agent). For fully manual control you can also `portal agent install --now` before launching the IDE.

What stays enabled is the systemd socket unit: a same-user local listening endpoint. The credential agent service is socket-activated on first connection and holds TTL credentials in memory. Stopping the service clears the in-memory credentials while the socket can still activate it again. To remove the units and config:

```bash
portal agent uninstall
```

Day-to-day inspection / maintenance:

```bash
portal agent status                  # socket path + running state + cache counts per kind
portal agent clear                   # flush every cached entry across all kinds (service keeps running)
portal ssh    list                   # one row per cached host: sha256 fingerprint + remaining TTL
portal ssh    show web01             # single host: fingerprint + TTL (NO plaintext)
portal ssh    confirm web01          # prompt twice, cache only if both entries match (no-echo)
portal ssh    clear web01            # drop a single entry
```

The `passphrase` / `sudo` / `secret` subcommand trees mirror this shape (key noun is `host` / `host` / `name` respectively).

> **Design principle — plaintext never leaves the agent's memory.** The CLI intentionally has **no `show plaintext` / `dump` verb** on any of `portal ssh` / `portal passphrase` / `portal sudo` / `portal secret`. `show` returns sha256[:16] + TTL only, `list` shows the same per cached key, `confirm` re-prompts and accepts only if the two no-echo entries match. The plaintext is fed only to same-uid consumers: asyncssh (SSH handshake / local key unlock), `sudo -S` (stdin), `$env` injection (subprocess env). Terminal scrollback, screenshots, OBS overlays, asciinema, remote view-session software and stdout pipes are all leak surfaces — printing the plaintext to a TTY would zero out everything the no-echo prompt was protecting. Same posture as ssh-agent (`-L` prints fingerprints, never private keys), gpg-agent (no passphrase export verb), vault agent (writes secrets to a template target file, not the TTY), polkit-agent (GUI-only). To export a stored value, drive a `password_command` / `passphrase_command` / `secrets.yaml` `command:` from your password manager rather than asking the credential agent to print it.

## Client integration

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%40latest%22%5D%7D) [![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%40latest%22%5D%7D&quality=insiders) [![Install in Cursor](https://img.shields.io/badge/Cursor-Install_Server-000000?style=flat-square&logo=cursor&logoColor=white)](https://cursor.com/en/install-mcp?name=portal&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJwb3J0YWwtbWNwLXNlcnZlckBsYXRlc3QiXX0=)

`portal-mcp-server` is a local stdio MCP server — any MCP-capable host can install it. Each section below gives the minimal config for a popular host. `uvx` pulls from PyPI and caches automatically — no clone or venv required.

> If your MCP client cannot find `uvx`, run `which uvx` (`where uvx` on Windows) and use that absolute path as `command`.

### Generic snippet

> Most hosts accept the `{ "mcpServers": { "<name>": { "command": ..., "args": [...] } } }` top-level schema. VS Code and Codex use their own schemas — see their dedicated sections below.

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": ["portal-mcp-server@latest"]
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

> 💡 **`timeout` is now a REQUIRED argument** (no default) on `portal_exec` / `portal_shell` / `portal_local_exec` — the agent must consciously pick a cut-off every call (pass a small one for exploratory commands to fail fast). While a command runs the server emits keepalive heartbeats that stop the MCP client from aborting a hung call on its own, so `timeout` is the only real cut-off. Foreground timeouts also have a **ceiling** `PORTAL_MAX_TIMEOUT` (seconds, default 300): a request above it is refused with a nudge to background the work with `portal_job` — don't pin a long task on one blocking call.

### Claude Code CLI

Edit `<project>/.mcp.json` (same schema as above), or register via CLI / slash command:

```bash
claude mcp add portal -- uvx portal-mcp-server@latest
# or run /mcp inside a Claude Code session; pass --scope user to register globally
```

<details>
<summary><b>GitHub Copilot CLI</b></summary>

Write `<project>/.mcp.json` for project scope, or register at user scope with one command (applies to every project):

```bash
copilot mcp add portal -- uvx portal-mcp-server@latest
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
      "args": ["portal-mcp-server@latest"]
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
args = ["portal-mcp-server@latest"]
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
| File paths | `PORTAL_SECRETS_YAML` | Named secrets YAML (source for `secrets=` in `portal_exec` / `portal_local_exec`) |
| File paths | `PORTAL_SSH_CONFIG` | OpenSSH client config path (the `ssh -F` analogue — see "File paths" below) |
| File paths | `PORTAL_LOG_DIR` | Audit + server log directory |
| Security & auth | `PORTAL_AUDIT_FAIL_OPEN` | Whether audit-write failure is fail-open |
| Security & auth | `PORTAL_AUDIT_MAX_BYTES` | `audit.jsonl` rotation threshold in bytes (default 10 MiB) |
| Security & auth | `PORTAL_AUDIT_BACKUPS` | How many rotated files `audit.jsonl.1..N` to keep (default 5) |
| Security & auth | `PORTAL_AUTH_TOKEN` | Bearer token for HTTP transport |
| Connection pool | `PORTAL_SSH_POOL_SIZE` | Max TCP connections per host |
| Connection pool | `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | Max concurrent channels per TCP connection |
| Connection pool | `PORTAL_SSH_MAX_IDLE_TIME` | Idle-close timeout in seconds |
| Connection pool | `PORTAL_SSH_MAX_CONN_AGE` | Max connection lifetime in seconds |
| Reliability | `PORTAL_BASH_HEARTBEAT_INTERVAL` | Keepalive heartbeat interval (s) while `portal_shell` runs |
| Reliability | `PORTAL_MAX_TIMEOUT` | Foreground per-command timeout **ceiling** (s, default 300) for `portal_exec` / `portal_shell` / `portal_local_exec`; `timeout` is required and a value above the ceiling is refused with a nudge to `portal_job` |
| Exec env | `PORTAL_LOGIN_SHELL` | Whether `portal_exec` / `portal_job` default to a login shell (`bash -lc`, loading `~/.profile`/`~/.bashrc` PATH+env). On by default; set `0`/`false`/`no`/`off` to disable. Overridable per-call via `login` and per-host via `login_shell:` |
| Remote read | `PORTAL_READ_MAX_LINES` | Max lines `portal_read` returns per page when `limit` is omitted (default 2000) |
| Remote read | `PORTAL_READ_MAX_BYTES` | Max bytes of content `portal_read` returns per page, so a large file doesn't blow past the MCP client's inline-output threshold (default 16384) |
| Background jobs | `PORTAL_JOB_PERSIST` | Whether the `portal_job` table persists across restarts (default on; `0`/`false` to disable) |
| Background jobs | `PORTAL_JOB_STATE_FILE` | Path of the persisted job table (default `<state>/jobs.json`) |
| Background jobs | `PORTAL_JOB_MAX_LIVE` | Cap on concurrently live background jobs (default 50) |
| Background jobs | `PORTAL_JOB_TTL` | How many seconds a finished job stays in the table before it's swept + its remote tmp deleted (default 3600) |
| Testing (dev only) | `PORTAL_TEST_LIVE` | Gate for live SSH integration tests |
| Testing (dev only) | `PORTAL_TEST_HOST` / `PORTAL_TEST_PORT` / `PORTAL_TEST_USER` / `PORTAL_TEST_KEY_PATH` | Live test target |

Detailed breakdown below.

### File paths

| Env var | Meaning | Default |
|---|---|---|
| `PORTAL_HOSTS_YAML` | Host registry YAML | `~/.config/portal-mcp-server/hosts.yaml` |
| `PORTAL_POLICIES_YAML` | Security policy YAML | `~/.config/portal-mcp-server/policies.yaml` |
| `PORTAL_SECRETS_YAML` | Named secrets YAML | `~/.config/portal-mcp-server/secrets.yaml` |
| `PORTAL_SSH_CONFIG` | OpenSSH client config path | `~/.ssh/config` |
| `PORTAL_LOG_DIR` | Audit + server log directory | `~/.local/state/portal-mcp-server/log/` |

> `PORTAL_SSH_CONFIG` is portal's `ssh -F`: OpenSSH has **no env var** for the config path (only `-F`), and portal is a daemon with no per-connection flag, so this variable **fully mirrors `-F`**: an **absolute path** reads **only that file** (suppressing the system-wide `/etc/ssh/ssh_config`, like `-F <file>`); the literal **`none`** (any case) reads **no config file at all** (`ssh -F none` — host resolution comes solely from hosts.yaml); **unset** reads user `~/.ssh/config` + system `/etc/ssh/ssh_config` (`%PROGRAMDATA%\ssh\ssh_config` on Windows) as a fallback, user wins. Relative values (other than `none`) are warned and ignored. Parsing is delegated wholesale to asyncssh (Include-aware, multi-platform `~` expansion); asyncssh's own discovery is a single `~/.ssh/config` line with no system-config support, so the system fallback and `-F`/`-F none` layer are portal's.

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

> **v2.0.0 breaking changes**:
> - Removed the `./config/hosts.yaml` / `./config/policies.yaml` / `./logs/` cwd-relative fallbacks — resolution is now env > XDG only.
> - Renamed the repo's `config/` directory to `examples/`; files dropped the `.example.` infix (the directory name now carries the "template" semantics).

### Security & auth

| Env var | Meaning | Default |
|---|---|---|
| `PORTAL_AUDIT_FAIL_OPEN` | Set to `1` → audit-write failures are warnings only; unset → **fail-closed**, audit-write failure aborts the operation | _(unset)_ |
| `PORTAL_AUDIT_MAX_BYTES` | Rotate `audit.jsonl` once it reaches this many bytes (a stdlib `RotatingFileHandler`, subclassed to stay fail-closed) | `10485760` (10 MiB) |
| `PORTAL_AUDIT_BACKUPS` | How many rotated `audit.jsonl.1..N` files to keep | `5` |
| `PORTAL_AUTH_TOKEN` | Bearer token for HTTP transport (`--transport streamable_http`); not needed for stdio | _(none)_ |

### Connection pool

Controls the in-process asyncssh connection pool. Defaults work well for most setups; tune only under high concurrency or unusual network conditions. Pool behaviour is documented in [§ In-process connection pool](#in-process-connection-pool).

| Env var | Meaning | Default |
|---|---|---|
| `PORTAL_SSH_POOL_SIZE` | Max TCP connections per host. When the pool is full and every connection is at the channel ceiling, the least-loaded connection is reused (with a warning) | `5` |
| `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | Max concurrent channels (SFTP, exec, tunnel, …) multiplexed over one TCP connection. New connections are opened when exceeded, up to `PORTAL_SSH_POOL_SIZE` | `5` |
| `PORTAL_SSH_MAX_IDLE_TIME` | Close idle connections (no active channels) after this many seconds. Set `0` to disable | `600` (10 min) |
| `PORTAL_SSH_MAX_CONN_AGE` | Max connection lifetime in seconds; aged connections with no active channels are closed. Guards against silent firewall / NAT drops | `3600` (1 hour) |

### Reliability

| Variable | Meaning | Default |
|---|---|---|
| `PORTAL_BASH_HEARTBEAT_INTERVAL` | How often (seconds) `portal_shell` / `portal_exec` / `portal_local_exec` emits an MCP progress notification as a keepalive while the command runs, so an output-silent command doesn't trip the client's idle timeout (JSON-RPC `-32001`). Independent of the server-side `timeout` parameter. Non-positive or invalid values fall back to the default | `5` (seconds) |
| `PORTAL_MAX_TIMEOUT` | Foreground per-command timeout **ceiling (seconds)** for `portal_exec` / `portal_shell` / `portal_local_exec`. `timeout` is now a **required** argument (the default was removed from all three), so the agent must pick one every call; a value **above** this ceiling is **refused** with a nudge to background the work with `portal_job` (which has no ceiling). This is a safety ceiling, not a default. Read on each call. Non-positive / invalid values fall back to the built-in default | built-in `300` (5 min) |
| `PORTAL_LOGIN_SHELL` | Whether `portal_exec`'s plain path and `portal_job` run the command in a **login shell** (`bash -lc`) so the user's `~/.profile` / `~/.bashrc` PATH+env (conda / nvm / pyenv / `~/.local/bin`) is loaded. **On** by default; only an explicit `0`/`false`/`no`/`off` disables it. Precedence: per-call `login` arg > a host's `login_shell:` in hosts.yaml > this env var. Hosts without bash degrade to a plain exec; `portal_shell` is unaffected (its persistent session is deliberately `--norc`) | `on` |

### Background jobs

Tune `portal_job` (background submit/poll/cancel/list). The job table is best-effort persisted across restarts; see [§ Tools](#tools).

| Env var | Meaning | Default |
|---|---|---|
| `PORTAL_JOB_PERSIST` | Whether the job table persists across a server restart (reloaded on startup, a poll re-probes the remote PID). `0`/`false`/`no`/`off` disables it | _(on)_ |
| `PORTAL_JOB_STATE_FILE` | Override the persisted job-table path | `<state>/jobs.json` |
| `PORTAL_JOB_MAX_LIVE` | Cap on concurrently live jobs; `submit` is refused beyond it | `50` |
| `PORTAL_JOB_TTL` | Seconds a finished job stays in the table before it's swept and its remote tmp files removed | `3600` (1 hour) |

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
      "args": ["portal-mcp-server@latest"],
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

Pick the path for your setup — SSH keys preferred, encrypted keys via ssh-agent; password auth is supported via `password_command` or `portal ssh set`, so plaintext credentials never reach the LLM.

### Credential-flow overview

There are five credential flows, each with a "password-manager style" (command source) and/or a "no-echo interactive style" (getpass + systemd --user credential agent). **As currently implemented:**

| Credential flow | Command source (password-manager style) | No-echo interactive entry (getpass style) | Cache key | Cache semantics | Trigger |
|---|---|---|---|---|---|
| **A. Remote SSH login password** | `password_command` (hosts.yaml) | ✅ `portal ssh set <host>` | host | agent in-memory TTL (default 900s, interactive entry only; command source fetched per connection) | on connect for `auth: password` / auto fallback when key auth refused |
| **B. SSH key passphrase** | `passphrase_command` (hosts.yaml) | ✅ `portal passphrase set <host>` | host | agent in-memory TTL (default 900s) | local encrypted-key unlock |
| **C. Remote sudo execution** | `sudo_password_command` (hosts.yaml) | ✅ `portal sudo set <host>` | host | agent in-memory TTL (default 900s) | `portal_exec(use_sudo=True)` |
| **C2. Local sudo execution** | `sudo_password_command` in a top-level `<local>:` section (hosts.yaml) | ✅ `portal sudo set-local` | `<local>` (reserved) | agent in-memory TTL (default 900s) | `portal_local_exec(use_sudo=True)` |
| **D. Secret injection · remote** | `command` in `secrets.yaml` (fetched each time) | ✅ `portal secret set <name>` | name | agent in-memory TTL (default 900s, `--ttl` configurable) | `portal_exec(secrets=[…])` |
| **E. Secret injection · local** | same as D (shares `secrets.yaml`) | same as D (shares `portal secret set`) | same as D | same as D | `portal_local_exec(secrets=[…])` |

Things to know:

- **D and E are one and the same credential pipeline** — they share `secrets.yaml` + `portal secret set` + the same per-user credential agent + the same name-keyed TTL cache; only the consuming tool differs (remote injects via SSH stdin, local via subprocess env).
- **A, B, C, and D share one per-user agent socket**, but the agent keeps separate `ssh` / `passphrase` / `sudo` / `secret` key spaces. A's password goes into `asyncssh.connect()` during the SSH handshake; B's passphrase unlocks a local private key; C's password is fed to `sudo -S` after the handshake; D/E are injected as environment variables.
- **A's resolution chain**: explicit `auth: password` login goes `cache (portal ssh set) → password_command → error`. Pure key hosts retry that same chain *once* when asyncssh raises `PermissionDenied`, but **only when a source is available**; with no cache and no `password_command` the original `PermissionDenied` propagates — so a stale config never masks the real "your key is rejected" failure.
- **Interactive entries (getpass style) = per-user agent in-memory TTL cache**: default 900s, reusable within the TTL, auto-cleared on expiry, gone on agent restart, never written to disk. **Command sources (password-manager style) = fetched each time**, no TTL.
- **Plaintext never leaves the credential agent's memory**: there is intentionally no `show plaintext` verb. `portal {ssh,passphrase,sudo,secret} show <key>` returns a sha256[:16] fingerprint + remaining TTL, `list` summarises every cached entry, and `confirm` re-prompts and compares two no-echo entries. The plaintext is fed only to same-uid consumers (asyncssh, local key unlock, `sudo -S`, `$env` injection). Full rationale in the [Credential agent (Linux systemd / macOS launchd / Windows scheduled task)](#credential-agent-linux-systemd--macos-launchd--windows-scheduled-task) section above.

#### The four credential mechanisms: implementation & why

The four credentials use four **different** mechanisms — not arbitrary; each credential's consumer dictates how it's injected:

| Credential | Implementation | Why this way |
|---|---|---|
| **SSH login password** | asyncssh `password=` (SSH-protocol level), source: `password_command` / `portal ssh set` cache | SSH natively supports password auth; the protocol frame is the cleanest path |
| **SSH key passphrase** | asyncssh `passphrase=`, source: ssh-agent → `portal passphrase set` cache → `passphrase_command`; or `use_ssh_agent` for pure agent | Decrypts the key locally; the passphrase never leaves the process. It is cached separately from the SSH login password, so a local key unlock phrase cannot be mistaken for a remote login or sudo password |
| **sudo password** | `sudo -S` fed via stdin (`conn.run(input=pw)`), source: `sudo_password_command` / `portal sudo set` cache | sudo only honours `-S`/`-A`/tty, not env. `-S` has the narrowest exposure: shortest password lifetime (read once, discarded), no remote on-disk artifact, no env exposure (vs `-A` askpass which drops a temp helper file + a helper process whose env holds the password). Cost: the sudo command's own stdin is consumed by the password and hits EOF early (curl/CLI flag-readers are unaffected) |
| **secrets** (API tokens) | `bash -s` + stdin feeding `export VAR=…\n<cmd>\n`, source: `secrets.yaml` `command` / `portal secret set` cache | tools generally read env (`GH_TOKEN`/`AWS_*`); the purer SSH-protocol env frame is blocked by sshd's `AcceptEnv` allowlist (default just `LANG`/`LC_*`), so it can't reach the remote — hence this workaround. The value sits briefly in the script string parsed on bash's stdin, but bash is use-and-discard, the value never hits argv (not in `ps`), never hits a log — far narrower than `--token=xxx` on argv |

#### ⚠️ Risks of configuring these passwords (please read)

Key-only login is the safest baseline. **The moment you configure an SSH login password / sudo password / secret for a host, you authorize "any agent that can call this MCP server" to act with that credential for as long as it's valid** — the agent no longer has to ask you, and is no longer stopped by a system password prompt. The two config paths trade off differently:

| Config method | Lifetime / exposure | Risk profile |
|---|---|---|
| **Permanent (password-manager command)** `sudo_password_command` / `password_command` / `secrets.yaml` `command:` | **fetched on each connect**, no TTL; usable as long as your store (`pass`/`op`/`bw`) is unlocked | exposure window = how long your store stays unlocked. The command lives in hosts.yaml/secrets.yaml (**config files — keep them out of git**); the value never hits disk but the agent can fetch it anytime |
| **Temporary (no-echo set)** `portal {ssh,passphrase,sudo,secret} set <key>` | held in the per-user credential agent's **memory**, default 900s TTL, auto-cleared, gone on agent restart, **never on disk** | exposure window = the TTL. Smallest blast radius — **prefer this**; reach for a password-manager command only when you genuinely need unattended automation |

Key points:

- **High-risk operations are flagged in the result**: `portal_exec(use_sudo=True)` / `secrets=[...]` and `portal_local_exec(secrets=[...])` results carry `"high_risk": true` + a `"high_risk_note"`, and the calling agent is asked to **briefly tell you it ran a privileged command with your password/secret**, or only do so with your explicit permission. Treat it as a receipt that "the agent just sudo'd on your behalf".
- **Shrink the blast radius**: use key-only where you can; prefer the temporary `set` (TTL) over a password-manager command; scope sudoers per-command instead of full NOPASSWD; review `audit.jsonl` periodically (every `sudo` / secret injection is recorded).
- **Credentials never enter the LLM context** — under every path the password/secret is never a tool parameter, never on `ps` argv, never in audit/log — but "whoever can drive this agent" effectively **holds** those privileges while the credential is valid. That is the inherent cost of configuring a password.
- **The first `portal {kind} set` auto-installs the agent**: if the credential agent isn't up yet, `set` automatically runs the equivalent of `portal agent install --now`, prints the install output for you, then takes the no-echo input — no need to `agent install` by hand first.

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

### Password auth: `password_command` or `portal ssh set`

Provided for legacy hosts that cannot be re-keyed. Two non-negotiable rules:

1. **Never** write `password: <plaintext>` in `hosts.yaml` — startup logs an ERROR and drops the field.
2. **Never** flow a password through an MCP tool — `portal_host` has no password parameter, so credentials cannot land in LLM tool-call traces.

Two sources (order: agent cache → `password_command` → error), same shape as sudo / secret:

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

2. **Seed it once (1b, `portal ssh set`, interactive)** — in a **separate terminal** (not the agent chat):

   ```bash
   portal ssh set legacy-host                  # getpass, no echo
   portal ssh set legacy-host --ttl 1800       # custom TTL (seconds); default 900 (15 min)
   portal ssh confirm legacy-host              # prompt twice, cache only on match
   portal ssh show legacy-host                 # sha256 fingerprint + remaining TTL (no plaintext)
   portal ssh list                             # every cached host: fingerprint + TTL
   portal ssh clear legacy-host                # drop a single entry
   ```

   The password travels over the systemd --user managed local unix socket into the per-user credential agent's memory cache: the `.socket` unit listens on `%t/portal-mcp-server/credentials.sock`, the installer records the resolved absolute path in `agent.json`, the directory is `0700`, the socket is `0600`, and the agent performs an `SO_PEERCRED` same-uid check. It is **never written to disk, never sent to the LLM**, and is dropped automatically when the TTL expires. Works even when the host has no `password_command` in `hosts.yaml`, or is a key-mode host (the default — no `auth:` field in hosts.yaml).

#### Auto-fallback: key failure → password

When asyncssh raises `PermissionDenied` for a key-mode host (the default — no `auth:` field in hosts.yaml), the server retries *once* via the password chain (agent cache → `password_command`), but **only when a source is available**. With no cache and no `password_command` the original `PermissionDenied` propagates — so a missing config never masks the real "your key is rejected" failure. Keys remain the preferred path; password is an opt-in safety net.

Runtime behaviour: `password_command` runs with a 10-second timeout, exactly one trailing newline stripped, stderr never logged (leak defence), and non-zero exit / empty output / non-UTF-8 output all hard-failing. Design rationale (why `shell=True`, why `client_keys=[]` is forced, why stderr never reaches the logs, …) lives in **[`SECURITY.md` § Authentication](./SECURITY.md#authentication)**.

### Encrypted-key passphrases: `portal passphrase set` / `passphrase_command` / `use_ssh_agent`

The passphrase is a **local private-key unlock phrase**, not the remote SSH login password and not the sudo password. It has a dedicated interactive entry and credential-agent kind.

Resolution order:

1. **ssh-agent** (`$SSH_AUTH_SOCK`, already unlocked via `ssh-add`) — preferred
2. **agent cache** (`portal passphrase set <host>`, no echo, TTL cache)
3. **`passphrase_command`** (password manager) — headless / CI
4. asyncssh's default key loading

```bash
portal passphrase set encrypted-key-host
portal passphrase confirm encrypted-key-host
portal passphrase show encrypted-key-host
```

```yaml
hosts:
  encrypted-key-host:
    host: 10.0.0.30
    user: deploy
    key: ~/.ssh/encrypted_key
    passphrase_command: pass show ssh/encrypted_key
```

Prefer ssh-agent when you have a usable terminal — UX is better. Use `passphrase_command:` only in headless / CI environments.

### Non-interactive sudo: `use_sudo` + `portal sudo set`

`portal_exec(host, cmd, use_sudo=True)` lets the agent run root commands, but **the sudo password never reaches the LLM** — `portal_shell` has no password parameter; the password is resolved server-side. Two sources (same philosophy as the SSH password):

1. **Password manager (automatic)** — set `sudo_password_command` on the host in `hosts.yaml`, fully symmetric with `password_command`:

   ```yaml
   hosts:
     prod-box:
       host: 10.0.0.50
       user: deploy
       sudo_password_command: pass show sudo/prod-box   # or op read / bw get / printf "$ENV"
   ```

   If this host's sudo password is **explicitly the same as the same user's SSH login password**, you can opt in on the host instead of configuring a separate sudo command source:

   ```yaml
   hosts:
     legacy-host:
       host: 10.0.0.40
       user: admin
       auth: password
       sudo_password_same_as_ssh: true
   ```

   After that, `portal ssh set legacy-host` writes the same no-echo input into both the `ssh` and `sudo` credential kinds. The default is `false`: without this field, you must still run `portal sudo set legacy-host` separately or configure `sudo_password_command`. This only reuses the **SSH login password**; it never reuses the private-key passphrase from `portal passphrase set`.

2. **Seed it once (interactive)** — in a **separate terminal** (not the agent chat):

   ```bash
   portal sudo set prod-box                  # getpass, no echo
   portal sudo set prod-box --ttl 1800       # custom TTL (seconds); default 900 (15 min)
   portal sudo confirm prod-box              # prompt twice, cache only on match
   portal sudo show prod-box                 # sha256 fingerprint + TTL (no plaintext)
   portal sudo list                          # every cached host
   ```

   The password travels over the systemd --user managed local unix socket into the per-user credential agent's memory cache: the `.socket` unit listens on `%t/portal-mcp-server/credentials.sock`, the installer records the resolved absolute path in `agent.json`, the directory is `0700`, and the socket is `0600` / same-user only. It is **never written to disk, never sent to the LLM**, and is dropped automatically when the TTL expires.

Resolution order: **agent memory cache (2) → `sudo_password_command` (1) → error** (telling you to run `portal sudo set` or configure `sudo_password_command`).

Implementation notes: `use_sudo` runs a one-shot `conn.run(input=pw, ...)` executing `sudo -S -k -p '' -- bash -c <cmd>`; it does **not** reuse the persistent `portal_shell` session (`sudo -S` reads the password from stdin, which the persistent PTY has no channel to feed — a bare sudo there is auto-Ctrl-C'd and the session is preserved). Consequently a sudo command does **not** inherit `cd` / `export` state from prior `portal_shell` calls — bake any `cd … && …` into the same command. `-k` forces fresh auth each time; `-p ''` suppresses the prompt. Genuinely interactive sudo (needs a TTY, or a password change) still can't go through `portal_shell` — have the user run `ssh -t host sudo …`.

#### Local sudo: `portal_local_exec(use_sudo=True)`

The above is **remote** sudo. The MCP server itself often needs sudo too (it can't run "as the right user"), so `portal_local_exec(use_sudo=True)` runs a privileged command on the **MCP server's own machine** via local `sudo -S -k`, with the password likewise **never reaching the LLM / argv / disk**. It reuses the same credential resolution as the remote path; only the identity is the reserved **`<local>`**:

- Temporary: `portal sudo set-local` (no echo, into the credential agent, `--ttl` configurable);
- Permanent: a **top-level `<local>:` section** in `hosts.yaml` (a sibling of `hosts:`, a reserved key, **not** a host) carrying `sudo_password_command`:

  ```yaml
  hosts:
    # ... your remote hosts ...
  "<local>":                                # top-level reserved key; THIS machine's local_exec
    sudo_password_command: pass show sudo/this-box
  ```

Resolution order mirrors the remote path: **agent cache → the top-level `<local>:` `sudo_password_command` → error**. `use_sudo` may be combined with `secrets`; the result is flagged `high_risk` and audited as `local_exec_sudo`.

> **Don't conflate `<local>` / `local` / `localhost`**: `<local>` is the **reserved identity** for the MCP server's own machine (`portal_local_exec`, no SSH); the angle brackets are illegal in a hostname, so it **can never** collide with a real host. `localhost` is an **ordinary SSH host** (connects to `127.0.0.1:22`, needs sshd + key/password), used by every remote tool. If you name some remote `local` in `hosts.yaml` / ssh config, it is likewise just an **ordinary remote host**, unrelated to the local `<local>`.

### Named-secret injection: `secrets=[…]` + `portal secret set`

Use this to hand a command an API token (a GitHub token, a deploy key, …) **without it entering the session history or being sent to the third-party LLM backend**. Same threat model as the sudo password: the agent passes only the secret's **name**, the server resolves the value and injects it as an **environment variable** into a one-shot command. The value travels via the process environment / SSH stdin (never on argv, so `ps` and the audit log can't see it), and any echo of it in the command output is redacted to `***` before the result reaches the agent.

> **Why not just `export`?** The pain point: a throwaway `export TOKEN=…` never reaches the agent's execution context — it only affects the new terminal *you* opened, while the agent runs commands in the MCP server process's environment, which can't see it. The only way to make the agent use it was to `vim` a `.env` / secrets file for it to source — which puts the secret back on disk and is easy to forget to delete. This design turns "hand over a key once" into a **native no-echo CLI prompt** (`portal secret set` uses `getpass`, just like typing a password), with the value living only in per-user credential agent memory and auto-expiring on a TTL — never on disk, never to the LLM.

- Remote: `portal_exec(host, cmd, secrets=["github_token"])`, referencing `$GITHUB_TOKEN` (the uppercased name) in `cmd`.
- Local: `portal_local_exec(cmd, secrets=["github_token"])` runs on the **MCP server host** (not over SSH). Local execution is an off-target derivative of the project's remote-driving goal, so it is **off by default** unless the server process has `PORTAL_ALLOW_LOCAL_EXEC=1`.

Two sources (order: agent memory cache → `secrets.yaml`):

1. **Secret manager (secrets.yaml)** — symmetric to `password_command`; a command that prints the secret to stdout:

   ```yaml
   secrets:
     github_token:
       command: pass show api/github      # or op read / printf "$ENV"
   ```

2. **Live input (`portal secret set`, interactive once)** — in a *separate* terminal:

   ```bash
   portal secret set github_token              # getpass, no echo
   portal secret set github_token --ttl 1800   # custom TTL (s), default 900
   portal secret confirm github_token          # prompt twice, cache only on match
   portal secret show github_token             # sha256 fingerprint + TTL
   portal secret list                          # every cached secret name
   ```

   The value is pushed over the systemd --user managed local unix socket into the per-user credential agent's memory cache: the `.socket` unit listens on `%t/portal-mcp-server/credentials.sock`, the installer records the resolved absolute path in `agent.json`, the directory is 0700, and the socket is 0600 / same-user only. It is never written to disk, never seen by the LLM, and is cleared on TTL expiry.

See [`examples/secrets.yaml`](./examples/secrets.yaml). `secrets` may be combined with `use_sudo` in a single `portal_exec` call.

#### Implementation detail: how sudo + secrets coexist

`use_sudo` and `secrets` share **one stdin channel**: the sudo password goes first, followed by each value in the order given in `secrets`. The actual command is wrapped with a small preamble that runs *after* sudo's `env_reset` (which by default wipes the inherited environment) has already fired, reading each value back line-by-line inside the now-elevated shell and re-exporting it:

```
IFS= read -r GITHUB_TOKEN
export GITHUB_TOKEN
<original command>
```

Because the `export` happens *after* `env_reset`, the value survives; because it travels on stdin rather than argv, it never shows up in `ps`, shell history, or the audit log, and no sudoers `env_keep` entry is required. The local (`portal_local_exec`) and remote (`portal_exec`) paths behave identically; see `sudo_stdin_secret_script()` in `secrets_store.py`.

#### Wait semantics: fail-fast → `ask_user` → retry

No-echo input inherently means "wait for the human to type it," but **that wait is never put on the agent's critical path** — the MCP server is usually headless with no access to the user's tty, so it can neither pop a `getpass` prompt nor block the tool call until it times out. The contract is therefore:

1. **Fail-fast**: when the secret (or sudo password) isn't ready, the tool **returns an error immediately and does not run the command**; the error never contains the value.
2. **Bounce it back to the user**: the error explicitly nudges the agent to use an interactive input/choice tool (e.g. `ask_user`) to ask the user to run `portal secret set <name>` / `portal sudo set <host>` in a *separate* terminal and reply "ok" when done; the agent then retries the call.
3. **No such tool → end the turn**: if the agent has no `ask_user`-style tool, it should **tell the user what to run and end its turn**, waiting for the user's next prompt to retry — rather than busy-waiting or polling.

So "waiting" surfaces only as a normal conversational turn handoff: the `getpass` block lives in the user's own terminal, while the agent side is always "check cache → run on hit / fail-fast with instructions on miss." **Never ask the user to paste the value into the conversation** — that would feed it straight to the third-party LLM and defeat the entire design.

> **How does this guidance reach the agent?** Entirely through **each `portal_*` tool's own description** — an MCP client always feeds tool descriptions to the model, so "when a task needs a token, steer the user to `portal secret set` instead of asking for plaintext" is written at the top of the `portal_exec` / `portal_local_exec` descriptions.
> MCP also defines a **server-level `instructions` field** (returned in the `initialize` response — a natural home for a global credential discipline), but the spec frames it as *“a ‘hint’ to the model … this information **MAY** be added to the system prompt”* ([InitializeResult.instructions](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle), an optional field) — **optional, entirely at the client's discretion**. Empirically (2026-06) **Copilot CLI / Codex CLI / Claude Code all ignore it** (none inject it into the model context), so portal does **not** rely on server-level instructions; the credential guidance lives in the per-tool descriptions instead.

### Host resolution: hosts.yaml, then ssh config

**Default lookup order** — resolving a host name is a two-step walk; the first hit wins:

1. **hosts.yaml** (first) — read from the XDG config dir (`~/.config/portal-mcp-server/hosts.yaml`, override with `PORTAL_HOSTS_YAML`; see [Environment variables → File paths](#file-paths)).
2. **OpenSSH client ssh config** (next) — when the name isn't in hosts.yaml, found via **OpenSSH's own logic**: user `~/.ssh/config` + system `/etc/ssh/ssh_config` fallback, **byte-for-byte aligned with `ssh -F`**. This step's parsing is delegated wholesale to **asyncssh's `SSHClientConfig` parser** (Include-aware, `%`-token and cross-platform `~` expansion) — portal doesn't hand-roll a scanner.

Step 2 is controlled by `PORTAL_SSH_CONFIG` (see [File paths](#file-paths)): an absolute path reads only that file, unset reads user + system, and **`none` disables step 2 entirely** — host resolution then comes solely from hosts.yaml. `portal_host(action="list")` lists hosts from both steps, each tagged with a `source` field.

**Precedence** — by default, once a host name appears in `hosts.yaml` it **fully overrides** ssh config; ssh config isn't consulted for it. Opt in **per host with `use_ssh_config: true`** to instead **MERGE**: the ssh config alias is the base (HostName / User / Port / IdentityFile / IdentityAgent / ProxyJump / …) and the hosts.yaml fields you explicitly set layer on top (hosts.yaml wins where set, ssh config fills the rest). Footguns are surfaced as warnings (never blocking), delivered via `portal_host(action="list")`'s `warnings` array (a stdio server's stderr is invisible to the user):

- a name defined on **both** sides *without* `use_ssh_config` → hosts.yaml silently wins and ssh config's `IdentityFile`/`ProxyJump`/`User` are ignored; set `use_ssh_config: true` to merge them in instead;
- `use_ssh_config: true` but no matching ssh-config alias → asyncssh falls back to a default DNS+user+key connection, probably not what you wanted;
- `use_ssh_config: true` with a `host:` that disagrees with the alias's HostName → **refused** at connect time (reconcile: put the address in the ssh config `HostName`, or drop `host:` so it's inherited).

**Merge recipe** (ssh config as the base, hosts.yaml overrides on top):

```yaml
hosts:
  web01:                          # key MUST == the Host alias in ssh config
    use_ssh_config: true          # MERGE: ssh config is the base (HostName/User/Port/IdentityFile/IdentityAgent/ProxyJump…); fields set below override it
    tags: [web, prod]             # portal-only: portal_exec's group_tag
    sudo_password_command: pass show sudo/web01   # portal-only
    # user: deploy                # optional override of ssh config's User
    # host: omit to inherit HostName; if set it MUST match the alias's HostName
```

`portal_host(action="register", name="web01")` with only `name` auto-checks ssh config — a matching alias is registered as exactly this overlay.

**`list` lists both steps**: `portal_host(action="list")` returns hosts.yaml / runtime entries **and** enumerates every `Host` alias in the ssh config (parsed by asyncssh, `Include`-aware, excluding `*`/`?`/`!` patterns), resolving real `HostName`/`User`/`Port`. Each entry carries a `source` field:

- `hosts.yaml` / `runtime` — from the hosts.yaml file / a runtime `register`; connection params are the fields themselves;
- `ssh-config` — an alias found only in ssh config (user-level or the system fallback);
- `hosts.yaml+ssh-config` / `runtime+ssh-config` — a `use_ssh_config: true` MERGE: connection params come from the ssh config alias as the base, with any fields set in the declared origin (tags/sudo… and any explicit `host`/`user`/`port`) overriding on top.

**Field mapping + incremental coverage**: the basics are all there (`host`/`port`/`user`/`key`/`known_hosts`/`strict_host_key_checking`/`auth`); the common advanced fields `proxy_jump` (→ asyncssh `tunnel`), `keepalive_interval` (→ ServerAliveInterval), `forward_agent` (→ agent forwarding) are now **natively supported**; any remaining ssh config field is inherited by turning on the `use_ssh_config: true` merge.

## Security

- **Default sandbox**: writes default to remote `/tmp/`; the agent must ask before touching `$HOME` or project source (a prompt-layer convention — see [Agent-side conventions](#agent-side-conventions)).
- **Policy gate**: host allowlist + command blocklist/allowlist + per-host rate limit; every state-changing tool runs through `_gate` with no side doors (`portal_host(register)` gates against the target IP, not the alias; `portal_tunnel(action=close)` is gated; multi-host gates are two-phase). An optional [cc-safety-net](https://github.com/kenryu42/cc-safety-net) semantic gate (`policies.safety_net.enabled`) layers in at the same chokepoint: each command is handed to `cc-safety-net explain --json` for bypass-resistant analysis and refused if it resolves to a destructive git/rm/interpreter pattern — the same rules the Copilot-CLI PreToolUse hook applies, except that hook only inspects the agent's own `bash` tool and never sees `portal_*` MCP commands. Fail-closed by default (a checker that can't produce a verdict refuses the command).
- **Authentication**: SSH keys are the default and recommended path; password auth is supported but only via `password_command` in `hosts.yaml`, never exposed through any MCP tool — config in [Authentication](#authentication), security design in [`SECURITY.md` § Authentication](./SECURITY.md#authentication).
- **Audit**: every state-changing operation is appended to `$PORTAL_LOG_DIR/audit.jsonl` (default `~/.local/state/portal-mcp-server/log/audit.jsonl`); fail-closed by default (`PORTAL_AUDIT_FAIL_OPEN=1` switches to fail-open).
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

`tests/live_smoke.py` imports the local working tree and drives a series of real SSH actions: stale `password:` field handling in `hosts.yaml`, basic `ssh_exec`, `portal_exec(host=[...]/group_tag=...)` against real hosts (verifying both blocked-command and not-in-allowlist hosts get rejected), per-command gating in `portal_shell`, a `portal_shell` + `portal_patch` round-trip in remote `/tmp/` (including the stale-hash rejection path), and audit.jsonl ingestion of the new operation tags.

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
# Temporary uvx run: refresh the cache and re-fetch the latest PyPI build
uvx portal-mcp-server@latest --help

# Persistent PATH install: upgrade the installed uv tool
uv tool upgrade portal-mcp-server
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

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp) (Apache 2.0)** — git ancestor; the lower-level modules (asyncssh engine, connection pool, tunnel manager, orchestrator, security policy) are inherited. The 14-tool `portal_*` upper layer is new.
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT)** — algorithmic reference for the SHA-256 hash-protected edit semantics in `remote_text_editor.py`, reimplemented for AsyncSSH SFTP.

> ⚠️ This tool gives an agent SSH access to remote systems. Use it only on systems you own or are authorised to access.
