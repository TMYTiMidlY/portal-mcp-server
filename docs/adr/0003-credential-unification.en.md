# 0003 — One in-process credential path; no detached subprocess for durability

Status: accepted

> 🌐 [中文版](./0003-credential-unification.md) ｜ English

Every connection portal makes goes through a single in-process asyncssh
authentication path. All credential kinds — SSH key, login password, key
passphrase, sudo password, named secret — are resolved on that path and handed
only to their real consumer (the asyncssh handshake, `sudo -S` on stdin, or an
injected environment variable); the plaintext never reaches the agent
conversation, a command line / `ps` argv, or disk. portal will NOT spawn a
detached subprocess (`nohup scp` / `rsync` / `ssh`) to make an operation outlive
the agent, because that subprocess cannot use this path.

## Context

Agents frequently want a transfer or command to "survive the agent stopping".
The obvious implementation is a detached `nohup rsync` / `scp` child process. But
portal's credential guarantees — a `password_command` fetched fresh at connect
time, the hosts.yaml ↔ ssh_config merge (see
[ADR-0002](0002-ssh-config-merge.md)), and "no password on argv" — only exist on
the in-process asyncssh path. A shelled-out `scp` / `rsync` would have none of
them: it would fall back to `sshpass`, putting the password on the argv
(`ps`-visible), or lose `password_command` / merge entirely.

## Considered options

- **Detached subprocess for durable ops** — rejected: bypasses the unified
  credential path and reintroduces exactly the argv / plaintext leakage the
  project exists to avoid.
- **HTTP transport to decouple from the stdio-client lifetime** — deferred *as
  the durability solution*: heavier to deploy and does not specifically solve
  durable transfer; a larger change than the problem warrants here. (An HTTP
  transport was later added for other reasons; it does not change this
  decision — durability still goes to `remote_job` / resume.)
- **Stay fully in-process; get durability elsewhere** — chosen. Durable work
  goes to `remote_job` (the command is `nohup`-ed on the REMOTE host, so the
  credential was already consumed at connect time and no local child needs to
  hold it); an interrupted upload recovers via `remote_transfer`'s `resume`.

## Consequences

- Foreground `remote_exec` / `remote_shell` / `remote_transfer` die with the
  agent / server by design — that is the documented exec-vs-job distinction, not
  a gap.
- "Durable" means `remote_job`; "recover an interrupted upload" means `resume`,
  not autonomous survival of a foreground transfer.
- The credential-unification invariant holds for every code path, which is what
  lets the project promise "plaintext never enters the LLM / argv / disk".
- The vocabulary — "Credential path", "Foreground / Background execution" — is
  defined in [`CONTEXT.md`](../../CONTEXT.md).
