# 0002 — hosts.yaml ↔ ssh_config merge is opt-in, with a HostName-mismatch guard

Status: accepted

> 🌐 [中文版](./0002-ssh-config-merge.md) ｜ English

When a host is resolved, `hosts.yaml` and `~/.ssh/config` are NOT merged by
default. Merging is opt-in per host via `use_ssh_config: true`: the ssh_config
`Host` alias becomes the base and the explicitly-set `hosts.yaml` fields override
on top. If `hosts.yaml` sets `host:` (the dial address / HostName) AND the host
name is an ssh_config alias AND the two HostNames differ, the connection tools
hard-error instead of silently connecting; `hosts(action=list)`
surfaces the same conflict as a warning so it stays diagnosable.

## Context

asyncssh (2.23.0) matches an ssh_config `Host` pattern against the host you
actually connect to, and resolves each option as "explicit kwarg, else config
value" (see [`asyncssh/config.py`](https://github.com/ronf/asyncssh/blob/v2.23.0/asyncssh/config.py)
and [`asyncssh/connection.py`](https://github.com/ronf/asyncssh/blob/v2.23.0/asyncssh/connection.py)).
To inherit an alias's long-tail options (`IdentityAgent`, `ProxyJump`,
keepalives, …) you must connect with `host=<alias>` so asyncssh matches that
`Host` block — but then `HostName` is pinned by ssh_config and a `hosts.yaml`
`host:` can no longer override it (every OTHER field still can). "Inherit the
alias's options AND dial a different HostName" is therefore not expressible in a
single connection.

## Considered options

- **Always merge, silently** — rejected: surprising. A `hosts.yaml` `host:` that
  disagrees with the alias's HostName would be silently ignored, connecting to
  the wrong address with no signal.
- **Never merge (pure passthrough, hosts.yaml only)** — rejected: can't inherit
  an alias's long-tail options; forces re-declaring `IdentityAgent` / `ProxyJump`
  / … in hosts.yaml.
- **Opt-in merge + hard-error on HostName mismatch** — chosen: default behavior
  is unchanged (non-breaking), opting in buys option inheritance, and the one
  case the model genuinely can't express (conflicting HostName) fails loudly and
  diagnosably rather than silently.

## Consequences

- Default resolution is unchanged for anyone not setting `use_ssh_config`.
- The hand-picked ssh-option fields (`proxy_jump`, `keepalive_interval`,
  `forward_agent`, `use_ssh_agent`) stay as explicit overrides, needed for
  pure-hosts.yaml hosts that have no ssh_config alias.
- A conflicting HostName is a hard error on connect (and a listed warning), not a
  silent wrong-address connection.
- The vocabulary — "Merge", "Host name" vs "HostName" — is defined in
  [`CONTEXT.md`](../../CONTEXT.md).
