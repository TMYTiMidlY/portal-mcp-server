# 0001 — Tool naming: drop `portal_`, prefix remote tools with `remote_`

Status: accepted

> 🌐 [中文版](./0001-tool-naming-scheme.md) ｜ English

MCP tool names drop the `portal_` prefix. Tools that act on a remote host are
prefixed `remote_` (`remote_exec`, `remote_shell`, `remote_read`,
`remote_patch`, `remote_grep`, `remote_glob`, `remote_transfer`,
`remote_tunnel`, `remote_job`, `remote_close`); the local-execution tool is
`local_exec`; control-plane tools take a plain descriptive name with no prefix
(`hosts`, `policy_check`, `inspect`). No backward-compatible aliases are kept —
this is a hard cutover shipped as a major version.

## Context

Every mainstream MCP client already namespaces tools by the client-side config
key (Copilot → `portal-<tool>`, Claude Code / Codex → `mcp__portal__<tool>`,
Gemini → `mcp_portal_<tool>`, Cursor → `portal-<tool>`), so a `portal_` prefix on
the tool name itself was redundant *stutter* — `portal-portal_exec`. The
five-client survey behind this is recorded in the [`CONTEXT.en.md`](../../CONTEXT.en.md)
tool-naming section.

## Considered options

- **Keep `portal_`** — rejected: the stutter is the whole problem.
- **All bare names** (`exec`, `read`, `shell`, …) — rejected: too generic and
  collision-prone on any client that does *not* namespace, and loses the
  "acts on a remote host" signal.
- **All `remote_`** including the control-plane tools — rejected:
  `hosts` / `policy_check` / `inspect` don't act on a remote host, so
  `remote_hosts` would misrepresent them.

## Consequences

- Breaking change for anyone referencing the old tool names in configs/prompts
  → shipped in the 4.0.0 line (first released as the `4.0.0a0` alpha).
- The `remote_` prefix is *semantic* (marks the remote data plane), not a
  server-name echo, so it survives the client's own namespacing without
  re-introducing stutter.
