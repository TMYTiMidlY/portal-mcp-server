<div align="center">

# portal-mcp-server

**面向 coding agent 的 SSH orchestration MCP server**

让 Claude Code、Copilot CLI、Cursor 等 agent 操作远端机器就像操作本地：持久 bash 会话、hash 保护的远端文件编辑、SFTP 文件传输、SSH 隧道、多机编排。基于 [AsyncSSH](https://github.com/ronf/asyncssh) + [FastMCP](https://modelcontextprotocol.io/)，连接池在 server 进程内跨工具复用，Windows / macOS / Linux 性能一致。

[![CI](https://github.com/TMYTiMidlY/portal-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/TMYTiMidlY/portal-mcp-server/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/portal-mcp-server)](https://pypi.org/project/portal-mcp-server/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen)](https://modelcontextprotocol.io/)
[![Last commit](https://img.shields.io/github/last-commit/TMYTiMidlY/portal-mcp-server)](https://github.com/TMYTiMidlY/portal-mcp-server/commits/main)
[![Issues](https://img.shields.io/github/issues/TMYTiMidlY/portal-mcp-server)](https://github.com/TMYTiMidlY/portal-mcp-server/issues)

简体中文 ｜ [English](./README.en.md)

</div>

---

<details>
<summary>📖 目录</summary>

- [简介](#简介)
- [项目特色](#项目特色)
- [为什么用 portal-mcp-server：和传统方案的对比](#为什么用-portal-mcp-server和传统方案的对比)
- [快速开始](#快速开始)
- [架构](#架构)
- [工具列表](#工具列表)
- [设计理念](#设计理念)
- [安装](#安装)
- [接入方式](#接入方式)
- [环境变量](#环境变量)
- [认证](#认证)
- [安全](#安全)
- [测试](#测试)
- [CI / Release](#ci--release)
- [常见问题](#常见问题)
- [贡献](#贡献)
- [协议与致谢](#协议与致谢)

</details>

## 简介

`portal-mcp-server` fork 自 [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0）：底层 SSH/asyncssh 引擎、连接池、tunnel 管理、多机编排算法、安全策略沿用上游模块。上层重新设计了一套面向 agent 的 `portal_*` 工具——围绕"持久 bash 会话 + 一次性 exec + 后台 job"三条执行路径，外加 hash 保护的远端文件编辑、结构化搜索、SFTP 传输、隧道、审计等原语。其中远端文件编辑（`portal_read` / `portal_patch`）的双层 hash 校验算法参考 [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT），并针对 SFTP 重写。

完整衍生关系与算法引用见 [`NOTICE`](./NOTICE) 与 [安全](#安全) 章节。

## 项目特色

- **跨工具连接复用**：所有 `portal_*` 工具共享同一进程内的 asyncssh 连接池；一次握手长期复用，单次调用摊销到 channel 创建（~10–30 ms）。
- **Windows 上同样快**：不依赖 OpenSSH `ControlMaster`，连接池是纯 Python 对象，三大平台获得一致的复用性能。
- **持久 shell 会话**：`portal_shell` 为每台 host 维护一个交互式 shell（bash/zsh），cwd / env 跨调用保留，还能用 `commands=[…]` 在同一会话里顺序跑多步；agent 不需要每条命令重建上下文。
- **hash 保护的远端编辑**：`portal_read` + `portal_patch` 用整文件 SHA-256 + 行范围 hash 双层校验，写入走 tmp + `posix_rename` 原子替换，写后再 hash 校验，杜绝并发覆盖。
- **agent-first 的精简工具面**：`action` / `mode` 字段合并语义重复的入口，每个工具只提供一条 bash 难以廉价合成的保证，减少 agent 选工具的歧义。工具 schema（name + description + inputSchema）合计约占 **~8k tokens**（≈ 200k 上下文窗口的 **~4%**；`tiktoken o200k_base` 估算）。
- **内建安全策略**：host allowlist、command blocklist/allowlist（fnmatch）、per-host rate limit、所有改状态操作落 audit log，默认 fail-closed；可选接入 [cc-safety-net](https://github.com/kenryu42/cc-safety-net) 语义级 command gate（opt-in，能脱 `bash -c` 壳 / 拦解释器单行 / 破坏性 git/rm，覆盖 `portal_exec`/`local_exec`/`shell`/`job` 这些绕过 agent `bash` PreToolUse hook 的执行路径，默认 fail-closed）。
- **OpenSSH 配置兼容**：`~/.ssh/config` 别名、`known_hosts`、ssh-agent 自动识别，无需重复登记主机。
- **零额外部署**：MCP client 通过 `uvx` 直接从 GitHub 拉运行，无需 clone、无需 venv。

## 为什么用 portal-mcp-server：和传统方案的对比

让 agent 操作远端，最朴素的方案是让它直接调 `bash` 跑 `ssh` / `scp` / `rsync`。这套"传统方案"在 Linux/macOS 配 `ControlMaster` 下勉强能用，但在 **Windows 上几乎不可用**，并且在文件编辑、sudo、多机、审计等多个维度都缺关键能力。下表把核心差异一次性列清楚——每一行就是一个具体的"agent 用传统方案会踩的坑"和 portal-mcp-server 怎么解。

| 维度 | 传统方案（bash + `ssh` / `scp` / `rsync`） | portal-mcp-server |
|---|---|---|
| **SSH 复用 · Linux/macOS** | OpenSSH `ControlMaster auto` + Unix socket；默认 `ControlPersist 10m`，超时后整条 master 断 | asyncssh **进程内连接池**，MCP server 活着就一直复用（小时级） |
| **SSH 复用 · Windows** | ❌ **不工作**——微软移植的 Win32-OpenSSH 自 v0.0.3.0 起 `ControlMaster` 失败（`muxclient socket(): Unknown error`），[issue #405](https://github.com/PowerShell/Win32-OpenSSH/issues/405) 自 2017 年起 open 至今（依赖 Unix domain socket fd 共享，Win 没有等价原语） | ✅ **和 Linux 同等性能**——连接池是纯 Python dict，asyncssh 不需要任何 OS 级 socket 共享 |
| **首次连接 / 后续命令延迟** | 首次 ~200–500ms；**无复用时每条命令都是新 TCP+auth ~300ms**（Win 默认场景）；有 ControlMaster 时后续 ~10–30ms | 首次 ~200–500ms，**后续 ~10–30ms（三平台一致）**——只开 channel |
| **跨"工具"复用** | `ssh` 和 `scp` 复用要求两边 `ControlPath` 完全一致；实际多数项目各开各的，scp 和 ssh 并不共享 master | ✅ 所有 `portal_*` 工具（bash / read / patch / transfer / tunnel ...）天然共享同一条 TCP |
| **持久 shell 状态** | 每次 `ssh host cmd` 是新 shell，`cd` / `export` / venv 激活 **全部丢失**；agent 必须每条命令重复 `cd /path && source venv/bin/activate && ...` | ✅ `portal_shell` 维护粘性交互式 shell（bash/zsh），cwd / env / venv 跨调用保留 |
| **远端文件编辑（safe edit）** | 三种都不安全：① `scp` 拉到本地→改→`scp` 推回（无并发检测，并发改 silently 丢；非原子）；② `ssh host "sed -i ..."`（无 dry-run、无 rollback、行号易错）；③ `ssh host "cat > file"`（并发覆盖、写一半连接断就半截文件） | ✅ `portal_read` 返回 SHA-256 + 行范围 hash；`portal_patch` 校验 hash → 写入 `*.mcp_tmp.*` → `posix_rename` 原子替换 → 写后再 rehash。**并发改 / 中途断连 / 行号漂移全部失败而非污染** |
| **文件 / 目录传输** | `scp` 无增量、单文件失败整批挂；`rsync` 较好但每次 fork 新进程，**进度无法回传给 agent**，大文件传输期间 MCP client idle 超时会断 | ✅ `portal_transfer` 增量短路（size+mtime 或 sha256）、**MCP progress 心跳防 idle 超时**、单文件失败进 `failed[]` 不中断批量、`paths_json` 支持任意 local↔remote 文件对批传 |
| **sudo 密码输入便利性** | 全是坑：① `ssh -t host sudo cmd` **每次弹密码**，agent 跑不了；② `echo $PASS \| ssh host "sudo -S cmd"`——**密码进 LLM 上下文**；③ `sshpass -p $PASS ssh ...`——**密码进 `ps` argv 和 LLM**；④ NOPASSWD sudoers——彻底放弃认证 | ✅ `portal_exec(use_sudo=True)`：密码源 = ① `sudo_password_command`（从密码管理器 `pass` / `op` / `bw` 现取，全自动）或 ② `portal sudo set <host>`（用户在另一终端 `getpass` 无回显输入一次，进 systemd `--user` 凭据 agent 内存 TTL）。**密码全程不进 LLM、不进 ps argv、不落盘** |
| **多机并发执行** | `for h in $hosts; do ssh $h cmd; done`——**串行**启动（每次 fork + auth），无 policy gate，一台失败靠 `set -e` 或脚本自己处理 | ✅ `portal_exec(host=[…])` 真并发 + 两阶段安全 gate（先 check 所有 host 再执行），`serialize=True`+`delay_s` 走滚动，`commands=[…]` 跑多步序列 |
| **SSH 隧道生命周期** | `ssh -L 8080:db:5432 host -fN` 后台跑飞，**没人管它什么时候关**、谁开的、是否还活着；要靠 `pgrep` 自己找 | ✅ `portal_tunnel(action=open)` 返回 `tunnel_id`，`portal_tunnel(action=list)` 看所有活跃隧道，`portal_tunnel(action=close)` 显式关；audit 可追溯 |
| **命令审计** | 无——要审计得自己用 `script(1)` / shell history wrapper 包一遍，agent 调用不可见 | ✅ 状态变更工具先过策略门禁 `_gate`（拒则不执行、不留痕），通过后结构化写 `audit.jsonl`（host、operation、command、result、timestamp）；审计写失败默认 fail-closed（中止操作），可设 `PORTAL_AUDIT_FAIL_OPEN=1` 放宽 |
| **结构化搜索** | `ssh host "grep -rn ... \| head"` 返回 **raw text，agent 自己 parse**；远端没装 rg 就降级 | ✅ `portal_grep` / `portal_glob` 优先 `rg --json`，自动 fallback `grep -rn` / `find`；返回 `{file, line, text}` 结构化 |

> **Windows 用户特别留意**：表格里的"SSH 复用 · Windows"那一行不是细节，是**根本性差距**。Windows 默认的 OpenSSH 客户端没有 ControlMaster，意味着 agent 每跑一条远端命令都要等 ~300ms 的 TCP+auth；跑 50 次就是 15 秒纯 overhead。portal-mcp-server 在 Win 上首条 ~280ms、后续 ~20ms，和 Linux 完全一致——这是为什么我们默认推荐它而不是 `ssh` 子进程方案。

## 快速开始

```bash
# 1. 在 Claude Code 里登记（--scope user 对所有 repo 生效；其他 MCP client 见"接入方式"节）
claude mcp add --scope user portal -- uvx portal-mcp-server@latest

# 2. 确保目标 host 在 ~/.ssh/config 或 hosts.yaml 里
#    （hosts.yaml 默认从 ~/.config/portal-mcp-server/hosts.yaml 读，
#     可用 PORTAL_HOSTS_YAML 覆盖；详见"环境变量"节）

# 3. 在 agent 对话中使用
#    "帮我看看 myhost 上 /var/log/syslog 最后 50 行"
#    agent 会调用 portal_exec("myhost", "tail -50 /var/log/syslog")
```

不需要 clone 仓库、不需要 venv——`uvx` 会自动拉取并运行。开发者安装见 [安装](#安装)。

## 架构

```
┌──────────────┐    stdio / SSE     ┌─────────────────────────────────────┐
│  MCP Client  │ ◄────────────────► │       portal-mcp-server             │
│ (Claude Code │                    │                                     │
│  Copilot CLI │                    │  ┌──────────┐   ┌────────────────┐  │
│  Cursor ...) │                    │  │ 14 tools │──►│ security gate  │  │
└──────────────┘                    │  └──────────┘   │ + audit log    │  │
                                    │                  └───────┬────────┘  │
                                    │                          │           │
                                    │              ┌───────────▼────────┐  │
                                    │              │  asyncssh 连接池    │  │
                                    │              │  (进程内, 跨工具    │  │
                                    │              │   复用同一 TCP)     │  │
                                    │              └──┬──────┬──────┬──┘  │
                                    └─────────────────┼──────┼──────┼─────┘
                                                      │      │      │
                                               SSH    │      │      │
                                              ┌───────▼─┐ ┌──▼──┐ ┌─▼──────┐
                                              │ Host A  │ │ ... │ │ Host N │
                                              └─────────┘ └─────┘ └────────┘
```

## 工具列表

14 个工具。去留判据：**只保留 agent 自己合成不出来的保证**（并发、原子/hash 防冲突、凭据不泄漏、安全 gate、真结构化输出）；"只是把一段脚本/状态打包"的（playbook、ping、rolling-as-tool、独立 tmp 清理）一律删掉或折进原语。

### 跑命令：exec 家族（按"有状态 / 本地 / 同步 vs 异步"选）

| 工具 | 什么时候用 |
|---|---|
| `portal_exec` | **默认主力**。无状态一次性，立刻拿结果（**分离的** stdout/stderr + exit code）。`host` 可单机 / 列表 / `group_tag`；`command` 单条或 `commands` 序列；多机默认并行，`serialize=True`(+`delay_s`) 走滚动；`use_sudo` / `secrets` 带外注入凭据。复用连接池，快。 |
| `portal_shell` | 需要 **cwd/env 跨调用保留**（`cd`/`export`/venv）时才用——每 host 一个粘性交互式 shell（bash/zsh），可选 `commands=[…]` 多步（状态跨步延续）。输出是**合并**流（PTY 把 stdout/stderr 并了）。否则用 `portal_exec`（更快、可多机）。 |
| `portal_job` | **后台**长任务。`submit` 秒回 `job_id`（远端 `nohup`+tmp 文件，**连接断了也接着跑**），`poll` 取增量输出 / 状态，`cancel` 杀，`list` 列。job 表内存态、有上限、TTL 清理；后台**不支持** sudo/secrets（用 `portal_exec`）。 |
| `portal_local_exec` | 在 **MCP server 自己机器**上跑（不走 SSH）——偏离了本项目以远端编排为核心的设计目标，是实用但 off-target 的衍生功能，故默认关闭，须 operator 显式设 `PORTAL_ALLOW_LOCAL_EXEC=1`。`use_sudo=True` 走本地 `sudo -S -k`（保留身份 `<local>`，密码来自 `portal sudo set-local` 或顶层保留段 `<local>:` 的 `sudo_password_command`），可与 `secrets` 同时使用。 |
| `portal_close_shell` | 关掉某 host 的粘性 `portal_shell` 会话（下次 `portal_shell` 自动重开）。罕用，仅用于重置脏会话。 |

> **★ 两层"复用"别搞混**：**连接复用**=asyncssh 的 TCP/channel 池，**所有**工具共享，纯为**速度**（首连 ~280ms，之后每 call ~10-30ms）；**会话复用**=只有 `portal_shell` 用的那个每 host 一个粘性交互式 shell（bash/zsh），为**状态连续**。shell 会话骑在池化 channel 上，两者正交。也正因为会话是隐式 plumbing，它的状态表归 `portal_audit(view="sessions")`，而不像 tunnel/host/job 那样自带 `list`。

### 文件编辑 / 搜索 / 传输

| 工具 | 给 agent 的能力 |
|---|---|
| `portal_read` / `portal_patch` | 读远端文件并拿 SHA-256；patch 用 `file_hash` + per-range hash 防并发覆盖，写入走 tmp + `posix_rename` 原子替换，写后再 hash 校验。**patch 成功后顺手扫掉同目录里 >1h 的孤儿 `*.mcp_tmp.*`**（白嫖已开的 SFTP 会话，异常完全隔离，绝不影响 patch 结果）——所以没有独立的 cleanup 工具。 |
| `portal_grep` | 忠实移植 Claude Code 的 Grep：`output_mode=files_with_matches`(默认，路径按 mtime 倒序) / `content`(匹配行+可选上下文，`head_limit` 封顶**总行数**、`offset` 分页) / `count`。清晰参数名（`before_context`/`after_context`/`context`/`ignore_case` 取代 CC 的 `-B`/`-A`/`-C`/`-i`），尊重 `.gitignore`，每个结果带 `truncated` 标志。**别用 `portal_exec` 跑裸 `rg`**。 |
| `portal_glob` | 忠实移植 CC 的 Glob：`rg --files --no-ignore --sort modified -g`，**按 mtime 倒序**、硬上限 100、带 `truncated`，返回 `{filenames, num_files, truncated, duration_ms}`。不尊重 `.gitignore`（CC Glob 默认）。**别用 `portal_exec` 跑裸 `find`**。 |
| `portal_transfer` | `direction=upload\|download\|sync\|mirror\|upload-list\|download-list`。SFTP 二进制安全；`sync` 推目录、`mirror` 拉目录、`*-list` 传 `paths_json` 给定的一批任意 local↔remote 文件对，默认 size+mtime 增量短路（`checksum=True` 改用 sha256）；单文件失败进 `failed[]` 不中断整批；大文件传输用 MCP progress 心跳防 client idle 超时。 |

### 资源（agent 显式管理，所以 `list` 跟工具走）

| 工具 | action / 参数 | 用途 |
|---|---|---|
| `portal_host` | `action=list\|register\|remove` | 主机注册。`register` 要 `name`+`host`——或只给 `name`（若 `~/.ssh/config` 有同名 Host 别名，自动登记 `use_ssh_config` 叠加）。`tags` 喂 `portal_exec` 的 `group_tag`。`list` 同时枚举 ssh config 里的 `Host` 别名并解析真实 `HostName`/`User`/`Port`，每条带 `source` 字段（`hosts.yaml`/`runtime`/`ssh-config`/`…+ssh-config`）；可能带 per-host `warnings`（如 hosts.yaml↔ssh config 冲突），要转告用户。**无 password 参数**。 |
| `portal_tunnel` | `action=open\|close\|list`，`kind=local\|reverse\|socks` | 单入口 SSH 隧道（仿 `portal_host`）。`action` 选操作、`kind` 选隧道种类。`open` 走 host gate，`close` 按 `tunnel_id`（gate 在源 host）。 |

### introspection / 策略

| 工具 | view / 参数 | 用途 |
|---|---|---|
| `portal_check` | `host`，optional `command` | 安全策略 dry-run，不执行。返回 `ALLOWED` / `BLOCKED: <reason>`。⚠️ 默认策略**宽松**——`ALLOWED` 只表示"当前没规则拦它"，不代表安全。 |
| `portal_audit` | `view=snapshot\|server\|sessions\|history\|stats\|policy` | 只读内省**中枢**：server 元数据 + 连接池 + bash 会话 + 审计 stats + 策略。**hosts/tunnels 不在这里**——它们是资源，分别由 `portal_host(action=list)` / `portal_tunnel(action=list)` 列出。`sessions` view 是 plumbing 诊断（host→session_id 粘性会话表）。 |

### 怎么选：专用工具 vs `portal_exec`/`portal_shell`

`portal_exec` 能跑任意命令，但**能用专用工具就别用裸命令替代**——专用工具要么有安全保证，要么有结构化输出：

| 你要做的事 | 用这个（**不要**裸命令） | 为什么 |
|---|---|---|
| 读 / 改远端文件 | `portal_read` → `portal_patch` | SHA-256 + per-range hash 防并发覆盖，atomic rename，写后 rehash |
| 搜文件内容 / 找文件 | `portal_grep` / `portal_glob` | 结构化 JSON + token 护栏；别用 `portal_exec` 跑裸 `rg`/`find` |
| 传文件 / 同步目录 | `portal_transfer` | SFTP 二进制安全 + 增量短路 + progress 心跳 |
| 多机执行 | `portal_exec(host=[...])` / `group_tag=` | 并发 / 滚动 + 两阶段 gate；bash 里 `for h; ssh $h` 没 gate |
| 开隧道 | `portal_tunnel` | 受管生命周期、可 list；bash 里 `ssh -L` 跑飞了没人收 |
| 长任务丢后台 | `portal_job` | 暴露状态 + 交还控制，可 poll/cancel；裸 `nohup &` 失联无回路 |

### 给 agent 的使用约定

`portal-mcp-server` 只提供工具，不强制怎么用。建议在 `AGENTS.md` / 系统 prompt 加：

- **优先确认 host 别名**——不在 `~/.ssh/config` / `hosts.yaml` 的主机先问用户
- **写文件走 read → patch**——冲突时 patch 返回新 hash，重读重改
- **默认沙箱 `/tmp/`**——改 `$HOME` 或源码前先确认
- **不混用工具**——一次任务要么走 `portal_*`，要么走 bash 里的 `ssh`/`scp`，混用会绕过 hash 校验或打断 sudo 流
- **多机用 `portal_exec(host=[...])`**，不要在 bash 里循环 `ssh`
- **sudo 三选一**——① host 配 `sudo_password_command`（密码管理器拉，全自动）；② 用户 `portal sudo set <host>` 预塞密码再 `portal_exec(..., use_sudo=True)`；③ 真要交互式 prompt 的让用户 `ssh -t host sudo ...`。若某台密码登录 host 的 sudo 密码明确等于 SSH 登录密码，可在 `hosts.yaml` 显式设 `sudo_password_same_as_ssh: true`，之后 `portal ssh set <host>` 会同时预塞 sudo 缓存；默认不启用。

<details>
<summary>📋 完整逐工具参考（签名 · 返回结构 · 源码位置）</summary>

> 所有工具对大模型可见的完整签名（`ctx` 是 MCP 进度 / keepalive 上下文，由 FastMCP 在异步工具上注入，**不**出现在下面的 schema 里）。针对 host 的工具都收 `host`，解析顺序 hosts.yaml / 运行时注册表 → OpenSSH 客户端 config（复用 asyncssh 的 `SSHClientConfig`、跟 `Include`），详见[环境变量 → 文件路径](#文件路径)。状态变更工具写 `audit.jsonl`；只读工具（`portal_read` / `portal_grep` / `portal_glob` / `portal_check` / `portal_audit` 及 `portal_tunnel` / `portal_job` 的读 action）**刻意不审计**。

### 跑命令：exec 家族

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `portal_exec` | `(host='' \| [host…], command='', commands=None, group_tag='', timeout=3600.0, use_sudo=False, secrets=None, serialize=False, delay_s=0.0, stop_on_error=True)` | 走连接池的无状态一次性。**单 host + 单 command → 一个 dict**（**分离**的 stdout/stderr + exit code）；多 host / `commands` 序列 → **list**（多命令 host 为 `{host, results:[…]}`）。并行 / 滚动、`use_sudo` / `secrets` 语义见上文工具列表与[认证](#认证)。 |
| `portal_shell` | `(host, command='', commands=None, stop_on_error=True, timeout=3600.0)` | 每 host 一个持久交互 shell。单 command → `{host, session_id, command, exit_code, output, duration_s}`（`output` 是 PTY 合并流，超限截断标 `truncated`）；`commands=[…]` 在**同一** session 顺序跑 → `{host, session_id, results:[…], duration_s}`，`stop_on_error` 首败即停并加 `stopped_at`。卡交互提示被自动 Ctrl-C → `exit_code:-1` + `error:"interactive_prompt_blocked"` + `session_preserved:true`。命令边界协议见下文 **设计理念 · 持久 shell 会话** 一节。 |
| `portal_job` | `(action=submit\|poll\|cancel\|list, host='', command='', job_id='', since=0, tail=0, max_bytes=65536, signal=TERM\|KILL, use_sudo=False, secrets=None)` | `submit` 秒回 `job_id`（远端 `nohup` + tmp 文件，断连仍跑）；`poll` 按需分页（`since=<offset>` 只回更新字节、单次封顶 `max_bytes` 默认 64 KiB、带 `more`；或 `tail=N` 瞄尾），chunk base64 + 边界安全 UTF-8 解码；`cancel` 发 `signal`，`list` 列全部。job 表跨重启 best-effort 持久化、有上限、TTL 清理（`PORTAL_JOB_*`）。`use_sudo` / `secrets` **后台不支持、传即拒**并指向 `portal_exec`。 |
| `portal_local_exec` | `(command, secrets=None, use_sudo=False, timeout=3600.0)` | 在 **MCP server 自己机器**上跑（**不**走 SSH），默认关闭，须 `PORTAL_ALLOW_LOCAL_EXEC=1`。`use_sudo=True` 用保留身份 **`<local>`**（≠ 普通 SSH host `local` / `localhost`）走本地 `sudo -S -k`，密码来自 `portal sudo set-local` 或 hosts.yaml 顶层 `<local>:` 段，可合 `secrets`，标 `high_risk`。 |
| `portal_close_shell` | `(host)` | 关掉某 host 缓存的 `portal_shell` 会话（下次自动重开）。罕用，仅重置脏会话。 |

### 文件编辑（hash 保护）

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `portal_read` | `(host, path, start=1, end=None, limit=None, encoding='utf-8')` | → `{content, file_hash, range_hash, start, end, total_lines, truncated}`，两个 SHA-256 是 `portal_patch` 的前置。**分页**：单次 ≤ `limit` 行（默认 `PORTAL_READ_MAX_LINES=2000`）+ `PORTAL_READ_MAX_BYTES`（默认 16384）字节；提前截断则 `truncated=true` 且 `next_start` 给续读点，页按行边界切故 `range_hash` 仍有效。 |
| `portal_patch` | `(host, path, file_hash, patches_json, encoding='utf-8', auto_newline=False)` | hash 保护的行范围 patch：文件自 `portal_read` 后变了即拒（回 `current_file_hash`）、原文件不动；patch 自底向上、重叠拒、走 `*.mcp_tmp.<12hex>` + `posix_rename`（原子）、写后 rehash。成功后顺扫同目录 >1h 孤儿 tmp（白嫖已开 SFTP、隔离，进可选 `swept` 键）。`patches_json` = `[{"start":int,"end":int\|null,"contents":str,"range_hash":str}, …]`。 |

### 远端搜索（忠实移植 Claude Code）

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `portal_grep` | `(host, pattern, path='.', glob='', file_type='', output_mode=files_with_matches\|content\|count, ignore_case=False, before_context=0, after_context=0, context=0, head_limit=250, offset=0, multiline=False)` | 正则内容搜索（`rg`，fallback `grep`）。`output_mode`：`files_with_matches`（默认，路径 mtime 倒序）/ `content`（匹配行 + 可选上下文，`head_limit` 封顶**总行数**、`offset` 分页）/ `count`。尊重 `.gitignore`，每结果带 `truncated`。清晰参数名取代 CC 的 `-A`/`-B`/`-C`/`-i`。 |
| `portal_glob` | `(host, pattern, path='.')` | 按 glob 找文件，`rg --files --no-ignore --sort modified -g`，**mtime 倒序**，硬上限 100 + `truncated` → `{filenames, num_files, truncated, duration_ms}`。不尊重 `.gitignore`（对齐 CC Glob）。 |

### 文件传输（SFTP）

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `portal_transfer` | `(direction=upload\|download\|sync\|mirror\|upload-list\|download-list, host, local_path, remote_path, checksum=False, paths_json='')` | 二进制安全、原子 SFTP。单文件模式（`upload`/`download`）→ `{status, direction, host, bytes, duration_s, …}`；增量模式（`sync` 推目录 / `mirror` 拉目录 / `*-list`）跳过 size+mtime 匹配（`checksum=True` 改 sha256）→ `{status, uploaded\|downloaded, skipped, failed[], bytes_total, bytes_transferred, duration_s}`，单文件失败进 `failed[]` 不中断。`*-list` 需 `paths_json` = `[{"local":…,"remote":…}, …]`，只拷普通文件。 |

### 资源（agent 显式管理）

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `portal_tunnel` | `(action=open\|close\|list, kind=local\|reverse\|socks, host='', tunnel_id='', local_port=0, local_bind='127.0.0.1', remote_host='', remote_port=0)` | `open` 过 `host` 开隧道：`local` 转发 `localhost:local_port → remote_host:remote_port`、`reverse` 把 `local_bind:local_port` 暴露成 `host:remote_port`、`socks` 是 SOCKS5 代理。`close` 按 `tunnel_id`（gate 在源 host）；`list` 列所有活跃隧道。 |
| `portal_host` | `(action=list\|register\|remove, name='', host='', user='root', port=22, key_path='', tags='')` | 运行时 host 注册表。`register` 要 `name`+`host`——或只 `name`（`~/.ssh/config` 有同名 `Host` 别名时自动登记 `use_ssh_config` 叠加）。`tags`（逗号分隔）喂 `group_tag`。`list` 同时枚举 ssh config 别名（解析真实 `HostName`/`User`/`Port`），每条带 `source`（`hosts.yaml`/`runtime`/`ssh-config`/`…+ssh-config`）+ 可能的 per-host `warnings`——转告用户。**无 password 参数。** |

### 内省 / 策略

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `portal_check` | `(host, command='')` | 过安全策略 dry-run，不执行 → `"ALLOWED"` 或 `"BLOCKED: <reason>"`。⚠️ 默认策略**宽松**——`ALLOWED` 只表示"当前无规则拦它"，不代表安全。 |
| `portal_audit` | `(view=snapshot\|server\|sessions\|history\|stats\|policy, limit=50, host_filter='')` | 只读内省 server **plumbing** + 历史。`snapshot`（元数据 + 连接池 + bash 会话 + 审计 stats + 策略摘要）/ `server`（仅版本 / 元数据）/ `sessions`（持久 bash 会话的 `host→session_id` 表）/ `history`（最近 `limit` 条，可过滤）/ `stats`（按 operation 计数）/ `policy`。**hosts / tunnels 不在这**——它们是资源，由 `portal_host` / `portal_tunnel` 各自的 `list` 出。 |

> **凭据 CLI（带外，非 MCP 工具）**：agent 永远看不到凭据值。密码 / passphrase / secret 由人在另一个终端用 `portal {ssh,sudo,passphrase,secret} set` 预置、per-user agent 持有；`show` / `list` 只回 sha256[:16] 指纹 + TTL，`confirm` 二次输入比对。完整机制、跨平台自动安装与「明文永不离开 agent」原则见[认证](#认证)与[凭据 agent](#凭据-agentlinux-systemd--macos-launchd--windows-计划任务)。

### 源码位置

| 模块 | 负责的工具 / 职责 |
| --- | --- |
| `connection_manager.py` | 所有工具共用的连接池 + host 注册表 |
| `shell_engine.py` | `portal_exec`（一次性 `ssh_exec`） |
| `remote_bash.py` | `portal_shell` / `portal_close_shell` + sudo/secrets 一次性路径 |
| `session_manager.py` | 持久交互式 shell 会话（bash/zsh；cwd/env、exit code，OSC 133 边界协议、soft-cancel） |
| `job_manager.py` | `portal_job`（后台 submit/poll/cancel/list） |
| `local_exec.py` | `portal_local_exec` |
| `remote_text_editor.py` | `portal_read`、`portal_patch`（+ 孤儿 tmp 清扫） |
| `remote_search.py` | `portal_grep`、`portal_glob` |
| `file_ops.py` | `portal_transfer` |
| `network_tools.py` | `portal_tunnel` |
| `credential_agent.py` | `portal {ssh,sudo,secret} set` 的 per-user socket 激活 TTL 缓存 |
| `ssh_creds.py` / `sudo_creds.py` / `secrets_store.py` | 凭据解析 + 输出脱敏 |
| `security.py` | `_gate()` / `_gate_exec()` 策略闸门 |
| `audit.py` | `audit_log()` 写入 + `portal_audit` introspection |

</details>

<details>
<summary>🔀 从旧工具名迁移</summary>

> 这轮重构改了名 / 合并 / 删除了一批工具（破坏性，但 commit 故意不标 `!`，版本号手控）。对照：

| 旧 | 新 |
|---|---|
| `portal_bash(host, cmd)` | `portal_shell(host, cmd)`（持久会话）或 `portal_exec(host, cmd)`（一次性，更快） |
| `portal_bash(..., use_sudo=True / secrets=[…])` | `portal_exec(..., use_sudo=True / secrets=[…])` |
| `portal_bash_close` | `portal_close_shell` |
| `portal_multi_exec(mode=parallel, hosts_json=…)` | `portal_exec(host=[…])` |
| `portal_multi_exec(mode=rolling, …)` | `portal_exec(host=[…], serialize=True, delay_s=N)` |
| `portal_multi_exec(mode=broadcast, commands_json=…)` | `portal_exec(host=[…], commands=[…])` |
| `portal_playbook(host=…/group_tag=…)` | `portal_exec(host=…/group_tag=…, commands=[…])` |
| `portal_ping(hosts_json=…)` | `portal_exec(host=[…], command="echo pong")` |
| `portal_tunnel_open/_close/_list` | `portal_tunnel(action=open\|close\|list, kind=…)` |
| `portal_cleanup_tmps` | 删除——`portal_patch` 成功后自动清扫同目录孤儿 tmp |
| `portal_bash_status` | `portal_audit(view="sessions")` |
| — | **新增** `portal_job(action=submit\|poll\|cancel\|list)` 后台任务 |

</details>

## 设计理念

### 工具精简：少而正交

Anthropic 的 [_Writing Tools for Agents_](https://www.anthropic.com/engineering/writing-tools-for-agents) 明确说：

> "More tools don't always lead to better outcomes... Tools that merely wrap existing software functionality is a common error... Too many tools or overlapping tools can also distract agents from pursuing efficient strategies."

portal-mcp-server 据此把工具面收敛到一组**少而正交**的原语。判据只有一条：**一个工具只在它能提供 bash 难以廉价合成的保证时才保留**（并发安全、原子 / hash 保护写入、凭据非泄漏、安全 gate、真正的结构化输出）。凡是"一行 bash 就能办、彼此语义重叠"的便利封装，都不单独立工具，交给 `portal_shell`（持久 bash 会话）+ `portal_exec`（一次性，含多机 fanout / sudo / secrets）覆盖。剩下的工具各自守住一条这样的保证：

| 工具面 | 提供的"bash 难以廉价合成"的保证 |
|---|---|
| `portal_read` + `portal_patch` | SHA-256 整文件 + 行范围双层 hash，取代裸 `cat` / `sed` / `> file` 的并发覆盖与中途断连漏洞 |
| `portal_grep` / `portal_glob` | 忠实移植 Claude Code 搜索 schema 的结构化输出（`rg --json` 优先，自动 fallback `grep` / `find`），不让 agent 自己 parse raw text |
| `portal_shell`(`_close`) / `portal_exec` | 持久 shell + exit code；一次性 + 真并发多机 fanout + 两阶段安全 gate + 凭据非泄漏 |
| `portal_transfer` | SFTP 增量短路（size+mtime 或 sha256）+ 进度心跳防 idle 超时 + 批量单点容错 |
| `portal_job` | 后台 submit/poll/cancel/list，给 agent"丢后台、中途思考、随时打断"的能力 |
| `portal_tunnel` / `portal_host` / `portal_audit` | 用 `action` / `view` 字段把同一资源的多个动作合并进一个工具，而非每个动作各立一个 tool |

所有派发参数（`action` / `view` / `output_mode` / ...）用 `typing.Literal` 标注，schema 层直接带 `enum`，client 可校验——agent 不必在多个语义重复的工具里反复选择。**工具 schema 上下文占用**：所有工具的 name + description + inputSchema 合计约 **~8k tokens**（`tiktoken o200k_base` 估算，约为 200k 上下文窗口的 **~4%**；descriptions 含 sudo / secrets / 安全约定等护栏文案，故偏厚）。


### 进程内连接池

portal-mcp-server 在 server 进程内部维护 asyncssh 连接池——所有工具调用（`portal_shell`、`portal_read`、`portal_transfer` ...）共享同一条 TCP。**除第一次连接外全部摊销到 channel 创建（~10–30 ms）**，覆盖维度（vs OpenSSH `ControlMaster` / Win OpenSSH 无复用 / `ssh-scp` 跨工具复用 / persistent shell / 跨平台）已在上文 [§ 为什么用 portal-mcp-server](#为什么用-portal-mcp-server和传统方案的对比) 一次性对比过；下面只补几条机制层面的实现细节：

- **池形态**：`PORTAL_SSH_POOL_SIZE` 控制每 host 最多 TCP 连接数（默认 5），`PORTAL_SSH_MAX_CHANNELS_PER_CONN` 控制单条 TCP 上 channel 上限（默认 5）；超出后新建 TCP，再超出则按"最空闲"复用并 warning。asyncio 在同一条 TCP 上支持多 channel **真并发**，不像 plain ssh 必须串行启动多个 ssh 进程（每个 channel 一个 fork+auth）。
- **空闲与老化**：`PORTAL_SSH_MAX_IDLE_TIME` 默认 600 秒、`PORTAL_SSH_MAX_CONN_AGE` 默认 3600 秒；空闲到期或超龄且无活跃 channel 即关闭，防止 NAT/防火墙静默断连。
- **长连接稳定性**：池连接随 MCP server 进程持续（小时级），相对 `ControlMaster` 默认 10 分钟 `ControlPersist`，长会话里的 reconnect 抖动也省了。
- **微基准（脱敏）**：同 LAN（< 1ms RTT）跑 100 次 `echo pong`，plain ssh + ControlMaster 平均 23 ms；portal-mcp-server 通过 `portal_shell` 平均 18 ms（省了 ssh 子进程启动）。首次两边都 ~280 ms（auth 占大头）。
- **Windows 上的具体表现**：plain ssh 每条命令 ~300 ms × N（无复用，连实验性的 named-pipe fallback 也常出问题），portal-mcp-server 首次 ~280 ms、后续 ~20 ms 直降到 channel 创建极限——asyncssh 是纯 Python，连接池放在自己进程内存，不依赖任何 OS 级 socket 共享（这正是 Win OpenSSH 的 ControlMaster 挂掉的地方）。

### 持久 shell 会话：命令边界借鉴 OSC 133（iTerm2 / VS Code 那套）

`portal_shell` 给 agent 的是**每 host 一个、跨命令存活**的 `bash -i` / `zsh -i`——cwd、env、shell 函数在多次调用间自动保留（底层是同一个进程）。这是上文连接池之外的**第二层复用**：连接池复用 TCP channel 图**快**，持久会话复用同一个交互 shell 图**状态连续**（上文工具列表的 ★ 注记把这两层 "don't conflate them" 专门点了名）。

难题在于：一个 `bash -i` 把许多命令跑在**同一条 SSH channel** 上，而 SSH 只在 channel **关闭时**才报退出码。要在不拆 channel（拆了就丢 cwd/env）的前提下拿到每条命令的 `$?`，就得自己划命令边界。旧方案是 in-band sentinel（命令后追加 `echo <哨兵>:$?` 再扫 stdout），把控制信号混进数据流，从根上就脆（细节见下）。

**现方案借鉴 iTerm2 / VS Code 集成终端 / Kitty / WezTerm 用的 OSC 133（FinalTerm）Shell Integration**：让 **shell 自己发命令边界标记**。首次使用时经 stdin 注入一小段集成脚本（**只走 stdin、从不落盘**），给 shell 挂 `PROMPT_COMMAND` / `precmd` hook，在每条命令后打印 `\x1b]133;D;<exit>\x07`；我们退化成**纯解析器**。这段序列以 ESC 字节打头，普通文本——**哪怕正文 literally 写着 `]133;D;0`**——也伪造不出来，`$?` 直接从标记里读，整类哨兵脆弱性**从根上消失**。

顺带白捡两个能力：命令卡在交互提示（sudo / ssh 首连 / `mysql -p` / gpg passphrase）时**自动 Ctrl-C 且保留会话**（soft-cancel，cwd/env 不丢、下条命令立刻能跑），而一次性路径 `portal_exec` 因为每命令开新 channel、从 asyncssh 直接拿原生退出码，完全不受这套影响。

> **旧 sentinel 为何从根上脆**（换掉它的动机）：① `sudo`（或任何抢 stdin 的程序）把哨兵**当密码吞掉**，命令挂到 `timeout`（默认 3600 s）；② 大输出撑爆无界 buffer；③ stdout 里 literally 含哨兵字符串的命令被误判成已完成。ESC 打头的 OSC 133 标记把这三类一次性消掉。

> **真机 spike 出来的坑**（都记在 `session_manager.py`，对应 commit `466108b` / `46b1440`）：shell 必须 `--noprofile --norc` / `--no-rcs`，否则用户 rc 覆写 hook 静默打断协议；**zsh 必须 `unsetopt zle`**——ZLE 无视 `stty -echo` 回显命令行、把命令文本漏进输出，只有 zsh 5.9 真机暴出，bash 的 readline 认 `stty -echo` 故一开始就干净；**多行命令包 `{ … }`**——交互 shell 每读一个顶层输入行就 fire 一次标记，不包会一行一个 D 错位后续调用，用花括号组而非 `( … )` 子 shell 才能让 `cd`/`export` 持久；**fish 暂缺**——`fish_postexec` 未做真机验证，回退 bash。

### 技术选型：asyncssh 而非 subprocess

[asyncssh](https://github.com/ronf/asyncssh)（EPL-2.0 / GPL-2.0 双许可）是 SSHv2 协议的**独立纯 Python 实现**，与 OpenSSH 协议层等价：

- **单进程多连接、单连接多 session**：连接池就是 Python dict，没有进程边界、没有 fd 共享需求——也是为什么 portal 能在 Win 上做到和 Linux 一致的复用性能（OpenSSH master/child 模型在 Win 没法工作）。
- **协议层完整覆盖**：local/remote/dynamic 端口转发、SFTP、SCP、X11 fwd、TUN/TAP——OpenSSH 能干的协议层动作 asyncssh 全都能干。
- **OpenSSH 兼容**：原生解析 `~/.ssh/config`、`known_hosts`、`authorized_keys`、ssh-agent / Pageant。portal 的 host 别名探测与 `portal_host(action=list)` 对 ssh config 的枚举/解析也**直接架在 asyncssh 的 `SSHClientConfig` 解析器上**（跟 `Include`、多端适配），不是手写扫描。
- **仅依赖 PyCA `cryptography`**：装上 Python 就能跑，无 C 依赖、无 OS 特定 IPC。

对比"用 subprocess 调 `ssh` / `scp`"：免去每命令 ~50–100 ms 的 fork、不需要协调多进程之间共享 SSH 复用（这正是 ControlMaster 在 Win 上挂的根因）、错误处理 / 重试 / 超时都是 Python 异步原语，而不是解析 stderr 字符串。

### 反馈通道：warning 走 tool result，不走 stderr

`portal-mcp-server` 把所有**用户需要看到的运行时 warning / error** 都塞进 tool result 的返回内容，不靠 server stderr 或日志文件喊话。这不是审美选择，是 MCP 协议在 client 端实际行为反推出来的硬约束。

**协议层** — [MCP 2025-06-18 spec · transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#stdio) 原文：

> The server **MAY** write UTF-8 strings to its standard error (`stderr`) for logging purposes. **Clients MAY capture, forward, or ignore this logging.**

第二条候选 `notifications/message`（[logging capability](https://modelcontextprotocol.io/specification/2025-06-18/server/utilities/logging)）同样自由："Implementations are free to expose logging through any interface pattern that suits their needs—the protocol itself does not mandate any specific user interaction model."

**主流 client 实测**：

| Client | server stderr 去向 | 用户能否看到 |
|---|---|---|
| Claude Desktop（[docs](https://modelcontextprotocol.io/docs/develop/connect-local-servers#getting-logs-from-claude-desktop)） | 写 `~/Library/Logs/Claude/mcp-server-<name>.log` | ❌ 零 UI 提示，要 `tail -f` 该文件 |
| Claude Code（[docs](https://docs.anthropic.com/en/docs/claude-code/debug-your-config#check-mcp-servers)） | 默认丢弃；官方推荐 "run `claude --debug mcp` to see the server's stderr output" | ❌ 除非用户主动 debug 重启 |
| Python MCP SDK 通用 client | `errlog: TextIO = sys.stderr` 转发到 client 自己的 stderr | 看 client 进程把自己 stderr 怎么处理 |

**真正可靠的反馈路径只剩两条**：tool result 的 `content` 数组（agent 一定读）和 JSON-RPC error response（多数 client 会展示）。所以我们：

- **重要 warning**（yaml 配错、缺凭据、被忽略的字段……）→ 进 server 内 `_config_warnings` 集合，挂在 `portal_host(action="list")` 的返回值上随调用返回（见 `connection_manager.py`）
- **致命 config 错误** → 操作时 inline 在 tool result 里 raise，不是启动时打一行 log 就当结案
- **info 级 stderr** → 只留给 server 作者 debug 时看；不假设用户会看
- **audit log** → 写 `$XDG_STATE_HOME/portal-mcp-server/log/`（[XDG Base Directory Spec](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html) 把 logs / history 这种"持久但非关键"状态明确归到 state home），给运维和事后审计用，不假设用户实时读

这条原则倒过来约束 server 内部代码：**任何"用户该知道但 server 自己不能立即 raise"的事**，必须挂到下一次相关 tool call 的返回值里输出。`logger.error()` 完就当结案 = 死信。

## 安装

按身份选路径。

### 终端用户（用 MCP server，不动源码）

不需要 clone，让 MCP client 通过 `uvx` 直接从 PyPI 拉运行——见下方 [接入方式](#接入方式)。`uvx` 第一次启动缓存依赖，后续重启秒级。

shell 里手动 smoke test：

```bash
uvx portal-mcp-server@latest --help
```

### 开发者（要改代码 / 跑测试）

推荐 `uv sync`，按 `pyproject.toml` + `uv.lock` 一次到位准备好 `.venv`：

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras
source .venv/bin/activate
pytest                        # 应全绿（live SSH 测试默认 skip）
```

要让 MCP client 直接跑这个本地 checkout，可安装成固定可执行文件：

```bash
uv tool install --force --editable .  # 指向当前 checkout；源码改动立即生效
```

不想用 uv 也可以走标准 pip editable install：

```bash
pip install -e ".[dev]"       # -e/--editable 指向当前源码；含 pytest 等 dev 依赖
# 或纯运行时
pip install -e .
```

### 短命令别名 `portal`

`uv tool install portal-mcp-server`（或上面的 `uv tool install --force --editable .`）之后，PATH 里同时出现两个等价的 entry point：

```bash
portal agent install --now           # 安装并启动 systemd --user 凭据 agent
portal agent uninstall               # 停用并移除 agent 用户级 unit/config
portal-mcp-server sudo set web01     # 全名
portal sudo set web01                # 短名（推荐手敲场景）
portal ssh set web01                 # SSH 登录密码
portal passphrase set web01          # SSH 私钥 passphrase
portal secret set GITHUB_TOKEN
```

`uvx portal-mcp-server xxx` 模式仍然要全名（`uvx` 不接受 alias）。短名只在 `uv tool install` / `pip install` 之后的常驻命令里生效。

> **⚠️ 已知命名冲突**：[`SpatiumPortae/portal`](https://github.com/SpatiumPortae/portal)（一个 P2P 文件传输 CLI，Homebrew core 收录）也叫 `portal`。**Homebrew 用户可能撞名**——`uv tool install` 把二进制放 `~/.local/bin/portal`，Homebrew 装在 `/opt/homebrew/bin/portal` 或 `/usr/local/bin/portal`，哪个先在 `$PATH` 里哪个赢。排查：
>
> ```bash
> which -a portal      # 列出所有同名可执行；上面一条是当前生效的
> ```
>
> 撞了就用全名 `portal-mcp-server`，或调整 PATH 顺序。`uv tool install` 不会静默覆盖别人的二进制——文件已存在时会报错让你确认。

### 凭据 agent（Linux systemd / macOS launchd / Windows 计划任务）

> **⚠️ 自动安装：Linux + macOS + Windows，全是 per-user**。`portal agent install` 按 OS 自动分派，三者都让 agent **以你的身份、在你的会话里**跑：**Linux** 装一对 **systemd 用户级单元**（`.socket` + `.service`，放 `~/.config/systemd/user/`，socket activation 拉起）；**macOS** 装一个 **launchd LaunchAgent**（`~/Library/LaunchAgents/com.tmytimidly.portal-credential-agent.plist`，run-and-keepalive——agent 自己 bind AF_UNIX socket，省掉 `launch_activate_socket` 的 ctypes 复杂度）；**Windows** 装一个 **per-user 登录计划任务**（Task Scheduler，**InteractiveToken** 主体——以你的身份、只在你登录时跑，**绝不以 SYSTEM**、不存密码；XML 里 `ExecutionTimeLimit=PT0S` 防 72h 被杀 + `RestartOnFailure` 近似 KeepAlive），IPC 走**命名管道**（没有 AF_UNIX）。Windows 的命名管道传输 + 计划任务 install 都由 `windows-latest` CI job 真机实测覆盖。
>
> 没有 agent（其它平台）时的替代方案：用 `hosts.yaml` 的 `password_command` / `passphrase_command` / `sudo_password_command`，或 `secrets.yaml` 的 `command:` 字段，从系统密码管理器（Keychain、`pass`、`secret-tool`、`gopass`、1Password CLI 等）按需读取——见下文「[认证](#认证)」。MCP server 本身（`portal_shell` 等所有远端工具）在 Windows / macOS / Linux 都正常工作。

`portal ssh set` / `portal passphrase set` / `portal sudo set` / `portal secret set` 的无回显交互值不再塞进某个 MCP server 进程自己的内存，而是进入一个 per-user、systemd socket-activated 的**凭据 agent**（credential agent）。**首次跑 `portal {ssh,passphrase,sudo,secret} set` 时如果 agent 还没起，会自动执行下面这条安装（等价 `portal agent install --now`）、把安装输出打给你看，然后再让你输入** —— 所以下面这步通常不用手动做，列在这里是为了让你知道背后发生了什么、以及如何显式预装：

```bash
portal agent install --now
```

这会写入 `~/.config/systemd/user/portal-credential-agent.{socket,service}`。`.socket` 和 `.service` 同名配对，按 systemd 默认约定 socket unit 收到第一次连接时拉起同名 service unit，并把 listening fd 通过 `LISTEN_PID` / `LISTEN_FDS` 环境变量传给 service（socket activation）；`.socket` 监听 systemd user manager 的 `%t/portal-mcp-server/credentials.sock`，由 systemd 创建和移除；安装命令同时把 systemd specifier（`%t`）解析展开后的绝对 socket 路径写进 `~/.config/portal-mcp-server/agent.json`，让 MCP client 直接读这份路径（或显式的 `PORTAL_CREDENTIAL_AGENT_SOCKET`），不必自己猜运行时目录——GUI app 拉起的子进程的 `XDG_RUNTIME_DIR` 不一定对，这个 cache 是必要的。

> **使用顺序**：`portal {secret,sudo,ssh,passphrase} set` 会按需自动安装并启动凭据 agent（见上），所以一般直接 `set` 即可。**但**让 MCP server（IDE / agent 里那个）能读到凭据，agent 必须在 MCP server 启动**之前或之后重载**一次：若你是先开着 IDE 才第一次 `set`，安装完请重载 MCP/plugin 或重启 agent（Claude Code 里 reload MCP/plugin，Copilot CLI 里 `/restart`，或直接重启对应 IDE/agent）。想完全手动可控也可以先 `portal agent install --now` 再开 IDE。

常驻的是 systemd 的 socket unit，本身只是一条本用户可访问的本地监听端点；credential agent service 会在第一次连接时被 socket 激活，用内存保存 TTL 凭据。停止 service 会清掉内存凭据，socket 仍可继续按需拉起它。完全卸载：

```bash
portal agent uninstall
```

日常巡检 / 维护命令：

```bash
portal agent status                  # 显示 socket 路径 + 是否运行 + 各 kind 缓存条数
portal agent clear                   # 清空所有 kind 的缓存（agent 进程保留）
portal ssh    list                   # 列出每个 host 的 sha256 指纹 + 剩余 TTL
portal ssh    show web01             # 查看单条凭据的指纹 + TTL（无明文）
portal ssh    confirm web01          # 二次输入比对，匹配才更新（替代"显示明文"）
portal ssh    clear web01            # 清掉单条
```

`passphrase` / `sudo` / `secret` 子命令树结构完全一致（key 名分别是 `host` / `host` / `name`）。

> **设计原则：plaintext 永不离开 agent 内存**。整套 CLI **故意没有 `show plaintext` / `dump` 这种动词**：`show` 只回 sha256[:16] 指纹 + TTL，`list` 同理，`confirm` 是让你重新输入一遍跟内存里的比对。明文只交给同 uid 的真消费者——asyncssh（SSH 握手 / 本地 key 解锁）、`sudo -S`（stdin）、`$env` 注入（subprocess env）。terminal scrollback / 截图 / OBS / asciinema / 远程 view session / stdout pipe 全是泄露面，明文 echo 会把无回显输入的所有保护清零。业内同类工具（ssh-agent `-L` 只列指纹 / gpg-agent 无导出 passphrase / vault agent 走 template / polkit-agent 纯 GUI）全是同一套姿态。需要把值导出去用，应该走 `password_command` / `passphrase_command` / `secrets.yaml` 的 `command:` 从密码管理器临时拉，**不要**让 credential agent 回吐明文。

## 接入方式

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%40latest%22%5D%7D) [![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%40latest%22%5D%7D&quality=insiders) [![Install in Cursor](https://img.shields.io/badge/Cursor-Install_Server-000000?style=flat-square&logo=cursor&logoColor=white)](https://cursor.com/en/install-mcp?name=portal&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJwb3J0YWwtbWNwLXNlcnZlckBsYXRlc3QiXX0=)

`portal-mcp-server` 是一个本地 stdio MCP server，所有支持 MCP 的 host 都能接入。下面给常见 host 的最小配置——`uvx` 会自动从 PyPI 拉取并缓存，后续启动秒级。

> 如果 MCP client 找不到 `uvx`，用 `which uvx`（Windows 用 `where uvx`）查完整路径，并把 `command` 改成该绝对路径。

### 通用配置片段

> 大多数 host 都接受 `{ "mcpServers": { "<name>": { "command": ..., "args": [...] } } }` 这种顶层 schema；VS Code 和 Codex 用各自专有 schema，单独列出。

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": ["portal-mcp-server@latest"]
    }
  }
}
```

如果需要传环境变量（指向自定义的 hosts/policies/log 路径），追加 `env`：

```json
"env": {
  "PORTAL_HOSTS_YAML": "/path/to/hosts.yaml",
  "PORTAL_POLICIES_YAML": "/path/to/policies.yaml",
  "PORTAL_LOG_DIR": "/path/to/logs"
}
```

> 💡 **建议顺手设一个较小的默认超时** `PORTAL_DEFAULT_TIMEOUT`（秒）。内置默认是保守的 1h，而 `portal_exec` / `portal_shell` 执行期间会发 keepalive 心跳，让 MCP client **不会**主动掐断一个挂起的调用——也就是说 `timeout` 几乎是唯一的真正截断点。设成例如 `"PORTAL_DEFAULT_TIMEOUT": "120"`，能让忘了显式传 `timeout` 的挂起命令快速失败而不是占满整窗口；agent 对真正慢的命令（构建 / 安装）仍可按需用 per-call `timeout` 调大。

### Claude Code CLI

直接编辑 `<project>/.mcp.json`（同上 schema），或用 CLI / 斜杠命令登记：

```bash
# 推荐：user 级，对所有 repo 生效
claude mcp add --scope user portal -- uvx portal-mcp-server@latest

# 不加 --scope 默认是 local，只在「当前目录」生效，换个目录 claude mcp list 就看不到
claude mcp add portal -- uvx portal-mcp-server@latest
# 或在 Claude Code 会话内输入 /mcp 交互登记
```

> ⚠️ Claude Code 有三档 scope：`local`（**默认**，仅当前目录）、`user`（所有 repo）、`project`（写进 repo 的 `.mcp.json`，随仓库共享）。要「装一次处处可用」**务必带 `--scope user`**——这点和 Codex（`mcp add` 即 global）/ Copilot CLI（`mcp add` 即 User 级）不一样，最易踩坑。

<details>
<summary><b>GitHub Copilot CLI</b></summary>

写 `<project>/.mcp.json` 即在该项目内生效；或一行命令登记到 user 级（对所有项目生效）：

```bash
copilot mcp add portal -- uvx portal-mcp-server@latest
# 或在 Copilot CLI 会话内输入 /mcp 走交互登记
```

验证：

```bash
copilot mcp list                # 应看到 portal
copilot mcp get portal          # 检查 Source 是 Workspace / User
```

</details>

<details>
<summary><b>Cursor</b></summary>

点上方 「Install in Cursor」badge 即可一键安装；或手动把通用片段写进 `~/.cursor/mcp.json`（全局生效）或 `<project>/.cursor/mcp.json`（仅当前项目）。Cursor → Settings → Tools & MCP 里能看到 `portal` 并启用。

</details>

<details>
<summary><b>VS Code（Copilot Chat / Agent mode）</b></summary>

点上方 「Install in VS Code」badge 即可一键安装；或手动写入 `<project>/.vscode/mcp.json`（VS Code 用专有 schema，顶层 key 是 `servers` 而非 `mcpServers`）：

```json
{
  "servers": {
    "portal": {
      "type": "stdio",
      "command": "uvx",
      "args": ["portal-mcp-server@latest"]
    }
  }
}
```

要全局生效，可以把同样的 `servers` 段写进 VS Code 用户 `settings.json` 的 `mcp` 字段（路径随 OS 不同）。

> 与 `mcpServers` 不兼容；同时用 Copilot CLI / Claude Code / Cursor 和 VS Code 时需各维护一份。

</details>

<details>
<summary><b>Claude Desktop</b></summary>

把通用片段贴到 `claude_desktop_config.json` 的 `mcpServers` 下，重启 Claude Desktop。配置文件位置：

- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`

</details>

<details>
<summary><b>Windsurf</b></summary>

Windsurf 用同一份 `mcpServers` schema。在 Cascade 面板点插件按钮 → 「Manually configure MCP」，把通用片段写进 `~/.codeium/windsurf/mcp_config.json`，回 Cascade 启用即可。

</details>

<details>
<summary><b>OpenAI Codex CLI</b></summary>

新版 Codex 直接一行命令登记（global，对所有目录生效）：

```bash
codex mcp add portal -- uvx portal-mcp-server@latest
codex mcp list          # 应看到 portal
```

或手动编辑 `~/.codex/config.toml`（旧版，或想精细控制时）：

```toml
[mcp_servers.portal]
command = "uvx"
args = ["portal-mcp-server@latest"]
```

启动 Codex 后在 TUI 输入 `/mcp` 确认 `portal` 已加载。

</details>

<details>
<summary><b>其它 host（Cline / Continue / Roo Code / Zed …）</b></summary>

- **Cline / Continue / Roo Code 等 VS Code 插件**：通常都接受 `{ "mcpServers": ... }` 通用片段，写到各自插件的 MCP 设置面板或工作区配置即可
- **任意 MCP 兼容 host**：把通用片段贴到该 host 的 MCP 配置入口；stdio 不需要额外代理

</details>

## 环境变量

portal-mcp-server 的全部可配置项都通过环境变量传入；统一 `PORTAL_*` 前缀，避免和 OpenSSH 自带的 `SSH_*`、或其他 MCP server 的命名空间冲突。在 MCP client 的 `env` 字段里设置即可——这些变量只对 MCP server 子进程生效，不影响其他程序。

> **v1.1.0 重命名提醒**：1.0.x 时期的 `SSH_*` / `SSH_MCP_*` / `MCP_*` 三套前缀已统一改成 `PORTAL_*`，不向后兼容。从 1.0.x 升级时按下表照单全收一次即可。完整迁移表见 [CHANGELOG](./CHANGELOG.md)。

### 总览

| 分类 | 变量 | 用途一句话 |
|---|---|---|
| 文件路径 | `PORTAL_HOSTS_YAML` | 主机注册 YAML |
| 文件路径 | `PORTAL_POLICIES_YAML` | 安全策略 YAML |
| 文件路径 | `PORTAL_SECRETS_YAML` | 命名密钥 YAML（`portal_exec` / `portal_local_exec` 的 `secrets=` 参数解析源） |
| 文件路径 | `PORTAL_SSH_CONFIG` | OpenSSH 客户端 config 路径（`ssh -F` 等价物，详见下方"文件路径"节） |
| 文件路径 | `PORTAL_LOG_DIR` | audit + server log 目录 |
| 安全与认证 | `PORTAL_AUDIT_FAIL_OPEN` | audit 写盘失败时是否 fail-open |
| 安全与认证 | `PORTAL_AUDIT_MAX_BYTES` | `audit.jsonl` 轮转阈值（字节，默认 10 MiB） |
| 安全与认证 | `PORTAL_AUDIT_BACKUPS` | 保留的轮转文件数 `audit.jsonl.1..N`（默认 5） |
| 安全与认证 | `PORTAL_AUTH_TOKEN` | HTTP transport 的 Bearer token |
| 连接池 | `PORTAL_SSH_POOL_SIZE` | 每 host 最大 TCP 连接数 |
| 连接池 | `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | 每条 TCP 最大并发 channel 数 |
| 连接池 | `PORTAL_SSH_MAX_IDLE_TIME` | 空闲连接自动关闭超时（秒） |
| 连接池 | `PORTAL_SSH_MAX_CONN_AGE` | 连接最大存活时间（秒） |
| 后台任务 | `PORTAL_JOB_PERSIST` | `portal_job` 任务表是否跨重启持久化（默认开；`0`/`false` 关） |
| 后台任务 | `PORTAL_JOB_STATE_FILE` | 任务表持久化文件路径（默认 `<state>/jobs.json`） |
| 后台任务 | `PORTAL_JOB_MAX_LIVE` | 并发存活后台任务上限（默认 50） |
| 后台任务 | `PORTAL_JOB_TTL` | 完成任务在表中保留多少秒后清理 + 删远端 tmp（默认 3600） |
| 可靠性 | `PORTAL_BASH_HEARTBEAT_INTERVAL` | `portal_shell` 执行期间 keepalive 心跳间隔（秒） |
| 可靠性 | `PORTAL_DEFAULT_TIMEOUT` | `portal_exec` / `portal_shell` / `portal_local_exec` 省略 `timeout` 时的默认每命令超时（秒）；per-call `timeout` 覆盖之 |
| 远端读 | `PORTAL_READ_MAX_LINES` | `portal_read` 省略 `limit` 时每页返回的最大行数（默认 2000） |
| 远端读 | `PORTAL_READ_MAX_BYTES` | `portal_read` 每页返回内容的最大字节数，避免大文件撑爆 MCP 客户端的内联输出阈值（默认 16384） |
| 测试（仅 dev） | `PORTAL_TEST_LIVE` | 是否执行真实 SSH 集成测试 |
| 测试（仅 dev） | `PORTAL_TEST_HOST` / `PORTAL_TEST_PORT` / `PORTAL_TEST_USER` / `PORTAL_TEST_KEY_PATH` | live 测试目标 |

下面分类详述。

### 文件路径

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_HOSTS_YAML` | 主机注册 YAML | `~/.config/portal-mcp-server/hosts.yaml` |
| `PORTAL_POLICIES_YAML` | 安全策略 YAML | `~/.config/portal-mcp-server/policies.yaml` |
| `PORTAL_SECRETS_YAML` | 命名密钥 YAML | `~/.config/portal-mcp-server/secrets.yaml` |
| `PORTAL_SSH_CONFIG` | OpenSSH 客户端 config 路径 | `~/.ssh/config` |
| `PORTAL_LOG_DIR` | audit + server log 目录 | `~/.local/state/portal-mcp-server/log/` |

> `PORTAL_SSH_CONFIG` 是 portal 版的 `ssh -F`：OpenSSH 本身**不读任何环境变量**改 config 路径，只能靠 `-F`；portal 是常驻 daemon，没有逐次连接的命令行参数，故用此变量，并**完全仿照 `-F`**：设成**绝对路径**就**只读这一个文件**（和 `-F <file>` 一样，连系统级 `/etc/ssh/ssh_config` 也一并抑制）；设成字面量 **`none`（大小写不限）就一个 config 文件都不读**（等价 `ssh -F none`，主机解析全靠 hosts.yaml）；**不设**时读用户级 `~/.ssh/config` + 系统级 `/etc/ssh/ssh_config`（Windows 为 `%PROGRAMDATA%\ssh\ssh_config`）作 fallback，用户级优先（OpenSSH 的"先取到的值生效"）。绝对路径/`none` 之外的相对值告警并忽略。解析整段复用 asyncssh 的 config 解析器（`Include` 原生跟进、多端 `~` 展开）；asyncssh 自身的"找 config"只有一行 `~/.ssh/config`、不读系统级，故系统级 fallback 与 `-F`/`-F none` 这层是 portal 自己补的。

路径解析优先级：**环境变量 > XDG 目录**（`$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` 受 spec 支持）。当前工作目录 **不** 参与解析——`portal-mcp-server` 是用户级常驻服务，不是项目工具，cwd-relative 自动加载会让任意工作目录默默劫持你的真实配置（`ssh` / `gh` / `docker` / `kubectl` / `rclone` 等用户级 CLI 均不这么做）。

仓库的 [`examples/`](./examples/) 目录是 schema 模板——里面所有 `*.yaml` 都是**只读示例**，不会被自动加载。第一次使用从模板拷贝到 XDG 目录：

```bash
mkdir -p ~/.config/portal-mcp-server
cp examples/hosts.yaml    ~/.config/portal-mcp-server/hosts.yaml
cp examples/policies.yaml ~/.config/portal-mcp-server/policies.yaml
cp examples/secrets.yaml  ~/.config/portal-mcp-server/secrets.yaml
# 然后把 ~/.config/portal-mcp-server/*.yaml 改成你的真值
```

**`~/.config/portal-mcp-server/hosts.yaml` 含真实凭据，永远别 commit**。

> **v2.0.0 breaking changes**：
> - 移除了 `./config/hosts.yaml` / `./config/policies.yaml` / `./logs/` 的 cwd-relative 兜底——现在只走 env > XDG
> - 仓库内 `config/` 目录改名为 `examples/`，文件不再带 `.example.` 中缀（目录名本身承担"模板"语义）

### 安全与认证

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_AUDIT_FAIL_OPEN` | 设 `1` → audit 写盘失败时仅 warning 并继续；默认 → **fail-closed**，audit 写不进则操作 raise 中止 | _(unset)_ |
| `PORTAL_AUTH_TOKEN` | HTTP transport（`--transport streamable_http`）的 Bearer token；stdio 模式不需要 | _(none)_ |

### 连接池

控制 asyncssh 进程内连接池的行为。默认值适合大多数场景，仅在高并发或特殊网络环境下需要调整。详细的池行为说明见 [§ 进程内连接池](#进程内连接池)。

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_SSH_POOL_SIZE` | 每 host 最大 TCP 连接数。连接池满且所有连接都达到 channel 上限时，会复用最空闲的连接（带 warning） | `5` |
| `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | 每条 TCP 上最大并发 channel 数（SFTP 会话、exec、tunnel 等共享）。超出后新建 TCP，直到 `PORTAL_SSH_POOL_SIZE` 上限 | `5` |
| `PORTAL_SSH_MAX_IDLE_TIME` | 无活跃 channel 的连接空闲多久后自动关闭（秒）。设 `0` 禁用 | `600`（10 分钟） |
| `PORTAL_SSH_MAX_CONN_AGE` | 连接最大存活时间（秒），超龄且无活跃 channel 时关闭。防止防火墙 / NAT 静默断连 | `3600`（1 小时） |

### 可靠性

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_BASH_HEARTBEAT_INTERVAL` | `portal_shell` / `portal_exec` / `portal_local_exec` 在命令执行期间每隔多少秒发一条 MCP progress 通知作 keepalive。命令无输出也不会让 client 撞 idle 超时（JSON-RPC `-32001`）；与服务端 `timeout` 参数相互独立。非正数或非法值回退到默认 | `5`（秒） |
| `PORTAL_DEFAULT_TIMEOUT` | `portal_exec` / `portal_shell` / `portal_local_exec` 在 agent **省略** `timeout` 参数时使用的默认每命令超时（**单位：秒**）。三个工具统一走这一个口子，内置默认都是保守的 1h——因为执行期间的 keepalive 心跳会让 client 不主动中断挂起的调用，`timeout` 几乎是唯一真正的截断点。调小（如 `120`）可让挂起命令快速失败而非占满整窗口。**agent 每次调用显式传的 `timeout` 永远覆盖此默认**；调小默认不影响 agent 对慢命令按需调大。在 import 时读取一次（即随 MCP server 进程的 `env` 块生效）。非正数 / 非法值回退到内置默认 | 内置 `3600`（即 1h） |

### 测试（仅 dev）

只在跑 `tests/` 时用到，正常 MCP 部署不需要设置。详细测试用法见 [§ 测试](#测试)。

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_TEST_LIVE` | 设 `1` / `true` / `yes` 才会运行 `tests/test_live_ssh.py` 中的真实 SSH 测试；否则全部 skip | _(unset)_ |
| `PORTAL_TEST_HOST` | live 测试目标主机 | `127.0.0.1` |
| `PORTAL_TEST_PORT` | live 测试目标端口 | `22` |
| `PORTAL_TEST_USER` | live 测试登录用户 | `$USER` 或 `root` |
| `PORTAL_TEST_KEY_PATH` | live 测试用的私钥路径 | `~/.ssh/id_ed25519` |

### 完整示例

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": ["portal-mcp-server@latest"],
      "env": {
        "PORTAL_HOSTS_YAML": "/home/me/.config/portal-mcp-server/hosts.yaml",
        "PORTAL_POLICIES_YAML": "/home/me/.config/portal-mcp-server/policies.yaml",
        "PORTAL_SSH_POOL_SIZE": "10",
        "PORTAL_SSH_MAX_CHANNELS_PER_CONN": "8"
      }
    }
  }
}
```

## 认证

按你的认证方式跳——优先 SSH key，passphrase 优先走 ssh-agent；密码登录支持 `password_command` 或 `portal ssh set`，命令行明文密码从不进 LLM。

### 凭据流总览

口令/密钥类凭据一共五条流，各自的"密码管理器派（命令源）"和"无回显交互派（getpass + systemd --user 凭据 agent）"如下（**按当前实现**）：

| 凭据流 | 命令源（密码管理器派） | 无回显交互入口（getpass 派） | 缓存 key | 缓存语义 | 触发点 |
|---|---|---|---|---|---|
| **A. 远程 SSH 登录密码** | `password_command`（hosts.yaml） | ✅ `portal ssh set <host>` | host | agent 内存 TTL（默认 900s，仅交互入口；命令源每次现取） | `auth: password` 连接时 / 密钥失败时自动 fallback |
| **B. SSH key passphrase** | `passphrase_command`（hosts.yaml） | ✅ `portal passphrase set <host>` | host | agent 内存 TTL（默认 900s） | 本地解锁加密私钥 |
| **C. 远程 sudo 执行** | `sudo_password_command`（hosts.yaml） | ✅ `portal sudo set <host>` | host | agent 内存 TTL（默认 900s） | `portal_exec(use_sudo=True)` |
| **C2. 本地 sudo 执行** | 顶层保留段 `<local>:` 的 `sudo_password_command`（hosts.yaml） | ✅ `portal sudo set-local` | `<local>`（保留身份） | agent 内存 TTL（默认 900s） | `portal_local_exec(use_sudo=True)` |
| **D. secret 注入·远程** | `secrets.yaml` 的 `command`（每次现取） | ✅ `portal secret set <name>` | name | agent 内存 TTL（默认 900s，`--ttl` 可调） | `portal_exec(secrets=[…])` |
| **E. secret 注入·本地** | 同 D（共用 `secrets.yaml`） | 同 D（共用 `portal secret set`） | 同 D | 同 D | `portal_local_exec(secrets=[…])` |

几点要知道：

- **D 和 E 是同一套凭据管道**——共用 `secrets.yaml` + `portal secret set` + 同一个 per-user credential agent + 同一个按 name 的 TTL 缓存，区别只在消费它的工具不同（远程走 SSH stdin 注入 / 本地走 subprocess env）。
- **A、B、C、D 的交互入口共用一个 per-user agent socket**，但 agent 内部按 `ssh` / `passphrase` / `sudo` / `secret` kind 分开 key 空间：A 的密码进 `asyncssh.connect()` 做 SSH 握手，B 的 passphrase 只用于本地解锁私钥，C 的密码在握手后喂 `sudo -S`，D/E 作为环境变量注入命令。
- **A 的回落顺序**：`auth: password` 主动登录走 `cache（portal ssh set）→ password_command → 错误`；纯密钥 host 在 asyncssh 抛 `PermissionDenied` 时自动 retry 一次密码路径（同一条 chain），有 cache 或 `password_command` 才 retry，否则原异常透传——免得"配置缺失"的报错盖掉"密钥真不对"的真因。
- **交互入口（getpass 派）= per-user agent 内存 TTL 缓存**：默认 900 秒、TTL 内可复用、到期自动清、agent 重启即丢、从不落盘。**命令源（密码管理器派）= 每次现取**，无 TTL。
- **明文永不离开 agent 内存**：CLI 故意没有 `show plaintext` 动词；`portal {ssh,passphrase,sudo,secret} show <key>` 只回 sha256[:16] 指纹 + 剩余 TTL，`list` 汇总，`confirm` 二次输入比对。明文只交给同 uid 的真消费者（asyncssh / 本地 key 解锁 / `sudo -S` / `$env` 注入）。完整 rationale 见上文 [凭据 agent](#凭据-agentlinux-systemd--macos-launchd--windows-计划任务) 段。

#### 四套凭据机制：实现与为什么

四种凭据走四套**不同**机制，不是随意挑的——每种凭据的消费方决定了注入方式：

| 凭据类型 | 实现 | 为什么这么选 |
|---|---|---|
| **SSH 登录密码** | asyncssh `password=` 参数（SSH 协议级），源：`password_command` / `portal ssh set` 缓存 | SSH 协议原生支持密码认证，直接走协议帧最干净 |
| **SSH key passphrase** | asyncssh `passphrase=` 参数，源：ssh-agent → `portal passphrase set` 缓存 → `passphrase_command`；或 `use_ssh_agent` 纯走 agent | 本地解密 key，passphrase 不出当前进程。它和 SSH 登录密码分开缓存，避免把本地私钥解锁口令误当成远端登录密码或 sudo 密码 |
| **sudo 密码** | `sudo -S` + 喂 stdin（`conn.run(input=pw)`），源：`sudo_password_command` / `portal sudo set` 缓存 | sudo 只认 `-S`/`-A`/tty，不读 env。`-S` 安全暴露面最窄：密码寿命极短（读完即弃）、无远端落地物、不进 env（对比 `-A` askpass 要落临时 helper 文件 + helper 进程 env 含密码）。代价：sudo 命令本体的 stdin 被密码占用、提前 EOF（curl/CLI flag-reading 工具无影响） |
| **secrets**（API tokens） | `bash -s` + stdin 喂 `export VAR=…\n<cmd>\n`，源：`secrets.yaml` `command` / `portal secret set` 缓存 | 工具普遍读 env（`GH_TOKEN`/`AWS_*`）；更纯的 SSH 协议级 env 帧被 sshd `AcceptEnv` 白名单（默认只 `LANG`/`LC_*`）卡住到不了远端，只能这么 workaround。值短暂在 bash stdin 解析的脚本串里，但 bash 即用即弃、不进 argv（不在 `ps`）、不进 log，比 `--token=xxx` 走 argv 窄得多 |

#### ⚠️ 配置这些密码的风险（务必读）

key-only 登录是最安全的基线。**一旦你给某台 host 配了 SSH 登录密码 / sudo 密码 / secret，就等于授权"任何能调用这个 MCP server 的 agent"在凭据有效期内代你做特权操作**——agent 不需要再问你、也不会再被系统弹密码挡住。两条配置路各有取舍：

| 配置方式 | 存活/暴露 | 风险定性 |
|---|---|---|
| **永久（密码管理器命令）** `sudo_password_command` / `password_command` / `secrets.yaml` 的 `command:` | 每次连接**现取**、无 TTL，只要你的密码库（`pass`/`op`/`bw`）处于解锁态就一直可用 | 暴露窗口 = 密码库解锁时长。命令写在 hosts.yaml/secrets.yaml（**配置文件，别进 git**），值不落盘但 agent 随时能取 |
| **临时（无回显 set）** `portal {ssh,passphrase,sudo,secret} set <key>` | 进 per-user 凭据 agent **内存**，默认 900s TTL，到期自动清、agent 重启即丢、**从不落盘** | 暴露窗口 = TTL。blast radius 最小，**优先用这条**；只在确需无人值守自动化时才上密码管理器命令 |

要点：

- **高风险操作会在返回值里标记**：`portal_exec(use_sudo=True)` / `secrets=[...]` 和 `portal_local_exec(secrets=[...])` 的结果带 `"high_risk": true` + `"high_risk_note"`，调用方 agent 被要求**简要告知你它用你的密码/secret 跑了特权命令**，或在你明确许可下才做。把这当成"agent 替你 sudo 了一次"的回执。
- **缩小爆炸半径**：能用 key-only 就别配密码；能用临时 `set`（TTL）就别配密码管理器命令；sudoers 尽量按命令收窄而不是给全权 NOPASSWD；定期看 `audit.jsonl`（每次 `sudo`/secret 注入都有结构化记录）。
- **凭据永不进 LLM 上下文**：所有路径下密码/secret 都不作为 MCP 工具参数、不进 `ps` argv、不进 audit/log——但"能驱动这个 agent 的人"在凭据有效期内**实际拥有**这些特权，这是配置密码的固有代价。
- **首次用 `portal {kind} set` 会自动装 agent**：若凭据 agent 还没起，`set` 会自动执行等价于 `portal agent install --now` 的安装并把安装输出打给你看，然后再让你无回显输入——不用先手动 `agent install`。


### SSH key（首选）

用 ed25519 即可：

```bash
ssh-keygen -t ed25519 -C "you@example.com"
ssh-copy-id -i ~/.ssh/id_ed25519.pub user@your-host
```

GitHub 也接收同一把 key——把公钥加到账号上的官方步骤：[Generating a new SSH key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent) 与 [Adding a new SSH key to your GitHub account](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account)。

### 加密私钥：ssh-agent

一次解锁、长期复用，asyncssh 通过 `$SSH_AUTH_SOCK` 自动认到：

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519        # 输一次 passphrase
```

headless / CI 跑不动 ssh-agent 时，可在 `hosts.yaml` 写 `passphrase_command:`（见下）。

### 密码登录：`password_command` 或 `portal ssh set`

兼容历史不让换 key 的远端机器。两条铁律：

1. **绝不** 在 `hosts.yaml` 写 `password: 明文`——启动会 ERROR 拒绝、字段被丢
2. **绝不** 通过 MCP 工具传——`portal_host` 没有 password 参数，密码不会进 LLM tool-call trace

两条来源（顺序：agent 内存缓存 → `password_command` → 报错），同 sudo / secret 一脉相承：

1. **密码管理器（1a，全自动）**——hosts.yaml 里 `auth: password` + 一段输出密码到 stdout 的 shell 命令，思路同 Borg 的 `BORG_PASSCOMMAND`、restic 的 `RESTIC_PASSWORD_COMMAND`、msmtp 的 `passwordeval`：

   ```yaml
   hosts:
     legacy-host:
       host: 10.0.0.40
       user: admin
       auth: password
       # CI / 环境变量（GitHub Secrets、Vault 注入到 env 后直接取）：
       password_command: printf '%s' "$LEGACY_HOST_PASSWORD"
       # 或从密码管理器拉：
       # password_command: pass show ssh/legacy-host
       # password_command: bw get password legacy-host
       # password_command: op read "op://Private/legacy-host/password"
   ```

2. **临时塞入（1b，`portal ssh set`，交互一次）**——在**另一个终端**（不是 agent 对话）跑：

   ```bash
   portal ssh set legacy-host                  # getpass 隐藏输入，不回显
   portal ssh set legacy-host --ttl 1800       # 自定义 TTL（秒），默认 900（15 分钟）
   portal ssh confirm legacy-host              # 二次输入比对，匹配才更新
   portal ssh show legacy-host                 # 看 sha256 指纹 + 剩余 TTL（无明文）
   portal ssh list                             # 汇总所有已缓存 host
   portal ssh clear legacy-host                # 删掉这条
   ```

   密码经 systemd --user 管理的本地 unix socket 推进 per-user credential agent 内存缓存：`.socket` unit 监听 `%t/portal-mcp-server/credentials.sock`，安装器在 `agent.json` 记录解析后的绝对路径，目录 0700 / socket 0600，agent 用 `SO_PEERCRED` 校验对端 uid。密码**从不落盘、从不进 LLM**，TTL 到期自动清除。即使 host 没在 `hosts.yaml` 里写 `password_command`、甚至根本是默认的密钥模式（hosts.yaml 不写 `auth:` 字段），`portal ssh set` 推一条进去就能用。

#### 自动 fallback：密钥失败 → 密码

密钥模式的 host（即默认；hosts.yaml 不写 `auth:` 字段）在 asyncssh 抛 `PermissionDenied` 时，会自动 retry 一次密码路径（agent cache → `password_command`），有源才 retry。**没源**（既没 `portal ssh set` 缓存也没 `password_command`）时原 `PermissionDenied` 直接透传——避免"我以为是密钥坏，实际是配置漏了"。所以**密钥首选**仍然成立，密码是 opt-in 的兜底。

运行时行为：`password_command` 10 秒超时，结尾换行剥掉一个，stderr 永不进日志（防泄密），非 0 退出 / 空输出 / 非 UTF-8 输出全部硬失败。设计细节（为什么 `shell=True`、为什么强制 `client_keys=[]`、为什么 stderr 不进日志…）见 **[`SECURITY.md` § Authentication](./SECURITY.md#authentication)**。

### 加密私钥的 passphrase：`portal passphrase set` / `passphrase_command` / `use_ssh_agent`

passphrase 是**本地私钥解锁口令**，不是远端 SSH 登录密码，也不是 sudo 密码。它有独立的交互入口和 agent kind，避免把私钥 passphrase 误用于远端认证。

完整 passphrase 优先级链（按顺序尝试）：

1. **ssh-agent**（`$SSH_AUTH_SOCK`，用户已 `ssh-add` 解锁过的 key）—— **首选**，最常用、key 永不出 agent
2. **agent 缓存**（`portal passphrase set <host>` 临时无回显输入，TTL 缓存）—— 临时用一阵子
3. **`passphrase_command`**（密码管理器拉）—— headless / CI
4. asyncssh 默认加载 key 文件

```bash
portal passphrase set encrypted-key-host
portal passphrase confirm encrypted-key-host
portal passphrase show encrypted-key-host
```

```yaml
hosts:
  encrypted-key-host:
    host: 10.0.0.30
    user: deploy
    key: ~/.ssh/encrypted_key
    passphrase_command: pass show ssh/encrypted_key
    # 显式控制 ssh-agent：true=纯走 agent（不传 client_keys，用 agent 持有的 key）；
    # false=硬禁用 agent（只用 key 文件）；省略=auto（asyncssh 自己兜底试 SSH_AUTH_SOCK）
    use_ssh_agent: true
```

ssh-agent 跑得起来时**首选** agent（链路第 1 条），体验最好；`passphrase_command` 只在 headless / CI 没有交互终端的场景用。

### 非交互 sudo：`use_sudo` + `portal sudo set`

`portal_exec(host, cmd, use_sudo=True)` 让 agent 跑需要 root 的命令，但 **sudo 密码永远不进 LLM**——`portal_exec` 没有 password 参数，密码由 server 端就地解析。两条来源（同 SSH 密码一样的哲学）：


1. **密码管理器（1a，全自动）**——`hosts.yaml` 里给 host 配 `sudo_password_command`，机制与 `password_command` 完全对称：

   ```yaml
   hosts:
     prod-box:
       host: 10.0.0.50
       user: deploy
       sudo_password_command: pass show sudo/prod-box   # 或 op read / bw get / printf "$ENV"
   ```

   如果这台机器的 sudo 密码**明确就是同一个用户的 SSH 登录密码**，也可以不用单独的 sudo 命令源，而是在 host 上显式声明：

   ```yaml
   hosts:
     legacy-host:
       host: 10.0.0.40
       user: admin
       auth: password
       sudo_password_same_as_ssh: true
   ```

   之后 `portal ssh set legacy-host` 会把同一个无回显输入同时缓存到 `ssh` 和 `sudo` 两个 kind。默认是 `false`，不写这个字段时仍然必须单独 `portal sudo set legacy-host` 或配置 `sudo_password_command`。这个开关只复用 **SSH 登录密码**，不会复用 `portal passphrase set` 里的私钥 passphrase。

2. **临时塞入（1b，交互一次）**——在**另一个终端**（不是 agent 对话）跑：

   ```bash
   portal sudo set prod-box                  # getpass 隐藏输入，不回显
   portal sudo set prod-box --ttl 1800       # 自定义 TTL（秒），默认 900（15 分钟）
   portal sudo confirm prod-box              # 二次输入比对
   portal sudo show prod-box                 # 看 sha256 指纹 + TTL（无明文）
   portal sudo list                          # 汇总
   ```

   密码经 systemd --user 管理的本地 unix socket 推进 per-user credential agent 内存缓存：`.socket` unit 监听 `%t/portal-mcp-server/credentials.sock`，安装器在 `agent.json` 记录解析后的绝对路径，目录 0700 / socket 0600，仅本用户可达。密码**从不落盘、从不进 LLM**，TTL 到期自动清除。

取密码顺序：**agent 内存缓存（1b）→ `sudo_password_command`（1a）→ 报错**（提示去 `portal sudo set` 或配 `sudo_password_command`）。

实现要点：`use_sudo` 走一次性 `conn.run(input=pw, ...)` 执行 `sudo -S -k -p '' -- bash -c <cmd>`，**不**复用持久 `portal_shell` 会话（`sudo -S` 要从 stdin 读密码，而持久会话的 PTY 没有喂密码的通道——裸 sudo 在那里会被自动 Ctrl-C 软取消、并保留会话）。因此 sudo 命令**不继承** 之前 `portal_shell` 调用里 `cd` / `export` 出来的 cwd / env；需要的话在同一条命令里自带 `cd ... && ...`。`-k` 强制每次重新认证，`-p ''` 抑制 prompt 文本。交互式 sudo（要 TTY、要改密码）仍然 `portal_shell` 处理不了，让用户 `ssh -t host sudo ...`。

#### 本地 sudo：`portal_local_exec(use_sudo=True)`

上面讲的是**远端** sudo。MCP server 自己也常需要 sudo（它无法以"对应用户"身份运行），所以 `portal_local_exec(use_sudo=True)` 在 **MCP server 本机**跑特权命令，走本地 `sudo -S -k`，密码同样**不进 LLM / 不进 argv / 不落盘**。它复用远端那套凭据解析，只是身份是保留的 **`<local>`**：

- 临时：`portal sudo set-local`（无回显，进凭据 agent，`--ttl` 可调）；
- 永久：`hosts.yaml` 里一个**顶层保留段 `<local>:`**（与 `hosts:` 平级，是保留字段、**不是** host）放 `sudo_password_command`：

  ```yaml
  hosts:
    # ... 你的远端主机 ...
  "<local>":                                # 顶层保留字段，专指本机 local_exec
    sudo_password_command: pass show sudo/this-box
  ```

取密码顺序同远端：**agent 缓存 → 顶层 `<local>:` 的 `sudo_password_command` → 报错**。`use_sudo` 可与 `secrets` 同时使用；结果标 `high_risk`，审计记为 `local_exec_sudo`。

> **`<local>` / `local` / `localhost` 别混**：`<local>` 是**保留身份**，专指 MCP server 本机（`portal_local_exec`，不走 SSH），尖括号是 hostname 非法字符，**永不**和真实主机撞名；`localhost` 是一台**普通 SSH host**（连 `127.0.0.1:22`，要 sshd + 公钥/密码），被所有远端工具用；你若在 `hosts.yaml` / ssh config 里把某台远端命名为 `local`，它也只是一台**普通远端 host**，与本机 `<local>` 互不相干。

### 命名 secret 注入：`secrets=[…]` + `portal secret set`

需要给命令一个 API token（GitHub token、部署密钥等）、又**不想让它进 session 历史、不想发给第三方 LLM 后端**时用这个。和 sudo 密码同一套威胁模型：agent 只传 secret 的**名字**，server 端解析出值、作为**环境变量**注入一次性命令，值经进程环境 / SSH stdin 传递（不进 argv，所以 `ps` 和审计都看不到），命令输出里任何对该值的回显都会在返回给 agent 前替换成 `***`。

> **为什么不直接 `export`？** 痛点在于：临时 `export TOKEN=…` 注入不进 agent 的执行上下文——它只对你手里那个新开的终端生效，agent 跑命令用的是 MCP server 进程的环境，根本看不到。要让 agent 用上，过去只能 `vim` 一个 `.env` / secrets 文件让它去 source，于是 secret 又落了盘、又容易忘删。这个设计把"临时给一次密钥"做成了**原生的无回显 CLI 输入**（`portal secret set` 走 `getpass`，和你平时输密码一样），值只进 per-user credential agent 内存、带 TTL 自动过期，既不落盘也不进 LLM。

- 远程：`portal_exec(host, cmd, secrets=["github_token"])`，命令里写 `$GITHUB_TOKEN`（secret 名大写）。
- 本地：`portal_local_exec(cmd, secrets=["github_token"])`，在 **MCP server 本机**跑命令（不走 SSH）。本地执行偏离了本项目以远端编排为核心的设计目标、是实用但 off-target 的衍生功能，**默认关闭**，须给 server 进程显式设 `PORTAL_ALLOW_LOCAL_EXEC=1` 才开。

两条来源（顺序：agent 内存缓存 → `secrets.yaml`）：

1. **secret 管理器（secrets.yaml）**——和 `password_command` 对称，写一条打印 secret 到 stdout 的命令：

   ```yaml
   secrets:
     github_token:
       command: pass show api/github      # 或 op read / printf "$ENV"
   ```

2. **临时塞入（`portal secret set`，交互一次）**——在**另一个终端**跑：

   ```bash
   portal secret set github_token              # getpass 隐藏输入，不回显
   portal secret set github_token --ttl 1800   # 自定义 TTL（秒），默认 900
   portal secret confirm github_token          # 二次输入比对
   portal secret show github_token             # 看 sha256 指纹 + TTL（无明文）
   portal secret list                          # 汇总所有已缓存的 secret 名
   ```

   值经 systemd --user 管理的本地 unix socket 推进 per-user credential agent 内存缓存：`.socket` unit 监听 `%t/portal-mcp-server/credentials.sock`，安装器在 `agent.json` 记录解析后的绝对路径，目录 0700 / socket 0600，仅本用户可达。值**从不落盘、从不进 LLM**，TTL 到期自动清除。

完整配置见 [`examples/secrets.yaml`](./examples/secrets.yaml)。`secrets` 可与 `use_sudo` 在同一次 `portal_exec` 调用里同时使用。

#### 实现细节：sudo + secrets 如何共存

`use_sudo` 与 `secrets` 共用**同一条 stdin**：sudo 密码先送入，随后按 `secrets` 列表顺序送入每个值。真正的命令被包了一层小前缀，在 sudo **完成 `env_reset`（默认会清空继承的环境变量）之后**，于已提权的 shell 内逐行 `read` 回这些值再 `export`：

```
IFS= read -r GITHUB_TOKEN
export GITHUB_TOKEN
<原始命令>
```

因为 `export` 发生在 `env_reset` **之后**，值不会被清掉；因为值走 stdin 而非命令行参数，`ps`、shell 历史、审计日志都看不到它，也不需要任何 sudoers `env_keep` 配置。`portal_exec`（远端）与 `portal_local_exec`（本机）两条路径实现一致，源码见 `secrets_store.py` 的 `sudo_stdin_secret_script()`。

#### 等待语义：fail-fast → `ask_user` → 重试

无回显输入天然要"等人输完"，但**这个等待绝不挂在 agent 的关键路径上**——MCP server 通常 headless、没有用户的 tty，既弹不出 `getpass`、也不该把工具调用阻塞到撞超时。所以约定是：

1. **fail-fast**：secret（或 sudo 密码）没就绪时，工具**立刻返回错误、命令不执行**，错误串里不含任何值。
2. **让 agent 把球踢给用户**：错误串显式建议 agent 用 `ask_user` 这类**要求用户输入/选择的工具**，请用户在**另一个终端**跑 `portal secret set <name>` / `portal sudo set <host>`，搞定后回个"ok"；agent 收到 ok 再重试本次调用。
3. **没有这类工具就结束这轮**：若当前 agent 环境没有 `ask_user` 之类的交互工具，就**把要跑的命令告诉用户、主动结束这一轮**，等用户下一个 prompt 再重试——而不是干等或反复轮询。

于是"等待"只体现为一次正常的对话轮次交接：阻塞的是用户自己终端里的 `getpass`，agent 端永远是"查缓存 → 命中就跑 / 没命中就 fail-fast 给指令"。**绝不要让用户把值粘进对话**——那等于把它喂给了第三方 LLM，整套设计就白做了。

> **这条引导靠什么到达 agent？** 全靠**每个 `portal_*` 工具自己的 description**——MCP client 一定会把工具描述喂给模型，所以"任务需要 token 时先引导用户 `portal secret set`、而不是索取明文"这句就直接写在 `portal_exec` / `portal_local_exec` 描述的顶部。
> MCP 协议另有一个 **server 级 `instructions` 字段**（`initialize` 响应里返回，本可放一段全局凭据纪律），但规范把它定义为 *“a ‘hint’ to the model … this information **MAY** be added to the system prompt”*（[InitializeResult.instructions](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)，字段可选）——**可选、client 用不用全凭自己**。实测（2026-06）**Copilot CLI / Codex CLI / Claude Code 三家都不把它注入模型上下文**，故 portal **不依赖** server 级 instructions，凭据引导一律落在工具描述里。

### host 查找：hosts.yaml + OpenSSH ssh config

**默认查找顺序**：解析一个 host 名时，portal 按两步走，第一步命中即止——

1. **hosts.yaml**（先）：从 XDG 配置目录读（`~/.config/portal-mcp-server/hosts.yaml`，可用 `PORTAL_HOSTS_YAML` 覆盖；解析规则见[环境变量 → 文件路径](#文件路径)）。
2. **OpenSSH 客户端 ssh config**（后）：hosts.yaml 里没有该名时，再按 **OpenSSH 自己的逻辑**找——默认用户级 `~/.ssh/config` + 系统级 `/etc/ssh/ssh_config` fallback，**行为逐字对齐 `ssh -F`**。这一步的解析**直接复用 asyncssh 提供的 ssh config 解析器**（`SSHClientConfig`，原生跟 `Include`、`%` token 展开、多端 `~` 展开），portal 不自己手写扫描。

第 2 步由 `PORTAL_SSH_CONFIG` 控制（详见[环境变量 → 文件路径](#文件路径)）：设绝对路径只读该文件、不设则用户级 + 系统级 fallback、设 **`none` 则完全禁用第 2 步的 ssh config 查找**，host 解析只剩 hosts.yaml。`portal_host(action="list")` 会把两步的来源都列出来，每条带 `source` 字段标明出处。

**优先级（无字段级 merge）**：同名 host 一旦在 `hosts.yaml` 出现，就**完全覆盖** ssh config，根本不查 ssh config——不存在"hosts.yaml 补 sudo，连接参数继承 ssh config 的 ProxyJump"这种混合。两个 footgun，server 都会发 warning（经 `portal_host(action=list)` 的 `warnings` 透出，因为 stdio server 的 stderr 用户看不见）：

- 同名 host 同时在两边 → hosts.yaml 默默赢，ssh config 的 `IdentityFile`/`ProxyJump`/`User` 全失效；
- `use_ssh_config: true` 但 ssh config 没对应 alias → asyncssh 回落到默认 DNS+user+key，多半不是你要的。

**叠加配方**（连接靠 ssh config，元数据靠 hosts.yaml）：

```yaml
hosts:
  web01:                          # key 必须 == ssh config 里 Host 别名
    use_ssh_config: true          # 连接参数全从 ssh config 拿（HostName/User/Port/IdentityFile/ProxyJump…）
    tags: [web, prod]             # portal 独有：portal_exec 的 group_tag
    sudo_password_command: pass show sudo/web01   # portal 独有
```

`portal_host(action="register", name="web01")` 只给 `name` 时，会自动查 ssh config——有同名 alias 就自动登记成上面这种叠加。

**`list` 会枚举 ssh config**：`portal_host(action="list")` 不只列 `hosts.yaml`/运行时注册的 host，也会用 asyncssh 的解析器（`Include` 原生跟进）**枚举 ssh config 里的所有 `Host` 别名**（排除 `*`/`?`/`!` 通配），并解析出真实 `HostName`/`User`/`Port`。每条带一个 `source` 字段标明来源：

- `hosts.yaml` / `runtime` —— 来自 hosts.yaml 文件 / 运行时 `register`，连接参数即字段本身；
- `ssh-config` —— 只在 ssh config 里（用户级或系统级 fallback）的别名；
- `hosts.yaml+ssh-config` / `runtime+ssh-config` —— `use_ssh_config: true` 叠加：元数据（tags/sudo…）来自声明处，`host`/`user`/`port` 解析自 ssh config。

**字段对照 + 渐进补全**：基础字段全有（`host`/`port`/`user`/`key`/`known_hosts`/`strict_host_key_checking`/`auth`），常用高级字段 `proxy_jump`（→ asyncssh `tunnel`）、`keepalive_interval`（→ ServerAliveInterval）、`forward_agent`（→ agent 转发）现已**原生支持**；其余 ssh config 字段走 `use_ssh_config: true` 叠加。

## 安全

- **默认沙箱**：写操作默认只到远端 `/tmp/`；改 `$HOME` 或项目代码前 agent 必须先问（约定靠 prompt 层强制，参考 [给 agent 的使用约定](#给-agent-的使用约定)）
- **策略闸门**：host allowlist + command blocklist/allowlist + per-host rate limit；每个状态变更工具都过 `_gate`，无侧门（`portal_host(register)` 按目标 IP 而非别名 gate；`portal_tunnel(action=close)` 也走 gate；多机 gate 两阶段）。可选的 [cc-safety-net](https://github.com/kenryu42/cc-safety-net) 语义闸（`policies.safety_net.enabled`）在同一处叠加：把命令交给 `cc-safety-net explain --json` 做抗绕过分析，命中破坏性 git/rm/解释器单行即拦——这正是 Copilot-CLI PreToolUse hook 用的那套规则，而该 hook 只看 agent 自己的 `bash` 工具、看不到 `portal_*` MCP 命令。默认 fail-closed（检查器跑不起来就拒绝执行）。
- **认证**：默认且推荐 SSH key；密码登录支持 `hosts.yaml` 的 `password_command` 或 `portal ssh set`，永远不暴露给 MCP 工具——配置见 [认证](#认证)，安全设计见 [`SECURITY.md` § Authentication](./SECURITY.md#authentication)
- **审计**：所有状态变更写 `$PORTAL_LOG_DIR/audit.jsonl`（默认 `~/.local/state/portal-mcp-server/log/audit.jsonl`）；默认 fail-closed（`PORTAL_AUDIT_FAIL_OPEN=1` 切 fail-open）
- **hash 保护编辑**：`portal_read` + `portal_patch` 用 SHA-256 + per-range hash + atomic `posix_rename` + 写后 rehash 保证并发安全
- **远端 bash history 风险（异常远端配置）**：`portal_exec(secrets=…)` 的注入依赖远端 bash **非交互模式默认关 history**（bash 上游设计，与 `ssh`/`ansible`/CI shell step 等所有 SSH 命令执行工具相同前提）。若远端管理员**强制**了 `BASH_ENV` + `set -o history`，或在 `/etc/bash.bashrc` 删了 `[[ $- != *i* ]] && return` 守卫，则**任何**走 SSH 的 secret 注入工具（含本工具、`ssh`、`ansible`、CI runner）都可能让 secret 值进 `~/.bash_history`。这不是本项目独有弱点，是 Unix/SSH 生态的普遍前提。部署前在你的远端验证 `bash -s <<< 'echo test'` 不写 history。


完整威胁模型、各防御层细节、运维 hygiene、已知限制、算法引用见 **[`SECURITY.md`](./SECURITY.md)**。

漏洞披露：**不要**开 public issue，请走 [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new)。响应窗口 48 小时确认 / 7 天初评 / 关键问题 30 天修复。


## 测试

### 单元 + 安全（不需要真实 SSH）

```bash
pytest tests/ -v
# live SSH 测试默认 skip（受 PORTAL_TEST_LIVE 环境变量控制）
```

覆盖：command injection regression、safety validators、hash-protected editor、concurrency、resource lifecycle、multi-host policy enforcement、password_command/passphrase_command 安全不变量、audit fail mode。

### 端到端 live smoke

`tests/live_smoke.py` 直接 import 本地工作树驱动一系列真实 SSH 行为：`hosts.yaml` 残留 `password:` 字段处理、`ssh_exec` 基础调用、`portal_exec(group_tag=...)` 在真实主机上的 gate（blocked 命令 + 不在 allowlist 的主机均拦截）、`portal_shell` 单命令的 gate、`portal_shell` + `portal_patch` 在远端 `/tmp/` 的 round-trip（含 stale-hash 拒绝路径）、audit.jsonl 是否吃到新加的 operation tag。

```bash
PORTAL_AUDIT_FAIL_OPEN=1 \
  PORTAL_TEST_HOST=<your-host> PORTAL_TEST_PORT=22 PORTAL_TEST_USER=<user> \
  PORTAL_TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ 它会在远端 `/tmp/portal-mcp-server-smoke-<pid>.txt` 写一次再删除——只动 `/tmp`。

## CI / Release

仓库用 GitHub Actions 自动化跑测试和发布，本地不需要手动 build：

- **CI**（[`ci.yml`](.github/workflows/ci.yml)）：每个 PR / push to `main` 在 Python **3.10 / 3.11 / 3.12 / 3.13** 上跑 `ruff check portal_mcp_server/` + `pytest tests/`，四个版本都绿才能 merge。
- **Release**（[`release.yml`](.github/workflows/release.yml)）：push 一个 `v*.*.*` tag 自动触发——`python -m build` 产出 wheel + sdist → 从 `CHANGELOG.md` awk 抽出对应版本段做 [GitHub Release](https://github.com/TMYTiMidlY/portal-mcp-server/releases) body → 通过 [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/)（OIDC 短令牌，无静态 token）发布到 [PyPI](https://pypi.org/project/portal-mcp-server/)。

完整发布流程、CHANGELOG 格式约束与 release 失败排障见 [`CONTRIBUTING.md` § CI & Release 自动化](./CONTRIBUTING.md#ci--release-自动化)。

## 常见问题

### 本地改动未在 agent 上生效

`uvx portal-mcp-server` 从 PyPI 缓存启动。如果你改了本地代码，agent 不会看到——它用的是 PyPI 发布的版本。

| 你在哪改 | agent 的 MCP server 看得见吗 |
|---|---|
| 本地工作树 | ❌ 看不见。uvx 走的是 PyPI，不是本地路径 |
| 已发布到 PyPI 的新版本 | ✅ 用 `uvx portal-mcp-server@latest` 或 `--refresh` 更新缓存 |

本地调试想让 agent 用上改动，把 `.mcp.json` 里的 `args` 临时改成：

```json
"args": ["--from", "/absolute/path/to/portal-mcp-server", "portal-mcp-server"]
```

（路径必须绝对）。**别把这条本地路径 commit 进项目级的 `.mcp.json`**。

### 连接超时 / Permission denied (publickey)

1. 确认 `ssh user@host` 能在终端直连
2. 检查私钥权限：`chmod 600 ~/.ssh/id_ed25519`
3. 如果用了 `~/.ssh/config`，确认 `Host` 别名、`HostName`、`User`、`IdentityFile` 配正确
4. 跳板机（ProxyJump）场景：asyncssh 原生支持 `~/.ssh/config` 的 `ProxyJump`，确认跳板机也能手动 ssh 通

### MCP client 重启后连接断了

这是正常行为——连接池跟随 MCP server 进程生命周期。MCP client 重启会关闭 server 进程，连接池随之释放。下次 agent 调用任意 `portal_*` 工具时会自动重建连接。

### 怎么更新到最新版

```bash
# 临时 uvx 运行：刷新缓存并重新拉取 PyPI 最新版
uvx portal-mcp-server@latest --help

# 持久安装到 PATH 的 uv tool：升级已安装 tool
uv tool upgrade portal-mcp-server
```

然后重启 MCP client。

## 贡献

欢迎 issue 与 PR。简版要点：

- Python 3.10+，I/O 全部 `async/await`，无阻塞调用
- 不出现硬编码 hostname / username / IP / path
- 新工具写好 docstring（FastMCP 用作 MCP description）+ 同步 README「工具列表」节（含折叠的完整签名 + 源码位置表）
- 状态变更工具必须过 `_gate` + 写 `audit_log`
- 测试覆盖关键路径；`pytest tests/ -v` 必须全绿
- 不 commit secret；`examples/hosts.yaml` 是唯一 schema 模板
- commit message 走 [Conventional Commits](https://www.conventionalcommits.org/)

完整开发流程、新工具开发清单、PR 模板、安全 / 隐私规则见 **[`CONTRIBUTING.md`](./CONTRIBUTING.md)**（[English](./CONTRIBUTING.en.md)）。

## 协议与致谢

Apache License 2.0（见 [`LICENSE`](LICENSE)）。

衍生关系与 third-party 算法引用见 [`NOTICE`](NOTICE)：

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0）**——git ancestry，底层模块（asyncssh 引擎、连接池、tunnel 管理、orchestrator、安全策略）沿用；上层 14 个 `portal_*` 工具是新设计
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT）**——`remote_text_editor.py` 的 SHA-256 hash-protected edit 算法参考来源，针对 AsyncSSH SFTP 重写

> ⚠️ 本工具让 agent 拥有对远端系统的 SSH 访问能力。请只在你拥有或被授权的系统上使用。
