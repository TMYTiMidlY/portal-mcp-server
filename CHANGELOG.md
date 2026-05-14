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
