<div align="center">

# portal-mcp-server

**Remote SSH tools for coding agents, over MCP**

Commands, persistent shells, file editing and transfer, tunnels, multiple hosts, and background jobs.

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
<summary>Table of contents</summary>

- [Overview](#overview)
- [Highlights](#highlights)
- [Install & quick start](#install)
- [Client integration](#client-integration)
- [Host configuration](#hosts)
- [Authentication & credentials](#authentication)
- [Tools](#tools)
- [Environment variables](#env-vars)
- [Security](#security)
- [FAQ](#faq)
- [Architecture & design](#architecture-design)
- [Development & testing](#testing)
- [CI / Release](#ci-release)
- [Contributing](#contributing)
- [License & credits](#license-credits)

</details>

## <a id="overview"></a>Overview

Portal gives MCP-compatible coding agents SSH tools for running commands, editing files, transferring data, opening tunnels, and coordinating work across hosts. Built on [AsyncSSH](https://github.com/ronf/asyncssh) and [FastMCP](https://modelcontextprotocol.io/), the server runs on Linux, macOS, and Windows.

There are two everyday entry points: **the MCP server provides tools for the agent; the `portal` CLI lets you supply credentials from a terminal.** Existing `~/.ssh/config` aliases can be used directly. Add `hosts.yaml` when you need groups, password sources, or other host settings.

## <a id="highlights"></a>Highlights

- **Shared connections**: commands, SFTP, and tunnels use the server's connection pool to avoid repeated SSH handshakes, without relying on OpenSSH `ControlMaster`.
- **State when you need it**: `remote_shell` preserves the working directory and environment across calls; `remote_exec` runs one-shot commands; `remote_job` manages remote background work.
- **Conflict-aware editing**: read a file and its SHA-256, validate both file and range hashes, then write through a temporary SFTP file and atomic replacement.
- **Structured operations**: file search, incremental transfer, concurrent or rolling multi-host execution, and tunnels have dedicated tools and result formats.
- **Credentials outside the conversation**: reuse ssh-agent or enter SSH passwords, key passphrases, sudo passwords, and tokens through a no-echo CLI. Tool calls refer to host or secret names.
- **Policy and audit**: host and command policies, rate limits, audit logs, and optional [cc-safety-net](https://github.com/kenryu42/cc-safety-net) checks.

The guide follows the setup flow: install, connect your client, configure hosts and credentials, then choose tools. Full signatures, tuning parameters, and design notes remain in expandable sections.

## <a id="install"></a>Install & quick start

You need Python 3.10+ and an MCP-compatible client. Install with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
uv tool install portal-mcp-server
portal-mcp-server --help
```

This installs two equivalent commands: `portal-mcp-server` and `portal`. With no subcommand, either starts the MCP server. The `ssh`, `passphrase`, `sudo`, `secret`, and `agent` subcommands are CLI operations; `portal` is convenient for typing them by hand.

### 1. Prepare a host

Reuse an existing SSH alias, or add one to `~/.ssh/config`:

```sshconfig
Host web01
    HostName server.example.com
    User deploy
    IdentityFile ~/.ssh/id_ed25519
```

Check the address, account, and authentication method in a terminal:

```bash
ssh web01
```

For an encrypted key, unlock it using the [ssh-agent setup](#ssh-agent). For SSH password login, enter the password at a hidden prompt with `portal ssh set web01`; see [Password login](#password-login).

### 2. Connect your MCP client

For example, with Claude Code:

```bash
claude mcp add --scope user portal -- portal-mcp-server
```

See [Client integration](#client-integration) for other clients and their JSON or TOML formats. If your client does not pass through the terminal's agent environment, explicitly set `SSH_AUTH_SOCK` in the MCP server's `env` or specify `IdentityAgent` in SSH config.

### 3. Use a tool

Ask for a concrete task, for example:

> Show the last 50 lines of /var/log/syslog on web01.

The agent can call `remote_exec("web01", "tail -50 /var/log/syslog", timeout=30)`. It can also start with `hosts(action="list")` to check host sources and configuration warnings.

<details>
<summary>Zero-install trial, upgrades, and command-name conflicts</summary>

Try the server without installing a command on PATH:

```bash
uvx portal-mcp-server@latest --help
```

Use `"command": "uvx"` and `"args": ["portal-mcp-server@latest"]` in your MCP config. This does not install the `portal` short command on PATH; `uv tool install` is more convenient for regular credential operations.

Update an installed version:

```bash
uv tool upgrade portal-mcp-server
```

Restart the MCP server after upgrading so the running process uses the new version.

The [SpatiumPortae/portal](https://github.com/SpatiumPortae/portal) file-transfer CLI also uses the name `portal`. If both are installed, check PATH order with `which -a portal` (`where portal` on Windows), or use the full command `portal-mcp-server`.

</details>

## <a id="client-integration"></a>Client integration

Clients have different config formats and environment inheritance rules. Expand your client's section below; see [SSH keys and ssh-agent](#ssh-agent) for endpoint precedence.

### Generic config snippet

**Recommended** (after `uv tool install portal-mcp-server`, `command` is the bare
binary name):

```json
{
  "mcpServers": {
    "portal": {
      "command": "portal-mcp-server",
      "args": []
    }
  }
}
```

Zero-install (no install, uvx pulls on launch):

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

> If the host can't find `portal-mcp-server` (or `uvx`) — common with GUI apps
> (Claude Desktop / VS Code) that don't inherit your shell PATH — put the absolute
> path from `which portal-mcp-server` (Windows: `where portal-mcp-server`) in
> `command`. The `uv tool` path (`~/.local/bin/portal-mcp-server`) is stable across
> `uv tool upgrade`, so hardcoding it is safe.

To pass an agent socket or custom config paths, add `env` to the server entry (replace the example UID and paths, and keep only the fields you need):

```json
"env": {
  "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.socket",
  "PORTAL_HOSTS_YAML": "/path/to/hosts.yaml",
  "PORTAL_POLICIES_YAML": "/path/to/policies.yaml",
  "PORTAL_LOG_DIR": "/path/to/logs"
}
```

<details>
<summary>Claude Code CLI</summary>

### Claude Code CLI

```bash
# Recommended: user scope, all repos (uv tool installed → command is portal-mcp-server)
claude mcp add --scope user portal -- portal-mcp-server
# Without --scope it defaults to local (current dir only)
claude mcp add portal -- portal-mcp-server
# Zero-install: replace portal-mcp-server with  uvx portal-mcp-server@latest
# or use /mcp inside a Claude Code session to inspect and manage connections
```

> ⚠️ Claude Code has three scopes: `local` (**default**, current dir), `user`
> (all repos), `project` (written into the repo's `.mcp.json`). For "install once,
> use everywhere" **use `--scope user`** — unlike Codex (`mcp add` = global) or
> Copilot CLI (`mcp add` = User scope).

</details>

<details><summary><b>GitHub Copilot CLI</b></summary>

```bash
copilot mcp add portal -- portal-mcp-server
# Zero-install: replace portal-mcp-server with  uvx portal-mcp-server@latest
# or /mcp inside a Copilot CLI session
```

Verify: `copilot mcp list` (should show portal) / `copilot mcp get portal`.

</details>

<details><summary><b>Cursor</b></summary>

Write the generic snippet into `~/.cursor/mcp.json` (global) or
`<project>/.cursor/mcp.json` (per-project). Enable under Settings → Tools & MCP.

</details>

<details>
<summary><b>VS Code (Copilot Chat / Agent mode)</b></summary>

For a workspace, use the generic `.mcp.json` / `mcpServers` format at the project root. The native `.vscode/mcp.json` format is also supported:

```json
{
  "servers": {
    "portal": {
      "type": "stdio",
      "command": "portal-mcp-server",
      "args": []
    }
  }
}
```

For configuration across workspaces, run **MCP: Open User Configuration**. **MCP: Add Server** offers guided setup. The two file formats have different top-level fields; use the format appropriate to the destination. See the [VS Code documentation](https://code.visualstudio.com/docs/agent-customization/mcp-servers#configure-the-mcpjson-file) for current supported locations.

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
codex mcp add portal -- portal-mcp-server   # global
# Zero-install: replace portal-mcp-server with  uvx portal-mcp-server@latest
```

Or edit `~/.codex/config.toml`:

```toml
[mcp_servers.portal]
command = "portal-mcp-server"
args = []
# Zero-install: command = "uvx", args = ["portal-mcp-server@latest"]

# Optional: pass the agent socket explicitly; use your actual path
[mcp_servers.portal.env]
SSH_AUTH_SOCK = "/run/user/1000/ssh-agent.socket"
```

</details>

<details><summary><b>Other hosts (Cline / Continue / Roo Code / Zed …)</b></summary>

Most accept the generic `{ "mcpServers": ... }` snippet in their MCP settings;
stdio needs no extra proxy.

</details>

## <a id="hosts"></a>Host configuration

The simplest setup is an existing `Host web01` alias in `~/.ssh/config`. Portal uses AsyncSSH's parser, including `Include`, `HostName`, `User`, `Port`, `IdentityFile`, `IdentityAgent`, and `ProxyJump`.

For Portal groups, password commands, or host-level settings, create `~/.config/portal-mcp-server/hosts.yaml`:

```yaml
hosts:
  web01:
    use_ssh_config: true
    tags: [web, prod]
    # user: deploy                # Optional: override SSH config's User
    # sudo_password_command: pass show sudo/web01
```

`use_ssh_config: true` starts with the same-named SSH alias and applies only explicitly supplied YAML fields on top. Omit `host` to inherit `HostName`. If you supply it, it must match the alias's resolved `HostName` or the connection is refused.

A host can also be defined entirely in YAML:

```yaml
hosts:
  web02:
    host: server2.example.com
    user: deploy
    port: 22
    key: ~/.ssh/id_ed25519
    tags: [web]
```

**Lookup priority: runtime registry / `hosts.yaml` → SSH config alias.** A same-named YAML host without `use_ssh_config: true` does not merge that alias's SSH config. Enable merging explicitly when you need the alias's `IdentityAgent`, `IdentityFile`, or `ProxyJump`.

`hosts(action="list")` lists configured hosts and SSH aliases, with a `source` and any warnings. `hosts(action="register", name="web01")` with only a name creates a merged entry from an existing SSH alias. Runtime registrations belong to the current MCP server's registry.

<details>
<summary>Config sources, jump hosts, and advanced options</summary>

`PORTAL_SSH_CONFIG` controls Portal's SSH config sources:

| Setting | Files read |
|---|---|
| Unset | User `~/.ssh/config`, with system client config as fallback |
| Absolute path | Only that file; suppress system config, like `ssh -F <file>` |
| `none` (case-insensitive) | Disable Portal's SSH config-file lookup |
| Other relative path | Warn and ignore |

`list` sources include `hosts.yaml`, `runtime`, and `ssh-config`; merged entries report `hosts.yaml+ssh-config` or `runtime+ssh-config`. Alias enumeration follows `Include` and excludes patterns containing `*`, `?`, or `!`.

Common YAML settings include `proxy_jump`, `keepalive_interval`, `forward_agent`, `use_ssh_agent`, and `login_shell`. Unset merged fields defer to SSH config. `proxy_jump: none` forces a direct connection; an empty string or `null` does not, so remove the field or use `none`.

When a jump host needs its own key, define its SSH alias, enable config merging, and load its key into ssh-agent. A bare `proxy_jump: user@jump` has extra limitations around default identities and inherited passphrases; see [Troubleshooting](#faq) and [ADR-0002](./docs/adr/0002-ssh-config-merge.en.md).

See [examples/hosts.yaml](./examples/hosts.yaml) for all fields and [File paths](#file-paths) for path resolution.

</details>

## <a id="authentication"></a>Authentication

Two local services have different jobs: **ssh-agent holds loaded SSH keys and signs authentication requests; Portal's credential agent holds passwords, key passphrases, and secrets entered through the CLI.** They use separate sockets and do not replace each other.

| What you need to supply | Recommended entry point | Used for |
|---|---|---|
| SSH key | `ssh-add ~/.ssh/id_ed25519` | SSH signatures through ssh-agent |
| SSH login password | `portal ssh set web01` | Establishing an SSH connection |
| Encrypted-key passphrase | `portal passphrase set web01` | Unlocking a local key file; usually prefer ssh-agent |
| Remote sudo password | `portal sudo set web01` | `remote_exec(..., use_sudo=True)` and other sudo-capable tools |
| Local sudo password | `portal sudo set-local` | `local_exec(..., use_sudo=True)` |
| API token or other secret | `portal secret set github_token` | `secrets=["github_token"]` in `remote_exec` / `local_exec` |

Run `portal … set` in your own terminal. Input is hidden; the default cache lifetime is 900 seconds, adjustable with `--ttl`. Do not paste credentials into the agent conversation or MCP tool arguments. Unattended workflows can also use password-manager commands, described below.

### <a id="ssh-agent"></a>SSH keys and ssh-agent

Reuse an existing key when available. To create a key and install its public half, for example:

```bash
ssh-keygen -t ed25519 -C "you@example.com"
ssh-copy-id -i ~/.ssh/id_ed25519.pub deploy@server.example.com
```

A running ssh-agent may have no identities. Load a key with `ssh-add` and enter its passphrase once:

```bash
# Start an agent only if none is available; otherwise reuse the system/desktop agent
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
ssh-add -l
```

Portal uses AsyncSSH to select an agent endpoint. **It does not scan every agent socket on the machine or unlock keys for you**:

1. If the SSH config used for the connection defines `IdentityAgent`, use that endpoint.
2. Otherwise read **`SSH_AUTH_SOCK` from the Portal process environment**. The MCP client may pass it through from its parent environment, or set it explicitly in the server's `env` configuration.
3. Without a usable agent, authentication depends on readable key files, available passphrases, or other configured sources.

For example, an already-running Linux user agent listening at `/run/user/1000/ssh-agent.socket` can be selected in SSH config:

```sshconfig
Host *
    IdentityAgent /run/user/%i/ssh-agent.socket
    AddKeysToAgent yes
```

`%i` is the local UID. **The socket must exist, and its path must match your actual agent.** `IdentityAgent` overrides `SSH_AUTH_SOCK`; it neither exports the variable nor starts an agent. `AddKeysToAgent` is an OpenSSH client caching option, not a way to make Portal unlock or load keys. `ssh-add` does not read `IdentityAgent` and still needs the correct `SSH_AUTH_SOCK`.

Alternatively, pass the endpoint explicitly to the MCP server:

```json
{
  "mcpServers": {
    "portal": {
      "command": "portal-mcp-server",
      "args": [],
      "env": {
        "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.socket"
      }
    }
  }
}
```

Replace the example UID `1000` or use your existing agent's path. MCP clients may filter inherited variables, and GUI clients may not inherit your terminal environment; an explicit `env` setting avoids that difference. Restart the MCP server after changing its launch environment. Running `export` later in another terminal does not update an existing Portal process.

Loaded keys remain usable until removed or the agent restarts. Their cache lifetime is independent of OpenSSH `ControlPersist` and Portal's connection-pool lifetime. The socket must be accessible to the user running Portal.

<details>
<summary>Headless Linux: a user-level ssh-agent with a fixed socket</summary>

First check `command -v ssh-agent` and `systemctl --user cat ssh-agent.service`. Reuse an existing suitable service and its socket. Otherwise create `~/.config/systemd/user/ssh-agent.service`:

```ini
[Unit]
Description=OpenSSH authentication agent
Documentation=man:ssh-agent(1)

[Service]
Type=simple
Environment=SSH_AUTH_SOCK=%t/ssh-agent.socket
ExecStart=/usr/bin/ssh-agent -D -a $SSH_AUTH_SOCK
UMask=0077

[Install]
WantedBy=default.target
```

Adjust the executable path to match `command -v`, then enable the service and load a key:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ssh-agent.service
export SSH_AUTH_SOCK="/run/user/$(id -u)/ssh-agent.socket"
ssh-add ~/.ssh/id_ed25519
```

systemd's `%t` means the user runtime directory, unlike SSH config's `%i`. To expose the socket to new terminals, add the same export to your shell startup configuration, or set the MCP environment explicitly. If user services must run after logout or before login at boot, enable `loginctl enable-linger` according to your system policy. Restarting the agent still requires loading keys again.

On macOS, reuse the login session's agent. Windows OpenSSH uses its `ssh-agent` service and named pipe; do not copy a Linux socket path.

</details>

The `use_ssh_agent` host setting controls Portal's key path:

| Value | Behavior |
|---|---|
| Omitted | Keep AsyncSSH's default selection: available file and agent identities may both participate |
| `true` | Portal does not explicitly pass `key` or a key passphrase; AsyncSSH selects the agent / default identities, and SSH config or default identities may still apply |
| `false` | Disable the agent and use file identities with the configured passphrase |

### <a id="password-login"></a>SSH password login

Define `web01` in SSH config or `hosts.yaml`, then run this in your own terminal:

```bash
portal ssh set web01
# Or choose a lifetime in seconds
portal ssh set web01 --ttl 1800
```

The first `set` installs and starts Portal's credential agent if needed. Passwords are cached by host alias: **use the same name in the CLI and MCP tool calls**.

Key authentication is tried by default. A `PermissionDenied` triggers a password retry only if a cached password or `password_command` is available; otherwise the original authentication error is preserved. To explicitly select the password-login path:

```yaml
hosts:
  web01:
    host: server.example.com
    user: deploy
    auth: password
```

Password-source priority is **CLI credential cache → `password_command` → error**. For automation, read from a password manager:

```yaml
hosts:
  web01:
    host: server.example.com
    user: deploy
    auth: password
    password_command: pass show ssh/web01
```

`op`, `bw`, `secret-tool`, or another command that prints the credential can also be used. **Do not write `password: plaintext`**: that field is ignored and generates a warning. Command sources have a 10-second timeout; nonzero exit, empty output, or non-UTF-8 output fails, and stderr is not included in the credential error. See [SECURITY.md](./SECURITY.md#authentication) for the full contract.

### Key passphrase: `portal passphrase set`

If you are not using ssh-agent, supply the passphrase that unlocks the local key file:

```bash
portal passphrase set web01
```

Alternatively set `passphrase_command: pass show ssh/web01-passphrase` for the host. Portal reads its credential cache first, then the command source, and passes the value to AsyncSSH for local key decryption. Key passphrases, SSH passwords, and sudo passwords have separate caches. The `use_ssh_agent: true` path skips Portal's passphrase resolution.

### sudo: `portal sudo set`

```bash
portal sudo set web01
```

The agent can then call `remote_exec("web01", "id", timeout=30, use_sudo=True)`, or use sudo-capable `remote_read` / `remote_patch`. Source priority is **CLI credential cache → `sudo_password_command` → error**:

```yaml
hosts:
  web01:
    use_ssh_config: true
    sudo_password_command: pass show sudo/web01
```

If the SSH login and sudo passwords really are identical, explicitly set `sudo_password_same_as_ssh: true`. Then `portal ssh set web01` populates both caches. This is off by default and never treats a key passphrase as a sudo password.

sudo operations use `sudo -S -k` with the password sent through stdin. They do not inherit the working directory or environment previously set in `remote_shell`; include those in the current command when needed. Interactive sudo prompts belong in your own SSH terminal.

<details>
<summary>Local sudo and the sudo + secrets transport</summary>

`local_exec` is disabled unless `PORTAL_ALLOW_LOCAL_EXEC=1`. Local sudo uses the reserved identity `<local>`:

```bash
portal sudo set-local
```

A top-level `"<local>": {sudo_password_command: ...}` entry in `hosts.yaml` is the command-source alternative. This differs from a `localhost` alias reached over SSH.

With sudo and `secrets` together, the password and secret values share stdin. The elevated shell reads and exports the secrets after sudo's `env_reset`, so no sudoers `env_keep` is needed. This transport also constrains the command's own stdin. See `sudo_stdin_secret_script()` in `secrets_store.py`.

</details>

### Secrets: `portal secret set`

Enter a token in a terminal:

```bash
portal secret set github_token
```

The agent supplies only its name, for example `remote_exec("web01", "gh auth status", timeout=30, secrets=["github_token"])`. The server injects its value as `GITHUB_TOKEN` for that command; `local_exec` supports the same parameter. Names default to uppercase environment names; see [examples/secrets.yaml](./examples/secrets.yaml) for configuration.

Source priority is **CLI credential cache → command source in `secrets.yaml`**:

```yaml
secrets:
  github_token:
    command: pass show api/github
```

`secrets` and `use_sudo` can be combined; background `remote_job` does not support either. Known secret values are replaced in command output returned to the agent. Still avoid printing credentials deliberately; see [Security](#security) for the limits.

### <a id="credential-agent"></a>Portal credential agent and everyday management

`ssh`, `passphrase`, `sudo`, and `secret` share the same CLI operations:

```bash
portal ssh show web01       # fingerprint and remaining TTL, never plaintext
portal ssh list             # entries for this kind
portal ssh confirm web01    # enter twice; matching inputs write/update the cache
portal ssh clear web01      # remove one entry

portal agent status        # endpoint, service status, and cache counts
portal agent clear         # clear every credential kind
```

Replace `ssh` with `passphrase`, `sudo`, or `secret` for the other kinds. `confirm` compares the two new inputs: **it does not compare against the old cached value or verify a remote login**. `show` / `list` expose only fingerprints and lifetime; there is no plaintext-export command.

Host-based `set` / `confirm` checks the alias first to catch typos. If a host exists only in the MCP server's runtime registry and the CLI cannot see it, use `portal ssh set web01 --force`. `secret` and `sudo set-local` need no such override.

Credentials live in service memory, expire after 900 seconds by default, and disappear on service restart. The CLI and MCP server must use the same user's configuration and credential service. A newly installed service can be discovered on the next credential request; **changing the MCP launch environment still requires restarting the MCP server**.

<details>
<summary>Platform installation, service endpoints, and credential access</summary>

Normally `portal … set` installs the service on demand. Explicit management is also available:

```bash
portal agent install --now
portal agent uninstall
```

| Platform | User-level service | Local transport |
|---|---|---|
| Linux | systemd `.socket` + `.service`, socket-activated | Unix socket |
| macOS | launchd LaunchAgent, supervised process | Unix socket |
| Windows | current-user logon task with an InteractiveToken principal | Named pipe |

The installer records the resolved endpoint in `agent.json` in the configuration directory; `PORTAL_CREDENTIAL_AGENT_SOCKET` can override it. Linux defaults to `%t/portal-mcp-server/credentials.sock`, separate from the SSH agent socket. The macOS LaunchAgent and Windows task run as the user. The Windows task does not run as SYSTEM or store a login password, and uses `ExecutionTimeLimit=PT0S` plus a restart policy. Linux socket access is checked at the same-user boundary; see [SECURITY.md](./SECURITY.md) for each platform's controls.

Values are supplied only to authorized local consumer processes for authentication or execution, not exposed through human-facing CLI output, MCP arguments, or audit logs. CLI-entered values are not written to configuration files. Password-manager commands fetch on demand and are independent of the interactive cache TTL.

Supplying credentials also grants agents that can call this MCP server the corresponding capabilities while those credentials are available. Choose a task-appropriate TTL, host/command policy, and sudo permissions. If a credential is missing, the tool returns an error asking you to set it in another terminal; the agent should retry after you finish, without requesting plaintext in chat.

</details>

## <a id="tools"></a>Tools

Portal exposes 14 MCP tools. Choose by task first, then use the expandable reference for complete signatures.

### Running commands

| Tool | When to use it | Key behavior |
|---|---|---|
| `remote_exec` | One-shot commands; single-host, concurrent, or rolling execution | Separate stdout/stderr and exit code; supports `group_tag`, `use_sudo`, and `secrets` |
| `remote_shell` | Preserve `cd`, `export`, venv, or other shell state across calls | One persistent shell per host; PTY output merges stdout/stderr |
| `remote_job` | Long-running work or jobs that should survive MCP server shutdown | `submit/poll/cancel/list`; remote `nohup`; per-process persisted job registry; no sudo/secrets |
| `local_exec` | Run a command on the MCP server's machine | Disabled unless `PORTAL_ALLOW_LOCAL_EXEC=1`; supports sudo/secrets |
| `remote_close` | Reset a host's persistent shell | Close the session; the next `remote_shell` call recreates it |

`remote_exec`, `remote_shell`, and `local_exec` require a `timeout` in seconds. The default cap is `PORTAL_MAX_TIMEOUT=300`; use `remote_job` for long tasks. MCP progress heartbeats are sent during execution, but whether they reset a client timeout depends on the client.

**Connection reuse and shell state are separate.** SSH tools can share the connection pool; only `remote_shell` preserves shell state. The normal `remote_exec` path defaults to a login shell; see [ADR-0004](./docs/adr/0004-login-shell-and-sudo-env.en.md) for sudo/secrets environment handling.

### Files, tunnels, and management

| Tool | Purpose |
|---|---|
| `remote_read` / `remote_patch` | Read content, file hash, and range hash; validate and atomically patch; sudo supported |
| `remote_grep` | Regex content search with filenames, matching content, or counts; paging and truncation flags |
| `remote_glob` | Glob-based file discovery, sorted by modification time; up to 100 results |
| `remote_transfer` | SFTP upload/download, incremental directories, or path lists; resumable uploads with verification |
| `remote_tunnel` | Manage local / reverse / SOCKS tunnels with `open/close/list` |
| `hosts` | List, register, or remove hosts; inspect sources and config warnings |
| `policy_check` | Check policy without executing; `ALLOWED` means the current policy permits it |
| `inspect` | Inspect the server, pool, shell sessions, history, statistics, and policy |

`remote_grep` respects `.gitignore`; `remote_glob` does not. Directory transfers do not follow local symlinks. Successful `remote_patch` calls also sweep same-directory orphan temporary files older than an hour; see the full reference for boundaries.

### <a id="agent-conventions"></a>Agent-side conventions

Add conventions to your `AGENTS.md` or system prompt and adapt them to the actual authorization:

- Complete one assessable step per call, read the output and exit code, then decide what to do next. Use `commands=[…]` for fixed batches that need no intermediate decision.
- Read and edit with `remote_read → remote_patch`. Re-read after a hash conflict instead of overwriting concurrent changes.
- Use dedicated search, transfer, tunnel, and background-job tools; use `remote_exec(host=[…])` or `group_tag` for multiple hosts.
- Check host aliases, config warnings, and authorization first. `/tmp/` is a suggested initial workspace; existing user authorization takes precedence.
- Have the user enter credentials through the CLI in another terminal. If missing, provide the exact command and retry after confirmation; never request plaintext in chat.
- Use `remote_read(use_sudo=True)` / `remote_patch(use_sudo=True)` for root-owned files. sudo patching retains hash checks, atomic replacement, and owner/mode; the target must already exist.
- Use `remote_job` for work that must outlive the MCP server. Foreground calls and transfers stop with the process; interrupted uploads can resume on retry.

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

Portal-specific settings use the `PORTAL_*` prefix and belong in the MCP server's `env`. `SSH_AUTH_SOCK` is the additional standard SSH variable; `IdentityAgent` comes from SSH config. See [Authentication](#ssh-agent).

### <a id="file-paths"></a>File paths

These are Linux defaults. Other platforms use their corresponding user configuration/state directories. Environment overrides can select absolute paths:

| Variable | Purpose | Linux default |
|---|---|---|
| `PORTAL_HOSTS_YAML` | Host config | `~/.config/portal-mcp-server/hosts.yaml` |
| `PORTAL_POLICIES_YAML` | Security policy | `~/.config/portal-mcp-server/policies.yaml` |
| `PORTAL_SECRETS_YAML` | Secret command sources | `~/.config/portal-mcp-server/secrets.yaml` |
| `PORTAL_SSH_CONFIG` | SSH config source selection | User config + system fallback; absolute path or `none` supported |
| `PORTAL_LOG_DIR` | Audit and server logs | `~/.local/state/portal-mcp-server/log/` |
| `PORTAL_CREDENTIAL_AGENT_SOCKET` | Portal credential-service endpoint | Installed `agent.json` |

Path priority is **explicit environment variable → platform user directory**. Linux supports `XDG_CONFIG_HOME` / `XDG_STATE_HOME`. The current directory and repository `examples/` are not automatically used as live configuration. See [Host configuration](#hosts) for SSH file-selection rules.

When YAML is needed, choose a template from [examples/](./examples/), copy it to the user config directory, and edit it. Using SSH aliases alone does not require creating all three YAML files. Keep real configuration and credential sources out of Git.

<details>
<summary>All tuning parameters, defaults, and test variables</summary>

### Security & auth

| Variable | Meaning | Default |
|---|---|---|
| `PORTAL_AUDIT_FAIL_OPEN` | `1` → a failed audit write only warns and continues; default → **fail-closed**, the tool errors after execution without rolling back completed changes | _(unset)_ |
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


### Audit rotation, background jobs, and read limits

| Variable | Purpose | Default |
|---|---|---|
| `PORTAL_AUDIT_MAX_BYTES` | Audit-log rotation threshold | `10485760` (10 MiB) |
| `PORTAL_AUDIT_BACKUPS` | Number of rotated logs to retain | `5` |
| `PORTAL_JOB_PERSIST` | Persist job registry; `0` / `false` disables | On |
| `PORTAL_JOB_STATE_FILE` | Persistence path; an explicit setting uses a fixed file | `jobs/<pid>.json` in the state directory |
| `PORTAL_JOB_MAX_LIVE` | Maximum live background jobs | `50` |
| `PORTAL_JOB_TTL` | Retain completed jobs, then clean state and remote temporary files | `3600` seconds |
| `PORTAL_READ_MAX_LINES` | Default maximum lines per `remote_read` page | `2000` |
| `PORTAL_READ_MAX_BYTES` | Maximum bytes per `remote_read` page | `16384` |

For old variable prefixes, cwd-based configuration, and tool-name migration, see [CHANGELOG.md](./CHANGELOG.md) and [legacy tool migration](#tools).

</details>

## <a id="security"></a>Security

Portal's capabilities depend on the server user's permissions, SSH identities, available credentials, and policy. Limit hosts, commands, and sudo scope, then supply credentials for the task.

- **Policy**: host allowlists, command allowlists/blocklists, rate limits, and two-phase multi-host checks. Optional `policies.safety_net.enabled` adds [cc-safety-net](https://github.com/kenryu42/cc-safety-net); checker failures default to refusal.
- **Authorization**: starting in remote `/tmp/` is an agent convention, **not an enforced filesystem sandbox**. Follow user authorization for home directories and project files.
- **Credentials**: CLI input is not a tool argument or configuration-file value; command sources read from a password manager. Agents that can call the server can use the corresponding capabilities, so TTL and policy still matter.
- **HTTP**: binds `127.0.0.1` by default; non-loopback binding requires `PORTAL_AUTH_TOKEN`. The service speaks HTTP; terminate TLS at a proxy when exposing it publicly.
- **Tunnels and transfer**: tunnels bind loopback unless `PORTAL_ALLOW_TUNNEL_EXPOSURE=1` permits wider exposure. Directory transfer does not follow local symlinks, but retains the server user's local filesystem access.
- **Audit**: state changes write `$PORTAL_LOG_DIR/audit.jsonl` after execution. A failed write makes the tool error by default; **it does not roll back a completed remote change**. `PORTAL_AUDIT_FAIL_OPEN=1` changes this to warning and continuation.
- **Edits**: hashes and atomic replacement detect conflicts and reduce interrupted-write damage. This is optimistic concurrency control, not protection against every race. Plain SFTP writes do not guarantee owner/mode preservation; sudo patching preserves them explicitly.

<details>
<summary>Limits of output redaction and remote shell history</summary>

Known secret values are replaced in returned output, but transformed, split, exported, or inadvertently logged values still need command and environment controls. Non-interactive bash normally does not record history. If remote configuration such as `BASH_ENV` forces history on, stdin-injected content may reach `~/.bash_history`. Check the real remote environment; redaction does not replace access control.

</details>

See [SECURITY.en.md](./SECURITY.en.md) for the full threat model, peer checks, audit semantics, and known limitations. Report vulnerabilities privately through [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new). The project's target response windows are 48 hours to acknowledge, 7 days to assess, and 30 days for a critical fix.

## <a id="faq"></a>FAQ

### Terminal SSH works, but Portal authentication fails

Your terminal may have unlocked a key or reused an OpenSSH master connection; Portal uses its own AsyncSSH connection. Check the actual endpoint:

```bash
ssh -G web01
echo "$SSH_AUTH_SOCK"
ssh-add -l
# Check a fixed socket; replace with your actual path
SSH_AUTH_SOCK=/run/user/1000/ssh-agent.socket ssh-add -l
```

Compare the effective `IdentityAgent` with the MCP server's `env.SSH_AUTH_SOCK`, then check that the agent holds the required key. `ssh-add` reads only the environment: its failure does not prove that a client using `IdentityAgent` cannot reach an agent. If `hosts.yaml` shadows the alias, check whether `use_ssh_config: true` is needed. See [Hosts](#hosts) and [Authentication](#ssh-agent).

### `portal … set` reports an unknown host, or credentials are not used

Use the same host alias as the MCP call and make sure the CLI and server read the same configuration. A runtime-only registration may need `--force`. Check service status and TTL with `portal agent status` and `portal ssh show web01`. Restart the MCP server after changing its launch environment; a newly installed credential service itself can be discovered on demand.

### Local changes don't show up in the agent

Whether run via the `uv tool install`'d `portal-mcp-server` or `uvx`, both use the
**PyPI-published build**, not your working tree — local edits aren't seen. For
local debugging, either do an editable install (recommended; `command` stays
`portal-mcp-server`):

```bash
uv tool install --force --editable .
```

or, with uvx, temporarily set `.mcp.json` `args` to
`["--from", "/absolute/path/to/portal-mcp-server", "portal-mcp-server"]` (absolute
path). **Don't commit that local path into a project `.mcp.json`.**

### Connection timeout / Permission denied (publickey)

1. Confirm `ssh user@host` connects directly in a terminal.
2. Check key perms: `chmod 600 ~/.ssh/id_ed25519`.
3. If using `~/.ssh/config`, confirm the `Host` alias / `HostName` / `User` /
   `IdentityFile`.
4. For ProxyJump, asyncssh honors `~/.ssh/config`'s `ProxyJump`; confirm the
   bastion connects manually too. **Mind the jump-credential boundary**: a bare
   `proxy_jump: user@jump` in hosts.yaml reaches the bastion with the **default
   key/agent** only, and reuses the passphrase resolved for the **target** to
   unlock **the local key that logs into the bastion** (a differently-encrypted
   bastion key then fails with `Incorrect passphrase`); it does **not** read a
   bastion-specific `IdentityFile`. To give the bastion its own key/passphrase,
   set `use_ssh_config: true` (asyncssh then reads the bastion's `Host`
   `IdentityFile`) and load the bastion key into **ssh-agent** (agent auth needs
   no passphrase, so the clash disappears). To force a direct connection
   (ignoring an ssh-config `ProxyJump`), set `proxy_jump: none`.

### Connection drops after the MCP client restarts

Expected — the pool follows the MCP server process lifecycle. A client restart
closes the server; the next tool call rebuilds connections automatically.

### Update to the latest version

```bash
uv tool upgrade portal-mcp-server      # installed (recommended); or uv tool upgrade --all
uvx portal-mcp-server@latest --help    # zero-install: refresh the uvx cache
```

Then restart the MCP client.

## <a id="architecture-design"></a>Architecture & design

MCP clients call Portal through stdio or optional HTTP. The server handles host resolution, policy, credentials, and audit; SSH operations use an AsyncSSH connection pool, while `local_exec` runs on the server's own machine.

### <a id="vs-traditional"></a>How this relates to plain ssh / scp

Existing OpenSSH scripts remain useful. Portal provides a common interface for agents: pooled connections, stateful shells, hash-checked editing, structured search, credential input, and task management.

<details>
<summary>Compare capabilities</summary>

| Capability | Direct SSH / scp / rsync | Portal |
|---|---|---|
| Connection reuse | `ControlMaster` on Linux/macOS; see Windows OpenSSH limitations in [issue #405](https://github.com/PowerShell/Win32-OpenSSH/issues/405) | AsyncSSH connections shared across tools in the Python process |
| Shell state | Independent `ssh host command` invocations create fresh execution environments | `remote_shell` preserves state |
| File edits | Scripts manage conflicts, staging, and replacement | File/range hash checks, temporary writes, atomic replacement |
| Search and transfer | Scripts parse output and manage incremental work and retries | Structured results, incremental checks, progress, and upload resume |
| Multiple hosts and jobs | Scripts manage concurrency, processes, and state | Concurrent/rolling calls and `remote_job` lifecycle |
| Tunnels | Manage SSH processes yourself | Common `open/close/list` interface |
| Credentials and audit | Arrange input channels, policies, and logs separately | CLI credential service, tool-level policy, and audit |

Reuse generally reduces handshake overhead on later operations; actual latency depends on the network, host, and task. Portal neither relies on OpenSSH control sockets nor inherits an existing OpenSSH master connection.

</details>

### <a id="architecture"></a>Call path

<details>
<summary>Data flow and CLI / MCP cooperation</summary>

```text
MCP client → Portal tools → host / policy / credentials → AsyncSSH pool → remote hosts
                    │
                    └→ local_exec → server's local machine

User terminal → portal CLI → Portal credential agent ← MCP server
User terminal → ssh-add    → ssh-agent               ← AsyncSSH
```

#### <a id="cli-vs-mcp"></a>CLI and MCP server

`portal` and `portal-mcp-server` share a Python entry point but are launched separately by the user's terminal and the MCP client. The CLI does not write passwords directly into a running MCP server; it stores them through the credential service, which the server queries when needed.

Shared configuration includes `hosts.yaml`, `policies.yaml`, `secrets.yaml`, and `agent.json` for the credential endpoint. Each process reads its own configuration. Upgrade them together to avoid a new field taking effect on only one side. ssh-agent is a separate signing service selected through `IdentityAgent` or `SSH_AUTH_SOCK`.

</details>

### <a id="design-principles"></a>Design principles

Prefer a small set of tools with distinct jobs: one-shot execution, persistent sessions, background work, files, transfer, and resource management. A resource's `action` / `view` stays in one tool, and `Literal` annotations produce schema enums. These choices also draw on [Writing Tools for Agents](https://www.anthropic.com/engineering/writing-tools-for-agents).

<details>
<summary>Tool count and context overhead</summary>

The 14 tools' names, descriptions, and schemas total roughly 9k tokens in the original `tiktoken o200k_base` measurement; the exact count changes with descriptions and versions. A tool earns its place through useful state management, concurrency/write guarantees, credential boundaries, or structured output. Convenience commands can remain commands instead of becoming individual tools.

</details>

### <a id="step-wise-exec"></a>Step-wise execution and background work

`remote_exec` / `remote_shell` are foreground steps: run, inspect the result, then decide what comes next. Use `remote_job` for unattended long-running work.

<details>
<summary>Timeouts, batches, keepalive, and background lifecycle</summary>

Foreground `timeout` is required and capped by `PORTAL_MAX_TIMEOUT`; calls over the cap are refused. `commands=[…]` suits fixed batches; split dependent work into assessable steps. MCP progress heartbeats report that a call is active, but do not replace server-side timeouts or guarantee that every client extends its deadline.

Foreground calls live in the MCP server process. Closing a stdio client normally also closes its server. `remote_job` starts work on the remote host, with a best-effort per-process persisted registry, task limits, and TTL. It can survive a client disconnect, but is not a full workflow scheduler. Transfers do not keep themselves alive; interrupted uploads can resume on retry.

</details>

### <a id="connection-pool"></a>Connection pool and persistent shells

<details>
<summary>Connections, channels, command boundaries, and process model</summary>

The pool is keyed by host, with defaults of 5 TCP connections per host and 5 concurrent channels per connection. SFTP, exec, and tunnels share connections. At full load, the least-busy connection may be reused with a warning. Idle or aged connections are cleaned up on later acquisition according to [tuning settings](#env-vars).

A connection carries traffic; `remote_shell` sessions are managed separately. Persistent shells use a PTY and OSC 133 markers to recognize command boundaries rather than guessing from quiet periods. Interactive prompts trigger soft cancellation. If the prompt can be recovered, the session is preserved; otherwise it is destroyed after the recovery timeout. Output is capped in memory and flagged `truncated` when needed.

AsyncSSH keeps sessions, SFTP, tunnels, and asynchronous cancellation in one process for resource management and credential reuse. Background work uses remote `nohup`, not an extra local SSH subprocess that keeps a foreground call alive.

</details>

### <a id="credential-unification"></a>Shared authentication and visible feedback

<details>
<summary>Credential boundaries, warnings, and maintainer constraints</summary>

SSH tools resolve identities through the same connection manager; the server injects sudo and secret values when needed. Passwords are not MCP tool arguments. Background tasks currently do not support sudo/secrets. See [ADR-0003](./docs/adr/0003-credential-unification.en.md).

MCP clients may capture or ignore server stderr, so actionable configuration warnings are returned by `hosts(action="list")` and fatal errors by the relevant tool. Logs support diagnosis and audit rather than being the sole feedback channel. Tool descriptions explain how to request CLI input for missing credentials, without relying on a client to insert server-level `instructions` into the model context.

Preserve these boundaries during maintenance:

- Command results may strip trailing newlines; file reads must preserve content so hashes remain valid.
- SSH config merging connects through the alias and rejects `HostName` mismatches; see [ADR-0002](./docs/adr/0002-ssh-config-merge.en.md).
- sudo patching records owner/group/mode, stages in a private user directory, and atomically replaces beside the target; do not substitute root ownership or default permissions.
- Policy rejection, read-only audit behavior, and post-write audit failure semantics follow [SECURITY.md](./SECURITY.md).

</details>

## <a id="testing"></a>Testing

### Development environment

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras
uv run ruff check portal_mcp_server/ tests/
uv run python -m pytest tests/ -v
```

To run this checkout from your MCP client, use `uv tool install --force --editable .` and restart the server. See [CONTRIBUTING.en.md](./CONTRIBUTING.en.md) for the full workflow.

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
  PORTAL_TEST_HOST=server.example.com PORTAL_TEST_PORT=22 PORTAL_TEST_USER=deploy \
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
