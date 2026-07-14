# portal-mcp-server — Context

> 🌐 [中文版](./CONTEXT.md) ｜ English

Glossary for the MCP server that lets an AI agent drive remote hosts over SSH as
if they were local. Terms here are the project's canonical vocabulary; keep this
file a glossary only (no implementation detail).

## Tool families

The MCP tools fall into three families. The family decides the tool's name
prefix (see **Tool naming**).

**Remote tool** (remote data plane):
A tool that acts on a *remote host* over SSH — running commands, editing files,
searching, transferring, tunnelling, backgrounding. Named `remote_*`.
_Avoid_: "portal tool" (every tool is a portal tool; that's the server, not a family).

**Local execution**:
Running a command on the MCP server's *own* machine, not over SSH. The single
tool `local_exec`. Deliberately off by default and off-goal (the project's goal
is driving *remote* hosts).
_Avoid_: "remote local exec" (oxymoron — it does not use SSH).

**Control-plane tool** (introspection / management):
A tool that manages or inspects *portal itself* — its host registry, its policy,
its own runtime state — rather than executing anything on a host. `hosts`,
`policy_check`, `inspect`. Named with a plain descriptive noun/verb, no
`remote_` prefix (they don't act on a remote host).
_Avoid_: "audit tool" (too narrow — `inspect` also shows pool/session/server state).

## Tool naming

Tool names carry **no `portal_` prefix**. Every mainstream MCP client already
namespaces tools by the client-side config key (Copilot → `portal-<tool>`,
Claude/Codex → `mcp__portal__<tool>`, …), so a `portal_` prefix on the tool name
itself is redundant *stutter* (`portal-portal_exec`). See the five-client survey
recorded in [`docs/adr/0001-tool-naming-scheme.md`](docs/adr/0001-tool-naming-scheme.md).
Remote tools use a `remote_` prefix because it is *semantic* (marks "acts
on a remote host"), not a server-name echo.

## Host vocabulary

**Host name** (alias / identifier):
The string that identifies a host — the `hosts.yaml` key or the `~/.ssh/config`
`Host` line (e.g. `web01`). Always present; never carries surrounding
whitespace (trimmed on the way in). This is *not* the network address.
_Avoid_: using "host" ambiguously for both the identifier and the address.

**HostName** (dial address):
The network address actually connected to (IP / DNS), from hosts.yaml `host:` or
ssh_config `HostName`. A connection parameter, not an identifier.

**Merge** (hosts.yaml ↔ ssh_config):
Opt-in via `use_ssh_config: true`: the ssh_config alias is the base and the
hosts.yaml fields explicitly set override on top. Distinct from the default,
where a hosts.yaml host fully overrides ssh_config.

## Execution mode

**Foreground execution**:
Running a command synchronously — the tool call blocks until the command exits
(`remote_exec`, `remote_shell`, `local_exec`). The work runs inside the MCP
server process, so it ends when the agent (and thus the server) stops.
_Avoid_: calling this "a job" (a job is the background counterpart).

**Background execution**:
Running a command detached on the remote host (`remote_job`) via `nohup`: it
keeps running after the SSH connection drops or the agent stops, and is polled
for output. The durable counterpart to foreground execution.
_Avoid_: "async exec" (every tool is async at the transport level; this term is
about the *command* outliving the *call*).

## Credential path

**Credential path** (unified):
The single in-process asyncssh authentication route every connection goes
through. Every credential kind — SSH key, login password, key passphrase, sudo
password, named secret — is resolved on this one path, and the plaintext is
handed only to its real consumer (the asyncssh handshake, `sudo -S` stdin, an
injected env var); it never reaches the agent conversation, a command line, or
disk.
_Avoid_: treating a shelled-out `ssh` / `scp` / `sshpass` subprocess as
equivalent — it does not share this path (see
`docs/adr/0003-credential-unification.md`).
