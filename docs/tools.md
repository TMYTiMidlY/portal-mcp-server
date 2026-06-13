# Tool Reference

Complete index of the **14 MCP tools** exposed by `portal-mcp-server`.

Design axis: tools are kept only when they provide a guarantee the agent
**cannot cheaply synthesize itself** (concurrency, atomic/hash-protected
writes, credential non-leakage, the security gate, real structured output).
Anything that was just "a packaged script" (playbook, ping, rolling-as-a-tool,
a standalone tmp janitor) was removed and folded into a primitive.

Every tool that targets a host accepts a `host` parameter. The host can be:
- a name registered via `portal_host(action="register", ...)`, **or**
- a `Host` alias from `~/.ssh/config` (auto-resolved on first use; explicit
  registration is only needed for tag-based grouping).

All state-changing tools write to `$PORTAL_LOG_DIR/audit.jsonl` (default
`~/.local/state/portal-mcp-server/log/audit.jsonl`). Read-only tools
(`portal_read`, `portal_grep`, `portal_glob`, `portal_check`, `portal_audit`,
and the read actions of `portal_tunnel`/`portal_job`) are intentionally not
audited.

`ctx` (the MCP progress/keepalive context) is injected by the runtime on the
async tools and is hidden from callers, so it never appears in a signature
below.

---

## Running commands (the exec family)

Four tools run commands; pick by **state / locality / sync-vs-async**. Each
docstring's first sentence points at the right sibling, so an agent that reads
only the first line still chooses correctly.

| Tool | Signature | Purpose |
|---|---|---|
| `portal_exec` | `(host="" \| [host…], command="", commands=None, group_tag="", timeout=3600.0, use_sudo=False, secrets=None, serialize=False, delay_s=0.0, stop_on_error=True)` | **Default workhorse.** Stateless one-shot over the connection pool; returns immediately with **split** stdout/stderr + exit code. Targets one host, an explicit list, or a `group_tag`. Runs one `command` or a `commands` sequence. Fan-out is parallel by default; `serialize=True` (+`delay_s`) does a rolling rollout. `use_sudo` / `secrets` inject credentials out-of-band (see below). Single host + single command → one dict; otherwise a list (a multi-command host carries `{host, results:[…]}`). |
| `portal_shell` | `(host, command="", commands=None, stop_on_error=True, timeout=3600.0)` | **Persistent interactive-shell session** (bash or zsh) for one host — `cwd` and env (`cd` / `export` / venv) survive across calls. Completion + exit codes ride on **OSC 133 (FinalTerm) Shell Integration**: the shell itself emits `\x1b]133;D;<exit>\x07` via a PROMPT_COMMAND/precmd hook injected once over stdin (never written to disk). Output is the **combined** stream (a PTY merges stdout/stderr); oversize output is capped + flagged `truncated`. Single `command` → `{host, session_id, command, exit_code, output, duration_s}`. A `commands=[…]` sequence runs in the SAME session so `cd` / `export` / `source venv/bin/activate` carry across steps (what `portal_exec`'s multi-command path can't do) → `{host, session_id, results:[…], duration_s}`, stopping at the first failure when `stop_on_error=True` (adds `stopped_at`). A command that wedges on an interactive prompt (sudo / ssh / passphrase) is auto-Ctrl-C'd and returns `exit_code:-1` + `error:"interactive_prompt_blocked"` + `session_preserved:true` — the session/cwd/env survive, so the next non-interactive command runs straight away. Use only when you need state continuity; otherwise `portal_exec` is faster. |
| `portal_job` | `(action=submit\|poll\|cancel\|list, host="", command="", job_id="", since=0, tail=0, max_bytes=65536, signal=TERM\|KILL, use_sudo=False, secrets=None)` | **Background** execution. `submit` returns a `job_id` immediately (the job runs under `nohup` + remote tmp files, surviving a dropped connection); `poll` fetches status + new output **on demand** — `since=<new_offset>` returns only newer bytes, capped at `max_bytes` (default 64 KiB) per call, with a `more` flag so the agent pages through a big backlog instead of getting it all at once (or `tail=N` to peek the last N lines); the chunk is base64-transferred + boundary-aware UTF-8 decoded so chunk seams never split a multibyte char. `cancel` signals it; `list` shows all jobs. Job table is **best-effort persisted** across restarts (`<state>/jobs.json`; `PORTAL_JOB_PERSIST=0` to disable), bounded (`PORTAL_JOB_MAX_LIVE`), TTL-swept (`PORTAL_JOB_TTL`). `use_sudo`/`secrets` are background-unsafe — passing them is **rejected with a redirect** to `portal_exec`. |
| `portal_local_exec` | `(command, secrets=None, timeout=600.0)` | Run a command on the **MCP server's own machine** (not over SSH). DISABLED unless the operator sets `PORTAL_ALLOW_LOCAL_EXEC=1` (larger threat surface). For tasks that genuinely belong on the server host (e.g. a local secret); remote work goes through `portal_exec`. |
| `portal_close_shell` | `(host)` | Close the cached `portal_shell` session for a host (next `portal_shell` reopens). Rarely needed — only to reset a dirtied session. |

> **sudo**: whenever the user wants to run anything as root, use
> `portal_exec(use_sudo=True)` — it feeds the stored sudo password (from the
> per-user credential agent / `hosts.yaml` `sudo_password_command`, never from
> the agent) to `sudo -S -k` on stdin, audits as `exec_sudo`, and flags the
> result `high_risk`. Do NOT embed bare `sudo` in `portal_shell` or plain
> `portal_exec`; both fail fast with guidance pointing here (in `portal_shell`
> it is auto-Ctrl-C'd, leaving the session alive — see that tool's row).

> **portal_shell protocol & shells.** Command boundaries use **OSC 133
> (FinalTerm) Shell Integration** — the same事实标准 iTerm2 / VS Code's
> integrated terminal use. A tiny integration script is injected over stdin on
> first use (never written to disk); thereafter the shell reports each exit
> code in `\x1b]133;D;<exit>\x07`. Supported shells: **bash** (default) and
> **zsh**; any other login shell falls back to bash (which must exist — else
> the call is refused, pointing you at one-shot `portal_exec`). fish has a
> documented `fish_postexec` hook too but isn't shipped active yet (pending a
> real-hardware spike).

> **secrets (`secrets=[…]`)** — for `portal_exec` and `portal_local_exec`, the
> agent passes secret **names**, never values. Each name resolves (via the
> credential agent populated by `portal secret set <name>`, or a `command:` in
> `secrets.yaml`) to a value exported as the uppercased env var (`github_token`
> → `$GITHUB_TOKEN`). The value travels via the process environment / SSH stdin
> (never on argv, so it stays out of `ps` and the audit log) and is redacted to
> `***` before the result reaches the agent. The result is flagged
> **`"high_risk": true`** (a stored credential was used). ⚠️ See the README
> security section for the rare bash-history caveat on misconfigured remotes.

> **Two layers of reuse (don't conflate them).** *Connection reuse* = the
> asyncssh TCP/channel pool, shared by **every** tool, purely for **speed**
> (~10-30 ms/call after the first ~280 ms connect). *Session reuse* = the one
> sticky interactive shell (bash/zsh) per host that only `portal_shell` uses,
> for **state** continuity. That shell rides on a pooled channel; orthogonal.

---

## Hash-protected file editing

| Tool | Signature | Purpose |
|---|---|---|
| `portal_read` | `(host, path, start=1, end=None, encoding="utf-8")` | Read a file (or 1-based line range) and return `{content, file_hash, range_hash, start, end, total_lines}`. The two SHA-256 hashes are required by `portal_patch`. |
| `portal_patch` | `(host, path, file_hash, patches_json, encoding="utf-8", auto_newline=False)` | Apply line-range patches under hash protection: if the file changed since `portal_read`, the patch is rejected with `{"result":"error","reason":"Content hash mismatch…","current_file_hash":…}` and the file is untouched. Patches apply bottom-to-top; overlap is rejected; writes go through `*.mcp_tmp.<12hex>` + `posix_rename` (atomic) and are re-hashed after write. **After a successful write it opportunistically sweeps stale orphan tmp files** (older than 1h) in the same directory, reusing the open SFTP session — fully isolated, so a sweep failure never affects the patch; swept paths appear under an optional `swept` key. |

`patches_json` decodes to `[{"start": int, "end": int|null, "contents": str, "range_hash": str}, ...]`.

---

## Remote search (Claude-Code-faithful)

| Tool | Signature | Purpose |
|---|---|---|
| `portal_grep` | `(host, pattern, path=".", glob="", file_type="", output_mode=files_with_matches\|content\|count, ignore_case=False, before_context=0, after_context=0, context=0, head_limit=250, offset=0, multiline=False)` | Regex content search (`rg`, fallback `grep`). `output_mode`: `files_with_matches` (default, paths newest-first), `content` (matching lines + optional context, `head_limit` caps the TOTAL lines and `offset` paginates), `count` (per-file counts + total). Respects `.gitignore`. Every result carries a `truncated` flag; paths are relativized to `path`. **Prefer this over raw `rg` through `portal_exec`.** |
| `portal_glob` | `(host, pattern, path=".")` | Find files by glob (`**/*.py`, `src/**/*.{ts,tsx}`), **newest first**, via `rg --files --no-ignore --sort modified -g`. Hard-caps at 100 with a `truncated` flag; returns `{filenames, num_files, truncated, duration_ms}`. Does NOT respect `.gitignore` (matches CC Glob). **Prefer this over raw `find` through `portal_exec`.** |

The clear parameter names (`before_context`/`after_context`/`context`/
`ignore_case`/`output_mode`/`head_limit`/`offset`/`multiline`) carry CC's Grep
semantics without CC's cryptic `-A`/`-B`/`-C`/`-i`/`-n` flags.

---

## File transfer (SFTP)

| Tool | Signature | Purpose |
|---|---|---|
| `portal_transfer` | `(direction=upload\|download\|sync\|mirror\|upload-list\|download-list, host, local_path, remote_path, checksum=False, paths_json="")` | Binary-safe, atomic SFTP transfer. `upload`/`download` move a single file. `sync` recursively syncs a local tree → remote; `mirror` is the remote → local counterpart. `upload-list`/`download-list` move an explicit list of pairs from `paths_json`. The four incremental modes skip files already present with a matching size+mtime (or sha256 when `checksum=True`); a single file's failure is collected in `failed[]` without aborting. For text-only edits prefer `portal_patch`. |

`paths_json` decodes to `[{"local": ..., "remote": ...}, ...]` and is required
by the `*-list` modes. Single-file modes return `{status, direction, host,
bytes, duration_s, …}`; incremental modes return `{status,
uploaded|downloaded, skipped, failed[], bytes_total, bytes_transferred,
duration_s}`. Directory/list modes copy *files* only (symlinks/special files
skipped).

---

## Resources (agent-managed, so `list` lives on the tool)

| Tool | Signature | Purpose |
|---|---|---|
| `portal_tunnel` | `(action=open\|close\|list, kind=local\|reverse\|socks, host="", tunnel_id="", local_port=0, local_bind="127.0.0.1", remote_host="", remote_port=0)` | `open`: open a tunnel through `host` — `kind=local` forwards `localhost:local_port → remote_host:remote_port`; `kind=reverse` exposes `local_bind:local_port` as `host:remote_port`; `kind=socks` is a SOCKS5 proxy. `close`: close by `tunnel_id` (gated on the originating host). `list`: all active tunnels. |
| `portal_host` | `(action=list\|register\|remove, name="", host="", user="root", port=22, key_path="", tags="")` | Manage the runtime host registry. `register` needs `name`+`host` — or just `name` if `~/.ssh/config` has a matching `Host` alias (it auto-registers a `use_ssh_config` overlay). `tags` (comma-separated) feed `portal_exec`'s `group_tag`. `list` may include a per-host `warnings` array (e.g. a hosts.yaml↔ssh-config conflict) — relay those to the user. **No password parameter — key/side-channel auth only.** |

---

## Introspection & policy

| Tool | Signature | Purpose |
|---|---|---|
| `portal_check` | `(host, command="")` | Dry-run a host (and optional command) through the security policy without executing. Returns `"ALLOWED"` or `"BLOCKED: <reason>"`. ⚠️ Default policy is **permissive** — `ALLOWED` means "no current rule blocks this", not "this is safe". |
| `portal_audit` | `(view=snapshot\|server\|sessions\|history\|stats\|policy, limit=50, host_filter="")` | Read-only introspection of server **plumbing** + history. `snapshot`: server metadata + connection pool + bash sessions + audit stats + policy summary. `server`: cheap version/metadata only. `sessions`: the `host → session_id` map of persistent bash sessions (plumbing diagnostics). `history`: last `limit` audit entries (filterable). `stats`: counts by operation. `policy`: full policy detail. Note: **hosts and tunnels are NOT here** — they're resources, listed by `portal_host(action="list")` / `portal_tunnel(action="list")`. |

---

## Credential CLIs (out-of-band, not MCP tools)

The agent never sees a credential value. Passwords / passphrases / secrets are
provisioned by a human in a separate terminal and held by a per-user agent.

> **⚠️ Auto-install: Linux + macOS + Windows — all per-user.** The credential
> agent install is OS-dispatched (`portal agent install`) and every backend runs
> the agent as the logged-in user: **Linux** uses **systemd user units**
> (`.socket` + `.service`, socket-activated); **macOS** uses a **launchd
> LaunchAgent** (`~/Library/LaunchAgents/com.tmytimidly.portal-credential-agent.plist`,
> run-and-keepalive — the agent self-binds its AF_UNIX socket); **Windows**
> registers a **per-user logon scheduled task** (Task Scheduler, InteractiveToken
> principal — runs as you, only while logged on, never SYSTEM, no stored
> password; XML sets `ExecutionTimeLimit=PT0S` + a `RestartOnFailure` keepalive)
> with a **named-pipe** transport (no AF_UNIX). The Windows transport +
> scheduled-task install are verified by the `windows-latest` CI job. Where
> there's no agent (other platforms), drive sudo and SSH credentials from
> `hosts.yaml`'s `password_command` / `passphrase_command` /
> `sudo_password_command`, and named secrets from `secrets.yaml`'s `command:` —
> the rest of the server (every `portal_*` remote tool) is platform-agnostic.

> **Design principle — plaintext never leaves the agent's memory.** There is no
> `show plaintext` / `dump` verb on `portal ssh` / `portal sudo` / `portal
> secret`. `show <key>` prints a sha256[:16] fingerprint + remaining TTL;
> `list` lists every key with fingerprint + TTL; `confirm <key>` re-prompts and
> accepts only on a match. The plaintext is only ever handed to a same-uid
> consumer (the SSH connect loop, sudo stdin, `$env` injection) — the same
> posture as ssh-agent / gpg-agent. To export a value, drive a
> `password_command` / `secrets.yaml` `command:` from your password manager.

`portal ssh set <host>` caches a single per-host SSH credential — used as the
**login password** for `auth: password` hosts, or as the **key passphrase** for
key-auth hosts (the connection picks the right use by the host's auth mode).

---

## Where to look in the code

Every tool is registered in `portal_mcp_server/cli.py` with `@mcp.tool()`. The
implementation modules:

| Module | Backs |
|---|---|
| `connection_manager.py` | the connection pool + host registry used by every tool |
| `shell_engine.py` | `portal_exec` (one-shot `ssh_exec`) |
| `remote_bash.py` | `portal_shell` / `portal_close_shell` (persistent session) + sudo/secrets one-shot paths |
| `session_manager.py` | the persistent interactive-shell session — OSC 133 boundary protocol (cwd/env, exit codes, soft-cancel) |
| `job_manager.py` | `portal_job` (background submit/poll/cancel/list) |
| `local_exec.py` | `portal_local_exec` |
| `remote_text_editor.py` | `portal_read` / `portal_patch` (+ orphan-tmp sweep) |
| `remote_search.py` | `portal_grep` / `portal_glob` |
| `file_ops.py` | `portal_transfer` |
| `network_tools.py` | `portal_tunnel` |
| `credential_agent.py` | per-user socket-activated TTL cache for `portal {ssh,sudo,secret} set` |
| `ssh_creds.py` / `sudo_creds.py` / `secrets_store.py` | credential resolution + output redaction |
| `security.py` | the `_gate*()` helpers used by every state-changing tool |
| `audit.py` | `audit_log()` writes + `portal_audit` introspection |
