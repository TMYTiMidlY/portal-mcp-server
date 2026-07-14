# 0002 — hosts.yaml ↔ ssh_config 合并是 opt-in，并带 HostName 不一致护栏

> 🌐 简体中文 ｜ [English](./0002-ssh-config-merge.en.md)

状态：已采纳（accepted）

解析一台主机时，`hosts.yaml` 与 `~/.ssh/config` **默认不合并**。合并按主机经
`use_ssh_config: true` 显式开启：ssh_config 的 `Host` 别名做基底，把显式设置的
`hosts.yaml` 字段叠在上面覆盖。如果 `hosts.yaml` 设了 `host:`（拨号地址 / HostName），
且该主机名是一个 ssh_config 别名，且两个 HostName 不一致，连接工具会**直接报错**而非
静默连接；`hosts(action=list)` 会把同一冲突作为 warning 暴露出来，便于诊断。

## 背景

asyncssh（2.23.0）按"你实际去连的那个 host"匹配 ssh_config 的 `Host` 段，且每个选项按
"显式 kwarg，否则取 config 值"解析（见
[`asyncssh/config.py`](https://github.com/ronf/asyncssh/blob/v2.23.0/asyncssh/config.py)
与 [`asyncssh/connection.py`](https://github.com/ronf/asyncssh/blob/v2.23.0/asyncssh/connection.py)）。
要继承某别名的长尾选项（`IdentityAgent`、`ProxyJump`、keepalive…），就必须以
`host=<别名>` 去连，让 asyncssh 匹配那个 `Host` 段——但这样 `HostName` 就由 ssh_config
定死，`hosts.yaml` 的 `host:` 再也覆盖不了它（其余字段仍可覆盖）。"既继承别名选项、又拨
一个不同的 HostName"在单次连接里因此不可兼得。

## 考虑过的方案

- **总是静默合并**——否决：太意外。与别名 HostName 不一致的 `hosts.yaml` `host:` 会被
  静默忽略，连到错误地址而无任何信号。
- **从不合并（纯 passthrough，只用 hosts.yaml）**——否决：无法继承别名的长尾选项，逼
  你在 hosts.yaml 里重新声明 `IdentityAgent` / `ProxyJump` / …。
- **opt-in 合并 + HostName 不一致就报错**——选中：默认行为不变（非破坏性），opt-in 换来
  选项继承，而模型唯一真正无法表达的那种情况（HostName 冲突）会响亮且可诊断地失败，而
  非静默连错地址。

## 后果

- 对不设 `use_ssh_config` 的人，默认解析不变。
- 手挑的那几个 ssh 选项字段（`proxy_jump`、`keepalive_interval`、`forward_agent`、
  `use_ssh_agent`）仍作为显式覆盖保留，供没有 ssh_config 别名的纯 hosts.yaml 主机使用。
- HostName 冲突在连接时是硬错误（且列表里有 warning），而非静默连错地址。
- 词汇——"合并（Merge）"、"主机名（Host name）"vs"HostName"——定义在
  [`CONTEXT.md`](../../CONTEXT.md)。
