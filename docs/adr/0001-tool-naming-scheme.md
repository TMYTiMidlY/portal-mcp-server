# 0001 — 工具命名：去掉 `portal_`，远端工具加 `remote_` 前缀

> 🌐 简体中文 ｜ [English](./0001-tool-naming-scheme.en.md)

状态：已采纳（accepted）

MCP 工具名去掉 `portal_` 前缀。作用于远端主机的工具加 `remote_` 前缀（`remote_exec`、
`remote_shell`、`remote_read`、`remote_patch`、`remote_grep`、`remote_glob`、
`remote_transfer`、`remote_tunnel`、`remote_job`、`remote_close`）；本机执行工具是
`local_exec`；控制面工具用朴素描述名、不加前缀（`hosts`、`policy_check`、`inspect`）。
不保留任何向后兼容别名——这是一次硬切换，随主版本发布。

## 背景

所有主流 MCP client 本就按客户端侧配置 key 给工具加命名空间（Copilot →
`portal-<tool>`，Claude Code / Codex → `mcp__portal__<tool>`，Gemini →
`mcp_portal_<tool>`，Cursor → `portal-<tool>`），所以在工具名本身再加 `portal_` 前缀是
冗余的**口吃**——`portal-portal_exec`。背后的五客户端调研记录在
[`CONTEXT.md`](../../CONTEXT.md) 的"工具命名"一节。

## 考虑过的方案

- **保留 `portal_`**——否决：口吃正是问题本身。
- **全用裸名**（`exec`、`read`、`shell`…）——否决：太通用、在不做命名空间的 client 上
  易撞名，且丢掉"作用于远端主机"的信号。
- **全加 `remote_`（含控制面工具）**——否决：`hosts` / `policy_check` / `inspect` 不
  作用于远端主机，`remote_hosts` 会误导它们。

## 后果

- 对任何在配置 / prompt 里引用旧工具名的人是破坏性变更 → 随 4.0.0 系列发布（首个版本
  为 `4.0.0a0` 预发布）。
- `remote_` 前缀是**语义性**的（标明远端数据面），不是 server 名回声，所以能在 client
  自己的命名空间之下存活、不重新引入口吃。
