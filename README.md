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

## 简介

`portal-mcp-server` fork 自 [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0）。底层 SSH/asyncssh 引擎、连接池、tunnel 管理、多机编排算法、安全策略沿用上游模块；上层重新设计了 18 个面向 agent 的 `portal_*` 工具：

- **2 个** hash-protected 的远端文件编辑工具（`portal_read` / `portal_patch`），算法参考 [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT），针对 SFTP 重写
- **6 个** 核心 IO / 搜索 / 持久 bash 工具
- **10 个** 用 `mode` 字段合并的高层工具（隧道、文件传输、多机编排、playbook、审计 ...）

完整衍生关系与算法引用见 [`NOTICE`](./NOTICE) 与 [安全](#安全) 章节。

## 项目特色

- **跨工具连接复用**：所有 `portal_*` 工具共享同一进程内的 asyncssh 连接池；一次握手长期复用，单次调用摊销到 channel 创建（~10–30 ms）。
- **Windows 上同样快**：不依赖 OpenSSH `ControlMaster`，连接池是纯 Python 对象，三大平台获得一致的复用性能。
- **持久 bash 会话**：`portal_bash` 为每台 host 维护一个 `bash -i`，cwd / env 跨调用保留；agent 不需要每条命令重建上下文。
- **hash 保护的远端编辑**：`portal_read` + `portal_patch` 用整文件 SHA-256 + 行范围 hash 双层校验，写入走 tmp + `posix_rename` 原子替换，写后再 hash 校验，杜绝并发覆盖。
- **agent-first 工具数量**：把上游 57 个工具收敛到 18 个，tool-list context 从 ~7.5k tokens 降到 ~2.5k；`mode` 字段合并语义重复的入口。
- **内建安全策略**：host allowlist、command blocklist/allowlist（fnmatch）、per-host rate limit、所有改状态操作落 audit log，默认 fail-closed。
- **OpenSSH 配置兼容**：`~/.ssh/config` 别名、`known_hosts`、ssh-agent 自动识别，无需重复登记主机。
- **零额外部署**：MCP client 通过 `uvx` 直接从 GitHub 拉运行，无需 clone、无需 venv。

## 快速开始

```bash
# 1. 在 Claude Code 里登记（其他 MCP client 见"接入方式"节）
claude mcp add portal -- uvx portal-mcp-server

# 2. 确保目标 host 在 ~/.ssh/config 或 config/hosts.yaml 里

# 3. 在 agent 对话中使用
#    "帮我看看 myhost 上 /var/log/syslog 最后 50 行"
#    agent 会调用 portal_bash("myhost", "tail -50 /var/log/syslog")
```

不需要 clone 仓库、不需要 venv——`uvx` 会自动拉取并运行。开发者安装见 [安装](#安装)。

## 架构

```
┌──────────────┐    stdio / SSE     ┌─────────────────────────────────────┐
│  MCP Client  │ ◄────────────────► │       portal-mcp-server             │
│ (Claude Code │                    │                                     │
│  Copilot CLI │                    │  ┌──────────┐   ┌────────────────┐  │
│  Cursor ...) │                    │  │ 18 tools │──►│ security gate  │  │
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

### 8 个核心工具（首选入口）

| 工具 | 给 agent 的能力 |
|---|---|
| `portal_read` / `portal_patch` | 读远端文件并拿 SHA-256；patch 用 `file_hash` + per-range hash 防并发覆盖，写入走 tmp + `posix_rename` 原子替换，写后再 hash 校验 |
| `portal_grep` / `portal_glob` | 远端 `rg --json` / `find` 结构化输出，首次连接探测一次缓存 |
| `portal_bash` / `portal_bash_close` / `portal_bash_status` | 每个 host 一个粘性 `bash -i`，cwd / env 跨调用保留；PTY echo + bracketed-paste 关闭以让 sentinel 正确工作 |
| `portal_cleanup_tmps` | 清理 patch 中断后留下的孤儿 `*.mcp_tmp.*` |

### 10 个高层工具（mode 切换）

| 工具 | mode / 参数 | 用途 |
|---|---|---|
| `portal_host` | `action=list\|register\|remove` | 主机注册（用于 tag 分组；`~/.ssh/config` 别名自动解析无需登记） |
| `portal_transfer` | `direction=upload\|download\|sync` | SFTP 文件传输（二进制安全） |
| `portal_tunnel_open` / `_close` / `_list` | `mode=local\|reverse\|socks` | SSH 隧道（端口转发 / 反向 / SOCKS5） |
| `portal_multi_exec` | `mode=parallel\|rolling\|broadcast`，`hosts_json\|group_tag` | 多机命令编排 |
| `portal_playbook` | `host\|group_tag` | 多步骤剧本 |
| `portal_ping` | optional `hosts_json` | 健康检查（单机或全 fleet） |
| `portal_audit` | `view=snapshot\|history\|stats\|policy` | 审计日志 + 服务器内部状态 introspection |
| `portal_check` | `host`，optional `command` | 安全策略 dry-run |

### 给 agent 的使用约定

`portal-mcp-server` 只提供工具，并不强制 agent 怎么用。如果你希望 agent 在这套工具上行为可预期、不爆炸，建议在 `AGENTS.md` / `CLAUDE.md` 或系统 prompt 里加上以下规约：

- **优先确认 host 别名**——目标主机如果不在 `~/.ssh/config` 或 `hosts.yaml`，先问用户，不要随便注册一个新 host
- **写文件走 read → patch**——先 `portal_read` 拿 `file_hash` 和 `range_hash`，再用同一组 hash 调 `portal_patch`；冲突时 patch 会返回新 hash，重读重改即可
- **默认沙箱 `/tmp/`**——写操作默认落在远端 `/tmp/` 下；改 `$HOME` 或项目源码前必须先确认
- **不混用工具**——一次任务里要么走 `portal_*`（hash 保护、连接池复用），要么走 bash 里的 `ssh`/`scp`，不要混用——混用会绕过 hash 校验或打断 sudo 流
- **多机用专用工具**——`portal_multi_exec(mode="parallel")` / `portal_playbook(group_tag=...)`，不要在 bash 里循环 `ssh host1; ssh host2; ...`
- **sudo 走交互**——需要交互式 sudo 的操作 `portal_bash` 处理不了，让用户在 terminal 里 `ssh -t host sudo ...`

## 设计理念

### 工具精简：18 vs. 57

Anthropic 的 [_Writing Tools for Agents_](https://www.anthropic.com/engineering/writing-tools-for-agents) 明确说：

> "More tools don't always lead to better outcomes... Tools that merely wrap existing software functionality is a common error... Too many tools or overlapping tools can also distract agents from pursuing efficient strategies."

上游 `ssh-shell-mcp` 把每种 ergonomic 都做成单独 tool（`ssh_run` / `ssh_run_batch` / `ssh_run_script` / `ssh_run_with_env` / `ssh_session_exec` / `ssh_ps` / `ssh_kill` / `ssh_df` / `ssh_free` / `ssh_journalctl` / `ssh_docker` / `ssh_tmux_*` ...），共 **57 个**。这些工具大部分是 bash 一行命令的包装，**`portal_bash`（持久 bash 会话）一个工具就能覆盖**。

| 类别 | 数量 | 处理方式 |
|---|---:|---|
| **保留并重新设计** | 8 | `portal_read` + `portal_patch` 用 SHA-256 hash 保护取代裸 cat/write 的并发漏洞；`portal_grep` / `portal_glob` 提供结构化搜索结果；`portal_bash`(`_close`/`_status`) 持久 shell；`portal_cleanup_tmps` 处理中断遗留 |
| **mode-flag 合并** | 10 | `portal_tunnel_open(mode=local\|reverse\|socks)` 取代上游 3 个独立 tool；`portal_multi_exec(mode=parallel\|rolling\|broadcast)` 取代 4 个；`portal_audit(view=...)` 合并 status/history/stats/policy 4 个 introspection 接口 |
| **完全砍掉** | 27 | 全部能由 `portal_bash` 直接覆盖：命令执行族 5、多 session 族 6、系统检查族（ps/df/free/journalctl/info/netstat/service）7、进程管理族 5、tmux 族 4 |

收益：context 从 ~7.5k tokens 降到 ~2.5k；agent 不再需要在多个语义重复的工具里选择。

### 进程内连接池

portal-mcp-server 在 server 进程内部维护 asyncssh 连接池——所有工具调用（`portal_bash`、`portal_read`、`portal_transfer` ...）共享同一条 TCP，**除第一次连接外全部摊销到 channel 创建（~10–30 ms）**。

与「裸 ssh + ControlMaster」（最佳 plain 方案）对比：

| 维度 | portal-mcp-server | plain ssh + ControlMaster |
|---|---|---|
| 复用机制 | asyncssh 进程内连接池（每条连接最多 5 个并发操作，按需新建） | OpenSSH master 进程 + Unix domain socket |
| 复用粒度 | 进程级（MCP server 活着就持续） | 会话级（默认 10min `ControlPersist`） |
| 第一次连接 | TCP + auth（~200–500 ms） | TCP + auth（~200–500 ms） |
| 后续命令 | 复用连接，开新 channel（~10–30 ms） | 复用 master，开新 channel（~10–30 ms） |
| 跨工具复用 | ✅ `portal_bash` 和 `portal_read` 共享同一 TCP | ❌ `ssh` 和 `scp` 复用要求两边 `ControlPath` 一致 |
| 持久 shell 状态 | ✅ `portal_bash` 维护 `bash -i`，cwd/env 跨调用保留 | ❌ 每次 `ssh host cmd` 是新 shell，cwd/env 不留 |
| 并发 | asyncio 多 channel 真并发 | 多 ssh 进程串行启动（共享 master） |
| Windows | ✅ 任何能跑 Python 的平台都享受同等性能 | ❌ Windows OpenSSH 不支持 ControlMaster |

实测脱敏：同 LAN（< 1ms RTT）跑 100 次 `echo pong`，plain ssh + ControlMaster 平均 23 ms；portal-mcp-server 通过 `portal_bash` 平均 18 ms（省了 ssh 客户端进程启动）。第一次连接两边都 ~280 ms（auth 占大头）。

### Windows 上的差距

`ControlMaster` 在 Windows OpenSSH 上**不工作**——它依赖 Unix domain socket 实现 master/子进程之间共享文件描述符，Win10/11 默认编译不带这个机制（实验性 named-pipe 也常出问题）。

portal-mcp-server **完全不依赖** OS 级 socket 共享：连接池放在 MCP server 自己的 Python 进程内存里（asyncssh 是纯 Python），任何能跑 Python 的平台（Windows / macOS / Linux）都享受**与 Linux 一致**的复用性能。

```text
Windows 下：
  plain ssh:        每次 cmd 都新建 TCP+auth      → ~300 ms × N
  portal-mcp-server: 第一次 ~280 ms，后续 ~20 ms  → 单调下降到 channel 极限
```

副作用红利：池连接随 MCP server 进程持续（小时级），不是 `ControlPersist` 默认的 10 分钟，长会话里的 reconnect 抖动也省了。

### 技术选型：asyncssh 而非 subprocess

[asyncssh](https://github.com/ronf/asyncssh)（EPL-2.0 / GPL-2.0 双许可）是 SSHv2 协议的**独立纯 Python 实现**，与 OpenSSH 协议层等价：

- **单进程多连接、单连接多 session**：连接池就是 Python dict，没有进程边界、没有 fd 共享需求
- **协议层完整覆盖**：local/remote/dynamic 端口转发、SFTP、SCP、X11 fwd、TUN/TAP——OpenSSH 能干的协议层动作 asyncssh 全都能干
- **OpenSSH 兼容**：原生解析 `~/.ssh/config`、`known_hosts`、`authorized_keys`、ssh-agent / Pageant
- **仅依赖 PyCA `cryptography`**：装上 Python 就能跑，无 C 依赖、无 OS 特定 IPC

对比「用 subprocess 调 `ssh` / `scp`」：

- 不用每次 fork 新进程（启动 ~50–100 ms 没了）
- 不用协调多进程之间共享 SSH 复用（这正是 ControlMaster 在 Win 上挂的地方）
- 错误处理、重试、超时都是 Python 异步原语，不是解析 stderr 字符串

## 安装

按身份选路径。

### 终端用户（用 MCP server，不动源码）

不需要 clone，让 MCP client 通过 `uvx` 直接从 PyPI 拉运行——见下方 [接入方式](#接入方式)。`uvx` 第一次启动缓存依赖，后续重启秒级。

shell 里手动 smoke test：

```bash
uvx portal-mcp-server --help
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
uv tool install --force .      # --force 覆盖旧 tool，确保用当前 checkout
```

不想用 uv 也可以走标准 pip editable install：

```bash
pip install -e ".[dev]"       # -e/--editable 指向当前源码；含 pytest 等 dev 依赖
# 或纯运行时
pip install -e .
```

## 接入方式

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%22%5D%7D) [![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%22%5D%7D&quality=insiders) [![Install in Cursor](https://img.shields.io/badge/Cursor-Install_Server-000000?style=flat-square&logo=cursor&logoColor=white)](https://cursor.com/en/install-mcp?name=portal&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJwb3J0YWwtbWNwLXNlcnZlciJdfQ==)

`portal-mcp-server` 是一个本地 stdio MCP server，所有支持 MCP 的 host 都能接入。下面给常见 host 的最小配置——`uvx` 会自动从 PyPI 拉取并缓存，后续启动秒级。

### 通用配置片段

> 大多数 host 都接受 `{ "mcpServers": { "<name>": { "command": ..., "args": [...] } } }` 这种顶层 schema；VS Code 和 Codex 用各自专有 schema，单独列出。

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": ["portal-mcp-server"]
    }
  }
}
```

如果需要传环境变量（指向自定义的 hosts/policies/log 路径），追加 `env`：

```json
"env": {
  "SSH_HOSTS_YAML": "/path/to/hosts.yaml",
  "SSH_POLICIES_YAML": "/path/to/policies.yaml",
  "SSH_MCP_LOG_DIR": "/path/to/logs"
}
```

### Claude Code CLI

直接编辑 `<project>/.mcp.json`（同上 schema），或用 CLI / 斜杠命令登记：

```bash
claude mcp add portal -- uvx portal-mcp-server
# 或在 Claude Code 会话内输入 /mcp 交互登记；加 --scope user 可登记到 user 级
```

<details>
<summary><b>GitHub Copilot CLI</b></summary>

写 `<project>/.mcp.json` 即在该项目内生效；或一行命令登记到 user 级（对所有项目生效）：

```bash
copilot mcp add portal -- uvx portal-mcp-server
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
      "args": ["portal-mcp-server"]
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

Codex 用 TOML，编辑 `~/.codex/config.toml`：

```toml
[mcp_servers.portal]
command = "uvx"
args = ["portal-mcp-server"]
```

启动 Codex 后在 TUI 输入 `/mcp` 确认 `portal` 已加载。

</details>

<details>
<summary><b>其它 host（Cline / Continue / Roo Code / Zed …）</b></summary>

- **Cline / Continue / Roo Code 等 VS Code 插件**：通常都接受 `{ "mcpServers": ... }` 通用片段，写到各自插件的 MCP 设置面板或工作区配置即可
- **任意 MCP 兼容 host**：把通用片段贴到该 host 的 MCP 配置入口；stdio 不需要额外代理

</details>

## 配置

所有配置通过环境变量传入。在 MCP client 的 `env` 字段里设置即可——这些变量只对 MCP server 子进程生效，不影响其他程序。

### 文件路径

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `SSH_HOSTS_YAML` | 主机注册 YAML | `./config/hosts.yaml` 若存在，否则 `~/.config/portal-mcp-server/hosts.yaml` |
| `SSH_POLICIES_YAML` | 安全策略 YAML | `./config/policies.yaml` 若存在，否则 `~/.config/portal-mcp-server/policies.yaml` |
| `SSH_MCP_LOG_DIR` | audit + server log 目录 | `./logs/` 若存在，否则 `~/.local/state/portal-mcp-server/logs/` |

路径解析优先级：环境变量 > 当前目录下的 `./config/` 或 `./logs/`（兼容开发者 checkout 布局）> XDG 目录。`config/hosts.example.yaml` 给了完整 schema 模板。**`hosts.yaml` 含真实凭据，已在 `.gitignore`，永远别 commit**。

### 安全与认证

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `SSH_MCP_AUDIT_FAIL_OPEN` | 设 `1` → audit 写盘失败时仅 warning 并继续；默认 → **fail-closed**，audit 写不进则操作 raise 中止 | _(unset)_ |
| `MCP_AUTH_TOKEN` | HTTP transport（`--transport streamable_http`）的 Bearer token；stdio 模式不需要 | _(none)_ |

### 连接池

控制 asyncssh 进程内连接池的行为。默认值适合大多数场景，仅在高并发或特殊网络环境下需要调整。

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `SSH_POOL_SIZE` | 每 host 最大 TCP 连接数。连接池满且所有连接都达到 channel 上限时，会复用最空闲的连接（带 warning） | `5` |
| `SSH_MAX_CHANNELS_PER_CONN` | 每条 TCP 上最大并发 channel 数（SFTP 会话、exec、tunnel 等共享）。超出后新建 TCP，直到 `SSH_POOL_SIZE` 上限 | `5` |
| `SSH_MAX_IDLE_TIME` | 无活跃 channel 的连接空闲多久后自动关闭（秒）。设 `0` 禁用 | `600`（10 分钟） |
| `SSH_MAX_CONN_AGE` | 连接最大存活时间（秒），超龄且无活跃 channel 时关闭。防止防火墙 / NAT 静默断连 | `3600`（1 小时） |

### 完整示例

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": ["portal-mcp-server"],
      "env": {
        "SSH_HOSTS_YAML": "/home/me/.config/portal-mcp-server/hosts.yaml",
        "SSH_POLICIES_YAML": "/home/me/.config/portal-mcp-server/policies.yaml",
        "SSH_POOL_SIZE": "10",
        "SSH_MAX_CHANNELS_PER_CONN": "8"
      }
    }
  }
}
```

## 认证

按你的认证方式跳——优先 SSH key，passphrase 走 ssh-agent；密码登录支持但需要走 `password_command`，命令行明文密码从不进 LLM。

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

### 密码登录：opt-in 走 `password_command`

兼容历史不让换 key 的远端机器。两条铁律：

1. **绝不** 在 `hosts.yaml` 写 `password: 明文`——启动会 ERROR 拒绝、字段被丢
2. **绝不** 通过 MCP 工具传——`portal_host` 没有 password 参数，密码不会进 LLM tool-call trace

写法（hosts.yaml 里 `auth: password` + 一段输出密码到 stdout 的 shell 命令，思路同 Borg 的 `BORG_PASSCOMMAND`、restic 的 `RESTIC_PASSWORD_COMMAND`、msmtp 的 `passwordeval`）：

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

运行时行为：每次新建连接执行一次（连接池复用，所以实际很少触发），10 秒超时，结尾换行剥掉一个，stderr 永不进日志（防泄密），非 0 退出 / 空输出 / 非 UTF-8 输出全部硬失败。设计细节（为什么 `shell=True`、为什么强制 `client_keys=[]`、为什么 stderr 不进日志…）见 **[`SECURITY.md` § Authentication](./SECURITY.md#authentication)**。

### 加密私钥的 passphrase：`passphrase_command`

同款机制，应用到加密私钥的解锁 passphrase：

```yaml
hosts:
  encrypted-key-host:
    host: 10.0.0.30
    user: deploy
    key: ~/.ssh/encrypted_key
    passphrase_command: pass show ssh/encrypted_key
```

ssh-agent 跑得起来时**不要**用这个，agent 体验更好；只在 headless / CI 这种没有交互终端的场景下用。

## 安全

- **默认沙箱**：写操作默认只到远端 `/tmp/`；改 `$HOME` 或项目代码前 agent 必须先问（约定靠 prompt 层强制，参考 [给 agent 的使用约定](#给-agent-的使用约定)）
- **策略闸门**：host allowlist + command blocklist/allowlist + per-host rate limit；每个状态变更工具都过 `_gate`，无侧门（`portal_host(register)` 按目标 IP 而非别名 gate；`portal_tunnel_close` 也走 gate；多机 gate 两阶段）
- **认证**：默认且推荐 SSH key；密码登录支持但只走 `hosts.yaml` 的 `password_command`，永远不暴露给 MCP 工具——配置见 [认证](#认证)，安全设计见 [`SECURITY.md` § Authentication](./SECURITY.md#authentication)
- **审计**：所有状态变更写 `logs/audit.jsonl`；默认 fail-closed（`SSH_MCP_AUDIT_FAIL_OPEN=1` 切 fail-open）
- **hash 保护编辑**：`portal_read` + `portal_patch` 用 SHA-256 + per-range hash + atomic `posix_rename` + 写后 rehash 保证并发安全

完整威胁模型、各防御层细节、运维 hygiene、已知限制、算法引用见 **[`SECURITY.md`](./SECURITY.md)**。

漏洞披露：**不要**开 public issue，请走 [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new)。响应窗口 48 小时确认 / 7 天初评 / 关键问题 30 天修复。


## 测试

### 单元 + 安全（不需要真实 SSH）

```bash
pytest tests/ -v
# live SSH 测试默认 skip（受 SSH_TEST_LIVE 环境变量控制）
```

覆盖：command injection regression、safety validators、hash-protected editor、concurrency、resource lifecycle、multi-host policy enforcement、password_command/passphrase_command 安全不变量、audit fail mode。

### 端到端 live smoke

`tests/live_smoke.py` 直接 import 本地工作树驱动一系列真实 SSH 行为：`hosts.yaml` 残留 `password:` 字段处理、`ssh_exec` 基础调用、`portal_multi_exec(mode="parallel", group_tag=...)` 在真实主机上的 gate（blocked 命令 + 不在 allowlist 的主机均拦截）、`portal_bash` 单命令的 gate、`portal_bash` + `portal_patch` 在远端 `/tmp/` 的 round-trip（含 stale-hash 拒绝路径）、audit.jsonl 是否吃到新加的 operation tag。

```bash
SSH_MCP_AUDIT_FAIL_OPEN=1 \
  TEST_HOST=<your-host> TEST_PORT=22 TEST_USER=<user> \
  TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ 它会在远端 `/tmp/portal-mcp-server-smoke-<pid>.txt` 写一次再删除——只动 `/tmp`。

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
# uvx 缓存清理 + 重新拉取
uvx portal-mcp-server@latest --help
```

然后重启 MCP client。

## 贡献

欢迎 issue 与 PR。简版要点：

- Python 3.10+，I/O 全部 `async/await`，无阻塞调用
- 不出现硬编码 hostname / username / IP / path
- 新工具写好 docstring（FastMCP 用作 MCP description）+ 同步 `docs/tools.md`
- 状态变更工具必须过 `_gate` + 写 `audit_log`
- 测试覆盖关键路径；`pytest tests/ -v` 必须全绿
- 不 commit secret；`config/hosts.example.yaml` 是唯一 schema 模板
- commit message 走 [Conventional Commits](https://www.conventionalcommits.org/)

完整开发流程、新工具开发清单、PR 模板、安全 / 隐私规则见 **[`CONTRIBUTING.md`](./CONTRIBUTING.md)**（[English](./CONTRIBUTING.en.md)）。

## 协议与致谢

Apache License 2.0（见 [`LICENSE`](LICENSE)）。

衍生关系与 third-party 算法引用见 [`NOTICE`](NOTICE)：

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0）**——git ancestry，底层模块（asyncssh 引擎、连接池、tunnel 管理、orchestrator、安全策略）沿用；上层 18 个 `portal_*` 工具是新设计
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT）**——`remote_text_editor.py` 的 SHA-256 hash-protected edit 算法参考来源，针对 AsyncSSH SFTP 重写

> ⚠️ 本工具让 agent 拥有对远端系统的 SSH 访问能力。请只在你拥有或被授权的系统上使用。
