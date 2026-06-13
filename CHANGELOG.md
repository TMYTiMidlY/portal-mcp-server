## v3.3.2 (2026-06-14)

### Fix

- harden gate, job manager, and credential agent
- repair latent bugs in dormant helper functions
- drop unused server-level MCP instructions, relax mcp[cli] floor to >=1.0.0

## v3.3.1 (2026-06-14)

### Fix

- **cli**: front-load out-of-band credential onboarding for agents
- **local_exec**: make the disabled-tool error point at the MCP config env block

## v3.3.0 (2026-06-13)

### Feat

- **security**: gate portal exec paths through cc-safety-net

### Fix

- **portal_shell**: run multi-line commands as one compound command

## v3.2.1 (2026-06-13)

### Fix

- scrub hardcoded internal test host and sync portal_shell docs to OSC 133

## v3.2.0 (2026-06-13)

### Feat

- **portal_shell**: switch command boundary to OSC 133 shell integration

## v3.1.2 (2026-06-11)

### Fix

- resolve code-review findings across exec/jobs/creds/search + doc drift

## v3.1.1 (2026-06-09)

## v3.1.0 (2026-06-09)

### Feat

- **exec**: route sudo here, flag credentialed runs high-risk, auto-install agent
- **job**: best-effort persist the job table across restarts
- **job**: reject use_sudo/secrets on submit with a redirect to portal_exec
- **audit**: size-based log rotation via stdlib RotatingFileHandler
- **server**: close pool + sessions on shutdown via FastMCP lifespan
- **credential-agent**: Windows per-user logon scheduled-task install
- **credential-agent**: named-pipe transport for Windows + CI matrix
- **job**: page portal_job poll output on demand; clean UTF-8 chunk seams
- **agent**: cross-platform credential agent install (Linux + macOS)
- **auth**: symmetrize SSH passphrase with the login-password side channel
- **host**: add proxy_jump / keepalive_interval / forward_agent to hosts.yaml
- **host**: detect hosts.yaml <-> ssh config conflicts; fix use_ssh_config
- **job**: add portal_job — background submit/poll/cancel/list (L1 async)
- **bash**: return exit codes from the persistent session path
- **bash**: emit MCP progress heartbeats during portal_bash/portal_local_exec
- **audit**: expose server metadata via portal_audit (view="server" + snapshot)

### Fix

- **grep**: force filename + ERE in the grep fallback so matches survive
- **bash**: require a newline terminator when parsing the session exit code

### Refactor

- **host**: detect ssh-config aliases via asyncssh parser (follow Include)
- **output**: single-source ANSI stripping in safety.strip_ansi
- **patch**: fold orphan-tmp cleanup into portal_patch; drop the tool
- **tools**: delete portal_ping; synthesize it with portal_exec
- **search**: port grep/glob to Claude Code's schema + structured output
- **tools**: consolidate tunnel into portal_tunnel(action=); narrow audit
- **exec**: delete portal_playbook; its semantics live in portal_exec
- **exec**: absorb portal_multi_exec into portal_exec as orthogonal flags
- **exec**: split portal_bash into portal_shell + portal_exec
- **schema**: type dispatch params with Literal so enums reach clients
- **audit**: fold portal_bash_status into portal_audit view="sessions"

## v3.0.1 (2026-06-08)

### Fix

- **paths**: reject relative PORTAL_* and PORTAL_CREDENTIAL_AGENT_SOCKET overrides

## v3.0.0 (2026-06-08)

### BREAKING CHANGE

- on macOS and Windows, config and logs now live under the platform-native locations above instead of ~/.config / ~/.local/state. Linux paths are unchanged except logs move from .../state/portal-mcp-server/logs (plural) to .../state/portal-mcp-server/log (singular, matching the XDG spec and platformdirs' default). Operators who want the legacy path can pin it via PORTAL_LOG_DIR=~/.local/state/portal-mcp-server/logs; see CHANGELOG for the migration steps (cat old/audit.jsonl >> new/audit.jsonl, then trash-put the old dir, after upgrading the running portal-mcp-server).

### Feat

- **paths**: adopt platformdirs for native config/state/log dirs

## v2.0.2 (2026-06-01)

### Fix

- **cli**: drop unused f-string prefix flagged by ruff F541

## v2.0.1 (2026-06-01)

### Refactor

- rename credential broker to credential agent + restructure CLI to portal {agent,ssh,sudo,secret} <verb> tree

## v2.0.0 (2026-06-01)

### BREAKING CHANGE

- - ./config/hosts.yaml / ./config/policies.yaml / ./logs/ are no longer
  auto-loaded. Set PORTAL_HOSTS_YAML / PORTAL_POLICIES_YAML /
  PORTAL_SECRETS_YAML / PORTAL_LOG_DIR explicitly, or place files at
  the XDG defaults (~/.config/portal-mcp-server/ and
  ~/.local/state/portal-mcp-server/logs/).
- Template files moved: config/{hosts,secrets}.example.yaml and
  config/policies.yaml are now examples/{hosts,secrets,policies}.yaml.
- Tools that previously returned an error string (BLOCKED, Invalid,
  Error: …) now raise an MCP error response with isError=true. Clients
  must handle these as tool failures rather than successful results
  whose text happens to start with "Error". Tests doing
  `assert "BLOCKED" in result` need to switch to
  `with pytest.raises(ToolError, match="BLOCKED"):` (see updated
  tests/test_policy_enforcement.py, test_gate_coverage_fixes.py,
  test_transfer_lists.py, test_secret_injection.py for the pattern).

### Feat

- add systemd credential broker

### Refactor

- env+XDG-only paths, examples/ template dir, ToolError errors

## v1.4.0 (2026-05-31)

### Feat

- **cli**: add short alias `portal` as a second entry point

## v1.3.0 (2026-05-31)

### Feat

- **auth**: ssh-login CLI + key→password fallback + peer-uid socket guard
- **secrets**: inject named API tokens into local/remote exec without exposing values to the LLM
- **registry**: surface hosts.yaml config warnings to the agent

## v1.2.0 (2026-05-31)

### Feat

- **transfer**: add upload-list/download-list modes for explicit file batches
- **transfer,sudo**: structured incremental transfers + out-of-band sudo

## v1.1.2 (2026-05-30)

### Fix

- raise default portal_bash timeout 60s→3600s

## v1.1.1 (2026-05-27)

### Refactor

- **internal naming**: renamed the module-level constant
  `connection_manager.SSH_DECODE_ERRORS` to `DEFAULT_DECODE_ERRORS` so it
  aligns with the surrounding `DEFAULT_*` defaults (`DEFAULT_MAX_IDLE_TIME`,
  `DEFAULT_MAX_CONN_AGE`, …) and stops being misread as either an environment
  variable or an `SSH_AUTH_SOCK`-style OpenSSH name. Purely internal: the
  constant is not part of any public API, never read from the environment,
  and never appeared in the README or `CHANGELOG`. Behaviour unchanged.

## v1.1.0 (2026-05-16)

### BREAKING CHANGES

- **env vars**: unified all 9 product + 5 test environment variables under
  the `PORTAL_*` prefix. The legacy `SSH_*` / `SSH_MCP_*` / `MCP_*` names
  are **no longer recognised**. Rename in your MCP client `env` block:

  | Old name | New name |
  |---|---|
  | `SSH_HOSTS_YAML` | `PORTAL_HOSTS_YAML` |
  | `SSH_POLICIES_YAML` | `PORTAL_POLICIES_YAML` |
  | `SSH_MCP_LOG_DIR` | `PORTAL_LOG_DIR` |
  | `SSH_MCP_AUDIT_FAIL_OPEN` | `PORTAL_AUDIT_FAIL_OPEN` |
  | `MCP_AUTH_TOKEN` | `PORTAL_AUTH_TOKEN` |
  | `SSH_POOL_SIZE` | `PORTAL_SSH_POOL_SIZE` |
  | `SSH_MAX_CHANNELS_PER_CONN` | `PORTAL_SSH_MAX_CHANNELS_PER_CONN` |
  | `SSH_MAX_IDLE_TIME` | `PORTAL_SSH_MAX_IDLE_TIME` |
  | `SSH_MAX_CONN_AGE` | `PORTAL_SSH_MAX_CONN_AGE` |
  | `SSH_TEST_LIVE` | `PORTAL_TEST_LIVE` |
  | `TEST_HOST` / `TEST_PORT` / `TEST_USER` / `TEST_KEY_PATH` | `PORTAL_TEST_HOST` / `PORTAL_TEST_PORT` / `PORTAL_TEST_USER` / `PORTAL_TEST_KEY_PATH` |

  Rationale: the old `SSH_*` prefix collided with OpenSSH's own variable
  namespace (`SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `SSH_CLIENT`, …); `MCP_AUTH_TOKEN`
  was so generic it would clash with any other MCP server running in the
  same shell. Single prefix + clear semantic infix (`PORTAL_SSH_*` for
  connection-pool tunables, plain `PORTAL_*` for everything else) makes
  the configuration self-documenting and conflict-free.

### Fix

- **portal_bash**: WSL/Windows hosts whose default codepage is GBK (PowerShell
  / cmd.exe Chinese output, e.g. `netsh interface portproxy show all`) no
  longer crash the SSH channel. asyncssh's stream readers are now configured
  with `errors='backslashreplace'`, so non-UTF-8 bytes surface as visible
  `\xd3` escapes instead of raising `UnicodeDecodeError` and tearing down
  the persistent session. As a second line of defence, `execute_in_session`
  evicts dead sessions and raises a typed `SessionDead`, which `remote_bash`
  catches to transparently rebuild the session and retry once — so the
  agent never sees the cascading "Channel not open for sending" follow-up.

### Tests

- Added `tests/test_encoding_resilience.py` (7 cases) covering the
  decode-errors kwarg injection, `SessionDead` propagation, and the
  transparent auto-rebuild path.

## v1.0.1 (2026-05-15)

### Fix

- **packaging**: render PyPI readme with absolute links via hatch-fancy-pypi-readme

## v1.0.0 (2026-05-14)

### Feat

- **pool**: add connection lifecycle management; expose pool config via env vars
- **auth**: opt-in password authentication via password_command
- **remote-edit**: align with mcp-text-editor + add safety nets
- **safety**: central input validators for paths/env/signals/etc
- **packaging**: package as ssh-remote-mcp installable via uvx
- **tools**: add remote_* tools — hash-protected edit, remote rg/glob, persistent bash
- **connection**: auto-resolve hosts from ~/.ssh/config

### Fix

- **security**: close 2 critical pool leaks + 4 gate-coverage gaps
- **paths**: rename XDG namespace ssh-remote-mcp → portal-mcp-server
- **portal_bash**: silence PS2 to keep heredoc/multi-line output clean
- **remote_text_editor**: rename remote_read/remote_patch refs to portal_*
- **security**: constant-time hash comparison in remote_patch
- **security**: fail-closed audit by default; extend coverage to all state-changing tools
- **security**: gate multi-host orchestration and per-session commands
- **security**: remove password auth from HostConfig and ssh_register_host
- **robustness**: isolate playbook failures; opt-in fail-closed audit
- **concurrency**: per-host locks; cleanup pool & locks on remove_host
- **reliability**: no SFTP/connection leaks in file_ops
- **security**: default to strict SSH host-key checking
- **security**: plug command-injection sinks in shell/session/process

### Refactor

- consolidate docs into README; drop unused modules; gate portal_bash_close
- rename package ssh_remote_mcp → portal_mcp_server
- **tools**: collapse 51 tools into 18 portal_* tools
