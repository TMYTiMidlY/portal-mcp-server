# Tool Reference

Complete index of all **18 MCP tools** exposed by `portal-mcp-server`.
Tools are split into two layers:

- **9 portal core** — direct, single-purpose entry points used by the agent for day-to-day work (read / patch / search / persistent bash / local exec).
- **10 portal high-level** — `mode`-switched orchestration tools that consolidate what the upstream `ssh-shell-mcp` exposed as 30+ separate tools.

Every tool that targets a host accepts a `host` parameter. The host can be:
- a name registered via `portal_host(action="register", ...)`, **or**
- a `Host` alias from `~/.ssh/config` (auto-resolved on first use; explicit registration is only needed for tag-based grouping).

All state-changing tools write to `$PORTAL_LOG_DIR/audit.jsonl` (default `~/.local/state/portal-mcp-server/log/audit.jsonl`). Read-only tools are intentionally not audited (see the Security section of [`README.md`](../README.md#security)).

---

## Portal Core (9 tools)

These are the tools an agent should reach for first. They share one SSH connection per host through the in-process pool.

### Hash-protected file editing

| Tool | Signature | Purpose |
|---|---|---|
| `portal_read` | `(host, path, start=1, end=None, encoding="utf-8")` | Read a remote file (or 1-based line range) and return JSON `{content, file_hash, range_hash, start, end, total_lines}`. The two SHA-256 hashes are required by `portal_patch`. |
| `portal_patch` | `(host, path, file_hash, patches_json, encoding="utf-8", auto_newline=False)` | Apply a list of line-range patches under hash protection: if the file changed since `portal_read`, the patch is rejected with `{"result":"error","reason":"Content hash mismatch...","current_file_hash":...}` and the file is left untouched. Patches are applied bottom-to-top; overlap is rejected; writes go through `*.mcp_tmp.<hex>` + `posix_rename` (atomic) and are re-hashed after write. |
| `portal_cleanup_tmps` | `(host, directory, max_age_s=3600)` | Garbage-collect `*.mcp_tmp.*` orphans left by interrupted `portal_patch` writes (e.g. SSH connection death between tmp creation and rename). Pass `max_age_s=0` to remove every match unconditionally. |

`patches_json` decodes to: `[{"start": int, "end": int|null, "contents": str, "range_hash": str}, ...]`

### Remote search

| Tool | Signature | Purpose |
|---|---|---|
| `portal_grep` | `(host, path, pattern, glob="", file_type="", ignore_case=False, max_count=0)` | Regex search under `path`. Uses `rg --json` if ripgrep is on the remote PATH (cached after first probe), else falls back to `grep -rn`. Returns structured matches. |
| `portal_glob` | `(host, pattern, path=".")` | Glob match (`**/*.py`, `*.toml`, …) under `path`. Returns matching file list. |

### Persistent bash

| Tool | Signature | Purpose |
|---|---|---|
| `portal_bash` | `(host, command, timeout=3600.0, use_sudo=False, secrets=None)` | Run `command` in a sticky `bash -i` for `<host>`. First call auto-creates the session; subsequent calls reuse the same shell so `cwd` and exported env vars survive. PTY echo + bracketed-paste are disabled so sentinel parsing is reliable. ⚠️ Each command is gated by the security policy — a persistent session does not authorise arbitrary commands. `use_sudo=True` runs the command via `sudo -S` using a password obtained out-of-band (never passed by the agent) — see the sudo note below; this runs as a one-shot command, so the persistent session's `cwd`/env do not apply. `secrets=["name", …]` injects named API tokens as env vars for that one command — see the secrets note below (mutually exclusive with `use_sudo`). |
| `portal_bash_close` | `(host)` | Close the cached default bash session for `<host>` (next `portal_bash` call reopens). |

> Prompt-layer convention (not enforced in code): write operations should target remote `/tmp/` unless the user has explicitly approved another path. `portal_bash` itself does **not** scope paths — see the README's *Agent-side conventions* section for the recommended ruleset.

> **Sudo (`use_sudo=True`)** — the password is **never** supplied by the agent. It comes from the per-user credential agent populated out-of-band by `portal sudo set <host>` (prompted with no echo in a separate terminal), or from the host's `sudo_password_command` in `hosts.yaml`. If neither is available the call returns an error telling you to run `portal sudo set`. The command runs via `sudo -S -k` with the password fed on stdin (never on the command line) and is audited as `remote_sudo`.

> **SSH login password (auth)** — never an agent-supplied parameter either. Sources are resolved in the order **per-user credential agent (populated by `portal ssh set <host>`, no echo, separate terminal) → host `password_command` in `hosts.yaml` → error**. For key-mode hosts (the default — no `auth:` field in hosts.yaml) the chain is only consulted as a *fallback* when asyncssh raises `PermissionDenied` and a source is actually configured, so a missing config never masks a real key-rejection failure. Live cache is per-host with TTL (default 900s, `--ttl` configurable).

### Local execution

| Tool | Signature | Purpose |
|---|---|---|
| `portal_local_exec` | `(command, secrets=None, timeout=600.0)` | Run a **one-shot command on the MCP server host** (not over SSH), optionally with named secrets injected as env vars. **Disabled unless** the operator sets `PORTAL_ALLOW_LOCAL_EXEC=1` — local execution is a larger threat surface than the remote tools. Audited as `local_exec`. |

> **Secrets (`secrets=[…]`)** — for both `portal_bash` and `portal_local_exec`, the agent passes secret **names**, never values. Each name resolves (via the per-user credential agent populated by `portal secret set <name>`, or a `command:` in `secrets.yaml`) to a value that is exported as the uppercased env var (`github_token` → `$GITHUB_TOKEN`). The value travels via the process environment / SSH stdin (never on argv, so it stays out of `ps` and the audit log) and any echo of it in the output is redacted to `***` before the result reaches the agent. See [`examples/secrets.yaml`](../examples/secrets.yaml).

The interactive credential CLIs (`portal secret set`, `portal sudo set`, `portal ssh set`) require the per-user systemd credential agent to be installed first. Operators should run `portal agent install --now` before starting the agent / IDE portal MCP server. If the agent is already running, reload the MCP/plugin integration or restart the agent after installing the credential agent (for example Claude Code MCP/plugin reload, Copilot CLI `/restart`, or restarting the IDE/agent). `portal agent uninstall` removes the user units/config. `portal agent status` shows whether the agent is running and how many entries are cached, and `portal agent clear` flushes every cached entry across all kinds.

> **⚠️ Platform: Linux only.** The credential agent is implemented as a pair of **systemd user units** (`.socket` + `.service` under `~/.config/systemd/user/`), supervised by the **systemd user instance** (`systemd --user`) and lazily started via **socket activation**. There is no shipped launchd / Windows Service equivalent, so `portal agent install` and the three CLIs that depend on it (`portal secret set` / `portal sudo set` / `portal ssh set`) are only supported on Linux. On macOS / Windows hosts, drive sudo and SSH passwords from `hosts.yaml`'s `password_command` / `sudo_password_command`, and named secrets from `secrets.yaml`'s `command:` field, instead — the rest of the MCP server (every `portal_*` remote tool) is platform-agnostic.

> **Design principle — plaintext never leaves the agent's memory.** There is no `show plaintext` / `dump` verb on any of `portal ssh` / `portal sudo` / `portal secret`. `show <key>` prints a sha256[:16] fingerprint + remaining TTL; `list` lists every key with its fingerprint and TTL; `confirm <key>` re-prompts for the value and accepts only if the two entries match. The plaintext is only ever handed to a same-uid consumer (the SSH connect loop, sudo stdin, `$env` injection). Same posture as ssh-agent / gpg-agent / vault agent / polkit-agent: any echo to a TTY is one screenshot / scrollback / asciinema / OBS overlay away from a leak, so the credential agent simply refuses to do it. To export a stored value outside the agent, drive a `password_command` / `secrets.yaml` `command:` from your password manager rather than asking the credential agent to print it.

---

## Portal High-Level (10 tools)

Each tool below takes a `mode`, `direction`, `action`, or `view` parameter that selects the behaviour. This is the consolidation layer that absorbs ~30 upstream tools.

### Host registry

| Tool | Modes | Purpose |
|---|---|---|
| `portal_host` | `action=list \| register \| remove` | Manage the runtime host registry. `register` requires `name` + `host`, with optional `user` (default `root`), `port` (default `22`), `key_path`, `tags` (comma-separated, used by `portal_multi_exec`'s group_tag and by `portal_playbook`). `~/.ssh/config` aliases are auto-resolved without registration; explicit registration is only needed for tag grouping. **No password parameter — key-only auth.** |

### File transfer (SFTP)

| Tool | Modes | Purpose |
|---|---|---|
| `portal_transfer` | `direction=upload \| download \| sync \| mirror \| upload-list \| download-list` | Binary-safe, atomic SFTP transfer. `upload` / `download` move a single file. `sync` recursively syncs a local directory tree → remote (upload); `mirror` is the remote → local counterpart (download). `upload-list` / `download-list` move an explicit list of file pairs from `paths_json` (an arbitrary local→remote mapping, not a whole directory). The four incremental modes (`sync` / `mirror` / `upload-list` / `download-list`) skip files already present with a matching size+mtime — or sha256 when `checksum=True` — so re-runs only move changed files; a single file's failure is collected in `failed[]` without aborting the batch. For text-only edits prefer `portal_patch` (which is hash-protected). |

Full signature: `(direction, host, local_path, remote_path, checksum=False, paths_json="")` (`ctx` is injected by the MCP runtime and hidden from callers). `paths_json` decodes to `[{"local": ..., "remote": ...}, ...]` and is **required** by the `upload-list` / `download-list` modes (ignored otherwise). Single-file modes return `{status, direction, host, bytes, duration_s, ...}`; the incremental modes return `{status, uploaded|downloaded, skipped, failed[], bytes_total, bytes_transferred, duration_s}`. Directory/list modes copy *files* only — symlinks and special files are skipped.

### SSH tunnels

| Tool | Modes / args | Purpose |
|---|---|---|
| `portal_tunnel_open` | `mode=local \| reverse \| socks` | `local`: forward `localhost:local_port → remote_host:remote_port` via `host`. `reverse`: expose `local_bind:local_port` on `host` as `host:remote_port`. `socks`: SOCKS5 proxy on `localhost:local_port` via `host`. Returns `{tunnel_id, type, host, local, remote}`. |
| `portal_tunnel_close` | `(tunnel_id)` | Close a tunnel by the ID returned from `portal_tunnel_open`. |
| `portal_tunnel_list` | `()` | List all currently active tunnels (JSON). Read-only, not audited. |

### Multi-host orchestration

| Tool | Modes / args | Purpose |
|---|---|---|
| `portal_multi_exec` | `mode=parallel \| rolling \| broadcast` | `parallel`: same `command` on every host simultaneously (asyncio.gather). `rolling`: `command` sequentially across hosts with `delay_s` between them and `stop_on_error` (zero-downtime restart pattern). `broadcast`: a JSON array of `commands_json` is run on every host in parallel (each host runs the full sequence). Hosts come from `hosts_json` (JSON array) or `group_tag` (parallel mode only). Every command is policy-gated against every target host before execution begins. |
| `portal_playbook` | `(playbook_json, host="" \| group_tag="")` | Run a multi-step playbook against one host (`host=...`) or a tag group (`group_tag=...`). Playbook schema: `{"name": str, "on_error": "stop"\|"continue", "steps": [str, ...]}`. Every step is gated against the security policy before the playbook starts; one rate-limit consumption per host (not per step). |

### Health & observability

| Tool | Args | Purpose |
|---|---|---|
| `portal_ping` | `(hosts_json="")` | SSH connectivity test. Empty `hosts_json` pings every registered host in parallel; `hosts_json='["web01"]'` pings the listed subset. Returns `{"online": N, "total": M, "hosts": [{host, reachable, latency_s, exit_code}, ...]}`. |
| `portal_check` | `(host, command="")` | Dry-run a host (and optionally a command) through the security policy without executing anything. Returns `"ALLOWED"` or `"BLOCKED: <reason>"`. ⚠️ Default policy is **permissive** — empty allowlists allow everything. `ALLOWED` means "no current rule blocks this", not "this is safe". Use `portal_audit(view="policy")` to inspect what is loaded. |
| `portal_audit` | `view=snapshot \| server \| sessions \| history \| stats \| policy` | Read-only introspection. `snapshot` (default): registered hosts, connection pool state, bash sessions, active tunnels, audit aggregates, security summary, plus a `server` block (version, pid, uptime, transport, resolved config paths). `server`: cheap version+metadata-only view. `sessions`: the `host → session_id` map of cached persistent bash sessions (replaces the former `portal_bash_status` tool). `history`: last `limit` audit entries (default 50), filterable by `host_filter`. `stats`: aggregate counts by operation and error rate. `policy`: full security policy detail (allowlists, blocklists, rate limits). |

---

## Tool count summary

| Layer | Count | Replaces upstream |
|---|---:|---|
| portal core | 9 | ~20 wrappers (cat/write/ls/exec families) plus the persistent-session family |
| portal high-level | 10 | ~30 mode-distinguished tools (3 tunnels + 4 multi-host + 4 introspection + 7 sysinfo + …) |
| **Total** | **19** | upstream ssh-shell-mcp: 57 |

Result: ~7.5k tokens of upstream tool descriptions collapse to ~2.5k. Agents no longer need to disambiguate between semantically overlapping tools.

---

## Where to look in the code

Every tool is registered in `portal_mcp_server/cli.py` with the `@mcp.tool()` decorator. The implementation modules are:

| Module | Backs |
|---|---|
| `connection_manager.py` | underlying connection pool used by every tool |
| `remote_text_editor.py` | `portal_read`, `portal_patch`, `portal_cleanup_tmps` |
| `remote_search.py` | `portal_grep`, `portal_glob` |
| `remote_bash.py` | `portal_bash`, `portal_bash_close` |
| `local_exec.py` | `portal_local_exec` (local one-shot execution) |
| `credential_agent.py` | per-user systemd socket-activated TTL cache for `portal ssh set`, `portal sudo set`, and `portal secret set` |
| `secrets_store.py` | named-secret resolution / agent + `secrets.yaml` lookup / output redaction |
| `file_ops.py` | `portal_transfer` |
| `network_tools.py` | `portal_tunnel_*` |
| `orchestrator.py` | `portal_multi_exec`, `portal_playbook` |
| `security.py` | the `_gate()` / `_gate_many()` / `_gate_playbook()` helpers used by every state-changing tool |
| `audit.py` | `audit_log()` writes used by every state-changing tool, plus `portal_audit` introspection |
