# Security Policy

> Reports written in Chinese are welcome — submit them to GitHub Security
> Advisories in any language you're comfortable with; the maintainer will
> reply in kind.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Use [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new)
to report privately. We aim for:

- **Acknowledgement** within 48 hours
- **Initial assessment** within 7 days
- **Critical fixes** shipped within 30 days

If you cannot use GitHub Security Advisories, contact the maintainer
through their GitHub profile.

When you report, please include:

- A clear description of the vulnerability
- Steps to reproduce (proof-of-concept welcome)
- Potential impact
- Any mitigations you have in mind

## Supported versions

| Version       | Supported              |
|---------------|------------------------|
| `main` branch | ✅ Active maintenance  |
| Older tags    | ❌ No back-ported fixes|

---

## Security model

`portal-mcp-server` is an MCP server that gives an LLM agent
programmatic SSH access to remote hosts. The threat model assumes the
agent is **semi-trusted** — it follows instructions from the human
operator but may make mistakes, hallucinate paths, or be steered by
prompt-injection content read from the remote.

The defences below are layered:

| Layer                 | Where                          | What it does                                                                 |
|-----------------------|--------------------------------|------------------------------------------------------------------------------|
| Prompt-layer skill    | The companion `remote` skill   | Tells the agent to default writes to remote `/tmp/` and to ask before touching `$HOME` or project source |
| Server-side policy    | `config/policies.yaml`         | Host allowlist, command blocklist / allowlist, per-host rate limit           |
| Per-tool gate         | `cli.py:_gate*`                | Every state-changing tool runs the policy on every call                      |
| Hash-protected edits  | `portal_read` + `portal_patch` | SHA-256 conflict detection refuses concurrent overwrites                     |
| Atomic write          | `portal_patch`                 | Tmp file + `posix_rename` + post-write rehash                                |
| Audit log             | `logs/audit.jsonl`             | Every state-changing op recorded; fail-closed by default                     |
| Key-only auth         | `connection_manager.py`        | Password fields are rejected and logged at ERROR                             |
| Strict host-key check | `connection_manager.py`        | Defaults to OpenSSH-equivalent `StrictHostKeyChecking`                       |

### Default constraint: sandbox `/tmp/`

`portal-mcp-server` does not enforce a path allowlist itself — that is
the companion `remote` skill's job at the prompt layer:

> **Default writes go to remote `/tmp/`. The agent must ask before
> touching `$HOME` or project source directories.**

For machine-level enforcement, add explicit rules to
`command_blocklist` in `config/policies.yaml` (e.g.
`"rm -rf /home/*"`).

### The policy gate

`SecurityPolicy` enforces:

- **Host allowlist** — fnmatch patterns; empty list = all hosts allowed
- **Command blocklist** — fnmatch patterns matched case-insensitively
- **Command allowlist** — if non-empty, commands must match at least one
- **Per-host rate limit** — sliding-window, default 10 req/s per host

Every state-changing entry point runs the gate; there are no side doors:

- `portal_host(action="register")` gates against the **target host**
  (the actual IP / DNS the connection will reach), so an agent cannot
  launder a non-allowlisted target through an alias whose name happens
  to match `safe-*`. `action="remove"` gates against the alias.
- `portal_tunnel_open` and `portal_tunnel_close` both gate the
  originating host — the close path resolves it from the active-tunnel
  record before tearing the listener down.
- `portal_bash` and `portal_bash_close` both gate the host (and the
  bash command, for `portal_bash`) — a persistent shell is **not** a
  blanket authorisation for arbitrary commands.
- Multi-host gates (`portal_multi_exec`, `portal_playbook` group path)
  are **two-phase**: every host is validated first, only then are
  per-host rate-limit tokens consumed. A single failing host cannot
  burn quota on the others.

### Authentication

**Key-based authentication only.** `HostConfig` has no `password`
field; `portal_host(action="register", ...)` has no `password`
parameter. Stale `password:` keys in `hosts.yaml` are detected at
startup, logged at ERROR level, and silently ignored.

### Audit log

All state-changing tools write `logs/audit.jsonl`:

- `exec` / `file write` / `patch` / `register` / `tunnel` / `playbook`
  / multi-host orchestration

Read-only tools — `portal_read`, `portal_grep`, `portal_glob`,
`portal_audit`, `portal_check`, `portal_tunnel_list` — explicitly do
**not** audit, to keep the log signal-rich.

The audit subsystem is **fail-closed by default**: if writing to disk
fails, the operation raises and aborts. Set
`SSH_MCP_AUDIT_FAIL_OPEN=1` to switch to fail-open behaviour (warning
only — appropriate for dev / test, not production).

> ⚠️ **Honest disclosure on fail-closed semantics.** Audit entries are
> written *after* the underlying operation completes (we need its
> result to know what to log). So if the disk write fails right after a
> successful operation, the agent sees a `RuntimeError` even though
> the remote patch / exec / register has already happened.
> `Fail-closed` prevents *subsequent* operations; it cannot roll back
> the one that just succeeded. If you need strict transactional
> auditing, fan out to an OS-level facility (`rsyslog`, central log
> collector) downstream.

### Hash-protected file editing

`portal_read` returns whole-file SHA-256 plus per-range SHA-256.
`portal_patch` requires the same `file_hash` (and per-patch
`range_hash`); if the file changed in the meantime, the patch is
rejected and the file is left untouched. Hashes are compared with
`hmac.compare_digest` (constant time) to remove timing-side-channel
risk on the `range_hash` check.

Patches are applied bottom-to-top so line numbers stay valid;
overlapping patches are rejected; writes go through a tmp file +
`posix_rename` (atomic on POSIX) and are re-hashed after the rename
to guarantee the on-disk state matches what was written.

### Algorithmic provenance

The hash-protected edit semantics in
`portal_mcp_server/remote_text_editor.py` are a deliberate port of the
safe-edit pattern from
[tumf/mcp-text-editor](https://github.com/tumf/mcp-text-editor) (MIT,
Copyright (c) 2024 tumf). No source code was copied; the
implementation is original and targets AsyncSSH SFTP. The diff:

| Upstream (`mcp-text-editor`)                             | Here (`remote_text_editor`)                              |
|----------------------------------------------------------|----------------------------------------------------------|
| Whole-file SHA-256 conflict detection                    | Same algorithm, runs over SFTP                           |
| Line-range patch model                                   | Same model, plus per-patch `range_hash`                  |
| Single-shot file overwrite                               | Replaced with tmp file + `posix_rename` (atomic)         |
| Local `open(...)` + `portalocker` advisory lock          | Replaced with AsyncSSH SFTP + connection-pool release    |

The upstream library is **not** a Python dependency: its
`TextEditorService` calls `open(file_path, ...)` directly and exposes
no file-backend interface — it cannot be retargeted to SFTP without
forking. The test suite in `tests/test_remote_text_editor.py` mirrors
the upstream test matrix (hash mismatch, overlap, beyond-EOF,
multi-patch ordering …) and adds SFTP-specific coverage
(`posix_rename` fall-back, post-write rehash, connection release on
every exit path).

---

## Operator hygiene

- Keep SSH private keys at `chmod 600`. Never commit `hosts.yaml` or
  any file containing real hostnames, usernames, or key paths.
- Run remote targets behind a VPN (e.g. Tailscale) where possible. The
  MCP server itself only speaks `stdio`; it opens no network ports
  unless the optional HTTP transport is enabled.
- Create dedicated SSH users for automated access; restrict them with
  `sshd_config`'s `AllowUsers`, `Match`, or `ForceCommand` rather than
  using `root` or personal accounts.
- Review `policies.yaml` allowlists and blocklists periodically — the
  default policy is **permissive** (empty allowlists = all allowed).
- Keep `logs/audit.jsonl` rotated and shipped off-host; the file is
  the only forensic record of what the agent did.

## Known limitations

- Password-based SSH authentication is not supported by design.
- Host key verification uses the system `known_hosts` by default;
  disabling it via `strict_host_key_checking: false` weakens MITM
  protection and is logged at WARNING for that reason.
- The audit log is best-effort with respect to operations that
  succeeded *before* the audit write failed — see the "fail-closed
  semantics" disclosure above.
- The default rate limit is per-host, not per-user or per-credential;
  if you need finer-grained quotas, drive the policy from an external
  policy engine.
