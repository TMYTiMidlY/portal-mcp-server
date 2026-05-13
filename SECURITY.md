# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| `main` branch | ✅ Active |
| Older tags | ❌ No backports |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report security issues privately via one of the following channels:

- **GitHub Security Advisories:** Use the [Report a Vulnerability](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new) button on this repo.
- **Email:** Contact the maintainer directly through their GitHub profile.

### What to include

- A clear description of the vulnerability
- Steps to reproduce (proof-of-concept if available)
- Potential impact assessment
- Any suggested mitigations

### Response timeline

- **Acknowledgement:** Within 48 hours
- **Initial assessment:** Within 7 days
- **Resolution target:** Within 30 days for critical issues

## Security Considerations for Users

### Credential handling
- Store SSH private keys with `chmod 600` permissions
- Never commit `config.json` or any file containing hostnames, usernames, or key paths
- Use dedicated deploy keys with minimal privilege where possible

### Network exposure
- This MCP server binds to `stdio` by default and does not open any network ports on the MCP host
- All outbound connections are explicitly to your configured SSH targets
- Running behind a VPN (e.g. Tailscale) for remote targets is strongly recommended

### Principle of least privilege
- Create dedicated SSH users for automated access rather than using `root` or personal accounts
- Restrict SSH user permissions using `AllowUsers`, `Match` blocks, or `ForceCommand` in `sshd_config`

## Known Limitations

- Password-based SSH authentication is not supported by design
- Host key verification uses the system `known_hosts` by default; disabling it weakens MITM protection

## References & Algorithmic Provenance

The hash-protected file editing in `portal_mcp_server.remote_text_editor`
(`remote_read` / `remote_patch`) is a deliberate port of the safe-edit
pattern from [tumf/mcp-text-editor](https://github.com/tumf/mcp-text-editor)
(MIT). Concretely:

| Upstream (`mcp-text-editor`) | Here (`remote_text_editor`)              |
|---|---|
| Whole-file SHA-256 conflict detection | identical algorithm, runs over SFTP |
| Line-range patch model | identical model, plus per-patch `range_hash` |
| Single-shot file overwrite | replaced with tmp-file + `posix_rename` (atomic) |
| Local `open(...)` + `fcntl.flock` | replaced with AsyncSSH SFTP + connection-pool release |

The upstream library is **not** a Python dependency because its
`TextEditorService` directly calls `with open(file_path, "r")` and exposes no
file-backend interface — it cannot be retargeted to SFTP without forking. The
test suite in `tests/test_remote_text_editor.py` mirrors the upstream test
matrix (hash mismatch, overlap, beyond-EOF, multi-patch ordering, ...) and
adds SFTP-specific coverage (`posix_rename` fall-back, post-write rehash,
connection release on every exit path).
