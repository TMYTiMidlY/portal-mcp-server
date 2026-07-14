# 0003 — One in-process credential path; no detached subprocess for durability

Status: accepted

> 🌐 [中文版](./0003-credential-unification.md) ｜ English

Every connection portal makes goes through exactly one in-process asyncssh
authentication path. All credential kinds — SSH key, login password, key
passphrase, sudo password, named secret — are resolved on that path and handed
only to their real consumer: the asyncssh handshake, `sudo -S` on stdin, or an
injected environment variable. Nothing else sees them — the plaintext never
reaches the agent conversation, a command line / `ps` argv, or disk.

Precisely because that guarantee lives only in-process, portal will **not** spawn
a detached subprocess (`nohup scp` / `rsync` / `ssh`) to make an operation
outlive the agent: such a subprocess cannot use this path, and so cannot uphold
the guarantee.

## Context

Agents frequently want a transfer or command to "keep running after the agent
stops", and the obvious implementation is a detached `nohup rsync` / `scp` child
process. But portal's credential guarantees — a `password_command` fetched fresh
at connect time, the hosts.yaml ↔ ssh_config merge (see
[ADR-0002](0002-ssh-config-merge.md)), and "no password on argv" — exist **only**
on the in-process asyncssh path. A shelled-out `scp` / `rsync` gets none of them:
it can only fall back to `sshpass` (putting the password on the `ps`-visible
argv), or drop `password_command` and the merge entirely.

## Considered options

- **A detached subprocess for durable ops** — rejected. It bypasses the unified
  credential path and reintroduces exactly the argv / plaintext leakage the
  project exists to eliminate.
- **An HTTP transport to decouple from the stdio-client lifetime** — deferred *as
  the durability solution*: heavier to deploy and does not specifically solve
  durable transfer — a larger change than the problem warrants. (An HTTP
  transport was later added for other reasons; that does not change this
  decision — durability still goes to `remote_job` / resume.)
- **Stay fully in-process and get durability elsewhere** — chosen. Durable work
  goes to `remote_job`: the command is `nohup`-ed on the **remote** host, so the
  credential was already spent at connect time and no local child needs to hold
  it; an interrupted foreground transfer recovers via `remote_transfer`'s
  `resume`.

## Consequences

- Foreground `remote_exec` / `remote_shell` / `remote_transfer` end together with
  the agent / server **by design** — that is the documented exec-vs-job
  distinction, not a gap.
- "Durable" means `remote_job`; "recover an interrupted upload" means `resume` —
  neither implies a foreground transfer surviving on its own.
- The credential-unification invariant holds on every code path, which is what
  lets the project promise "plaintext never enters the LLM / argv / disk".
- The vocabulary — "Credential path", "Foreground / Background execution" — is
  defined in [`CONTEXT.md`](../../CONTEXT.md).
