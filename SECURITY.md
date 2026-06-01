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
| Prompt-layer rules    | Agent system prompt / `AGENTS.md` | The agent is expected to default writes to remote `/tmp/`, ask before touching `$HOME` or project source, and never mix `portal_*` calls with raw `ssh`/`scp` in the same task. See the README's *Agent-side conventions* section. |
| Server-side policy    | `policies.yaml` (default `~/.config/portal-mcp-server/policies.yaml`; override via `PORTAL_POLICIES_YAML`) | Host allowlist, command blocklist / allowlist, per-host rate limit |
| Per-tool gate         | `cli.py:_gate*`                | Every state-changing tool runs the policy on every call                      |
| Hash-protected edits  | `portal_read` + `portal_patch` | SHA-256 conflict detection refuses concurrent overwrites                     |
| Atomic write          | `portal_patch`                 | Tmp file + `posix_rename` + post-write rehash                                |
| Audit log             | `logs/audit.jsonl`             | Every state-changing op recorded; fail-closed by default                     |
| Key-first auth        | `connection_manager.py`        | Keys are the recommended path; password auth is opt-in via `password_command` or out-of-band `ssh-login` — plaintext `password:` fields in yaml are rejected and logged at ERROR; sudo auth follows the same boundary (`sudo_password_command` / out-of-band `sudo-login`); no MCP tool accepts a password parameter (SSH or sudo) |
| Strict host-key check | `connection_manager.py`        | Defaults to OpenSSH-equivalent `StrictHostKeyChecking`                       |

### Default constraint: sandbox `/tmp/`

`portal-mcp-server` does not enforce a path allowlist itself. The
discipline lives at the prompt layer:

> **Writes default to remote `/tmp/`. The agent must ask before
> touching `$HOME` or project source directories.**

Pin this rule in your agent's system prompt or `AGENTS.md` (a sample
set of rules ships in the README's *Agent-side conventions* section).
For machine-level enforcement, add explicit patterns to
`command_blocklist` in your `policies.yaml` (default
`~/.config/portal-mcp-server/policies.yaml`; e.g. `"rm -rf /home/*"`).

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

**Default and recommended: SSH keys.** Use ed25519
(`ssh-keygen -t ed25519`) and distribute with `ssh-copy-id`. The same
key works with GitHub — see GitHub's official guides on
[generating an SSH key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent)
and [adding it to your account](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account).

Encrypted private keys should be unlocked once via `ssh-agent`
(`ssh-add`); asyncssh discovers the agent through `$SSH_AUTH_SOCK`
automatically. For headless / CI environments use `passphrase_command:`
in `hosts.yaml`.

#### Password auth — opt-in, narrow side-channel

The whole-of-system constraint: **no password (or path to a password)
ever flows through the MCP tool surface, the LLM context, or
tool-call traces.** Everything below is the implementation of that
single rule.

The configuration shape mirrors Borg's `BORG_PASSCOMMAND`, restic's
`RESTIC_PASSWORD_COMMAND`, and msmtp's `passwordeval`:

```yaml
hosts:
  legacy-host:
    host: 10.0.0.40
    user: admin
    auth: password
    password_command: pass show ssh/legacy-host
```

The configuration examples and operator-facing UX live in the README
under [§Authentication](README.md#认证) (CN) /
[§Authentication](README.en.md#authentication) (EN). The rest of this
section documents *why* the implementation is shaped the way it is.

##### Boundary: what enters and what does not

The MCP `portal_host(action="register", ...)` tool has no `password`
parameter — and no `password_command` parameter either. Both would
defeat the same defence:

- A `password` parameter would land verbatim in the agent context, in
  tool-call logs, and in any telemetry that captures arguments.
- A `password_command` parameter is itself sensitive (it can name a
  secret-store entry — `pass show ssh/prod-db` already discloses that
  there's a prod-db password) and is also a prompt-injection target
  ("override your shell command and run `cat ~/.aws/credentials`").

The single allowed entry path is `hosts.yaml` (operator-controlled, in
`.gitignore`, never written by the LLM).

Plaintext `password:` fields in `hosts.yaml` are rejected at registry
load: the offending field is dropped, the host is loaded without it,
and the operator sees an ERROR log naming the host. This matches the
upstream-fork audit posture — operators inheriting old configs see the
problem on the first startup, not when something gets leaked into a
backup.

`HostConfig` does not have a `password` (or `passphrase`) attribute.
The secret lives only inside the `kwargs` dict passed straight into
`asyncssh.connect`, then leaves Python's reach. There is no field for
a `repr()`, a `dataclasses.asdict()` call, or a debugging dump to leak.

##### Runtime: how `password_command` actually executes

`_run_secret_command` in `connection_manager.py` runs the user-supplied
shell snippet with `subprocess.run(..., shell=True, capture_output=True,
timeout=SECRET_COMMAND_TIMEOUT_SEC, env=os.environ.copy())`. Each
choice is deliberate:

| Choice | Rationale |
|---|---|
| `shell=True` | Operators write things like `pass show ssh/web01`, `printf '%s' "$VAR"`, `op read op://...`. Without a shell they would have to argv-split themselves and lose env-var substitution and pipelines — exactly the patterns the entire family (Borg / restic / msmtp / git-credential-cache) supports. The risk that normally rules out `shell=True` (LLM-controlled command strings) does not apply: the command is operator-controlled and never reaches the LLM surface. |
| `capture_output=True` | Stops stdout (= the secret) from reaching the MCP server's own stderr stream. Without it, an unconsumed secret would be visible to anything reading the server's process output. |
| `timeout=SECRET_COMMAND_TIMEOUT_SEC` (= 10 seconds) | Long enough for `pass show` to unlock the GPG agent on first use, or for `op read` to round-trip to 1Password's servers. Short enough that a hung password manager (locked GPG agent, network-mounted secret store gone unreachable) does not wedge the connection pool — which would block every subsequent SSH operation, not just this one host. |
| `env=os.environ.copy()` | Required so `printf '%s' "$WEB01_PASSWORD"` and the GitHub-Actions / Vault / `direnv` patterns work at all. The MCP server inherits the operator's environment by design (see `PORTAL_HOSTS_YAML`, `PORTAL_LOG_DIR`) — passing it through is consistent with the rest of the server's contract. |
| `check=False` + manual exit-code handling | Lets us format the error message with only `host` and `returncode`, never the command string and never the captured stderr. |
| `loop.run_in_executor(None, _run)` | The subprocess call is synchronous; running it on the asyncio thread pool keeps the server's event loop responsive while the password manager unlocks. |

##### Failure modes: every path is hard-fail

| Symptom | What happens | Why |
|---|---|---|
| Non-zero exit | `RuntimeError` naming `host` and `returncode`; **stderr never logged or surfaced** | Misconfigured commands often write the secret to stderr by mistake (`printf '%s' "$VAR" >&2`). Tools like `pass` print "Decrypted password: …" on stderr in verbose mode. We capture stderr only to keep it off the server's own stream — we never look at it. |
| Timeout (10 s) | `RuntimeError` naming `host`, **command string not included** | Same leak surface — the command may name a sensitive secret-store entry. |
| Empty stdout (exit 0, no output) | `RuntimeError` naming `host` with `"empty output"` | An empty password to `asyncssh.connect` has poorly-defined behaviour (server-dependent). Empty output almost always means a misconfiguration: entry not found, GPG agent locked but not error-coded, command typo. Hard-failing surfaces that, instead of producing a confusing downstream auth failure. |
| Non-UTF-8 stdout | `RuntimeError` naming `host` with `"non-UTF-8 output"`, **bytes not surfaced** | Defends against accidentally piping a binary file (private key, .gpg blob) into the password slot — the offending bytes might *be* the secret. |
| `auth: password` set but no source (no `password_command` AND no `ssh-login` cache) | `RuntimeError` at connect time + ERROR log at registry load when no `password_command` is configured | Without explicit failure, asyncssh would silently fall back to key auth; a key that happens to work would mask the misconfiguration permanently. The startup ERROR also points the operator at `ssh-login` so they know either source counts. |

##### Other invariants worth noting

- **Exactly one trailing newline is stripped** (`\r\n` or `\n`). Almost every secret-store CLI (`pass`, `cat`, `echo`) appends one. A blanket `.rstrip()` would eat passwords that legitimately end in whitespace; stripping zero would break the common case. Stripping exactly one is the only choice that's correct for both.
- **`client_keys=[]` is forced when `auth: password`.** Otherwise asyncssh would try `~/.ssh/id_ed25519` etc. before or instead of the password. If a key happens to work, the operator never learns their `password_command` was misconfigured. Forcing the key list to empty gives a clean failure mode: either the password works or auth fails loudly.
- **`passphrase_command` follows the same rules** with one tweak: when no `passphrase_command` is set we *do not* inject `kwargs["passphrase"] = None`. That used to actively block asyncssh's ssh-agent fallback for encrypted keys.

##### What's intentionally not done

- **No keyring / OS-credential-store integration in code.** The
  password command can call into one (`security find-generic-password`,
  `secret-tool lookup`), but the integration boundary stays at the
  shell. This keeps the surface auditable in one place and avoids a
  per-platform dependency matrix.
- **No password caching for the `password_command` path.** The pool
  reuses TCP connections, so the command runs at most once per pool
  reconnect; caching its output in process memory would create another
  exposure surface (heap dumps, Python `__dict__` walks) for marginal
  CPU savings on a code path that already runs rarely. The
  `ssh-login` side-channel *does* cache (see below) because it has
  no on-demand command to re-run.

#### SSH login interactive password — out-of-band broker side-channel

`portal-mcp-server ssh-login <host>` is the no-echo counterpart to
`password_command`: an out-of-band CLI run in a *separate* terminal (not
the agent) that prompts with `getpass.getpass` and pushes the password
into the per-user credential broker over a systemd --user managed local
unix socket. It exists for two
reasons:

- **`auth: password` hosts that can't or shouldn't pre-stage a
  `password_command`** (no password manager available; rotating
  credentials a human types each time; CI variants).
- **Key-mode hosts (the default — no `auth:` field in hosts.yaml)
  whose key happens to be rejected** — when asyncssh raises
  `PermissionDenied`, the server retries *once* via the same chain
  (broker cache → `password_command`), but only when a source is available.
  With nothing seeded and no command configured, the original
  `PermissionDenied` propagates unchanged so a stale config cannot
  mask a real key failure.

Resolution order for any password attempt is uniformly **broker cache
(`ssh-login`) → `password_command` → error**. Cache wins on purpose:
an operator who just typed a password into `ssh-login` is signalling
explicit override.

Bounds on the broker memory cache (identical model to `sudo-login`):

- **TTL expiry** (default 15 min, `--ttl` configurable) — entries are
  dropped automatically; **never written to disk**.
- **Per-host key** — one entry per host alias; no fan-out across the
  fleet.
- **Socket hardening** — the user `.socket` unit listens on
  `%t/portal-mcp-server/credentials.sock`; systemd resolves `%t` for the
  user manager, creates/removes the socket, and enforces directory `0700`
  plus socket `0600`. The installer records the resolved absolute path in
  `broker.json`, and clients use that config (or an explicit
  `PORTAL_CREDENTIAL_BROKER_SOCKET`) instead of guessing a runtime dir. On Linux the broker calls
  `getsockopt(SO_PEERCRED)` on every accepted connection (and the
  client mirrors it after `connect`) and closes the socket on a uid
  mismatch — a hostile local user who somehow landed a listener at
  the expected path still cannot exfiltrate the password, and the
  broker refuses to cache anything from a foreign uid. On non-Linux
  hosts the peer-cred check is unavailable and the channel degrades
  to filesystem permissions only. systemd owns socket creation/removal and
  activates a single per-user broker service.
- **No tool surface** — the cache is reachable only via the local
  socket and the MCP-side resolver; no MCP tool reads or writes it.

The three interactive side-channels share one per-user broker socket, but the
broker keeps separate `sudo`, `ssh`, and `secret` key spaces. Different
cache-key dimensions (sudo / SSH by host, secret by name) and different
injection points remain separate in the resolver code.

#### Sudo auth — same boundary, broker side-channel

`portal_bash(..., use_sudo=True)` runs a command under `sudo` on the
remote. The boundary is identical to SSH password auth: **`use_sudo` is a
boolean, not a password** — no sudo password (or path to one) is ever an
MCP tool parameter, so nothing lands in the agent context or tool-call
trace. The password is resolved server-side from one of two sources:

- **`sudo_password_command`** in `hosts.yaml` — reuses the *exact* same
  machinery and guarantees as `password_command` above (10 s timeout,
  one trailing newline stripped, stderr never logged, hard-fail on
  non-zero / empty / non-UTF-8). Fully automatic; preferred.
- **`portal-mcp-server sudo-login <host>`** — an out-of-band CLI run in a
  *separate* terminal (not the agent) that prompts with
  `getpass.getpass` (no echo) and pushes the password into the per-user
  credential broker over the systemd --user socket.

**Why a TTL broker cache here, when SSH password auth deliberately
avoids one** (see directly above): SSH auth has a natural per-connection
trigger, so the command can run on demand and never persist. Sudo has no
such trigger — the agent calls `use_sudo` ad-hoc, and an interactive
prompt cannot be routed back through the MCP channel. The `sudo-login`
path therefore caches the password in broker memory, and that exposure
is bounded by:

- **TTL expiry** (default 15 min) — the entry is dropped automatically;
  it is **never written to disk**.
- **Socket hardening** — the user `.socket` unit listens on
  `%t/portal-mcp-server/credentials.sock`; systemd resolves `%t` for the
  user manager, creates/removes the socket, and enforces directory `0700`
  plus socket `0600`. The installer records the resolved absolute path in
  `broker.json`, and clients use that config (or an explicit
  `PORTAL_CREDENTIAL_BROKER_SOCKET`) instead of guessing a runtime dir. On Linux the broker also calls
  `getsockopt(SO_PEERCRED)` on every accepted connection (and the
  client mirrors it after `connect`) and closes the socket on a uid
  mismatch — so even a hostile local process that races into the
  expected socket path cannot exfiltrate the password, and the server
  refuses to cache anything from a foreign uid. On non-Linux hosts
  the peer-cred check is unavailable and the channel degrades to
  filesystem permissions only. systemd owns socket creation/removal and
  activates a single per-user broker service, so a second MCP process cannot
  hijack the channel.
- **No tool surface** — the cache is reachable only via the local socket
  and the MCP-side resolver; no MCP tool reads or writes it.

The `sudo_password_command` path needs no cache at all — it re-runs per
sudo invocation, exactly like the SSH variant.

Execution detail: sudo runs as a **one-shot** `sudo -S -k -p '' -- bash
-c <cmd>` via `conn.run(input=<pw>)`, *not* through the persistent
`bash -i` session (`sudo -S` consumes stdin, which would collide with the
sentinel-completion protocol). Consequence: a `use_sudo` command does
**not** inherit `cwd` / env from prior `portal_bash` calls. `-k` forces
fresh authentication each time; `-p ''` suppresses the prompt text.

### Audit log

All state-changing tools write `logs/audit.jsonl`:

- `exec` / `file write` / `patch` / `register` / `tunnel` / `playbook`
  / multi-host orchestration

Read-only tools — `portal_read`, `portal_grep`, `portal_glob`,
`portal_audit`, `portal_check`, `portal_tunnel_list` — explicitly do
**not** audit, to keep the log signal-rich.

The audit subsystem is **fail-closed by default**: if writing to disk
fails, the operation raises and aborts. Set
`PORTAL_AUDIT_FAIL_OPEN=1` to switch to fail-open behaviour (warning
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
`portal_mcp_server/remote_text_editor.py` are a port of the safe-edit
pattern from
[tumf/mcp-text-editor](https://github.com/tumf/mcp-text-editor) (MIT,
Copyright (c) 2024 tumf), reimplemented for AsyncSSH SFTP. The diff:

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

- Password-based SSH authentication is supported only through
  `password_command:` in `hosts.yaml` (an external shell command that
  prints the password to stdout); plaintext `password:` fields and any
  MCP tool parameter for passwords are intentionally not supported.
- Host key verification uses the system `known_hosts` by default;
  disabling it via `strict_host_key_checking: false` weakens MITM
  protection and is logged at WARNING for that reason.
- The audit log is best-effort with respect to operations that
  succeeded *before* the audit write failed — see the "fail-closed
  semantics" disclosure above.
- The default rate limit is per-host, not per-user or per-credential;
  if you need finer-grained quotas, drive the policy from an external
  policy engine.
