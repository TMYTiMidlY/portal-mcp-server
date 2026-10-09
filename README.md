<div align="center">

# portal-mcp-server

**让 coding agent 通过 MCP 操作远端机器**

SSH 命令、持久 shell、文件编辑与传输、隧道、多机与后台任务。

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
<summary>目录</summary>

- [简介](#overview)
- [项目特色](#highlights)
- [安装与快速上手](#install)
- [接入方式](#client-integration)
- [主机配置](#hosts)
- [认证与凭据](#authentication)
- [工具列表](#tools)
- [环境变量](#env-vars)
- [安全](#security)
- [常见问题](#faq)
- [架构与设计](#architecture-design)
- [开发与测试](#testing)
- [CI / Release](#ci-release)
- [贡献](#contributing)
- [协议与致谢](#license-credits)

</details>

## <a id="overview"></a>简介

Portal 让支持 MCP 的 coding agent 通过 SSH 操作远端机器：运行命令、编辑文件、传输数据、建立隧道，以及管理多台主机上的任务。服务端基于 [AsyncSSH](https://github.com/ronf/asyncssh) 和 [FastMCP](https://modelcontextprotocol.io/)，可在 Linux、macOS、Windows 上运行。

日常使用分为两部分：**MCP server 提供给 agent 调用的工具，`portal` CLI 供你在终端配置凭据。** 主机可以直接沿用 `~/.ssh/config` 中的别名；需要分组、密码来源或额外配置时，再使用 `hosts.yaml`。

## <a id="highlights"></a>项目特色

- **连接复用**：命令、SFTP 和隧道共用服务端连接池，减少重复 SSH 握手；不依赖 OpenSSH 的 `ControlMaster`。
- **可保留状态的 shell**：`remote_shell` 跨调用保留工作目录和环境，`remote_exec` 处理一次性命令，`remote_job` 管理远端后台任务。
- **带冲突检查的文件编辑**：先读取文件与 SHA-256，再校验整文件和修改范围的 hash，通过 SFTP 临时文件和原子替换完成写入。
- **结构化操作**：文件搜索、增量传输、多机并发与滚动执行、SSH 隧道均有对应工具和返回结构。
- **凭据与对话分开**：复用 ssh-agent，或通过 CLI 无回显输入 SSH 密码、私钥口令、sudo 密码和 token；工具调用只传主机名或 secret 名。
- **策略与审计**：主机和命令策略、速率限制、审计日志，以及可选的 [cc-safety-net](https://github.com/kenryu42/cc-safety-net) 检查。

下面按安装、接入、主机、凭据和工具的顺序介绍。完整签名、调优参数与设计说明放在折叠区，首次使用可以先跳过。

## <a id="install"></a>安装与快速上手

需要 Python 3.10+ 和支持 MCP 的客户端。推荐用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 安装：

```bash
uv tool install portal-mcp-server
portal-mcp-server --help
```

安装后会得到两个等价的命令：`portal-mcp-server` 和 `portal`。不带子命令时启动 MCP server；带 `ssh`、`passphrase`、`sudo`、`secret` 或 `agent` 子命令时执行 CLI 操作。日常手动输入凭据时用短名 `portal` 即可。

### 1. 准备一台主机

如果已有可用的 SSH 别名，可以直接沿用。否则在 `~/.ssh/config` 添加：

```sshconfig
Host web01
    HostName server.example.com
    User deploy
    IdentityFile ~/.ssh/id_ed25519
```

在终端确认地址、账号和认证方式正确：

```bash
ssh web01
```

私钥有口令时，先按 [ssh-agent 配置](#ssh-agent) 解锁。使用 SSH 密码登录时，可以通过 `portal ssh set web01` 无回显输入密码，详见 [密码登录](#password-login)。

### 2. 接入 MCP 客户端

以 Claude Code 为例：

```bash
claude mcp add --scope user portal -- portal-mcp-server
```

其他客户端的 JSON、TOML 与配置文件位置见 [接入方式](#client-integration)。如果客户端没有继承终端的 agent 环境，在 MCP 配置的 `env` 中显式传入 `SSH_AUTH_SOCK`，或在 SSH 配置里指定 `IdentityAgent`。

### 3. 开始使用

在对话中提出一个具体任务，例如：

> 查看 web01 上 /var/log/syslog 的最后 50 行。

agent 可以调用 `remote_exec("web01", "tail -50 /var/log/syslog", timeout=30)`。也可以先调用 `hosts(action="list")` 核对主机来源和配置告警。

<details>
<summary>零安装试用、升级与命令重名</summary>

不安装到 PATH 也能试用：

```bash
uvx portal-mcp-server@latest --help
```

对应 MCP 配置使用 `"command": "uvx"` 和 `"args": ["portal-mcp-server@latest"]`。这不会把 `portal` 短命令安装到 PATH；经常需要设置凭据时，推荐 `uv tool install`。

更新已安装版本：

```bash
uv tool upgrade portal-mcp-server
```

更新后重启 MCP server，使运行中的进程使用新版本。

[SpatiumPortae/portal](https://github.com/SpatiumPortae/portal) 文件传输工具也使用 `portal` 这个命令名。若安装了两者，用 `which -a portal`（Windows 用 `where portal`）检查 PATH 顺序，或直接使用全名 `portal-mcp-server`。

</details>

## <a id="client-integration"></a>接入方式

不同客户端的配置格式和环境继承规则各有差异。以下客户端说明按需展开；agent 入口的优先级见 [SSH 密钥与 ssh-agent](#ssh-agent)。

[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_Server-0098FF?style=flat-square&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%40latest%22%5D%7D) [![Install in VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Install_Server-24bfa5?style=flat-square&logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=portal&config=%7B%22type%22%3A%22stdio%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22portal-mcp-server%40latest%22%5D%7D&quality=insiders) [![Install in Cursor](https://img.shields.io/badge/Cursor-Install_Server-000000?style=flat-square&logo=cursor&logoColor=white)](https://cursor.com/en/install-mcp?name=portal&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJwb3J0YWwtbWNwLXNlcnZlckBsYXRlc3QiXX0=)

`portal-mcp-server` 是一个本地 stdio MCP server，所有支持 MCP 的 host 都能接入。下面给常见 host 的最小配置，命令示例**默认用装好的 `portal-mcp-server`**（先 `uv tool install portal-mcp-server`）；零安装就把 `command` 换成 `uvx` + `portal-mcp-server@latest`（上方一键 badge 走的正是这个零安装形式）。

> 如果 MCP client 找不到 `portal-mcp-server`（或 `uvx`）——常见于 GUI app（Claude Desktop / VS Code）不继承你 shell 的 PATH——用 `which portal-mcp-server`（Windows 用 `where portal-mcp-server`）查绝对路径填进 `command`。`uv tool` 装的这个路径（`~/.local/bin/portal-mcp-server`）跨 `uv tool upgrade` 不变，写死也稳。

### 通用配置片段

> 大多数 host 都接受 `{ "mcpServers": { "<name>": { "command": ..., "args": [...] } } }` 这种顶层 schema；VS Code 和 Codex 用各自专有 schema，单独列出。

已安装（推荐，`command` 就是短二进制名）：

```json
{
  "mcpServers": {
    "portal": {
      "command": "portal-mcp-server",
      "args": []
    }
  }
}
```

零安装（不装，走 uvx 现拉现跑）：

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

需要传入 agent socket 或自定义配置路径时，在 server 条目中追加 `env`（UID 和路径均为示例，按实际环境替换；只保留需要的字段）：

```json
"env": {
  "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.socket",
  "PORTAL_HOSTS_YAML": "/path/to/hosts.yaml",
  "PORTAL_POLICIES_YAML": "/path/to/policies.yaml",
  "PORTAL_LOG_DIR": "/path/to/logs"
}
```

<details>
<summary>Claude Code CLI</summary>

### Claude Code CLI

直接编辑 `<project>/.mcp.json`（同上 schema），或用 CLI / 斜杠命令登记：

```bash
# 推荐：user 级，对所有 repo 生效（已 uv tool install，command 用短名 portal-mcp-server）
claude mcp add --scope user portal -- portal-mcp-server

# 不加 --scope 默认是 local，只在「当前目录」生效，换个目录 claude mcp list 就看不到
claude mcp add portal -- portal-mcp-server
# 零安装：把 portal-mcp-server 换成 uvx portal-mcp-server@latest
# 或在 Claude Code 会话内输入 /mcp 检查和管理连接
```

> ⚠️ Claude Code 有三档 scope：`local`（**默认**，仅当前目录）、`user`（所有 repo）、`project`（写进 repo 的 `.mcp.json`，随仓库共享）。要「装一次处处可用」**务必带 `--scope user`**——这点和 Codex（`mcp add` 即 global）/ Copilot CLI（`mcp add` 即 User 级）不一样，最易踩坑。

</details>

<details>
<summary><b>GitHub Copilot CLI</b></summary>

写 `<project>/.mcp.json` 即在该项目内生效；或一行命令登记到 user 级（对所有项目生效）：

```bash
copilot mcp add portal -- portal-mcp-server
# 零安装：把 portal-mcp-server 换成 uvx portal-mcp-server@latest
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

工作区可以在项目根目录使用前文的 `.mcp.json` / `mcpServers` 通用格式。也支持 `.vscode/mcp.json` 的 VS Code 原生格式：

```json
{
  "servers": {
    "portal": {
      "type": "stdio",
      "command": "portal-mcp-server",
      "args": []
    }
  }
}
```

跨工作区配置可通过命令面板的 **MCP: Open User Configuration** 打开；新增服务也可使用 **MCP: Add Server**。两种文件格式的顶层字段不同，按所选位置使用相应格式。配置位置和当前支持情况见 [VS Code 官方文档](https://code.visualstudio.com/docs/agent-customization/mcp-servers#configure-the-mcpjson-file)。

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
codex mcp add portal -- portal-mcp-server
# 零安装：把 portal-mcp-server 换成 uvx portal-mcp-server@latest
codex mcp list          # 应看到 portal
```

也可以编辑 `~/.codex/config.toml`：

```toml
[mcp_servers.portal]
command = "portal-mcp-server"
args = []
# 零安装：command = "uvx"，args = ["portal-mcp-server@latest"]

# 可选：显式传入 agent socket；换成实际路径
[mcp_servers.portal.env]
SSH_AUTH_SOCK = "/run/user/1000/ssh-agent.socket"
```

启动 Codex 后在 TUI 输入 `/mcp` 确认 `portal` 已加载。

</details>

<details>
<summary><b>其它 host（Cline / Continue / Roo Code / Zed …）</b></summary>

- **Cline / Continue / Roo Code 等 VS Code 插件**：通常都接受 `{ "mcpServers": ... }` 通用片段，写到各自插件的 MCP 设置面板或工作区配置即可
- **任意 MCP 兼容 host**：把通用片段贴到该 host 的 MCP 配置入口；stdio 不需要额外代理

</details>

## <a id="hosts"></a>主机配置

最简单的方式是直接使用 `~/.ssh/config` 中的 `Host web01` 别名。Portal 复用 AsyncSSH 的解析器，支持 `Include`、`HostName`、`User`、`Port`、`IdentityFile`、`IdentityAgent`、`ProxyJump` 等配置。

需要 Portal 的分组标签、密码命令或主机级设置时，创建 `~/.config/portal-mcp-server/hosts.yaml`：

```yaml
hosts:
  web01:
    use_ssh_config: true
    tags: [web, prod]
    # user: deploy                # 可选，覆盖 SSH 配置中的 User
    # sudo_password_command: pass show sudo/web01
```

`use_ssh_config: true` 表示以同名 SSH 别名为基础，再叠加 YAML 中显式设置的字段。省略 `host` 可以继承 `HostName`；如果填写，必须与别名解析出的 `HostName` 一致，否则拒绝连接。

也可以完全在 YAML 中定义主机：

```yaml
hosts:
  web02:
    host: server2.example.com
    user: deploy
    port: 22
    key: ~/.ssh/id_ed25519
    tags: [web]
```

**查找优先级：运行时注册表 / `hosts.yaml` → SSH 配置别名。** 同名 YAML 主机若没有 `use_ssh_config: true`，不会以该别名合并 SSH 配置；需要继承别名的 `IdentityAgent`、`IdentityFile` 或 `ProxyJump` 时，应明确启用合并。

`hosts(action="list")` 同时列出配置主机和 SSH 别名，返回 `source` 与告警。`hosts(action="register", name="web01")` 只给名字时，会从已有 SSH 别名创建合并配置；运行时登记的主机只存在于当前 MCP server 的注册表中。

<details>
<summary>配置来源、跳板机与高级选项</summary>

`PORTAL_SSH_CONFIG` 控制 Portal 的 SSH 配置来源：

| 设置 | 读取范围 |
|---|---|
| 不设置 | 用户 `~/.ssh/config`，再以系统客户端配置作为 fallback |
| 绝对路径 | 只读该文件，抑制系统配置，类似 `ssh -F <file>` |
| `none`（不区分大小写） | 禁用 Portal 的 SSH 配置文件查找 |
| 其他相对路径 | 告警并忽略 |

`list` 的 `source` 包括 `hosts.yaml`、`runtime`、`ssh-config`；启用合并时为 `hosts.yaml+ssh-config` 或 `runtime+ssh-config`。别名枚举跟随 `Include`，排除含 `*`、`?`、`!` 的模式。

常用 YAML 字段包括 `proxy_jump`、`keepalive_interval`、`forward_agent`、`use_ssh_agent` 和 `login_shell`。未显式设置的合并字段沿用 SSH 配置。`proxy_jump: none` 强制直连；空字符串或 `null` 不表示直连，应删除字段或使用 `none`。

跳板机需要自己的密钥时，优先定义其 SSH 别名并启用合并，把跳板密钥加入 ssh-agent。裸 `proxy_jump: user@jump` 的默认身份与口令继承有额外限制，见 [连接排障](#faq) 和 [ADR-0002](./docs/adr/0002-ssh-config-merge.md)。

完整字段模板见 [examples/hosts.yaml](./examples/hosts.yaml)，配置路径规则见 [文件路径](#file-paths)。

</details>

## <a id="authentication"></a>认证

先区分两种本地服务：**ssh-agent 保存已加载的 SSH 密钥并提供签名；Portal 凭据 agent 保存 CLI 输入的密码、私钥口令与 secret。** 它们使用不同的 socket，互不替代。

| 需要提供什么 | 推荐入口 | 使用位置 |
|---|---|---|
| SSH 密钥 | `ssh-add ~/.ssh/id_ed25519` | 通过 ssh-agent 完成 SSH 签名 |
| SSH 登录密码 | `portal ssh set web01` | 建立 SSH 连接 |
| 加密私钥的口令 | `portal passphrase set web01` | 在本机解锁私钥；通常优先用 ssh-agent |
| 远端 sudo 密码 | `portal sudo set web01` | `remote_exec(..., use_sudo=True)` 等支持 sudo 的工具 |
| 本机 sudo 密码 | `portal sudo set-local` | `local_exec(..., use_sudo=True)` |
| API token 等 secret | `portal secret set github_token` | `remote_exec` / `local_exec` 的 `secrets=["github_token"]` |

`portal … set` 在你自己的终端中无回显输入，默认缓存 900 秒，可用 `--ttl` 调整。不要把凭据粘贴到 agent 对话或 MCP 工具参数中。无人值守场景也可以配置密码管理器命令，见下文各节。

### <a id="ssh-agent"></a>SSH 密钥与 ssh-agent

已有可用密钥时无需新建。需要创建和安装公钥时，例如：

```bash
ssh-keygen -t ed25519 -C "you@example.com"
ssh-copy-id -i ~/.ssh/id_ed25519.pub deploy@server.example.com
```

ssh-agent 启动时不一定有密钥；用 `ssh-add` 加入，输入一次私钥口令：

```bash
# 仅在没有可用 agent 时启动；已有系统/桌面 agent 时沿用其入口
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
ssh-add -l
```

Portal 通过 AsyncSSH 选择 agent 入口，**不会扫描机器上所有 agent socket，也不会自动替你解锁私钥**：

1. 连接读取的 SSH 配置中有 `IdentityAgent` 时，使用该入口。
2. 否则读取 **Portal 进程环境中的 `SSH_AUTH_SOCK`**。这个变量可以由 MCP 客户端从父进程环境传入，也可以在 MCP 配置的 `env` 中显式指定。
3. 没有可用 agent 时，是否能连接取决于可读取的密钥文件、私钥口令或其他已配置认证来源。

例如，Linux 上一个已经启动的用户级 agent 固定监听 `/run/user/1000/ssh-agent.socket`，可在 SSH 配置中写：

```sshconfig
Host *
    IdentityAgent /run/user/%i/ssh-agent.socket
    AddKeysToAgent yes
```

`%i` 表示本地 UID。**这里的 socket 必须真实存在，路径应与实际 agent 一致。** `IdentityAgent` 覆盖 `SSH_AUTH_SOCK`；它不会设置环境变量，也不会启动 agent。`AddKeysToAgent` 是 OpenSSH 客户端的密钥缓存选项，不能让 Portal 自动解锁或加载密钥。`ssh-add` 不读取 `IdentityAgent`，仍需正确的 `SSH_AUTH_SOCK`。

也可以直接为 MCP server 传入入口：

```json
{
  "mcpServers": {
    "portal": {
      "command": "portal-mcp-server",
      "args": [],
      "env": {
        "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.socket"
      }
    }
  }
}
```

示例中的 UID `1000` 需要换成实际值，或使用已有 agent 提供的路径。MCP 客户端可能筛选继承的环境，GUI 客户端也未必继承终端环境；显式配置 `env` 能避免这一差异。修改启动环境后，重新启动 MCP server。在另一个终端后来执行 `export`，不会更新已运行 Portal 的环境。

密钥可以复用到被移除或 agent 重启；它的缓存寿命与 OpenSSH `ControlPersist`、Portal 连接池的连接寿命各自独立。agent socket 必须对运行 Portal 的用户可访问。

<details>
<summary>Linux 无桌面环境：建立固定 socket 的用户级 ssh-agent</summary>

先检查 `command -v ssh-agent` 和 `systemctl --user cat ssh-agent.service`。如果已有合适的服务，沿用它的 socket；没有时，可以创建 `~/.config/systemd/user/ssh-agent.service`：

```ini
[Unit]
Description=OpenSSH authentication agent
Documentation=man:ssh-agent(1)

[Service]
Type=simple
Environment=SSH_AUTH_SOCK=%t/ssh-agent.socket
ExecStart=/usr/bin/ssh-agent -D -a $SSH_AUTH_SOCK
UMask=0077

[Install]
WantedBy=default.target
```

根据 `command -v` 的结果调整程序路径，然后加载、启用并解锁：

```bash
systemctl --user daemon-reload
systemctl --user enable --now ssh-agent.service
export SSH_AUTH_SOCK="/run/user/$(id -u)/ssh-agent.socket"
ssh-add ~/.ssh/id_ed25519
```

systemd 的 `%t` 是用户运行时目录，与 SSH 配置的 `%i` 含义不同。若需要新终端自动找到 socket，将相同的 export 写入自己的 shell 启动配置，或在 MCP 配置里显式设置。需要登出后或开机无人登录时运行 user service，可按系统策略启用 `loginctl enable-linger`；agent 重启仍需要重新加入密钥。

macOS 可沿用登录会话的 agent；Windows OpenSSH 使用其 `ssh-agent` 服务和命名管道，无需照抄 Linux socket 路径。

</details>

`hosts.yaml` 的 `use_ssh_agent` 可控制密钥路径：

| 值 | 行为 |
|---|---|
| 省略 | 保留 AsyncSSH 默认选择：可用文件身份与 agent 身份均可参与认证 |
| `true` | Portal 不显式传入 `key` 或私钥口令，由 AsyncSSH 选择 agent / 默认身份；SSH 配置和默认身份仍可能生效 |
| `false` | 禁用 agent，使用文件身份与配置的私钥口令 |

### <a id="password-login"></a>SSH 密码登录

先确保 `web01` 已在 SSH 配置或 `hosts.yaml` 中定义，然后在自己的终端执行：

```bash
portal ssh set web01
# 或指定缓存时间（秒）
portal ssh set web01 --ttl 1800
```

首次 `set` 会按需安装并启动 Portal 凭据 agent。密码按主机别名缓存；**CLI 设置主机凭据时请使用与 MCP 工具相同的名字**。

默认先尝试密钥认证；失败为 `PermissionDenied` 且存在已缓存密码或 `password_command` 时，再尝试密码认证。没有密码来源时保留原始认证错误。要明确采用密码登录路径，可配置：

```yaml
hosts:
  web01:
    host: server.example.com
    user: deploy
    auth: password
```

密码来源的顺序是 **CLI 凭据缓存 → `password_command` → 报错**。自动化场景可以从密码管理器读取：

```yaml
hosts:
  web01:
    host: server.example.com
    user: deploy
    auth: password
    password_command: pass show ssh/web01
```

也可使用 `op`、`bw`、`secret-tool` 等能输出凭据的命令。**不要写 `password: 明文`**：该字段会被忽略并产生告警。命令来源有 10 秒超时，非零退出、空输出或非 UTF-8 输出均会失败；stderr 不会回传为凭据错误内容。完整约束见 [SECURITY.md](./SECURITY.md#authentication)。

### 私钥口令：`portal passphrase set`

如果不使用 ssh-agent，可以向 Portal 提供本机私钥的解锁口令：

```bash
portal passphrase set web01
```

或在该主机配置 `passphrase_command: pass show ssh/web01-passphrase`。Portal 优先读取凭据缓存，再调用 `passphrase_command`，将口令交给 AsyncSSH 解锁文件。私钥口令、SSH 登录密码和 sudo 密码分别缓存；`use_ssh_agent: true` 路径跳过 Portal 的私钥口令解析。

### sudo：`portal sudo set`

```bash
portal sudo set web01
```

然后由 agent 调用 `remote_exec("web01", "id", timeout=30, use_sudo=True)`，或支持 sudo 的 `remote_read` / `remote_patch`。密码来源是 **CLI 凭据缓存 → `sudo_password_command` → 报错**：

```yaml
hosts:
  web01:
    use_ssh_config: true
    sudo_password_command: pass show sudo/web01
```

若 SSH 登录密码与 sudo 密码确实相同，可以显式设置 `sudo_password_same_as_ssh: true`。此时 `portal ssh set web01` 会同时写入两种缓存；默认不启用，也不会把私钥口令当作 sudo 密码。

sudo 操作使用 `sudo -S -k`，通过 stdin 输入密码。它不继承 `remote_shell` 之前保留的工作目录与环境；需要时在本次命令里写明。交互式 sudo 提示不适合 MCP 工具，确需交互时在自己的 SSH 终端操作。

<details>
<summary>本机 sudo 与 sudo + secrets 的实现边界</summary>

`local_exec` 默认关闭，需要 `PORTAL_ALLOW_LOCAL_EXEC=1`。本机 sudo 使用保留身份 `<local>`：

```bash
portal sudo set-local
```

也可以在 `hosts.yaml` 顶层配置 `"<local>": {sudo_password_command: ...}`。这与通过 SSH 连接的 `localhost` 别名不同。

sudo 与 `secrets` 同用时，密码和值依次走 stdin。提权后的 shell 在 sudo 的 `env_reset` 之后读入并 export secret，因此不需要 sudoers `env_keep`；命令本体的 stdin 也会受到这条传输路径限制。实现见 `secrets_store.py` 的 `sudo_stdin_secret_script()`。

</details>

### secret：`portal secret set`

在终端输入 token：

```bash
portal secret set github_token
```

agent 只传名称，例如 `remote_exec("web01", "gh auth status", timeout=30, secrets=["github_token"])`。服务端将值注入该次命令的 `GITHUB_TOKEN` 环境变量；`local_exec` 支持同样的参数。名称默认转为大写，更多配置见 [examples/secrets.yaml](./examples/secrets.yaml)。

读取顺序是 **CLI 凭据缓存 → `secrets.yaml` 的命令来源**：

```yaml
secrets:
  github_token:
    command: pass show api/github
```

`secrets` 可与 `use_sudo` 同用，后台 `remote_job` 暂不支持这两项。返回给 agent 的命令输出会对已知 secret 值做替换；仍应避免主动打印凭据，完整限制见 [安全](#security)。

### <a id="credential-agent"></a>Portal 凭据 agent 与日常管理

`ssh`、`passphrase`、`sudo`、`secret` 共用一套 CLI 操作：

```bash
portal ssh show web01       # 指纹与剩余 TTL，不显示明文
portal ssh list             # 当前 kind 的缓存列表
portal ssh confirm web01    # 输入两次，比对一致后写入/更新缓存
portal ssh clear web01      # 移除这一条

portal agent status        # 服务地址、状态与缓存统计
portal agent clear         # 清空所有 kind 的缓存
```

换成 `passphrase`、`sudo` 或 `secret` 即可管理其他种类。`confirm` 比较的是本次两次输入，**不会读取旧缓存来核对密码是否正确，也不会验证远端登录**。`show` / `list` 只展示指纹与有效期，没有明文导出命令。

主机类 `set` / `confirm` 会先检查别名，避免把密码存到拼错的名字下。CLI 看不到只在 MCP 运行时注册的主机时，可用 `portal ssh set web01 --force` 跳过检查；`secret` 与 `sudo set-local` 不需要此选项。

凭据存在服务内存中，默认 900 秒后过期，服务重启也会清空。MCP server 与 CLI 必须使用同一用户的配置和凭据服务。新安装的服务可在下一次凭据请求时被发现；**修改 MCP 启动环境仍需重启 MCP server**。

<details>
<summary>平台安装、服务地址与凭据的使用边界</summary>

通常直接 `portal … set` 即可自动安装；显式管理命令是：

```bash
portal agent install --now
portal agent uninstall
```

| 平台 | 用户级服务 | 本地通信 |
|---|---|---|
| Linux | systemd `.socket` + `.service`，按需激活 | Unix socket |
| macOS | launchd LaunchAgent，运行并保持服务 | Unix socket |
| Windows | 当前用户登录计划任务，InteractiveToken 主体 | 命名管道 |

安装器把实际地址写入配置目录的 `agent.json`；`PORTAL_CREDENTIAL_AGENT_SOCKET` 可以覆盖它。Linux 默认 socket 为 `%t/portal-mcp-server/credentials.sock`，与 SSH agent 的 socket 不同。macOS 的 LaunchAgent 和 Windows 计划任务均以用户身份运行；Windows 不以 SYSTEM 运行，也不存登录密码，设置 `ExecutionTimeLimit=PT0S` 和失败重启策略。Linux socket 按同用户边界校验客户端；各平台访问控制见 [SECURITY.md](./SECURITY.md)。

凭据只向执行认证或命令的授权本地进程提供，不通过人可见的 CLI 输出、MCP 参数或审计日志展示。CLI 输入的值不写入配置文件；密码管理器命令每次现取，不受这套缓存 TTL 限制。

配置凭据也意味着：能够调用此 MCP server 的 agent，在凭据有效期间可以使用相应能力。按任务选择有限的 TTL、主机/命令策略与 sudo 权限。凭据未就绪时，工具返回错误并引导你在另一个终端设置；agent 应等待你完成后重试，不索取对话中的明文。

</details>

## <a id="tools"></a>工具列表

Portal 提供 14 个 MCP 工具。先按任务选择，再查折叠区中的完整签名。

### 执行命令

| 工具 | 适用任务 | 需要知道的行为 |
|---|---|---|
| `remote_exec` | 一次性命令；单机、多机并发或滚动执行 | 返回分离的 stdout/stderr 与退出码；支持 `group_tag`、`use_sudo`、`secrets` |
| `remote_shell` | 需要跨调用保留 `cd`、`export`、venv 等状态 | 每台主机一个持久 shell；PTY 输出合并 stdout/stderr |
| `remote_job` | 长任务，或需要在 MCP server 关闭后继续运行 | `submit/poll/cancel/list`；远端 `nohup`；任务表按进程持久化；不支持 sudo/secrets |
| `local_exec` | 在 MCP server 所在机器执行命令 | 默认关闭，需 `PORTAL_ALLOW_LOCAL_EXEC=1`；支持 sudo/secrets |
| `remote_close` | 重置某台主机的持久 shell | 关闭会话，下次 `remote_shell` 自动创建 |

`remote_exec`、`remote_shell`、`local_exec` 的 `timeout` 必填，单位为秒；默认上限 `PORTAL_MAX_TIMEOUT=300`。长任务使用 `remote_job`。执行期间会发 MCP progress 心跳，但客户端是否据此重置超时取决于客户端实现。

**连接复用与 shell 状态是两回事。** 所有 SSH 工具都可以共享连接池，只有 `remote_shell` 保留 shell 状态。`remote_exec` 普通路径默认使用登录 shell；sudo/secrets 路径的环境处理见 [ADR-0004](./docs/adr/0004-login-shell-and-sudo-env.md)。

### 文件、隧道与管理

| 工具 | 用途 |
|---|---|
| `remote_read` / `remote_patch` | 先读内容、整文件 hash 与范围 hash，再校验并原子修改；支持 sudo |
| `remote_grep` | 正则搜索内容；文件名、匹配内容或计数输出，支持分页和截断标记 |
| `remote_glob` | 按 glob 找文件；按修改时间排序，最多返回 100 个结果 |
| `remote_transfer` | SFTP 上传、下载、目录增量同步或路径列表传输；上传支持断点续传与校验 |
| `remote_tunnel` | 管理 local / reverse / SOCKS 隧道：`open/close/list` |
| `hosts` | 主机列表、登记与移除；查看来源和配置告警 |
| `policy_check` | 不执行命令的策略预检；`ALLOWED` 表示当前策略允许 |
| `inspect` | 查看 server、连接池、shell 会话、历史、统计与策略 |

`remote_grep` 尊重 `.gitignore`，`remote_glob` 不尊重；传输目录不会跟随本地符号链接。`remote_patch` 成功后会清理同目录中超过一小时的孤立临时文件，具体边界见完整参考。

### <a id="agent-conventions"></a>给 agent 的使用约定

建议写入自己的 `AGENTS.md` 或系统提示，按实际授权范围调整：

- 一次调用完成一个可判断的步骤，读取真实输出与退出码后再决定下一步。`commands=[…]` 用于不需要中间决策的固定批次。
- 文件读取和修改使用 `remote_read → remote_patch`；hash 冲突后重新读取，不覆盖并发修改。
- 搜索、传输、隧道和后台任务使用对应工具；多机执行用 `remote_exec(host=[…])` 或 `group_tag`。
- 先核对主机别名、配置告警和授权范围；`/tmp/` 是建议的初始工作范围，用户已有授权优先。
- 凭据由用户在另一个终端通过 CLI 输入；未就绪时说明具体命令，等待完成后重试，不要求在对话中提供明文。
- 修改 root 文件用 `remote_read(use_sudo=True)` / `remote_patch(use_sudo=True)`。sudo patch 保留 hash 校验、原子替换和原属主/权限，目标须已存在。
- 要在 MCP server 停止后继续运行的工作使用 `remote_job`。普通前台调用与传输随进程结束而中断；上传可在重试时续传。

<details>
<summary>📋 完整逐工具参考（签名 · 返回结构 · 源码位置）</summary>

> 所有工具对大模型可见的完整签名（`ctx` 是 MCP 进度 / keepalive 上下文，由 FastMCP 在异步工具上注入，**不**出现在下面的 schema 里）。针对 host 的工具都收 `host`，解析顺序 hosts.yaml / 运行时注册表 → OpenSSH 客户端 config（复用 asyncssh 的 `SSHClientConfig`、跟 `Include`），详见[环境变量 → 文件路径](#file-paths)。状态变更工具写 `audit.jsonl`；只读工具（`remote_read` / `remote_grep` / `remote_glob` / `policy_check` / `inspect` 及 `remote_tunnel` / `remote_job` 的读 action）**刻意不审计**。

### 跑命令：exec 家族

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `remote_exec` | `(host='' \| [host…], command='', commands=None, group_tag='', *, timeout, login=None, use_sudo=False, secrets=None, serialize=False, delay_s=0.0, stop_on_error=True)` | 走连接池的无状态一次性。**单 host + 单 command → 一个 dict**（**分离**的 stdout/stderr + exit code）；多 host / `commands` 序列 → **list**（多命令 host 为 `{host, results:[…]}`）。`timeout` **必填**（无默认，超 `PORTAL_MAX_TIMEOUT` 上限即拒并导流 `remote_job`）；`login` 默认登录 shell（`bash -lc`）。并行 / 滚动、`use_sudo` / `secrets` 语义见上文工具列表与[认证](#authentication)。 |
| `remote_shell` | `(host, command='', commands=None, stop_on_error=True, *, timeout)` | 每 host 一个持久交互 shell。单 command → `{host, session_id, command, exit_code, output, duration_s}`（`output` 是 PTY 合并流，超限截断标 `truncated`）；`commands=[…]` 在**同一** session 顺序跑 → `{host, session_id, results:[…], duration_s}`，`stop_on_error` 首败即停并加 `stopped_at`。卡交互提示被自动 Ctrl-C → `exit_code:-1` + `error:"interactive_prompt_blocked"` + `session_preserved:true`。`timeout` **必填**。命令边界协议见下文 **设计理念 · 持久 shell 会话** 一节。 |
| `remote_job` | `(action=submit\|poll\|cancel\|list, host='', command='', job_id='', since=0, tail=0, max_bytes=65536, signal=TERM\|KILL, login=None, use_sudo=False, secrets=None)` | `submit` 秒回 `job_id`（远端 `nohup` + tmp 文件，断连仍跑）；`poll` 按需分页（`since=<offset>` 只回更新字节、单次封顶 `max_bytes` 默认 64 KiB、带 `more`；或 `tail=N` 瞄尾——`tail` 是快照、**不受 `max_bytes` 限制**），chunk base64 + 边界安全 UTF-8 解码；`cancel` 对**进程组**发 `signal` 并重新探测（**不对终态 job 发信号**），`list` 列全部。job 表**按进程** best-effort 持久化、有上限、TTL 清理（`PORTAL_JOB_*`）。`use_sudo` / `secrets` **后台不支持、传即拒**并指向 `remote_exec`。 |
| `local_exec` | `(command, secrets=None, use_sudo=False, *, timeout)` | 在 **MCP server 自己机器**上跑（**不**走 SSH），默认关闭，须 `PORTAL_ALLOW_LOCAL_EXEC=1`。`timeout` **必填**（同 `PORTAL_MAX_TIMEOUT` 上限，超限拒但无后台可导流）。`use_sudo=True` 用保留身份 **`<local>`**（≠ 普通 SSH host `local` / `localhost`）走本地 `sudo -S -k`，密码来自 `portal sudo set-local` 或 hosts.yaml 顶层 `<local>:` 段，可合 `secrets`，标 `high_risk`。 |
| `remote_close` | `(host)` | 关掉某 host 缓存的 `remote_shell` 会话（下次自动重开）。罕用，仅重置脏会话。 |

### 文件编辑（hash 保护）

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `remote_read` | `(host, path, start=1, end=None, limit=None, encoding='utf-8', use_sudo=False)` | → `{content, file_hash, range_hash, start, end, total_lines, truncated}`，两个 SHA-256 是 `remote_patch` 的前置。**分页**：单次 ≤ `limit` 行（默认 `PORTAL_READ_MAX_LINES=2000`）+ `PORTAL_READ_MAX_BYTES`（默认 16384）字节；提前截断则 `truncated=true` 且 `next_start` 给续读点，页按行边界切故 `range_hash` 仍有效。`use_sudo=True` 走 `sudo cat` 读 root-only（600）文件、内容原样保真（hash 仍有效），标 `high_risk`。 |
| `remote_patch` | `(host, path, file_hash, patches_json, encoding='utf-8', auto_newline=False, use_sudo=False)` | hash 保护的行范围 patch：文件自 `remote_read` 后变了即拒（回 `current_file_hash`）、原文件不动；patch 自底向上、重叠拒、走 `*.mcp_tmp.<12hex>` + `posix_rename`（原子）、写后 rehash。成功后顺扫同目录 >1h 孤儿 tmp（白嫖已开 SFTP、隔离，进可选 `swept` 键）。`use_sudo=True` 读写 root 属主文件：`sudo cat` 读 + SFTP 暂存到用户私有 home + 一步 sudo `cp→旁边临时→还原属主/权限→原子 rename` 落位（保 hash 安全与原子性，目标须已存在），标 `high_risk`。`patches_json` = `[{"start":int,"end":int\|null,"contents":str,"range_hash":str}, …]`。 |

### 远端搜索（忠实移植 Claude Code）

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `remote_grep` | `(host, pattern, path='.', glob='', file_type='', output_mode=files_with_matches\|content\|count, ignore_case=False, before_context=0, after_context=0, context=0, head_limit=250, offset=0, multiline=False)` | 正则内容搜索（`rg`，fallback `grep`）。`output_mode`：`files_with_matches`（默认，路径 mtime 倒序）/ `content`（匹配行 + 可选上下文，`head_limit` 封顶**总行数**、`offset` 分页）/ `count`。尊重 `.gitignore`，每结果带 `truncated`。清晰参数名取代 CC 的 `-A`/`-B`/`-C`/`-i`。 |
| `remote_glob` | `(host, pattern, path='.')` | 按 glob 找文件，`rg --files --no-ignore --sort modified -g`，**mtime 倒序**，硬上限 100 + `truncated` → `{filenames, num_files, truncated, duration_ms}`。不尊重 `.gitignore`（对齐 CC Glob）。 |

### 文件传输（SFTP）

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `remote_transfer` | `(direction=upload\|download\|sync\|mirror\|upload-list\|download-list, host, local_path, remote_path, checksum=False, paths_json='', resume=True)` | 二进制安全、原子 SFTP。单文件模式（`upload`/`download`）→ `{status, direction, host, bytes, duration_s, …}`；增量模式（`sync` 推目录 / `mirror` 拉目录 / `*-list`）跳过 size+mtime 匹配（`checksum=True` 改 sha256）→ `{status, uploaded\|downloaded, skipped, failed[], bytes_total, bytes_transferred, duration_s}`，单文件失败进 `failed[]` 不中断。**upload 断点续传**（`resume=True` 默认）：远端有更小的半截文件时只补传尾巴，续传后整份 sha256 校验通过 → `resumed`；**无法校验（远端没 `sha256sum`）→ 重新整传 `restarted_unverifiable`**；校验不符 → 重新整传 `restarted_after_mismatch`；`resume=False` 强制重传。目录模式不跟随本地符号链接、且拒绝写穿符号链接目标。`*-list` 需 `paths_json` = `[{"local":…,"remote":…}, …]`，只拷普通文件。 |

### 资源（agent 显式管理）

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `remote_tunnel` | `(action=open\|close\|list, kind=local\|reverse\|socks, host='', tunnel_id='', local_port=0, local_bind='127.0.0.1', remote_host='', remote_port=0)` | `open` 过 `host` 开隧道：`local` 转发 `localhost:local_port → remote_host:remote_port`、`reverse` 把 `local_bind:local_port` 暴露成 `host:remote_port`、`socks` 是 SOCKS5 代理。`close` 按 `tunnel_id`（gate 在源 host）；`list` 列所有活跃隧道。 |
| `hosts` | `(action=list\|register\|remove, name='', host='', user='root', port=22, key_path='', tags='')` | 运行时 host 注册表。`register` 要 `name`+`host`——或只 `name`（`~/.ssh/config` 有同名 `Host` 别名时自动登记 `use_ssh_config` 叠加）。`tags`（逗号分隔）喂 `group_tag`。`list` 同时枚举 ssh config 别名（解析真实 `HostName`/`User`/`Port`），每条带 `source`（`hosts.yaml`/`runtime`/`ssh-config`/`…+ssh-config`）+ 可能的 per-host `warnings`——转告用户。**无 password 参数。** |

### 内省 / 策略

| 工具 | 签名 | 返回结构 / 关键行为 |
| --- | --- | --- |
| `policy_check` | `(host, command='')` | 过安全策略 dry-run，不执行 → `"ALLOWED"` 或 `"BLOCKED: <reason>"`。⚠️ 默认策略**宽松**——`ALLOWED` 只表示"当前无规则拦它"，不代表安全。 |
| `inspect` | `(view=snapshot\|server\|sessions\|history\|stats\|policy, limit=50, host_filter='')` | 只读内省 server **plumbing** + 历史。`snapshot`（元数据 + 连接池 + bash 会话 + 审计 stats + 策略摘要）/ `server`（仅版本 / 元数据）/ `sessions`（持久 bash 会话的 `host→session_id` 表）/ `history`（最近 `limit` 条，可过滤）/ `stats`（按 operation 计数）/ `policy`。**hosts / tunnels 不在这**——它们是资源，由 `hosts` / `remote_tunnel` 各自的 `list` 出。 |

> **凭据 CLI（非 MCP 工具）**：用户在终端用 `portal {ssh,sudo,passphrase,secret} set` 输入值，凭据服务按需交给本地消费者；工具参数只引用名称。`show` / `list` 返回指纹与 TTL，`confirm` 比较本次两次输入后写入缓存。平台安装和访问边界见[认证](#authentication)与[凭据 agent](#credential-agent)。

### 源码位置

| 模块 | 负责的工具 / 职责 |
| --- | --- |
| `cli.py` | 全部 `@mcp.tool()` 定义、`_gate()` / `_gate_exec()` 闸门封装、`inspect` 组装、凭据 CLI |
| `connection_manager.py` | asyncssh 连接池 + host 注册表（**SSH 工具共用**；`local_exec` / 控制面工具不走 SSH） |
| `shell_engine.py` | `remote_exec` 的一次性 `ssh_exec` 路径（普通执行；dispatch 还跨 `cli.py` / `remote_bash.py`） |
| `remote_bash.py` | `remote_shell` / `remote_close` + `remote_exec` 的 sudo / secrets 一次性路径 |
| `session_manager.py` | 持久交互式 shell 会话（bash/zsh；cwd/env、exit code，OSC 133 边界协议、soft-cancel、超时中断） |
| `job_manager.py` | `remote_job`（后台 submit/poll/cancel/list） |
| `local_exec.py` | `local_exec` |
| `remote_text_editor.py` | `remote_read`、`remote_patch`（+ 孤儿 tmp 清扫） |
| `remote_search.py` | `remote_grep`、`remote_glob` |
| `file_ops.py` | `remote_transfer` |
| `network_tools.py` | `remote_tunnel` |
| `credential_agent.py` | `portal {ssh,passphrase,sudo,secret} set` 的 per-user socket / 命名管道激活 TTL 缓存 |
| `ssh_creds.py` / `passphrase_creds.py` / `sudo_creds.py` / `secrets_store.py` | 各类凭据解析 + 输出脱敏 |
| `_peer_creds.py` | 凭据 agent 的同用户对端校验（Linux `SO_PEERCRED` / Windows 命名管道 SID） |
| `security.py` | 策略引擎：host allowlist、command blocklist/allowlist、per-host rate limit、cc-safety-net 接入 |
| `audit.py` | `audit_log()` 写入 + 历史 ring buffer（`inspect` 工具的组装在 `cli.py`） |

</details>

<details>
<summary>🔀 从旧工具名迁移</summary>

> **v4 起：所有工具去掉 `portal_` 前缀**——远程操作类加 `remote_`（`remote_exec` / `remote_shell` / `remote_read` / `remote_patch` / `remote_grep` / `remote_glob` / `remote_transfer` / `remote_tunnel` / `remote_job` / `remote_close`），本机执行 `local_exec`，控制面 `portal_host→hosts` / `portal_check→policy_check` / `portal_audit→inspect`。客户端本就按 config key 命名空间化（`portal-remote_exec`），`portal_` 前缀是冗余 stutter；详见 [ADR-0001](./docs/adr/0001-tool-naming-scheme.md)。下表另附更早的 `portal_bash` 时代迁移（合并 / 删除的工具）：

| 旧 | 新 |
|---|---|
| `portal_bash(host, cmd)` | `remote_shell(host, cmd)`（持久会话）或 `remote_exec(host, cmd)`（一次性，更快） |
| `portal_bash(..., use_sudo=True / secrets=[…])` | `remote_exec(..., use_sudo=True / secrets=[…])` |
| `portal_bash_close` | `remote_close` |
| `portal_multi_exec(mode=parallel, hosts_json=…)` | `remote_exec(host=[…])` |
| `portal_multi_exec(mode=rolling, …)` | `remote_exec(host=[…], serialize=True, delay_s=N)` |
| `portal_multi_exec(mode=broadcast, commands_json=…)` | `remote_exec(host=[…], commands=[…])` |
| `portal_playbook(host=…/group_tag=…)` | `remote_exec(host=…/group_tag=…, commands=[…])` |
| `portal_ping(hosts_json=…)` | `remote_exec(host=[…], command="echo pong")` |
| `portal_tunnel_open/_close/_list` | `remote_tunnel(action=open\|close\|list, kind=…)` |
| `portal_cleanup_tmps` | 删除——`remote_patch` 成功后自动清扫同目录孤儿 tmp |
| `portal_bash_status` | `inspect(view="sessions")` |
| — | **新增** `remote_job(action=submit\|poll\|cancel\|list)` 后台任务 |

</details>

## <a id="env-vars"></a>环境变量

Portal 自身的设置使用 `PORTAL_*` 前缀，在 MCP server 的 `env` 中传入。`SSH_AUTH_SOCK` 是额外沿用的 SSH 标准变量，`IdentityAgent` 来自 SSH 配置，详见 [认证](#ssh-agent)。

### <a id="file-paths"></a>文件路径

下表是 Linux 默认路径；其他平台使用对应的用户配置/状态目录。可以用环境变量指定绝对路径：

| 变量 | 用途 | Linux 默认 |
|---|---|---|
| `PORTAL_HOSTS_YAML` | 主机配置 | `~/.config/portal-mcp-server/hosts.yaml` |
| `PORTAL_POLICIES_YAML` | 安全策略 | `~/.config/portal-mcp-server/policies.yaml` |
| `PORTAL_SECRETS_YAML` | secret 命令来源 | `~/.config/portal-mcp-server/secrets.yaml` |
| `PORTAL_SSH_CONFIG` | SSH 配置读取范围 | 用户配置 + 系统 fallback；可用绝对路径或 `none` |
| `PORTAL_LOG_DIR` | 审计与服务日志 | `~/.local/state/portal-mcp-server/log/` |
| `PORTAL_CREDENTIAL_AGENT_SOCKET` | Portal 凭据服务地址 | 安装器写入的 `agent.json` |

配置路径优先级是 **显式环境变量 → 平台用户目录**；Linux 支持 `XDG_CONFIG_HOME` / `XDG_STATE_HOME`。当前工作目录和仓库的 `examples/` 不会自动成为真实配置来源。SSH 文件范围的规则见 [主机配置](#hosts)。

需要 YAML 配置时，从 [examples/](./examples/) 中选用相应模板，复制到用户配置目录后编辑。仅使用 SSH 别名时不必先创建三份 YAML。真实配置与凭据来源不应提交进 Git。

<details>
<summary>全部调优参数、默认值与测试变量</summary>

### 安全与认证

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_AUDIT_FAIL_OPEN` | 设 `1` → audit 写盘失败时仅 warning 并继续；默认 → **fail-closed**，操作后审计失败会让工具报错，不回滚已完成修改 | _(unset)_ |
| `PORTAL_ALLOW_LOCAL_EXEC` | 设 `1` 才启用 `local_exec`（本机执行，偏离远端编排目标，默认关） | _(unset)_ |
| `PORTAL_ALLOW_TUNNEL_EXPOSURE` | 设 `1` 才允许 `remote_tunnel` 绑非 loopback（local/socks 的 `local_bind`）或把反向隧道暴露到远端所有接口；默认只绑 loopback | _(unset)_ |
| `PORTAL_AUTH_TOKEN` | HTTP transport（`--transport streamable_http`）的鉴权令牌（客户端发 `Authorization: Bearer <token>`）。传输**默认 `--host 127.0.0.1`（仅本机）**；绑定非 loopback 地址而未设此值会**拒绝启动**。stdio 传输不需要 | _(none)_ |

### 连接池

控制 asyncssh 进程内连接池的行为。默认值适合大多数场景，仅在高并发或特殊网络环境下需要调整。详细的池行为说明见 [§ 进程内连接池](#connection-pool)。

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_SSH_POOL_SIZE` | 每 host 最大 TCP 连接数。连接池满且所有连接都达到 channel 上限时，会复用最空闲的连接（带 warning） | `5` |
| `PORTAL_SSH_MAX_CHANNELS_PER_CONN` | 每条 TCP 上最大并发 channel 数（SFTP 会话、exec、tunnel 等共享）。超出后新建 TCP，直到 `PORTAL_SSH_POOL_SIZE` 上限 | `5` |
| `PORTAL_SSH_MAX_IDLE_TIME` | 无活跃 channel 的连接空闲多久后自动关闭（秒）。**注意 `0` 不是"禁用"**——它让任何空闲连接立即可被回收 | `600`（10 分钟） |
| `PORTAL_SSH_MAX_CONN_AGE` | 连接最大存活时间（秒），超龄且无活跃 channel 时关闭。防止防火墙 / NAT 静默断连 | `3600`（1 小时） |

### 可靠性

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_BASH_HEARTBEAT_INTERVAL` | `remote_shell` / `remote_exec` / `local_exec` 在命令执行期间每隔多少秒发一条 MCP progress 通知作 keepalive。命令无输出时也会发送通知；客户端是否据此重置超时取决于其实现，与服务端 `timeout` 相互独立。非正数或非法值回退到默认 | `5`（秒） |
| `PORTAL_MAX_TIMEOUT` | `remote_exec` / `remote_shell` / `local_exec` 前台每命令超时的**上限（秒）**。`timeout` 现在是**必填**参数（三个工具都去掉了默认值），agent 每次必须显式选一个；传入超过此上限的值会被**拒绝**，并提示把长任务改用后台 `remote_job`（无上限）。这是安全护栏、不是默认值。每次调用时读取 Portal 进程环境。非正 / 非法值回退到内置默认 | 内置 `300`（5 分钟） |
| `PORTAL_LOGIN_SHELL` | `remote_exec` 普通路径与 `remote_job` 是否默认在**登录 shell**（`bash -lc`）里跑命令，从而加载用户 `~/.profile` / `~/.bashrc` 的 PATH 与环境（conda / nvm / pyenv / `~/.local/bin`）。默认**开**；只有显式 `0`/`false`/`no`/`off` 才关。优先级：per-call `login` 参数 > hosts.yaml 主机的 `login_shell:` > 此环境变量。没有 bash 的 sh-only 主机自动回退普通执行；`remote_shell` 不受影响（持久会话故意 `--norc`） | `on`（开） |

### Shell 会话

`remote_shell` 持久会话的时序旋钮，正常部署无需改。

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_SHELL_MAX_OUTPUT` | 单命令输出内存上限（字节）；超限丢弃头部并标 `truncated` | `8388608`（8 MiB） |
| `PORTAL_SHELL_BOOT_TIMEOUT` | 首次拉起持久会话（注入集成脚本 + 就绪标记）的超时（秒） | `10.0` |
| `PORTAL_SHELL_BOOT_QUIET` | bootstrap 完成前的静默确认窗口（秒） | `0.6` |
| `PORTAL_SHELL_INTERACTIVE_GRACE` | 侦测到交互提示后、判定卡死并 soft-cancel 前的宽限（秒） | `1.0` |
| `PORTAL_SHELL_SOFT_CANCEL_TIMEOUT` | soft-cancel（含前台超时中断）后等 OSC133 `D` 回到干净提示的超时（秒），超时则销毁会话 | `3.0` |

### 测试（仅 dev）

只在跑 `tests/` 时用到，正常 MCP 部署不需要设置。详细测试用法见 [§ 测试](#testing)。

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `PORTAL_TEST_LIVE` | 设 `1` / `true` / `yes` 才会运行 `tests/test_live_ssh.py` 中的真实 SSH 测试；否则全部 skip | _(unset)_ |
| `PORTAL_TEST_HOST` | live 测试目标主机 | `127.0.0.1` |
| `PORTAL_TEST_PORT` | live 测试目标端口 | `22` |
| `PORTAL_TEST_USER` | live 测试登录用户 | `$USER` 或 `root` |
| `PORTAL_TEST_KEY_PATH` | live 测试用的私钥路径 | `~/.ssh/id_ed25519` |


### 审计轮转、后台任务与读取上限

| 变量 | 用途 | 默认 |
|---|---|---|
| `PORTAL_AUDIT_MAX_BYTES` | 审计日志轮转阈值 | `10485760`（10 MiB） |
| `PORTAL_AUDIT_BACKUPS` | 保留的轮转文件数量 | `5` |
| `PORTAL_JOB_PERSIST` | 任务表持久化；`0` / `false` 关闭 | 开 |
| `PORTAL_JOB_STATE_FILE` | 持久化路径；显式指定时使用固定文件 | 状态目录的 `jobs/<pid>.json` |
| `PORTAL_JOB_MAX_LIVE` | 存活后台任务数量上限 | `50` |
| `PORTAL_JOB_TTL` | 完成任务保留时间，过期清理状态与远端临时文件 | `3600` 秒 |
| `PORTAL_READ_MAX_LINES` | `remote_read` 默认每页最大行数 | `2000` |
| `PORTAL_READ_MAX_BYTES` | `remote_read` 每页最大字节数 | `16384` |

旧版本的变量前缀、工作目录配置与工具名迁移见 [CHANGELOG.md](./CHANGELOG.md) 和 [旧工具名迁移](#tools)。

</details>

## <a id="security"></a>安全

Portal 的能力取决于运行 server 的用户权限、SSH 身份、已配置凭据和策略。建议先限制主机、命令与 sudo 范围，再按任务提供凭据。

- **策略检查**：主机 allowlist、命令 allowlist/blocklist、速率限制与多机两阶段检查。可选 `policies.safety_net.enabled` 接入 [cc-safety-net](https://github.com/kenryu42/cc-safety-net)；检查器不可用时默认拒绝操作。
- **授权范围**：先在远端 `/tmp/` 工作是给 agent 的使用约定，**不是文件系统强制沙箱**。操作用户目录或项目源文件应遵循用户授权。
- **凭据**：CLI 输入不进入工具参数或配置文件，命令来源从密码管理器读取。能调用 server 的 agent 也能使用这些凭据对应的能力；TTL 与策略仍然重要。
- **HTTP**：默认监听 `127.0.0.1`；绑定非 loopback 地址必须设置 `PORTAL_AUTH_TOKEN`。服务提供 HTTP，需要公网访问时由前置代理终止 TLS。
- **隧道与传输**：隧道默认绑定 loopback，扩大暴露需要 `PORTAL_ALLOW_TUNNEL_EXPOSURE=1`；目录传输不跟随本地符号链接，但仍拥有 server 用户可访问的本地文件权限。
- **审计**：状态变更写 `$PORTAL_LOG_DIR/audit.jsonl`。日志写入发生在操作之后；失败默认让工具报错，**不会回滚已经完成的远端修改**。`PORTAL_AUDIT_FAIL_OPEN=1` 可改为告警后继续。
- **文件修改**：hash 检查与原子替换用于检测冲突并减少中断造成的损坏，属于乐观并发控制，不能消除所有竞争窗口。普通 SFTP 写入不保证保留属主/权限，sudo patch 会显式保留。

<details>
<summary>输出脱敏与远端 shell 历史的限制</summary>

已知 secret 值会从返回输出中替换，但转换、拆分、外传或意外记录仍需由命令与环境约束。远端非交互 bash 通常不记录历史；若管理员通过 `BASH_ENV` 等配置强制开启 history，stdin 注入的内容可能写入 `~/.bash_history`。部署时检查实际远端环境，不依赖输出脱敏代替权限控制。

</details>

完整威胁模型、访问校验、审计边界和已知限制见 [SECURITY.md](./SECURITY.md)。漏洞请通过 [GitHub Security Advisories](https://github.com/TMYTiMidlY/portal-mcp-server/security/advisories/new) 私下披露；项目目标响应窗口为 48 小时确认、7 天初评、关键问题 30 天修复。

## <a id="faq"></a>常见问题

### 终端 SSH 能连，Portal 却提示认证失败

终端可能已解锁私钥或复用 OpenSSH master；Portal 使用自己的 AsyncSSH 连接。先核对实际入口：

```bash
ssh -G web01
echo "$SSH_AUTH_SOCK"
ssh-add -l
# 核对固定 socket（路径换成实际值）
SSH_AUTH_SOCK=/run/user/1000/ssh-agent.socket ssh-add -l
```

检查生效的 `IdentityAgent` 与 MCP server 的 `env.SSH_AUTH_SOCK`，再确认 agent 中有目标密钥。`ssh-add` 只看环境变量，它失败不代表读取 `IdentityAgent` 的客户端一定找不到 agent。若 `hosts.yaml` 覆盖了同名别名，确认是否需要 `use_ssh_config: true`。详见 [主机配置](#hosts) 和 [认证](#ssh-agent)。

### `portal … set` 显示未知主机，或设置后仍不可用

使用与 MCP 调用相同的主机别名，确认 CLI 与 MCP server 读取相同配置。只在运行时登记的主机可以用 `--force`；用 `portal agent status` 与 `portal ssh show web01` 检查服务与 TTL。修改启动环境后重启 MCP server；新安装凭据服务本身可以按需发现。

### 本地改动未在 agent 上生效

不管是 `uv tool install` 装的 `portal-mcp-server` 还是 `uvx portal-mcp-server`，跑的都是 **PyPI 发布版**，不是你的工作树——改了本地代码 agent 看不到。

| 你在哪改 | agent 的 MCP server 看得见吗 |
|---|---|
| 本地工作树 | ❌ 看不见（除非 editable 安装，见下）|
| 已发布到 PyPI 的新版本 | ✅ `uv tool upgrade portal-mcp-server`（装了的）；或 `uvx portal-mcp-server@latest` / `--refresh` 刷缓存 |

本地调试想让 agent 用上工作树的改动，两选一：

```bash
# 推荐：editable 安装，command 保持 portal-mcp-server 不变，源码改动即时生效
uv tool install --force --editable .
```

或走 uvx，把 `.mcp.json` 里的 `args` 临时改成（路径必须绝对）：

```json
"args": ["--from", "/absolute/path/to/portal-mcp-server", "portal-mcp-server"]
```

**别把这条本地路径 commit 进项目级的 `.mcp.json`**。

### 连接超时 / Permission denied (publickey)

1. 确认 `ssh user@host` 能在终端直连
2. 检查私钥权限：`chmod 600 ~/.ssh/id_ed25519`
3. 如果用了 `~/.ssh/config`，确认 `Host` 别名、`HostName`、`User`、`IdentityFile` 配正确
4. 跳板机（ProxyJump）场景：asyncssh 原生支持 `~/.ssh/config` 的 `ProxyJump`，确认跳板机也能手动 ssh 通。**留意跳板凭据的边界**：hosts.yaml 里裸写的 `proxy_jump: user@jump` 只用**默认 key/agent** 连跳板，而且会把为**目标**解出的 passphrase 拿去解**本机上那把登录跳板的私钥**（跳板 key 若加密且 passphrase 不同 → `Incorrect passphrase`）；它**不读**跳板专属的 `IdentityFile`。要让跳板用它自己的 key/passphrase：开 `use_ssh_config: true`（asyncssh 会读跳板 `Host` 段的 `IdentityFile`），并把跳板 key 加进 **ssh-agent**（agent 认证不吃 passphrase，冲突自然消失）。想强制直连（忽略 ssh config 的 `ProxyJump`）写 `proxy_jump: none`。

### MCP client 重启后连接断了

这是正常行为——连接池跟随 MCP server 进程生命周期。MCP client 重启会关闭 server 进程，连接池随之释放。下次 agent 调用任意 portal 工具时会自动重建连接。

### 更新到最新版

```bash
# 装了的（推荐）：升级到 PyPI 最新版
uv tool upgrade portal-mcp-server         # 或 uv tool upgrade --all

# 零安装 uvx：刷新缓存重新拉取最新版
uvx portal-mcp-server@latest --help
```

然后重启 MCP client。

## <a id="architecture-design"></a>架构与设计

MCP 客户端通过 stdio（或可选 HTTP）调用 Portal。服务端负责主机解析、策略检查、凭据解析与审计，SSH 操作进入 AsyncSSH 连接池；`local_exec` 在服务端本机运行。

### <a id="vs-traditional"></a>与直接使用 ssh / scp 的关系

已有脚本可以继续使用 OpenSSH。Portal 提供的是 agent 可调用的统一接口：连接池、保留状态的 shell、带 hash 检查的编辑、结构化搜索、凭据输入和任务管理。

<details>
<summary>按能力比较</summary>

| 能力 | 直接使用 SSH / scp / rsync | Portal |
|---|---|---|
| 连接复用 | Linux/macOS 可配置 `ControlMaster`；Windows OpenSSH 的相关限制见 [issue #405](https://github.com/PowerShell/Win32-OpenSSH/issues/405) | 在 Python 进程内复用 AsyncSSH 连接，跨工具共享 |
| shell 状态 | 每次独立 `ssh host command` 创建新执行环境 | `remote_shell` 跨调用保留状态 |
| 文件编辑 | 需脚本自行处理并发冲突、暂存与替换 | 整文件与范围 hash 校验、临时写入、原子替换 |
| 搜索与传输 | 需解析命令输出、管理增量与失败重试 | 结构化结果、增量判断、传输进度与续传 |
| 多机与后台任务 | 需脚本管理并发、进程和状态 | 多机并发/滚动调用、`remote_job` 生命周期 |
| 隧道 | 自行维护 SSH 进程 | `open/close/list` 统一管理 |
| 凭据与审计 | 需额外安排输入通道、策略和日志 | CLI 凭据服务、工具级策略与审计 |

连接复用通常减少后续操作的握手开销；实际延迟取决于网络、服务器和任务。Portal 不依赖 OpenSSH 的控制 socket，也不会继承已有 OpenSSH master 连接。

</details>

### <a id="architecture"></a>调用路径

<details>
<summary>数据流与 CLI / MCP 的协作方式</summary>

```text
MCP client → Portal tools → host / policy / credentials → AsyncSSH pool → remote hosts
                    │
                    └→ local_exec → server's local machine

User terminal → portal CLI → Portal credential agent ← MCP server
User terminal → ssh-add    → ssh-agent               ← AsyncSSH
```

#### <a id="cli-vs-mcp"></a>CLI 与 MCP server

`portal` 与 `portal-mcp-server` 指向同一个 Python 入口，但分别由用户终端和 MCP 客户端启动。CLI 不直接向正在运行的 MCP server 写入密码；它通过凭据服务保存，server 在需要时读取。

共享配置包括 `hosts.yaml`、`policies.yaml`、`secrets.yaml` 和记录凭据服务地址的 `agent.json`。CLI 与 server 各自读取配置；建议一起升级，避免新字段只在一端生效。ssh-agent 是独立的签名服务，通过 `IdentityAgent` 或 `SSH_AUTH_SOCK` 选择。

</details>

### <a id="design-principles"></a>设计原则

优先使用少量职责清楚的工具：一次执行、持久会话、后台任务、文件、传输和资源管理。相同资源的 `action` / `view` 在一个工具中表达，参数用 `Literal` 形成可校验的 schema 枚举。相关取舍也参考 [Writing Tools for Agents](https://www.anthropic.com/engineering/writing-tools-for-agents)。

<details>
<summary>工具数量与上下文开销</summary>

14 个工具的 name、description、inputSchema 合计约 9k tokens，原始测量使用 `tiktoken o200k_base`，具体数值会随描述和版本变化。保留工具的依据是它能提供有用的状态管理、并发/写入保证、凭据边界或结构化结果；便利命令可由执行工具覆盖，无需逐个变成新工具。

</details>

### <a id="step-wise-exec"></a>按步骤执行与后台任务

`remote_exec` / `remote_shell` 是前台步骤：执行、读取输出，再决定下一步。长时间无人交互的工作使用 `remote_job`。

<details>
<summary>timeout、批次、keepalive 与后台生命周期</summary>

前台 `timeout` 必填，上限由 `PORTAL_MAX_TIMEOUT` 控制；超过上限时拒绝调用。`commands=[…]` 适合固定批次，复杂依赖应拆成可检查的步骤。MCP progress 心跳用于向客户端报告调用仍活跃，不能代替服务端超时或保证所有客户端都会续期。

前台调用运行在 MCP server 进程中；stdio 客户端关闭通常也会结束 server。`remote_job` 将命令放到远端后台，任务表按进程尽力持久化，受任务上限和 TTL 管理。它能在客户端断开后继续执行，但不是完整的工作流调度系统。传输不会自行存活，上传重试可使用续传机制。

</details>

### <a id="connection-pool"></a>连接池与持久 shell

<details>
<summary>连接数、channel、shell 边界与进程模型</summary>

连接池按主机管理，每台默认最多 5 条 TCP 连接、每条默认 5 个并发 channel。SFTP、exec、隧道等共享连接。满载时可复用最空闲连接并告警；空闲与超龄连接按配置在后续获取连接时清理，见 [调优参数](#env-vars)。

连接只负责传输；`remote_shell` 的会话另行管理。持久 shell 使用 PTY 和 OSC 133 标记识别命令边界，不靠短暂静默猜测命令是否完成。交互提示会触发软取消；能恢复到提示符时保留会话，恢复超时则销毁。输出受内存上限约束，超限会标记 `truncated`。

AsyncSSH 让会话、SFTP、隧道与异步取消处于同一进程内，便于管理资源和复用凭据。这里的后台任务能力来自远端 `nohup`，并不通过另外启动一套本地 SSH 客户端来维持前台调用。

</details>

### <a id="credential-unification"></a>统一认证与可见反馈

<details>
<summary>凭据边界、告警反馈与维护约束</summary>

所有 SSH 工具经同一连接管理器解析身份；sudo 和 secret 值由服务端按需注入。密码没有 MCP 工具参数入口，后台任务目前不支持 sudo/secrets。设计取舍见 [ADR-0003](./docs/adr/0003-credential-unification.md)。

MCP 客户端可以捕获或忽略 server stderr，因此需要用户知道的配置告警放进 `hosts(action="list")` 的结果；致命错误由相关工具返回。日志供排障与审计使用，不作为唯一用户反馈。工具 description 说明凭据缺失时如何引导 CLI 输入；不依赖客户端把 server-level `instructions` 加入模型上下文。

维护时需要保留以下边界：

- 命令结果可以剥除尾部换行，文件读取必须保留原始内容，否则 hash 无法对应。
- SSH 别名合并需要用别名连接，`HostName` 不一致时拒绝；见 [ADR-0002](./docs/adr/0002-ssh-config-merge.md)。
- sudo patch 先记录原属主、组和权限，暂存到用户私有目录，再在目标旁原子替换；不能统一改成 root 或默认权限。
- 策略拒绝与只读操作的审计行为、写入后的审计失败语义应以 [SECURITY.md](./SECURITY.md) 为准。

</details>

## <a id="testing"></a>测试

### 开发环境

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras
uv run ruff check portal_mcp_server/ tests/
uv run python -m pytest tests/ -v
```

要让 MCP 客户端使用当前源码，可执行 `uv tool install --force --editable .`，再重启 server。完整开发流程见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

### 单元 + 安全（不需要真实 SSH）

```bash
pytest tests/ -v
# live SSH 测试默认 skip（受 PORTAL_TEST_LIVE 环境变量控制）
```

覆盖：command injection regression、safety validators、hash-protected editor、concurrency、resource lifecycle、multi-host policy enforcement、password_command/passphrase_command 安全不变量、audit fail mode。

### 端到端 live smoke

`tests/live_smoke.py` 直接 import 本地工作树驱动一系列真实 SSH 行为：`hosts.yaml` 残留 `password:` 字段处理、`ssh_exec` 基础调用、`remote_exec(group_tag=...)` 在真实主机上的 gate（blocked 命令 + 不在 allowlist 的主机均拦截）、`remote_shell` 单命令的 gate、`remote_shell` + `remote_patch` 在远端 `/tmp/` 的 round-trip（含 stale-hash 拒绝路径）、audit.jsonl 是否吃到新加的 operation tag。

```bash
PORTAL_AUDIT_FAIL_OPEN=1 \
  PORTAL_TEST_HOST=server.example.com PORTAL_TEST_PORT=22 PORTAL_TEST_USER=deploy \
  PORTAL_TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ 它会在远端 `/tmp/portal-mcp-server-smoke-<pid>.txt` 写一次再删除——只动 `/tmp`。

## <a id="ci-release"></a>CI / Release

仓库用 GitHub Actions 自动化跑测试和发布，本地不需要手动 build：

- **CI**（[`ci.yml`](.github/workflows/ci.yml)）：每个 PR / push to `main` 在 Python **3.10 / 3.11 / 3.12 / 3.13**（ubuntu）上 `ruff check portal_mcp_server/ tests/` + `pytest tests/`（产品代码和测试一起 lint），另有 macOS 全量 job 与 Windows 命名管道 / 计划任务 job；全绿才能 merge。
- **Release**（[`release.yml`](.github/workflows/release.yml)）：push 一个 `v*` tag 自动触发（含 PEP 440 预发布 / dev / post，如 `v4.0.0a0`）——`python -m build` 产出 wheel + sdist → 从 `CHANGELOG.md` awk 抽出对应版本段做 [GitHub Release](https://github.com/TMYTiMidlY/portal-mcp-server/releases) body → 通过 [PyPI trusted publishing](https://docs.pypi.org/trusted-publishers/)（OIDC 短令牌，无静态 token）发布到 [PyPI](https://pypi.org/project/portal-mcp-server/)。

完整发布流程、CHANGELOG 格式约束与 release 失败排障见 [`CONTRIBUTING.md` § CI & Release 自动化](./CONTRIBUTING.md#ci--release-自动化)。

## <a id="contributing"></a>贡献

欢迎 issue 与 PR。简版要点：

- Python 3.10+，I/O 全部 `async/await`，无阻塞调用
- 不出现硬编码 hostname / username / IP / path
- 新工具写好 docstring（FastMCP 用作 MCP description）+ 同步 README「工具列表」节（含折叠的完整签名 + 源码位置表）
- 状态变更工具必须过 `_gate` + 写 `audit_log`
- 测试覆盖关键路径；`pytest tests/ -v` 必须全绿
- 不 commit secret；`examples/hosts.yaml` 是唯一 schema 模板
- commit message 走 [Conventional Commits](https://www.conventionalcommits.org/)

完整开发流程、新工具开发清单、PR 模板、安全 / 隐私规则见 **[`CONTRIBUTING.md`](./CONTRIBUTING.md)**（[English](./CONTRIBUTING.en.md)）。

## <a id="license-credits"></a>协议与致谢

Apache License 2.0（见 [`LICENSE`](LICENSE)）。

衍生关系与 third-party 算法引用见 [`NOTICE`](NOTICE)：

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0）**——git ancestry，底层模块（asyncssh 引擎、连接池、tunnel 管理、orchestrator、安全策略）沿用；上层 14 个 portal 工具是新设计
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT）**——`remote_text_editor.py` 的 SHA-256 hash-protected edit 算法参考来源，针对 AsyncSSH SFTP 重写

> ⚠️ 本工具让 agent 拥有对远端系统的 SSH 访问能力。请只在你拥有或被授权的系统上使用。
