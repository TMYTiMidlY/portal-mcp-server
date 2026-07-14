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

> ℹ️ The Chinese [`README.md`](./README.md) is canonical; this file is kept in
> lockstep with it.

---

<details>
<summary>📖 Table of contents</summary>

- [Overview](#overview)
- [Highlights](#highlights)
- [Architecture & design](#architecture-design)
- [Install](#install)
- [Client integration](#client-integration)
- [Tools](#tools)
- [Environment variables](#env-vars)
- [Authentication](#authentication)
- [Security](#security)
- [Testing](#testing)
- [CI / Release](#ci-release)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License & credits](#license-credits)

</details>

## <a id="overview"></a>Overview

portal-mcp-server is built around three ideas: **few, orthogonal tools** (keep
only the guarantees bash can't cheaply synthesize), **step-wise & interruptible**
(the agent calls one step at a time, reads real output, then decides; long tasks
go to the background), and **credential unification** (every connection goes
through one in-process auth path; plaintext never enters the LLM / argv / disk).

`portal-mcp-server` is forked from
[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)
(Apache 2.0): the underlying SSH/asyncssh engine, connection pool, tunnel
management, multi-host orchestration and security policy come from upstream. The
upper layer is a redesigned agent-first tool surface built around three
execution paths — persistent bash sessions, one-shot exec, and background jobs —
plus hash-protected remote editing, structured search, SFTP transfer, tunnels
and audit. The double-hash safe-edit algorithm behind `remote_read` /
`remote_patch` is adapted from [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)
(MIT) and rewritten for SFTP.

Full derivation and third-party algorithm provenance are in [`NOTICE`](./NOTICE)
and the [Security](#security) section.

## <a id="highlights"></a>Highlights

- **Cross-tool connection reuse**: all portal tools share one in-process asyncssh
  connection pool; one handshake is reused for hours, and each call amortizes to
  channel creation (~10–30 ms).
- **Fast on Windows too**: no dependence on OpenSSH `ControlMaster`; the pool is
  plain Python objects, so all three platforms get the same reuse performance.
- **Persistent shell sessions**: `remote_shell` keeps one interactive shell
  (bash/zsh) per host — cwd / env persist across calls, and `commands=[…]` runs
  multiple steps in the same session; the agent needn't rebuild context per
  command.
- **Hash-protected remote edits**: `remote_read` + `remote_patch` use whole-file
  SHA-256 + per-range hashes, write via tmp + `posix_rename` (atomic), and
  re-hash after the write — **detecting** concurrent overwrites / mid-write
  disconnects / line-number drift (optimistic checking that narrows the conflict
  window; not a filesystem-level CAS).
- **Agent-first minimal tool surface**: `action` / `mode` fields merge
  semantically-overlapping entry points; each tool offers exactly one guarantee
  bash can't cheaply synthesize, reducing tool-choice ambiguity. The tool schemas
  (name + description + inputSchema) total about **~9k tokens** (≈ **4–5%** of a
  200k context window; `tiktoken o200k_base` measures ~8.8k).
- **Built-in security policy**: host allowlist, command blocklist/allowlist
  (fnmatch), per-host rate limit, an audit log for every state-changing op,
  fail-closed by default; optional [cc-safety-net](https://github.com/kenryu42/cc-safety-net)
  semantic command gate (opt-in, unwraps `bash -c` / interpreter one-liners,
  catches destructive git/rm, covering the `remote_exec`/`local_exec`/`shell`/`job`
  paths that bypass the agent's own `bash` PreToolUse hook; fail-closed).
- **OpenSSH config compatibility**: `~/.ssh/config` aliases, `known_hosts`,
  ssh-agent are recognized automatically — no need to re-register hosts.
- **Zero extra deployment**: the MCP client runs it straight from PyPI via
  `uvx` — no clone, no venv.

## <a id="architecture-design"></a>Architecture & design

portal-mcp-server is designed around three ideas: **few, orthogonal tools** (keep
only the guarantees bash can't cheaply synthesize), **step-wise & interruptible**
(one call = one decidable step; read real output, then decide; long tasks go to
the background), and **credential unification** (every connection goes through one
in-process auth path; plaintext never enters the LLM / argv / disk). Below: first
"how it differs from plain ssh" and the data flow, then the trade-offs behind
those three ideas — everything but the three-idea intro is collapsed by default.

### <a id="vs-traditional"></a>Versus plain ssh / scp

The naive approach is to let the agent `bash` its way through `ssh` / `scp` /
`rsync`. That is barely usable on Linux/macOS with `ControlMaster`, nearly
unusable on Windows, and lacks key capabilities for file editing, sudo,
multi-host and audit.

<details><summary>Expand the per-dimension comparison (incl. the Windows reuse gap)</summary>

| Dimension | Plain (bash + `ssh` / `scp` / `rsync`) | portal-mcp-server |
|---|---|---|
| **SSH reuse · Linux/macOS** | OpenSSH `ControlMaster auto` + Unix socket; default `ControlPersist 10m`, master drops after timeout | asyncssh **in-process pool**, reused as long as the MCP server lives (hours) |
| **SSH reuse · Windows** | ❌ **broken** — Microsoft's Win32-OpenSSH has had failing `ControlMaster` since v0.0.3.0 (`muxclient socket(): Unknown error`); [issue #405](https://github.com/PowerShell/Win32-OpenSSH/issues/405) open since 2017 (relies on Unix-domain-socket fd sharing, which Windows lacks) | ✅ **same performance as Linux** — the pool is a plain Python dict; asyncssh needs no OS-level socket sharing |
| **First / subsequent command latency** | first ~200–500 ms; **without reuse every command is a new TCP+auth ~300 ms** (Windows default); ~10–30 ms subsequently with ControlMaster | first ~200–500 ms, **~10–30 ms subsequently (all three platforms)** — only a channel opens |
| **Cross-"tool" reuse** | `ssh` and `scp` reuse requires identical `ControlPath` on both sides; in practice most projects don't share the master | ✅ all portal tools (bash / read / patch / transfer / tunnel …) naturally share one TCP |
| **Persistent shell state** | each `ssh host cmd` is a fresh shell; `cd` / `export` / venv activation **all lost**; the agent must repeat `cd /path && source venv/bin/activate && …` every command | ✅ `remote_shell` keeps a sticky interactive shell (bash/zsh); cwd / env / venv persist across calls |
| **Remote file editing (safe edit)** | all three are unsafe: ① `scp` down→edit→`scp` up (no concurrency detection, silent loss; non-atomic); ② `ssh host "sed -i …"` (no dry-run/rollback, error-prone line numbers); ③ `ssh host "cat > file"` (concurrent overwrite, half-file on disconnect) | ✅ `remote_read` returns SHA-256 + range hashes; `remote_patch` verifies → writes `*.mcp_tmp.*` → `posix_rename` (atomic) → re-hash. **Concurrent edit / mid-write disconnect / line drift all fail instead of corrupting** |
| **File / directory transfer** | `scp` has no incrementals, one failure sinks the batch; `rsync` is better but forks per run, **can't report progress to the agent**, and a large transfer can hit the MCP client's idle timeout | ✅ `remote_transfer` incremental short-circuit (size+mtime or sha256), **MCP progress heartbeat against idle timeout**, per-file failure goes to `failed[]` without aborting, `paths_json` batches arbitrary local↔remote pairs |
| **sudo password ergonomics** | all footguns: ① `ssh -t host sudo cmd` **prompts every time**; ② `echo $PASS \| ssh host "sudo -S cmd"` — **password enters the LLM context**; ③ `sshpass -p $PASS ssh …` — **password in `ps` argv and the LLM**; ④ NOPASSWD sudoers — auth abandoned | ✅ `remote_exec(use_sudo=True)`: source = ① `sudo_password_command` (pulled fresh from `pass` / `op` / `bw`, fully automatic) or ② `portal sudo set <host>` (a one-time no-echo `getpass` in another terminal → per-user credential-agent memory TTL). **Never in the LLM / ps argv / disk** |
| **Multi-host parallelism** | `for h in $hosts; do ssh $h cmd; done` — **serial** startup (fork+auth each), no policy gate, one failure handled by `set -e` or the script | ✅ `remote_exec(host=[…])` true parallelism + two-phase gate (check all hosts, then execute), `serialize=True`+`delay_s` for rolling, `commands=[…]` for a sequence |
| **SSH tunnel lifecycle** | `ssh -L 8080:db:5432 host -fN` runs away in the background — **nobody tracks when it closes**, who opened it, or if it's alive; you `pgrep` for it | ✅ `remote_tunnel(action=open)` returns a `tunnel_id`, `action=list` shows all live tunnels, `action=close` closes explicitly; audit-traceable |
| **Command audit** | none — you'd wrap it yourself with `script(1)` / a shell-history wrapper; agent calls are invisible | ✅ state-changing tools pass the policy gate `_gate` first (denied = not run, no trace), then write structured `audit.jsonl` (host, operation, command, result, timestamp); a failed audit write is fail-closed by default (abort), relax with `PORTAL_AUDIT_FAIL_OPEN=1` |
| **Structured search** | `ssh host "grep -rn … \| head"` returns **raw text the agent parses**; degrades if rg is absent | ✅ `remote_grep` / `remote_glob` prefer `rg --json`, auto-fallback to `grep -rn` / `find`; return `{file, line, text}` structured |

> **Windows users take note**: the "SSH reuse · Windows" row is not a detail, it's
> a **fundamental gap**. The default Windows OpenSSH client has no ControlMaster,
> so the agent pays ~300 ms TCP+auth per remote command; 50 commands = 15 s of
> pure overhead. On Windows portal-mcp-server is ~280 ms first, ~20 ms after —
> identical to Linux — which is why we recommend it over the `ssh` subprocess
> approach.

</details>

### <a id="architecture"></a>Architecture

The MCP client connects to the server over stdio (or optional HTTP); the 14 tools
pass the security gate + audit first, then SSH tools go through the in-process
asyncssh connection pool (reusing one TCP across tools, multiple per host);
`local_exec` / control-plane tools don't use SSH.

<details><summary>Expand the data-flow diagram</summary>

```
┌──────────────┐    stdio / http    ┌─────────────────────────────────────┐
│  MCP Client  │ ◄────────────────► │       portal-mcp-server             │
│ (Claude Code │                    │                                     │
│  Copilot CLI │                    │  ┌──────────┐   ┌────────────────┐  │
│  Cursor ...) │                    │  │ 14 tools │──►│ security gate  │  │
└──────────────┘                    │  └──────────┘   │ + audit log    │  │
                                    │                  └───────┬────────┘  │
                                    │                          │           │
                                    │              ┌───────────▼────────┐  │
                                    │              │  asyncssh pool      │  │
                                    │              │  (in-process, one   │  │
                                    │              │   TCP across tools) │  │
                                    │              └──┬──────┬──────┬──┘  │
                                    └─────────────────┼──────┼──────┼─────┘
                                                      │      │      │
                                               SSH    │      │      │
                                              ┌───────▼─┐ ┌──▼──┐ ┌─▼──────┐
                                              │ Host A  │ │ ... │ │ Host N │
                                              └─────────┘ └─────┘ └────────┘
```

</details>

### <a id="design-principles"></a>Design principles

The single criterion: **keep a tool only when it provides a guarantee bash can't
cheaply synthesize**. Each principle below is collapsed; the heading is the point.

### Few, orthogonal tools
<details><summary>Expand</summary>

Anthropic's [_Writing Tools for Agents_](https://www.anthropic.com/engineering/writing-tools-for-agents)
says plainly: "More tools don't always lead to better outcomes… Tools that
merely wrap existing software functionality is a common error… Too many tools or
overlapping tools can also distract agents from pursuing efficient strategies."

Accordingly the surface is a small, orthogonal set of primitives. Anything a
one-line bash could do, or that overlaps another tool, is not its own tool —
it's covered by `remote_shell` (persistent bash session) + `remote_exec`
(one-shot, incl. multi-host fanout / sudo / secrets). Each surviving tool holds
one such guarantee: `remote_read`+`remote_patch` (double hash vs. bare
`cat`/`sed`/`>`), `remote_grep`/`remote_glob` (structured output, `rg --json`
first, fallback `grep`/`find`), `remote_shell`/`remote_exec` (persistent shell +
exit code; true parallel fanout + two-phase gate + no credential leak),
`remote_transfer` (incremental short-circuit + progress heartbeat + per-file
tolerance), `remote_job` (background submit/poll/cancel/list), and
`remote_tunnel`/`hosts`/`inspect` (merge multiple actions of one resource into an
`action`/`view` field). All dispatch params are `typing.Literal` (schema-level
`enum`), so the agent needn't choose among overlapping tools. Tool schemas total
about **~9k tokens** (`tiktoken o200k_base` ~8.8k, ~4–5% of a 200k window).

</details>

### <a id="step-wise-exec"></a>Step-wise & interruptible execution
<details><summary>Expand</summary>

`remote_exec` / `remote_shell` are **single-step** primitives: one call = one
decidable step. Read the *real* stdout / stderr / exit code, reconcile with
expectations (an exit-0 step can still be wrong), then decide the next call — so
the agent stays in the loop and can correct on error.

- `commands=[…]` packs several commands into **one** call; the agent sees no
  intermediate output, so it's only for fixed, dependency-free batches that need
  no mid-inspection. Likewise don't bury a long branchy flow in one `a && b && c`.
- Foreground `timeout` is **mandatory** (no default), forcing the agent to think
  about "how long should this take" — small values (10–30 s) for exploratory /
  re-runnable commands.
- Foreground timeout is also capped by `PORTAL_MAX_TIMEOUT` (default 300 s); over
  the cap is refused — **truly long unattended work goes to the background
  `remote_job`** (instant submit, poll/cancel, survives disconnect).

</details>

### <a id="connection-pool"></a>In-process connection pool
<details><summary>Expand</summary>

The server keeps an asyncssh connection pool inside its own process — every tool
call shares one TCP. **All but the first connection amortize to channel creation
(~10–30 ms).**

- **Pool shape**: `PORTAL_SSH_POOL_SIZE` caps TCP connections per host (default
  5), `PORTAL_SSH_MAX_CHANNELS_PER_CONN` caps channels per TCP (default 5); over
  that opens a new TCP, and beyond the pool it reuses the least-busy connection
  with a warning. asyncio supports true concurrency of many channels on one TCP.
- **Idle & aging**: `PORTAL_SSH_MAX_IDLE_TIME` default 600 s, `PORTAL_SSH_MAX_CONN_AGE`
  default 3600 s; idle/aged connections with no active channel are closed to
  avoid silent NAT/firewall drops.
- **Micro-benchmark (sanitized)**: same LAN (<1 ms RTT), 100× `echo pong` — plain
  ssh + ControlMaster ~23 ms avg; portal via `remote_shell` ~18 ms avg. First
  connect ~280 ms both (auth dominates).
- **On Windows**: plain ssh is ~300 ms × N (no reuse); portal is ~280 ms first,
  ~20 ms after — asyncssh is pure Python, the pool lives in process memory, no
  OS-level socket sharing (exactly where Windows OpenSSH ControlMaster fails).

</details>

### Persistent shell sessions & command boundaries
<details><summary>Expand</summary>

`remote_shell` gives the agent one per-host, cross-command `bash -i` / `zsh -i` —
cwd, env and shell functions persist automatically (same underlying process).
This is a second layer of reuse on top of the connection pool: the pool reuses
TCP channels for **speed**, the persistent session reuses one interactive shell
for **state continuity**.

The hard part: one `bash -i` runs many commands on the **same** SSH channel, and
SSH reports the exit code only when the channel **closes**. To get each command's
`$?` without tearing down the channel (which would lose cwd/env), we mark command
boundaries. The old approach was an in-band sentinel (append `echo <sentinel>:$?`
and scan stdout), which mixes control into the data stream and is fragile at the
root.

The current approach borrows **OSC 133 (FinalTerm) shell integration** (used by
iTerm2 / VS Code / Kitty / WezTerm): the **shell itself emits** command
boundaries. On first use a small integration script is injected via stdin
(**stdin only, never on disk**), hooking `PROMPT_COMMAND` / `precmd` to print
`\x1b]133;D;<exit>\x07` after each command; we degrade to a **pure parser**. The
sequence starts with an ESC byte, so ordinary text — **even literally
`]133;D;0`** — can't forge it; `$?` is read straight from the marker, and the
whole class of sentinel fragility disappears.

Two capabilities come free: a command wedged on an interactive prompt (sudo / ssh
first-connect / `mysql -p` / gpg passphrase) is **auto-Ctrl-C'd with the session
preserved** (soft-cancel); a foreground timeout likewise Ctrl-C's and resyncs,
keeping the session if a clean prompt returns and dropping it otherwise. The
one-shot `remote_exec` path opens a fresh channel per command and reads the
native exit code from asyncssh, so it's immune to all of this.

> **Real-machine spikes** (recorded in `session_manager.py`): the shell must use
> `--noprofile --norc` / `--no-rcs`, or a user rc overwrites the hook; **zsh must
> `unsetopt zle`** (ZLE ignores `stty -echo` and leaks the command line); multi-line
> commands are wrapped in `{ … }` so an interactive shell fires one marker per
> top-level input line; fish is not verified and falls back to bash.

</details>

### Choosing asyncssh over subprocess
<details><summary>Expand</summary>

[asyncssh](https://github.com/ronf/asyncssh) (EPL-2.0 / GPL-2.0 dual-licensed) is
an independent pure-Python SSHv2 implementation, protocol-equivalent to OpenSSH.
Choosing it over shelling out to `ssh`/`scp` is what makes the in-process pool,
cross-tool channel reuse, the no-argv-password credential path, and identical
Windows performance possible — a shelled-out subprocess shares none of them (see
[Credential unification](#credential-unification)).

</details>

### <a id="credential-unification"></a>One in-process auth path
<details><summary>Expand</summary>

Every credential kind — SSH key, login password, key passphrase, sudo password,
named secret — is resolved on one in-process asyncssh path and handed only to its
real consumer (the handshake, `sudo -S` stdin, an injected env var); plaintext
never reaches the agent conversation, argv/`ps`, or disk.

The trade-off: treat credential unification as an inviolable invariant —
"survive the agent stopping" goes to `remote_job` (the command is `nohup`-ed on
the **remote** host, so the credential was already consumed at connect time and no
local child holds it), and an interrupted foreground transfer recovers via
`remote_transfer`'s `resume`, neither of which forks a credential-diverging
subprocess. Full rationale and rejected options in [ADR-0003](./docs/adr/0003-credential-unification.en.md).

</details>

### Feedback channel: warnings ride the tool result
<details><summary>Expand</summary>

A stdio MCP server's stderr is invisible to the user, so operationally important
warnings (misconfigured yaml, missing credentials, ignored fields, host conflicts)
are collected server-side and returned on `hosts(action="list")` rather than only
logged. The agent is expected to relay them to the user.

</details>

<details><summary>Maintainer boundaries & footguns</summary>

- **exec-output-strip vs. file-read-must-not-strip**: one-shot exec strips
  trailing newlines (shell convention), but `remote_read` must preserve bytes
  exactly (the hash depends on it) — don't unify them.
- **ssh_config merge internals**: to inherit an alias's long-tail options you must
  connect with `host=<alias>`, which pins `HostName`; see [ADR-0002](./docs/adr/0002-ssh-config-merge.en.md).
- **sudo write preserves owner/mode**: the sudo patch path stats and restores
  owner:group:mode; the staged plaintext copy is created `0600` and removed even
  on failure.

</details>

## <a id="install"></a>Install

portal-mcp-server is installed like any other MCP server — register it with your
MCP client (see [modelcontextprotocol.io](https://modelcontextprotocol.io/) for
what MCP is). It needs **no clone and no persistent install**: the client launches
it straight from PyPI via [`uv`](https://docs.astral.sh/uv/)'s `uvx`, caching
dependencies on first run and starting in seconds afterward.

If you don't have `uv`, install it (`curl -LsSf https://astral.sh/uv/install.sh | sh`;
Windows: [uv install docs](https://docs.astral.sh/uv/getting-started/installation/)).
How to enter `uvx portal-mcp-server@latest` per client is in
[Client integration](#client-integration).

Fastest start (Claude Code shown; other clients under [Client integration](#client-integration)):

```bash
# 1. Register (--scope user applies to all repos)
claude mcp add --scope user portal -- uvx portal-mcp-server@latest
# 2. Make sure the target host is in ~/.ssh/config or hosts.yaml
# 3. In chat, say "show the last 50 lines of /var/log/syslog on myhost";
#    the agent calls remote_exec("myhost", "tail -50 /var/log/syslog", timeout=30)
```

### Terminal users (use the MCP server, don't touch source)

No clone needed — let the client pull and run via `uvx` (see
[Client integration](#client-integration)). Manual smoke test:

```bash
uvx portal-mcp-server@latest --help
```

### Developers (change code / run tests)

<details><summary>Expand the dev setup</summary>

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras
source .venv/bin/activate
pytest                              # all green (live SSH tests skip by default)
uv tool install --force --editable .   # run this checkout as the MCP server (edits apply live)
```

</details>

### The `portal` short command

`portal` and `portal-mcp-server` are the same entry point. With no subcommand it
starts the MCP server; the credential-agent CLI lives under
`portal {agent,ssh,passphrase,sudo,secret} …` (see [Authentication](#authentication)).

### <a id="credential-agent"></a>Credential agent (systemd / launchd / scheduled task)

<details><summary>Expand credential-agent install</summary>

`portal agent install` installs a per-user credential agent that holds
interactively-entered credentials in memory with a TTL. Auto-install covers
**Linux + macOS + Windows**, always running **as the logged-in user** (never a
system/root service): systemd user units (Linux, `.socket` + `.service`,
socket-activated), a launchd LaunchAgent (macOS), or a per-user logon scheduled
task (Windows, Task Scheduler with an InteractiveToken principal). Linux/macOS
supervise it on an AF_UNIX socket; Windows uses a named pipe. The installer
records the resolved socket/pipe address in `~/.config/portal-mcp-server/agent.json`
so clients read it directly (or an explicit `PORTAL_CREDENTIAL_AGENT_SOCKET`). A
running MCP server discovers a freshly-installed agent on the next credential
request (it re-reads `agent.json`); no restart needed. See [Authentication](#authentication).

</details>

## <a id="client-integration"></a>Client integration

### Generic config snippet

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

To pass environment variables (pointing at custom hosts/policies/log paths),
add `env`:

```json
"env": {
  "PORTAL_HOSTS_YAML": "/path/to/hosts.yaml",
  "PORTAL_POLICIES_YAML": "/path/to/policies.yaml",
  "PORTAL_LOG_DIR": "/path/to/logs"
}
```

> 💡 **`timeout` is now mandatory** (no default) — `remote_exec` / `remote_shell`
> / `local_exec` each require the agent to pass a seconds value per call. A
> keepalive heartbeat is sent during execution so the MCP client won't cut a
> hanging call, making `timeout` the only real cutoff. Foreground timeout is also
> capped by `PORTAL_MAX_TIMEOUT` (default 300 s); over the cap is refused with a
> hint to use the background `remote_job`.

### Claude Code CLI

```bash
# Recommended: user scope, all repos
claude mcp add --scope user portal -- uvx portal-mcp-server@latest
# Without --scope it defaults to local (current dir only)
claude mcp add portal -- uvx portal-mcp-server@latest
# or type /mcp inside a Claude Code session
```

> ⚠️ Claude Code has three scopes: `local` (**default**, current dir), `user`
> (all repos), `project` (written into the repo's `.mcp.json`). For "install once,
> use everywhere" **use `--scope user`** — unlike Codex (`mcp add` = global) or
> Copilot CLI (`mcp add` = User scope).

<details><summary><b>GitHub Copilot CLI</b></summary>

```bash
copilot mcp add portal -- uvx portal-mcp-server@latest
# or /mcp inside a Copilot CLI session
```

Verify: `copilot mcp list` (should show portal) / `copilot mcp get portal`.

</details>

<details><summary><b>Cursor</b></summary>

Write the generic snippet into `~/.cursor/mcp.json` (global) or
`<project>/.cursor/mcp.json` (per-project). Enable under Settings → Tools & MCP.

</details>

<details><summary><b>VS Code (Copilot Chat / Agent mode)</b></summary>

VS Code uses a proprietary schema whose top-level key is `servers`, not
`mcpServers`:

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

Write it to `<project>/.vscode/mcp.json`, or the `mcp` field of user
`settings.json` for global use.

</details>

<details><summary><b>Claude Desktop</b></summary>

Paste the generic `mcpServers` snippet into `claude_desktop_config.json` and
restart. Location: macOS `~/Library/Application Support/Claude/…`; Windows
`%APPDATA%\Claude\…`.

</details>

<details><summary><b>Windsurf</b></summary>

Same `mcpServers` schema, written to `~/.codeium/windsurf/mcp_config.json` via
Cascade → plugins → "Manually configure MCP".

</details>

<details><summary><b>OpenAI Codex CLI</b></summary>

```bash
codex mcp add portal -- uvx portal-mcp-server@latest   # global
```

Or edit `~/.codex/config.toml`:

```toml
[mcp_servers.portal]
command = "uvx"
args = ["portal-mcp-server@latest"]
```

</details>

<details><summary><b>Other hosts (Cline / Continue / Roo Code / Zed …)</b></summary>

Most accept the generic `{ "mcpServers": ... }` snippet in their MCP settings;
stdio needs no extra proxy.

</details>

## <a id="tools"></a>Tools

14 tools. Inclusion criterion: **keep only guarantees the agent can't synthesize
itself** (concurrency, atomic/hash anti-conflict, no credential leak, security
gate, real structured output); anything that just "packages a script/state" is
cut or folded into a primitive.

### Running commands: the exec family (by stateful / local / sync vs async)

| Tool | When to use |
|---|---|
| `remote_exec` | **Default workhorse.** Stateless one-shot, immediate result (**separate** stdout/stderr + exit code). `host` single / list / `group_tag`; `command` or a `commands` sequence; multi-host parallel by default, `serialize=True`(+`delay_s`) for rolling; `use_sudo` / `secrets` inject credentials out-of-band. Reuses the pool; fast. |
| `remote_shell` | Use only when **cwd/env must persist across calls** (`cd`/`export`/venv) — one sticky interactive shell (bash/zsh) per host, optional `commands=[…]` multi-step (state continues). Output is a **merged** stream (PTY). Otherwise use `remote_exec` (faster, multi-host). |
| `remote_job` | **Background** long tasks. `submit` returns a `job_id` instantly (remote `nohup`+tmp, **survives disconnect**), `poll` fetches incremental output/status, `cancel` kills, `list` lists. Job table in-memory, capped, TTL-swept; sudo/secrets **not** supported in the background (use `remote_exec`). |
| `local_exec` | Runs on the **MCP server's own machine** (not over SSH) — off-target for a remote-orchestration project, so **off by default**; the operator must set `PORTAL_ALLOW_LOCAL_EXEC=1`. `use_sudo=True` uses local `sudo -S -k` (reserved identity `<local>`, password from `portal sudo set-local` or a top-level `<local>:` `sudo_password_command`), can combine with `secrets`. |
| `remote_close` | Closes a host's sticky `remote_shell` session (next `remote_shell` reopens). Rare; only to reset a dirty session. |

> **★ Two layers of "reuse", don't conflate**: **connection reuse** = the asyncssh
> TCP/channel pool, shared by **all** tools, purely for **speed**; **session reuse**
> = only `remote_shell`'s per-host sticky interactive shell, for **state
> continuity**. The shell session rides on a pooled channel; the two are
> orthogonal. Because the session is implicit plumbing, its state table lives in
> `inspect(view="sessions")`, not a `list` of its own.

### File editing / search / transfer

| Tool | What it gives the agent |
|---|---|
| `remote_read` / `remote_patch` | Read a remote file and get SHA-256; patch uses `file_hash` + per-range hash against concurrent overwrite, writes via tmp + `posix_rename` (atomic), re-hashes after. **On success sweeps orphan `*.mcp_tmp.*` >1h old in the same dir** (piggybacks the open SFTP session, fully isolated) — so there's no separate cleanup tool. |
| `remote_grep` | Faithful port of Claude Code's Grep: `output_mode=files_with_matches` (default, paths mtime-desc) / `content` (matches + optional context, `head_limit` caps **total lines**, `offset` paginates) / `count`. Clear param names (`before_context`/`after_context`/`context`/`ignore_case`), respects `.gitignore`, each result carries `truncated`. **Don't run bare `rg` via `remote_exec`.** |
| `remote_glob` | Faithful port of CC's Glob: `rg --files --no-ignore --sort modified -g`, **mtime-desc**, hard cap 100, `truncated`, returns `{filenames, num_files, truncated, duration_ms}`. Does not respect `.gitignore` (CC Glob default). **Don't run bare `find` via `remote_exec`.** |
| `remote_transfer` | `direction=upload\|download\|sync\|mirror\|upload-list\|download-list`. SFTP binary-safe; `sync` pushes a dir, `mirror` pulls a dir, `*-list` transfers arbitrary local↔remote pairs from `paths_json`, size+mtime incremental short-circuit by default (`checksum=True` for sha256); per-file failure to `failed[]`; MCP progress heartbeat against idle timeout. Directory modes skip local symlinks (no escaping the tree). |

### Resources (agent manages explicitly, so `list` rides with the tool)

| Tool | action / params | Purpose |
|---|---|---|
| `hosts` | `action=list\|register\|remove` | Host registry. `register` needs `name`+`host` — or just `name` (auto-registers a same-named `~/.ssh/config` alias overlay). `tags` feed `remote_exec`'s `group_tag`. `list` also enumerates ssh-config `Host` aliases and resolves real `HostName`/`User`/`Port`, each with a `source` field and possible per-host `warnings` (relay them). **No password parameter.** |
| `remote_tunnel` | `action=open\|close\|list`, `kind=local\|reverse\|socks` | Single-entry SSH tunnels. `open` passes the host gate; binds loopback by default (off-box exposure needs `PORTAL_ALLOW_TUNNEL_EXPOSURE=1`). `close` by `tunnel_id` (gate on the source host). |

### Introspection / policy

| Tool | view / params | Purpose |
|---|---|---|
| `policy_check` | `host`, optional `command` | Security dry-run, no execution. Returns `ALLOWED` / `BLOCKED: <reason>` (and longer strings such as "ALLOWED by policy but host … is not registered"). ⚠️ The default policy is **permissive** — `ALLOWED` only means "no rule currently blocks it". |
| `inspect` | `view=snapshot\|server\|sessions\|history\|stats\|policy` | Read-only introspection **hub**: server metadata + pool + bash sessions + audit stats + policy. **hosts/tunnels are not here** — they're resources, listed by `hosts(action=list)` / `remote_tunnel(action=list)`. The `sessions` view is plumbing diagnostics (host→session_id sticky table). |

### Picking a tool: dedicated vs `remote_exec`/`remote_shell`

`remote_exec` runs anything, but **prefer the dedicated tool** — each has either a
safety guarantee or structured output:

| To do | Use this (**not** a bare command) | Why |
|---|---|---|
| Read / edit a remote file | `remote_read` → `remote_patch` | SHA-256 + per-range hash, atomic rename, post-write rehash |
| Search content / find files | `remote_grep` / `remote_glob` | structured JSON + token guardrails |
| Transfer / sync | `remote_transfer` | SFTP binary-safe + incremental + progress heartbeat |
| Multi-host exec | `remote_exec(host=[...])` / `group_tag=` | parallel / rolling + two-phase gate |
| Open a tunnel | `remote_tunnel` | managed lifecycle, listable |
| Background a long task | `remote_job` | exposes state + hands back control |

### <a id="agent-conventions"></a>Agent-side conventions

`portal-mcp-server` only provides tools; it doesn't mandate usage. Recommended
additions to `AGENTS.md` / the system prompt: default writes to remote `/tmp/`;
ask before touching `$HOME` or project source; don't mix portal tool calls with
raw `ssh`/`scp` in one task; when a task needs a token, guide the user to
`portal secret set` rather than asking for the plaintext.

<details><summary>📋 Full per-tool reference (signatures · returns · source map)</summary>

### Running commands: the exec family

| Tool | Signature | Returns / key behavior |
| --- | --- | --- |
| `remote_exec` | `(host='' \| [host…], command='', commands=None, group_tag='', *, timeout, login=None, use_sudo=False, secrets=None, serialize=False, delay_s=0.0, stop_on_error=True)` | Stateless one-shot over the pool. **single host + single command → one dict** (**separate** stdout/stderr + exit code); multi-host / `commands` sequence → **list** (a multi-command host is `{host, results:[…]}`). `timeout` **required** (no default; over `PORTAL_MAX_TIMEOUT` is refused and routed to `remote_job`); `login` defaults to a login shell (`bash -lc`). |
| `remote_shell` | `(host, command='', commands=None, stop_on_error=True, *, timeout)` | One persistent interactive shell per host. single command → `{host, session_id, command, exit_code, output, duration_s}` (`output` is a merged PTY stream, over-limit truncation flags `truncated`); `commands=[…]` runs in the **same** session → `{host, session_id, results:[…], duration_s}`. A wedged interactive prompt is auto-Ctrl-C'd → `exit_code:-1` + `error:"interactive_prompt_blocked"` + `session_preserved:true`. A timeout Ctrl-C's the command and resyncs (session kept if a clean prompt returns, else dropped). `timeout` **required**. |
| `remote_job` | `(action=submit\|poll\|cancel\|list, host='', command='', job_id='', since=0, tail=0, max_bytes=65536, signal=TERM\|KILL, login=None, use_sudo=False, secrets=None)` | `submit` returns a `job_id` (remote `nohup` + tmp, survives disconnect); `poll` paginates (`since=<offset>` returns new bytes, capped at `max_bytes` default 64 KiB, with `more`; or `tail=N` for the tail — `tail` is a snapshot and is not bounded by `max_bytes`), base64 chunk + boundary-safe UTF-8 decode; `cancel` signals the process group and re-probes (won't signal a terminal job); `list` lists all. Job table best-effort persisted per process, capped, TTL-swept (`PORTAL_JOB_*`). `use_sudo` / `secrets` **not supported in the background**. |
| `local_exec` | `(command, secrets=None, use_sudo=False, *, timeout)` | Runs on the **MCP server's own machine** (**not** SSH), off by default (`PORTAL_ALLOW_LOCAL_EXEC=1`). `timeout` **required** (same `PORTAL_MAX_TIMEOUT` cap, no background to route to). `use_sudo=True` uses reserved identity **`<local>`** (≠ an SSH host `local`/`localhost`) via local `sudo -S -k`; combinable with `secrets`, flagged `high_risk`. |
| `remote_close` | `(host)` | Closes a host's cached `remote_shell` session (auto-reopens next time). Rare; reset a dirty session. |

### File editing (hash-protected)

| Tool | Signature | Returns / key behavior |
| --- | --- | --- |
| `remote_read` | `(host, path, start=1, end=None, limit=None, encoding='utf-8', use_sudo=False)` | → `{content, file_hash, range_hash, start, end, total_lines, truncated}`. Paginated: ≤ `limit` lines (default `PORTAL_READ_MAX_LINES=2000`) + `PORTAL_READ_MAX_BYTES` (default 16384); if truncated early, `truncated=true` and `next_start` gives the resume point (always returns at least one complete line even if it exceeds the byte cap). `use_sudo=True` reads root-only files via `sudo cat` (hash still valid), flagged `high_risk`. |
| `remote_patch` | `(host, path, file_hash, patches_json, encoding='utf-8', auto_newline=False, use_sudo=False)` | Hash-guarded range patch: rejected if the file changed since `remote_read` (returns `current_file_hash`); patches applied bottom-to-top, overlaps rejected, via `*.mcp_tmp.<12hex>` + `posix_rename`, re-hashed after. On success sweeps stale orphan tmp in the same dir. `use_sudo=True` reads/writes root-owned files (staged copy created `0600`, cleaned up even on failure), flagged `high_risk`. `patches_json` = `[{"start":int,"end":int\|null,"contents":str,"range_hash":str}, …]` (`end==start-1` is the pure-insert idiom; a negative `end` is clamped, not tail-sliced). |

### Remote search (faithful Claude Code port)

| Tool | Signature | Returns / key behavior |
| --- | --- | --- |
| `remote_grep` | `(host, pattern, path='.', glob='', file_type='', output_mode=files_with_matches\|content\|count, ignore_case=False, before_context=0, after_context=0, context=0, head_limit=250, offset=0, multiline=False)` | Regex content search (`rg`, fallback `grep`). Full CC-like guarantees (`.gitignore`, mtime-desc, structured) hold under `rg`; the `grep` fallback parses `-A/-B/-C` context rows (tagged `context:true`) but does not sort by mtime or honor `.gitignore`/`file_type`/`multiline`. |
| `remote_glob` | `(host, pattern, path='.')` | Glob file search, `rg --files --no-ignore --sort modified -g`, **mtime-desc**, hard cap 100 + `truncated` → `{filenames, num_files, truncated, duration_ms}`. Does not respect `.gitignore` (matches CC Glob). |

### File transfer (SFTP)

| Tool | Signature | Returns / key behavior |
| --- | --- | --- |
| `remote_transfer` | `(direction=upload\|download\|sync\|mirror\|upload-list\|download-list, host, local_path, remote_path, checksum=False, paths_json='', resume=True)` | Binary-safe SFTP. Single-file (`upload`/`download`) → `{status, direction, host, bytes, duration_s, …}`; incremental (`sync`/`mirror`/`*-list`) skips size+mtime matches (`checksum=True` → sha256) → `{status, uploaded\|downloaded, skipped, failed[], bytes_total, bytes_transferred, duration_s}`, per-file failure to `failed[]`. **Upload resume** (`resume=True`): a smaller remote partial gets only its tail appended, then the whole file sha256-verified; if that can't be verified (no remote `sha256sum`) it re-uploads fresh (`restarted_unverifiable`). Directory modes skip local symlinks and refuse symlink destinations. `*-list` needs `paths_json` = `[{"local":…,"remote":…}, …]`. |

### Resources (agent manages explicitly)

| Tool | Signature | Returns / key behavior |
| --- | --- | --- |
| `remote_tunnel` | `(action=open\|close\|list, kind=local\|reverse\|socks, host='', tunnel_id='', local_port=0, local_bind='127.0.0.1', remote_host='', remote_port=0)` | `open` passes the `host` gate: `local` forwards `localhost:local_port → remote_host:remote_port`, `reverse` exposes `local_bind:local_port` as `host:remote_port`, `socks` is a SOCKS5 proxy. Binds loopback by default; a non-loopback `local_bind`, or exposing a reverse tunnel on all remote interfaces, requires `PORTAL_ALLOW_TUNNEL_EXPOSURE=1`. `close` by `tunnel_id` (gate on the source host); `list` lists all. |
| `hosts` | `(action=list\|register\|remove, name='', host='', user='root', port=22, key_path='', tags='')` | Runtime host registry. `register` needs `name`+`host` — or just `name` (auto-overlays a same-named `~/.ssh/config` alias). `tags` (comma-separated) feed `group_tag`. `list` also enumerates ssh-config aliases (resolving real `HostName`/`User`/`Port`), each with a `source` field + possible per-host `warnings` — relay them. **No password parameter.** |

### Introspection / policy

| Tool | Signature | Returns / key behavior |
| --- | --- | --- |
| `policy_check` | `(host, command='')` | Security dry-run → `"ALLOWED"` / `"BLOCKED: <reason>"` (and longer diagnostics). Default policy is **permissive**. |
| `inspect` | `(view=snapshot\|server\|sessions\|history\|stats\|policy, limit=50, host_filter='')` | Read-only introspection of server **plumbing** + history. **hosts / tunnels are not here** — resources, listed by `hosts` / `remote_tunnel`. |

> **Credential CLI (out-of-band, not an MCP tool)**: the agent never sees
> credential values. Passwords / passphrases / secrets are pre-staged by a human
> in another terminal via `portal {ssh,sudo,passphrase,secret} set`, held by a
> per-user agent; `show` / `list` return only a sha256[:16] fingerprint + TTL,
> `confirm` re-types and compares. See [Authentication](#authentication).

### Source map

| Module | Tools / responsibility |
| --- | --- |
| `cli.py` | all `@mcp.tool()` definitions, `_gate()`/`_gate_exec()`, `inspect` assembly, credential CLI |
| `connection_manager.py` | asyncssh pool + host registry (**SSH tools only**; `local_exec` / control-plane tools don't use SSH) |
| `shell_engine.py` | `remote_exec`'s one-shot `ssh_exec` path (dispatch also spans `cli.py` / `remote_bash.py`) |
| `remote_bash.py` | `remote_shell` / `remote_close` + `remote_exec`'s sudo / secrets one-shot path |
| `session_manager.py` | persistent interactive shell sessions (OSC 133, soft-cancel, timeout interrupt) |
| `job_manager.py` | `remote_job` |
| `local_exec.py` | `local_exec` |
| `remote_text_editor.py` | `remote_read`, `remote_patch` (+ orphan tmp sweep) |
| `remote_search.py` | `remote_grep`, `remote_glob` |
| `file_ops.py` | `remote_transfer` |
| `network_tools.py` | `remote_tunnel` |
| `credential_agent.py` | per-user socket / named-pipe activated TTL cache for `portal {ssh,passphrase,sudo,secret} set` |
| `ssh_creds.py` / `passphrase_creds.py` / `sudo_creds.py` / `secrets_store.py` | credential resolution + output redaction |
| `_peer_creds.py` | same-user peer check (Linux `SO_PEERCRED` / Windows named-pipe SID) |
| `security.py` | policy engine: host allowlist, command blocklist/allowlist, per-host rate limit, cc-safety-net |
| `audit.py` | `audit_log()` write + history ring buffer (`inspect` assembly in `cli.py`) |

</details>

<details><summary>🔀 Migrating from old tool names</summary>

> **From v4: all tools drop the `portal_` prefix** — remote-acting tools take a
> `remote_` prefix (`remote_exec` / `remote_shell` / `remote_read` / `remote_patch`
> / `remote_grep` / `remote_glob` / `remote_transfer` / `remote_tunnel` /
> `remote_job` / `remote_close`), local execution is `local_exec`, control-plane
> tools are `portal_host→hosts` / `portal_check→policy_check` / `portal_audit→inspect`.
> Clients already namespace by config key (`portal-remote_exec`), so a `portal_`
> prefix is redundant stutter. The table also covers the older `portal_bash`-era
> migration:

| Old | New |
|---|---|
| `portal_bash(host, cmd)` | `remote_shell(host, cmd)` (persistent) or `remote_exec(host, cmd)` (one-shot, faster) |
| `portal_bash(..., use_sudo=True / secrets=[…])` | `remote_exec(..., use_sudo=True / secrets=[…])` |
| `portal_bash_close` | `remote_close` |
| `portal_multi_exec(mode=parallel, hosts_json=…)` | `remote_exec(host=[…])` |
| `portal_multi_exec(mode=rolling, …)` | `remote_exec(host=[…], serialize=True, delay_s=N)` |
| `portal_multi_exec(mode=broadcast, commands_json=…)` | `remote_exec(host=[…], commands=[…])` |
| `portal_playbook(host=…/group_tag=…)` | `remote_exec(host=…/group_tag=…, commands=[…])` |
| `portal_ping(hosts_json=…)` | `remote_exec(host=[…], command="echo pong")` |
| `portal_tunnel_open/_close/_list` | `remote_tunnel(action=open\|close\|list, kind=…)` |
| `portal_cleanup_tmps` | removed — `remote_patch` sweeps same-directory orphan tmps on success |
| `portal_bash_status` | `inspect(view="sessions")` |
| — | **new** `remote_job(action=submit\|poll\|cancel\|list)` |

</details>

## <a id="env-vars"></a>Environment variables

All configuration is via environment variables, uniformly prefixed `PORTAL_*`.
Set them in the MCP client's `env` field — they affect only the server
subprocess.

### Overview

| Category | Variable | One-liner |
|---|---|---|
| File paths | `PORTAL_HOSTS_YAML` | host registry YAML |
| File paths | `PORTAL_POLICIES_YAML` | security policy YAML |
| File paths | `PORTAL_SECRETS_YAML` | named-secret YAML (source for `secrets=` in `remote_exec` / `local_exec`) |
| File paths | `PORTAL_SSH_CONFIG` | OpenSSH client config path (the `ssh -F` equivalent) |
| File paths | `PORTAL_LOG_DIR` | audit + server log dir |
| File paths | `PORTAL_CREDENTIAL_AGENT_SOCKET` | credential-agent socket / named-pipe address override (defaults to the installed `agent.json`) |
| Security & auth | `PORTAL_AUDIT_FAIL_OPEN` | whether a failed audit write is fail-open |
| Security & auth | `PORTAL_AUDIT_MAX_BYTES` | `audit.jsonl` rotation threshold (bytes, default 10 MiB) |
| Security & auth | `PORTAL_AUDIT_BACKUPS` | rotated files kept `audit.jsonl.1..N` (default 5) |
| Security & auth | `PORTAL_AUTH_TOKEN` | HTTP transport (`--transport streamable_http`) auth token; **required** for a non-loopback bind, not needed for stdio / loopback |
| Security & auth | `PORTAL_ALLOW_TUNNEL_EXPOSURE` | allow `remote_tunnel` to bind non-loopback / expose a reverse tunnel on all remote interfaces (default off, loopback only) |
| Local exec | `PORTAL_ALLOW_LOCAL_EXEC` | whether `local_exec` is enabled (default off; set `1`) |
| Connection pool | `PORTAL_SSH_POOL_SIZE` | max TCP connections per host |
| Connection pool | `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | max concurrent channels per TCP |
| Connection pool | `PORTAL_SSH_MAX_IDLE_TIME` | idle-close timeout (s) |
| Connection pool | `PORTAL_SSH_MAX_CONN_AGE` | max connection lifetime (s) |
| Background jobs | `PORTAL_JOB_PERSIST` | persist the `remote_job` table across restarts (default on; `0`/`false` off) |
| Background jobs | `PORTAL_JOB_STATE_FILE` | job-table path (default **per-process** `<state>/jobs/<pid>.json`, so multiple server processes don't clobber each other; set = one fixed file) |
| Background jobs | `PORTAL_JOB_MAX_LIVE` | concurrent live-job cap (default 50) |
| Background jobs | `PORTAL_JOB_TTL` | seconds a finished job stays before sweep + remote-tmp removal (default 3600) |
| Reliability | `PORTAL_BASH_HEARTBEAT_INTERVAL` | keepalive heartbeat interval during foreground execution (s) |
| Reliability | `PORTAL_MAX_TIMEOUT` | **cap** on the per-command foreground `timeout` of `remote_exec` / `remote_shell` / `local_exec` (s, default 300); `timeout` is required, over the cap is refused and routed to `remote_job` |
| Exec env | `PORTAL_LOGIN_SHELL` | whether `remote_exec` / `remote_job` default to a login shell (`bash -lc`, loading `~/.profile`/`.bash_profile` PATH/env). Default on; `0`/`false`/`no`/`off` off. Per-call `login` / hosts.yaml `login_shell:` override |
| Remote read | `PORTAL_READ_MAX_LINES` | max lines per `remote_read` page when `limit` is omitted (default 2000) |
| Remote read | `PORTAL_READ_MAX_BYTES` | max bytes per page (default 16384) |
| Shell session | `PORTAL_SHELL_MAX_OUTPUT` | `remote_shell` per-command in-memory output cap (bytes, default 8 MiB, over-limit truncation flags `truncated`) |
| Shell session | `PORTAL_SHELL_BOOT_TIMEOUT` / `PORTAL_SHELL_BOOT_QUIET` | persistent-session bootstrap timeout / quiet window (s, default 10 / 0.6) |
| Shell session | `PORTAL_SHELL_INTERACTIVE_GRACE` / `PORTAL_SHELL_SOFT_CANCEL_TIMEOUT` | interactive-prompt grace / soft-cancel wait for the OSC133 D (s, default 1 / 3) |
| Testing (dev only) | `PORTAL_TEST_LIVE` | run the real-SSH integration tests |
| Testing (dev only) | `PORTAL_TEST_HOST` / `PORTAL_TEST_PORT` / `PORTAL_TEST_USER` / `PORTAL_TEST_KEY_PATH` | live-test target |

### <a id="file-paths"></a>File paths

| Variable | Meaning | Default |
|---|---|---|
| `PORTAL_HOSTS_YAML` | host registry YAML | `~/.config/portal-mcp-server/hosts.yaml` |
| `PORTAL_POLICIES_YAML` | security policy YAML | `~/.config/portal-mcp-server/policies.yaml` |
| `PORTAL_SECRETS_YAML` | named-secret YAML | `~/.config/portal-mcp-server/secrets.yaml` |
| `PORTAL_SSH_CONFIG` | OpenSSH client config path | `~/.ssh/config` |
| `PORTAL_LOG_DIR` | audit + server log dir | platform state dir (Linux `~/.local/state/portal-mcp-server/log/`; macOS/Windows use the native state dir) |

> `PORTAL_SSH_CONFIG` is portal's `ssh -F`: OpenSSH reads **no** env var for the
> config path, only `-F`; portal is a long-lived daemon with no per-connection
> flag, so it uses this variable and **mirrors `-F` exactly**: an **absolute path**
> reads only that one file (also suppressing system `/etc/ssh/ssh_config`); the
> literal **`none`** (any case) reads no config file at all (`ssh -F none`); unset
> reads user `~/.ssh/config` + system `/etc/ssh/ssh_config` (Windows
> `%PROGRAMDATA%\ssh\ssh_config`) as fallback, user-level first. Parsing reuses
> asyncssh's config parser (`Include`, `~` expansion).

Path resolution priority: **env var > XDG dir** (`$XDG_CONFIG_HOME` /
`$XDG_STATE_HOME`). The cwd is **not** consulted — portal is a user-level daemon,
not a project tool, so cwd-relative autoloading would let any working directory
silently hijack your real config.

The repo's [`examples/`](./examples/) dir is the schema template — all `*.yaml`
there are **read-only samples**, never autoloaded. On first use, copy them to the
XDG dir and edit in your real values. **`~/.config/portal-mcp-server/hosts.yaml`
holds real credentials — never commit it.**

### Security & auth

| Variable | Meaning | Default |
|---|---|---|
| `PORTAL_AUDIT_FAIL_OPEN` | `1` → a failed audit write only warns and continues; default → **fail-closed**, the op raises and aborts | _(unset)_ |
| `PORTAL_ALLOW_LOCAL_EXEC` | set `1` to enable `local_exec` (off-target local execution, default off) | _(unset)_ |
| `PORTAL_ALLOW_TUNNEL_EXPOSURE` | set `1` to let `remote_tunnel` bind non-loopback (`local_bind`) or expose a reverse tunnel on all remote interfaces; default loopback only | _(unset)_ |
| `PORTAL_AUTH_TOKEN` | HTTP transport auth token (client sends `Authorization: Bearer <token>`). Transport **defaults to `--host 127.0.0.1`**; binding a non-loopback address without this value **refuses to start**. Not needed for stdio | _(none)_ |

### Connection pool

| Variable | Meaning | Default |
|---|---|---|
| `PORTAL_SSH_POOL_SIZE` | max TCP connections per host; when the pool is full and all are at the channel limit, the least-busy is reused (with a warning) | `5` |
| `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | max concurrent channels per TCP (SFTP/exec/tunnel share); over that opens a new TCP up to `PORTAL_SSH_POOL_SIZE` | `5` |
| `PORTAL_SSH_MAX_IDLE_TIME` | close a channel-less connection after this idle time (s). **Note `0` is not "disable"** — it makes any idle connection immediately reclaimable | `600` (10 min) |
| `PORTAL_SSH_MAX_CONN_AGE` | max connection lifetime (s); closed when aged and channel-less. Guards against firewall/NAT silent drops | `3600` (1 h) |

### Reliability & execution

| Variable | Meaning | Default |
|---|---|---|
| `PORTAL_BASH_HEARTBEAT_INTERVAL` | how often (s) a MCP progress notification is sent as keepalive during execution; independent of the server-side `timeout` | `5` |
| `PORTAL_MAX_TIMEOUT` | **cap (s)** on the per-command foreground `timeout`. `timeout` is **required** (no default); over the cap is **refused** with a hint to use `remote_job`. A guardrail, not a default | built-in `300` |
| `PORTAL_LOGIN_SHELL` | whether `remote_exec`'s normal path and `remote_job` default to a **login shell** (`bash -lc`), loading the user's profile PATH/env. Default **on**; only `0`/`false`/`no`/`off` disables. Priority: per-call `login` > hosts.yaml `login_shell:` > this var. sh-only hosts auto-fallback; `remote_shell` is unaffected (persistent session uses `--norc`) | `on` |

### Shell session

Timing knobs for `remote_shell` persistent sessions; normal deployments needn't touch them.

| Variable | Meaning | Default |
|---|---|---|
| `PORTAL_SHELL_MAX_OUTPUT` | per-command in-memory output cap (bytes); over-limit drops the head and flags `truncated` | `8388608` (8 MiB) |
| `PORTAL_SHELL_BOOT_TIMEOUT` | timeout to bring up a persistent session (inject the integration script + readiness marker) (s) | `10.0` |
| `PORTAL_SHELL_BOOT_QUIET` | quiet confirmation window before bootstrap completes (s) | `0.6` |
| `PORTAL_SHELL_INTERACTIVE_GRACE` | grace after spotting an interactive prompt before deciding it's wedged and soft-cancelling (s) | `1.0` |
| `PORTAL_SHELL_SOFT_CANCEL_TIMEOUT` | timeout after soft-cancel (incl. foreground-timeout interrupt) waiting for the OSC133 `D` back to a clean prompt (s); on timeout the session is destroyed | `3.0` |

### Testing (dev only)

Used only when running `tests/`.

| Variable | Meaning | Default |
|---|---|---|
| `PORTAL_TEST_LIVE` | set `1`/`true`/`yes` to run the real-SSH tests in `tests/test_live_ssh.py`; else all skip | _(unset)_ |
| `PORTAL_TEST_HOST` / `PORTAL_TEST_PORT` / `PORTAL_TEST_USER` / `PORTAL_TEST_KEY_PATH` | live-test target | `127.0.0.1` / `22` / `$USER` or `root` / `~/.ssh/id_ed25519` |

## <a id="authentication"></a>Authentication

Jump by method — prefer SSH keys; passphrases prefer ssh-agent; password login
supports `password_command` or `portal ssh set`; plaintext passwords never reach
the LLM.

### Credential-flow overview

Five credential flows, each with a "password-manager (command source)" and a
"no-echo interactive (getpass + credential agent) source":

| Flow | Command source | No-echo interactive | Cache key | Cache semantics | Trigger |
|---|---|---|---|---|---|
| **A. Remote SSH login password** | `password_command` (hosts.yaml) | ✅ `portal ssh set <host>` | host | agent memory TTL (default 900 s, interactive only; command source fetches fresh) | `auth: password` connect / auto-fallback on key failure |
| **B. SSH key passphrase** | `passphrase_command` (hosts.yaml) | ✅ `portal passphrase set <host>` | host | agent memory TTL (900 s) | local decrypt of an encrypted key |
| **C. Remote sudo** | `sudo_password_command` (hosts.yaml) | ✅ `portal sudo set <host>` | host | agent memory TTL (900 s) | `remote_exec(use_sudo=True)` |
| **C2. Local sudo** | top-level `<local>:` `sudo_password_command` | ✅ `portal sudo set-local` | `<local>` | agent memory TTL (900 s) | `local_exec(use_sudo=True)` |
| **D/E. Secret injection (remote/local)** | `secrets.yaml` `command` | ✅ `portal secret set <name>` | name | agent memory TTL (900 s, `--ttl`) | `remote_exec` / `local_exec` `secrets=[…]` |

- **A/B/C/D share one per-user agent socket**, but the agent keeps separate
  `ssh`/`passphrase`/`sudo`/`secret` key spaces.
- **A's fallback order**: `auth: password` login is `cache (portal ssh set) →
  password_command → error`; a pure-key host auto-retries the password path once
  on `PermissionDenied`, but only if a source exists, else the original error
  propagates (so a missing config can't mask a real key failure).
- **Interactive sources = per-user agent memory TTL** (default 900 s, never on
  disk). **Command sources = fetched fresh, no TTL.**
- **Plaintext never leaves the agent**: there is no `show plaintext`; `portal
  {ssh,passphrase,sudo,secret} show <key>` returns only a sha256[:16] fingerprint
  + remaining TTL, `list` summarizes, `confirm` re-types and compares. See the
  [Security](#security) section and [`SECURITY.en.md`](./SECURITY.en.md).

<details><summary>The four credential mechanisms — implementation & why</summary>

| Type | Implementation | Why |
|---|---|---|
| **SSH login password** | asyncssh `password=` (SSH protocol level), source: `password_command` / `portal ssh set` cache | SSH natively supports password auth; the protocol frame is cleanest |
| **SSH key passphrase** | asyncssh `passphrase=`, source: ssh-agent → `portal passphrase set` cache → `passphrase_command`; or `use_ssh_agent` | local key decrypt; cached separately so a key-unlock passphrase isn't confused with a login/sudo password |
| **sudo password** | `sudo -S` fed on stdin (`conn.run(input=pw)`), source: `sudo_password_command` / `portal sudo set` cache | sudo only reads `-S`/`-A`/tty, not env; `-S` has the narrowest exposure (password lives briefly on stdin, nothing on disk, not in env) |
| **secrets** (API tokens) | `bash -s` + stdin `export VAR=…\n<cmd>\n`, source: `secrets.yaml` `command` / `portal secret set` cache | tools read env (`GH_TOKEN`/`AWS_*`); the value stays briefly in the bash stdin script, not on argv (`ps`) or in logs |

</details>

<details><summary>⚠️ Risk of configuring these passwords (read this)</summary>

Key-only login is the safest baseline. **Once you configure an SSH login
password / sudo password / secret for a host, you authorize "any agent that can
call this MCP server" to act with those credentials for their lifetime** — the
agent won't ask again. A **permanent** command source (`sudo_password_command`
etc.) is fetched fresh with no TTL and is usable as long as your password store
is unlocked; a **temporary** `portal … set` value lives in the per-user agent's
memory with a TTL and is dropped automatically, never on disk.

</details>

### SSH key (preferred)

Use ed25519 and distribute with `ssh-copy-id`; asyncssh discovers ssh-agent via
`$SSH_AUTH_SOCK`. For headless/CI, write `passphrase_command:` in `hosts.yaml`.

### Password login: `password_command` or `portal ssh set`

Two rules: **never** put a plaintext `password:` in `hosts.yaml` (rejected at
startup, field dropped); **never** pass it through an MCP tool (`hosts` has no
password parameter). Two sources (order: agent cache → `password_command` → error):

```yaml
hosts:
  legacy-host:
    host: 10.0.0.40
    user: admin
    auth: password
    password_command: pass show ssh/legacy-host   # or: bw get password … / op read op://… / printf '%s' "$ENV"
```

Or push interactively in **another terminal**: `portal ssh set legacy-host`
(no-echo getpass, TTL cache), `portal ssh confirm/show/list/clear`. Key-mode hosts
auto-fallback to the password path once on `PermissionDenied` when a source
exists. Design details (why `shell=True`, forced `client_keys=[]`, stderr never
logged) are in [`SECURITY.en.md` § Authentication](./SECURITY.en.md).

### Encrypted-key passphrase: `portal passphrase set` / `passphrase_command` / `use_ssh_agent`

A passphrase is a **local key-unlock secret**, separate from a remote login /
sudo password (separate agent kind). Full order: **ssh-agent → agent cache
(`portal passphrase set`) → `passphrase_command` → asyncssh default**. Prefer
ssh-agent when available; `passphrase_command` is for headless/CI.

```yaml
hosts:
  encrypted-key-host:
    host: 10.0.0.30
    user: deploy
    key: ~/.ssh/encrypted_key
    passphrase_command: pass show ssh/encrypted_key
    use_ssh_agent: true   # true=agent only; false=disable agent; omit=auto
```

### Non-interactive sudo: `use_sudo` + `portal sudo set`

`remote_exec(host, cmd, use_sudo=True)` runs a root command, but **the sudo
password never enters the LLM** (no password parameter; resolved server-side).
Sources: `sudo_password_command` in `hosts.yaml`, or `portal sudo set <host>`
(no-echo, TTL cache). `sudo_password_same_as_ssh: true` makes `portal ssh set`
also cache the same value for `sudo` (config-only, default false; does not reuse
the private-key passphrase). Order: agent cache → `sudo_password_command` → error.

Implementation: `use_sudo` runs a one-shot `conn.run(input=pw, …)` of
`sudo -S -k -p '' -- bash -c <cmd>` — **not** the persistent `remote_shell`
session (a PTY can't feed the `-S` password). So a sudo command **doesn't inherit**
prior `remote_shell` cwd/env; include `cd … && …` in the command if needed.

#### Local sudo: `local_exec(use_sudo=True)`

The **local** counterpart on the MCP server's own machine, via local
`sudo -S -k`, password also never in the LLM / argv / disk. Reserved identity
**`<local>`** (≠ an SSH host `local`/`localhost`). Source: `portal sudo set-local`
or a top-level `<local>:` section's `sudo_password_command` in hosts.yaml.
Combinable with `secrets`; flagged `high_risk`, audited as `local_exec_sudo`.

```yaml
hosts:
  # ... your remote hosts ...
"<local>":                                # top-level reserved key, for local_exec only
  sudo_password_command: pass show sudo/this-box
```

### Named secret injection: `secrets=[…]` + `portal secret set`

For giving a command an API token without it entering session history or the
third-party LLM: the agent passes only the **name**, the server resolves the
value and injects it as an **environment variable** into a one-shot command; any
echo of the value in output is redacted to `***` before returning.

- Remote: `remote_exec(host, cmd, secrets=["github_token"])`, write `$GITHUB_TOKEN`.
- Local: `local_exec(cmd, secrets=["github_token"])` on the MCP server's own
  machine (off-target derivative, **off by default**, needs `PORTAL_ALLOW_LOCAL_EXEC=1`).

Two sources (order: agent cache → `secrets.yaml`):

```yaml
secrets:
  github_token:
    command: pass show api/github      # or op read / printf "$ENV"
```

or `portal secret set github_token` (no-echo, TTL). Full config in
[`examples/secrets.yaml`](./examples/secrets.yaml). `secrets` can combine with
`use_sudo`.

<details><summary>Implementation: sudo + secrets coexistence, and the wait semantics</summary>

`use_sudo` and `secrets` share **one stdin**: the sudo password first, then each
secret value base64-encoded (one line each). The real command is prefixed with a
small preamble that, after sudo's `env_reset`, reads each base64 line and decodes
it inside the elevated shell — so a multi-line secret (PEM key, JSON blob)
survives intact, the value never lands on argv (`ps`), and no sudoers `env_keep`
is needed. Both `remote_exec` and `local_exec` implement this identically
(`secrets_store.sudo_stdin_secret_script/_values`).

**Wait semantics — fail-fast → ask_user → retry**: no-echo input inherently waits
for a human, but that wait never blocks the agent's critical path. If a secret /
sudo password isn't ready, the tool **returns an error immediately** (value-free)
suggesting the agent use an interactive/choice tool (e.g. `ask_user`) to have the
user run `portal secret set <name>` / `portal sudo set <host>` in another terminal
and confirm, then retry. **Never ask the user to paste the value into the
conversation.** This guidance reaches the agent via each tool's own description
(MCP's server-level `instructions` field is optional and not injected by Copilot
CLI / Codex / Claude Code, so portal doesn't rely on it).

</details>

### Host lookup: hosts.yaml + OpenSSH ssh config

**Default order** (first hit wins): (1) `hosts.yaml` (from the XDG config dir);
(2) OpenSSH ssh config (user `~/.ssh/config` + system fallback, mirroring `ssh -F`,
parsed by asyncssh). `PORTAL_SSH_CONFIG=none` disables step 2. `hosts(action="list")`
lists both with a `source` field.

**Priority**: by default a same-named `hosts.yaml` host **fully overrides** ssh
config. **Per-host `use_ssh_config: true`** switches to **merge**: the ssh-config
alias is the base (HostName / User / Port / IdentityFile / IdentityAgent /
ProxyJump …), with explicitly-set `hosts.yaml` fields overlaid. Footguns (all
surfaced as warnings via `hosts(action=list)`): a same-named host on both sides
without `use_ssh_config` (hosts.yaml silently wins); `use_ssh_config: true` with
no matching alias; `use_ssh_config: true` with a `host:` that disagrees with the
alias's HostName (**hard error on connect**). Base fields
(`host`/`port`/`user`/`key`/`known_hosts`/`strict_host_key_checking`/`auth`) plus
`proxy_jump` / `keepalive_interval` / `forward_agent` are natively supported; other
ssh-config fields need the merge.

## <a id="security"></a>Security

- **Default sandbox**: writes default to remote `/tmp/`; the agent must ask
  before touching `$HOME` or project source (enforced at the prompt layer — see
  [Agent-side conventions](#agent-conventions)).
- **Policy gate**: host allowlist + command blocklist/allowlist + per-host rate
  limit; every state-changing tool passes `_gate` with no side doors
  (`hosts(register)` gates the target IP not the alias; `remote_tunnel(close)`
  gates too; multi-host is two-phase). The optional
  [cc-safety-net](https://github.com/kenryu42/cc-safety-net) semantic gate
  (`policies.safety_net.enabled`) stacks in the same place — bypass-resistant
  analysis that catches destructive git/rm/interpreter one-liners, the same rules
  the Copilot-CLI PreToolUse hook uses (which never sees portal MCP commands).
  Fail-closed by default.
- **Authentication**: SSH key by default and recommended; password login via
  `password_command` or `portal ssh set`, never exposed to MCP tools — see
  [Authentication](#authentication) and [`SECURITY.en.md` § Authentication](./SECURITY.en.md).
- **HTTP transport (optional)**: binds `127.0.0.1` by default; a non-loopback
  bind without `PORTAL_AUTH_TOKEN` **refuses to start**, and portal serves
  plaintext HTTP so terminate TLS in front.
- **Tunnel / transfer boundaries**: `remote_tunnel` binds loopback by default
  (off-box / reverse exposure needs `PORTAL_ALLOW_TUNNEL_EXPOSURE=1`);
  `remote_transfer` directory modes don't follow local symlinks (no escaping the
  tree), but still have the server user's local filesystem reach (like `scp`).
- **Audit**: state changes write `$PORTAL_LOG_DIR/audit.jsonl` (dir `0700` /
  file `0600`). The audit write happens **after** the operation, so fail-closed is
  **response-level** — a failed write makes the tool error to the agent, but the
  remote change already happened (see [`SECURITY.en.md`](./SECURITY.en.md));
  `PORTAL_AUDIT_FAIL_OPEN=1` switches to fail-open.
- **Hash-protected edits**: `remote_read` + `remote_patch` use SHA-256 + per-range
  hash + atomic `posix_rename` + post-write rehash to **detect** concurrent
  overwrite / mid-write disconnect / line drift (optimistic, not a filesystem CAS;
  plain writes don't preserve mode/owner).
- **Remote bash-history risk (unusual remote config)**: `remote_exec(secrets=…)`
  injection relies on remote bash **disabling history in non-interactive mode**
  (bash upstream design, same premise as `ssh`/`ansible`/CI shell steps). If a
  remote admin **forces** `BASH_ENV` + `set -o history`, any SSH-based secret
  injection tool (this one, `ssh`, `ansible`, CI runners) could leak the value
  into `~/.bash_history`. This is a Unix/SSH-ecosystem premise, not a
  project-specific weakness. Verify with `bash -s <<< 'echo test'`.

Full threat model, per-layer detail, operator hygiene, known limitations and
algorithm provenance are in **[`SECURITY.en.md`](./SECURITY.en.md)**.

Vulnerability disclosure: **don't** open a public issue — use
[GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new).
Response window: 48 h ack / 7 day assessment / 30 day critical fix.

## <a id="testing"></a>Testing

### Unit + security (no real SSH)

```bash
pytest tests/ -v
# live SSH tests skip by default (gated by PORTAL_TEST_LIVE)
```

Covers: command-injection regression, safety validators, hash-protected editor,
concurrency, resource lifecycle, multi-host policy enforcement,
`password_command`/`passphrase_command` security invariants, audit fail mode.

### End-to-end live smoke

`tests/live_smoke.py` drives real SSH behavior directly from the local tree.

```bash
PORTAL_AUDIT_FAIL_OPEN=1 \
  PORTAL_TEST_HOST=<your-host> PORTAL_TEST_PORT=22 PORTAL_TEST_USER=<user> \
  PORTAL_TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ It writes once under remote `/tmp/portal-mcp-server-smoke-<pid>.txt` then
removes it — `/tmp` only.

## <a id="ci-release"></a>CI / Release

- **CI** ([`ci.yml`](.github/workflows/ci.yml)): every PR / push to `main` runs
  `ruff check portal_mcp_server/ tests/` + `pytest tests/` on Python
  **3.10 / 3.11 / 3.12 / 3.13** (ubuntu), plus a macOS full-suite job and a
  Windows named-pipe / scheduled-task job; all green to merge.
- **Release** ([`release.yml`](.github/workflows/release.yml)): pushing a `v*` tag
  (incl. PEP 440 pre/dev/post, e.g. `v4.0.0a0`) triggers `python -m build` (wheel +
  sdist) → GitHub Release body from the matching `CHANGELOG.md` section → publish
  to [PyPI](https://pypi.org/project/portal-mcp-server/) via
  [trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC, no static
  token).

Full release flow, CHANGELOG format constraints and failure triage are in
[`CONTRIBUTING.en.md` § CI & Release automation](./CONTRIBUTING.en.md).

## <a id="faq"></a>FAQ

### Local changes don't show up in the agent

`uvx portal-mcp-server` launches from the PyPI cache. If you edited local code,
the agent uses the published version. For local debugging, temporarily set
`.mcp.json` `args` to `["--from", "/absolute/path/to/portal-mcp-server", "portal-mcp-server"]`
(absolute path). **Don't commit that local path into a project `.mcp.json`.**

### Connection timeout / Permission denied (publickey)

1. Confirm `ssh user@host` connects directly in a terminal.
2. Check key perms: `chmod 600 ~/.ssh/id_ed25519`.
3. If using `~/.ssh/config`, confirm the `Host` alias / `HostName` / `User` /
   `IdentityFile`.
4. For ProxyJump, asyncssh honors `~/.ssh/config`'s `ProxyJump`; confirm the
   bastion connects manually too.

### Connection drops after the MCP client restarts

Expected — the pool follows the MCP server process lifecycle. A client restart
closes the server; the next tool call rebuilds connections automatically.

### Update to the latest version

```bash
uvx portal-mcp-server@latest --help    # refresh the uvx cache
uv tool upgrade portal-mcp-server      # if installed as a persistent uv tool
```

Then restart the MCP client.

## <a id="contributing"></a>Contributing

Issues and PRs welcome. Short version:

- Python 3.10+, all I/O `async/await`, no blocking calls.
- No hard-coded hostname / username / IP / path.
- New tools need a good docstring (FastMCP uses it as the MCP description) + a
  README "Tools" update (incl. the folded full signature + source-map table).
- State-changing tools must pass `_gate` + write `audit_log`.
- Tests cover key paths; `pytest tests/ -v` must be all green.
- Don't commit secrets; `examples/hosts.yaml` is the one schema template.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).

Full dev flow, new-tool checklist, PR template, and security / privacy rules are
in **[`CONTRIBUTING.en.md`](./CONTRIBUTING.en.md)** ([简体中文](./CONTRIBUTING.md)).

## <a id="license-credits"></a>License & credits

Apache License 2.0 (see [`LICENSE`](LICENSE)).

Derivation and third-party algorithm provenance are in [`NOTICE`](NOTICE):

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)
  (Apache 2.0)** — git ancestry; the underlying modules (asyncssh engine, pool,
  tunnel management, orchestrator, security policy) are carried over; the 14
  portal tools on top are a new design.
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) (MIT)** —
  the SHA-256 hash-protected edit algorithm behind `remote_text_editor.py`,
  rewritten for AsyncSSH SFTP.

> ⚠️ This tool gives an agent SSH access to remote systems. Use it only on
> systems you own or are authorized to access.
