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
