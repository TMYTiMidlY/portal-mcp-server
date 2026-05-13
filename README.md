# portal-mcp-server

> **Now you're thinking with portals.**
> 给 coding agent（Claude Code / Copilot CLI / Cursor …）用的 SSH orchestration MCP server——agent 操作远端就像操作本地。
> 18 个工具、AsyncSSH + FastMCP、SHA-256 防冲突的远端文件编辑、跨工具复用的连接池、跨平台一致的复用性能。

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen)](https://modelcontextprotocol.io/)

🌐 **Language**: **中文**（默认） · [English](./README.en.md)

---

## 目录

- [一句话定位](#一句话定位)
- [为什么 18 个工具，而不是 57 个](#为什么-18-个工具而不是-57-个)
- [18 个工具一览](#18-个工具一览)
- [为什么这套设计](#为什么这套设计)
- [安装](#安装)
- [接入 agent](#接入-agent)
- [配置](#配置)
- [安全](#安全)
- [测试](#测试)
- [本地改代码不生效？](#本地改代码不生效)
- [致谢与许可](#致谢与许可)

---

## 一句话定位

`portal-mcp-server` fork 自 [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0），底层 SSH/asyncssh 引擎、连接池、tunnel 管理、多机编排算法、安全策略沿用上游模块；上层重新设计了**面向 agent 的 18 个 `portal_*` 工具**——2 个 hash-protected 的远端文件编辑工具（`portal_read` / `portal_patch`，算法参考 [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor) MIT，clean-room 重写）+ 6 个核心 IO/搜索/持久 bash 工具 + 10 个用 `mode` 字段合并的高层工具。

详见 [`NOTICE`](./NOTICE) 与 [`SECURITY.md`](./SECURITY.md)。

---

## 为什么 18 个工具，而不是 57 个

Anthropic 的 [_Writing Tools for Agents_](https://www.anthropic.com/engineering/writing-tools-for-agents) 指南明确说：
> "More tools don't always lead to better outcomes... Tools that merely wrap existing software functionality is a common error... Too many tools or overlapping tools can also distract agents from pursuing efficient strategies."

上游 `ssh-shell-mcp` 把每种 ergonomic 都做成单独 tool（`ssh_run` / `ssh_run_batch` / `ssh_run_script` / `ssh_run_with_env` / `ssh_session_exec` / `ssh_ps` / `ssh_kill` / `ssh_df` / `ssh_free` / `ssh_journalctl` / `ssh_docker` / `ssh_tmux_*` …），共 **57 个**。这些工具大部分是 bash 一行命令的包装，**`portal_bash`（持久 bash session）一个工具就能覆盖**。

portal-mcp-server 的设计取舍：

| 类别 | 数量 | 处理方式 |
|---|---:|---|
| **保留并重新设计** | 8 个 portal core | `portal_read` + `portal_patch` 用 SHA-256 hash 保护取代裸 cat/write 的并发漏洞；`portal_grep` / `portal_glob` 提供结构化搜索结果；`portal_bash`(`_close`/`_status`) 持久 shell；`portal_cleanup_tmps` 处理中断遗留 |
| **mode-flag 合并** | 10 个 portal 高层 | `portal_tunnel_open(mode=local\|reverse\|socks)` 取代上游 3 个独立 tool；`portal_multi_exec(mode=parallel\|rolling\|broadcast)` 取代上游 4 个；`portal_audit(view=...)` 合并 status/history/stats/policy 4 个 introspection 接口；类似的合并多处 |
| **完全砍掉** | 27 个 | 全部能由 `portal_bash` 直接覆盖：命令执行族 5、多 session 族 6、系统检查族（ps/df/free/journalctl/info/netstat/service）7、进程管理族 5、tmux 族 4 |

结果：context 占用从 ~7.5k tokens 降到 ~2.5k；agent 不再需要在多个语义重复的工具里选择。

---

## 18 个工具一览

### 8 个 portal core（首选入口）

| 工具 | 给 agent 的能力 |
|---|---|
| `portal_read` / `portal_patch` | 读远端文件并拿 SHA-256；patch 用 `file_hash` + per-range hash 防并发覆盖，写入走 tmp + `posix_rename` 原子替换，写后再 hash 校验 |
| `portal_grep` / `portal_glob` | 远端 `rg --json` / `find` 结构化输出；首次连接探测一次缓存 |
| `portal_bash` / `portal_bash_close` / `portal_bash_status` | 每个 host 一个粘性 `bash -i`，cwd / env 跨调用保留；PTY echo + bracketed-paste 关掉以让 sentinel 完整工作 |
| `portal_cleanup_tmps` | patch 中断后留下的孤儿 `*.mcp_tmp.*` 清理 |

### 10 个 portal 高层（mode 切换）

| 工具 | mode/参数 | 用途 |
|---|---|---|
| `portal_host` | `action=list\|register\|remove` | 主机注册（用于 tag 分组；`~/.ssh/config` 别名自动解析无需登记） |
| `portal_transfer` | `direction=upload\|download\|sync` | SFTP 文件传输（二进制安全） |
| `portal_tunnel_open` / `_close` / `_list` | `mode=local\|reverse\|socks` | SSH 隧道（端口转发 / 反向 / SOCKS5） |
| `portal_multi_exec` | `mode=parallel\|rolling\|broadcast`，`hosts_json\|group_tag` | 多机命令编排 |
| `portal_playbook` | `host\|group_tag` | 多步骤剧本 |
| `portal_ping` | optional `hosts_json` | 健康检查（单机或全 fleet） |
| `portal_audit` | `view=snapshot\|history\|stats\|policy` | 审计日志 + 服务器内部状态 introspection |
| `portal_check` | `host`, optional `command` | 安全策略 dry-run |

> `~/.ssh/config` 别名**自动解析**——`get_connection("1810")` 找不到时自动从 `~/.ssh/config` 注册；asyncssh 原生处理 HostName / User / Port / IdentityFile / ProxyJump。
>
> 配套的 [`remote` skill](https://github.com/TMYTiMidlY/skills) 教 agent 何时按 read → patch 流程改远端代码、何时用 `/tmp` 沙箱、何时该问。

---

## 为什么这套设计

### 跨工具复用的进程内连接池

portal-mcp-server 在 server 进程内部维护 asyncssh 连接池——所有工具调用（`portal_bash`、`portal_read`、`portal_transfer` …）共享同一条 TCP，**除第一次连接外全部摊销到 channel 创建（~10–30 ms）**。

跟 "裸 ssh + ControlMaster"（最佳 plain 方案）对比：

| 维度 | portal-mcp-server | plain ssh + ControlMaster |
|---|---|---|
| 复用机制 | asyncssh 进程内连接池（每 host 最多 5 条 TCP） | OpenSSH master 进程 + Unix domain socket |
| 复用粒度 | **进程级**（MCP server 活着就持续） | 会话级（默认 10min `ControlPersist`） |
| 第一次连接 | TCP + auth（~200–500 ms） | TCP + auth（~200–500 ms） |
| 后续命令 | 复用连接，开新 channel（**~10–30 ms**） | 复用 master，开新 channel（**~10–30 ms**） |
| 跨工具复用 | ✅ `portal_bash` 和 `portal_read` 共享同一 TCP | ❌ `ssh` 和 `scp` 复用要求两边 `ControlPath` 一致 |
| 持久 shell 状态 | ✅ `portal_bash` 维护 `bash -i` 会话，cwd/env 跨调用保留 | ❌ 每次 `ssh host cmd` 是新 shell，cwd/env 不留 |
| 并发 | asyncio 多 channel 真并发 | 多 ssh 进程串行启动（共享 master） |
| Windows | ✅ 任何能跑 Python 的平台都享受同等性能 | ❌ Windows OpenSSH 不支持 ControlMaster |

> 实测脱敏：同 LAN（< 1ms RTT）跑 100 次 `echo pong`：plain ssh + ControlMaster 平均 23 ms；portal-mcp-server 通过 `portal_bash` 平均 18 ms（省了 ssh 客户端进程启动）。第一次连接两边都 ~280 ms（auth 占大头）。

### Windows 上的差距

`ControlMaster` 在 Windows OpenSSH 上**不工作**——它依赖 Unix domain socket 实现 master/子进程之间共享文件描述符，Win10/11 默认编译不带这个机制（实验性 named-pipe 也常出问题）。

portal-mcp-server **完全不依赖** OS 级 socket 共享：连接池放在 MCP server 自己的 Python 进程内存里（asyncssh 是纯 Python），任何能跑 Python 的平台（Windows / macOS / Linux）都享受**与 Linux 一致**的复用性能。

```text
Windows 下：
  plain ssh:        每次 cmd 都新建 TCP+auth      → ~300 ms × N
  portal-mcp-server: 第一次 ~280 ms，后续 ~20 ms  → 单调下降到 channel 极限
```

副作用红利：池连接随 MCP server 进程持续（小时级），不是 `ControlPersist` 默认的 10 分钟——长会话里的 reconnect 抖动也省了。

### 为什么 asyncssh，不是 subprocess 包装 OpenSSH

[asyncssh](https://github.com/ronf/asyncssh)（EPL-2.0/GPL-2.0 双许可）是 SSHv2 协议的**独立纯 Python 实现**，与 OpenSSH 协议层等价。核心特性：

- **单进程多连接、单连接多 session**：连接池就是 Python dict，没有进程边界、没有 fd 共享需求
- **协议层完整覆盖**：local/remote/dynamic 端口转发、SFTP、SCP、X11 fwd、TUN/TAP——OpenSSH 能干的协议层动作 asyncssh 全都能干
- **OpenSSH 兼容**：原生解析 `~/.ssh/config`、`known_hosts`、`authorized_keys`、ssh-agent / Pageant
- **仅依赖 PyCA `cryptography`**：装上 Python 就能跑，无 C 依赖、无 OS 特定 IPC

对比 "用 subprocess 调 `ssh` / `scp`"：
- 不用每次 fork 新进程（启动 ~50–100 ms 没了）
- 不用协调多进程之间共享 SSH 复用（这正是 ControlMaster 在 Win 上挂的地方）
- 错误处理、重试、超时都是 Python 异步原语，不是解析 stderr 字符串

---

## 安装

按身份选路径。

### Agent / 终端用户（用 MCP server，不动源码）

不需要 clone，让 MCP client 通过 `uvx` 直接从 GitHub 拉运行——见下方[接入 agent](#接入-agent)。`uvx` 第一次启动缓存依赖，后续重启秒级。

shell 里手动 smoke test：
```bash
uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git portal-mcp-server --help
```

### 开发者（要改代码 / 跑测试）

推荐 `uv sync`，按 `pyproject.toml` + `uv.lock` 一次到位准备好 `.venv`：

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras
source .venv/bin/activate
pytest                        # 129 passed, 22 skipped
```

不想用 uv 也可以走标准 pip editable install：
```bash
pip install -e ".[dev]"       # 含 pytest 等 dev 依赖
# 或纯运行时
pip install -e .
```

---

## 接入 agent

### Copilot CLI（项目级 `.mcp.json`）

Copilot CLI 原生支持工作区级 `.mcp.json`（与 Claude Code / Cursor 同格式）：

```json
{
  "mcpServers": {
    "portal": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/TMYTiMidlY/portal-mcp-server.git",
        "portal-mcp-server"
      ]
    }
  }
}
```

验证：
```bash
cd <project>
copilot mcp list                # → Workspace servers: portal (local)
copilot mcp get portal          # → Source: Workspace (<project>/.mcp.json)
```

> ⚠️ 不要用 `copilot mcp add portal -- ...`——它默认写到 user-level `~/.copilot/mcp-config.json`，会污染所有项目。直接编辑 `.mcp.json` 才能保持项目级。

### VS Code（`.vscode/mcp.json`）

VS Code 用不同 schema（顶层 key 是 `servers` 而非 `mcpServers`）：

```json
{
  "servers": {
    "portal": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/TMYTiMidlY/portal-mcp-server.git",
        "portal-mcp-server"
      ]
    }
  }
}
```

> 两种格式不兼容。同时用 Copilot CLI 和 VS Code 时需各维护一份。

### 配套 skill

在 [TMYTiMidlY/skills](https://github.com/TMYTiMidlY/skills) 安装 `remote` skill（按 `manage-skills` 流程软链到 `<target>/.agents/skills/`）。Agent 收到 "在 1810 上 ..." 之类指令时会自动遵循 hash-check 流程和 `/tmp` 默认沙箱规则。

---

## 配置

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `SSH_HOSTS_YAML` | 主机注册 YAML | `./config/hosts.yaml` 若存在，否则 `$XDG_CONFIG_HOME/portal-mcp-server/hosts.yaml` |
| `SSH_POLICIES_YAML` | 安全策略 YAML | `./config/policies.yaml` 若存在，否则 `$XDG_CONFIG_HOME/portal-mcp-server/policies.yaml` |
| `SSH_MCP_LOG_DIR` | audit + server log 目录 | `./logs/` 若存在，否则 `$XDG_STATE_HOME/portal-mcp-server/logs/` |
| `SSH_MCP_AUDIT_FAIL_OPEN` | 设 `1` → audit 写盘失败时仅 warning 并继续；默认（未设）→ **fail-closed**，audit 写不进则操作 raise 中止 | _(unset)_ |
| `MCP_AUTH_TOKEN` | HTTP transport 的 Bearer token | _(none)_ |

`config/hosts.example.yaml` 给了完整 schema 模板。**`hosts.yaml` 含真实凭据，已在 `.gitignore`，永远别 commit**。

---

## 安全

### 默认安全约束

portal-mcp-server 不强制路径白名单——这事交给配套 `remote` skill 在 prompt 层强制：
> **默认只可写远端 `/tmp/` 路径；改用户家目录或项目代码目录前必须先问。**

想做机器级强制，就在 `config/policies.yaml` 的 `command_blocklist` 加规则（如 `"rm -rf /home/*"`）。

### Policy gate

`SecurityPolicy` 检查：host allowlist（fnmatch）、command blocklist/allowlist（fnmatch）、per-host rate limit（sliding window）。所有命令执行类工具走 `_gate(host, command)`；多主机编排（`portal_multi_exec` 的 parallel/rolling/broadcast 模式、`portal_playbook` 的 group 路径）走 `_gate_many(hosts, command)`，playbook 还会遍历 `steps` 逐条过 blocklist。`portal_bash` 也对每条命令 gate（持久 session 不等于授权一切命令）。

### 认证

**仅支持 key-based auth**。`HostConfig` 不带 `password` 字段，`portal_host(action="register", ...)` 没有 `password` 参数。`hosts.yaml` 里若残留 `password:` 键会被启动时 ERROR 日志提示并忽略。

### Audit

所有改状态的工具写 `logs/audit.jsonl`（exec / file write / patch / register / tunnel / playbook / multi-host orchestration）。只读类（`portal_read` / `portal_grep` / `portal_glob` / `portal_audit` / `portal_check` / `portal_tunnel_list`）显式不审计以减噪音。

**默认 fail-closed**——audit 写盘失败时操作 raise 中止；设 `SSH_MCP_AUDIT_FAIL_OPEN=1` 切到 fail-open（仅 warning 后继续，适合 dev/test）。

更详细的算法依据与设计 diff 见 [`SECURITY.md`](./SECURITY.md)。漏洞披露请走 GitHub Security Advisories。

---

## 测试

### 单元 + 安全（不需要真实 SSH）

```bash
pytest tests/ -v
# 129 passed, 22 skipped (live SSH tests gated by SSH_TEST_LIVE)
```

覆盖：command injection regression、safety validators、hash-protected editor、concurrency、resource lifecycle、multi-host policy enforcement、no-password-auth invariants、audit fail mode。

### 端到端 live smoke

`tests/live_smoke.py` 直接 import 本地工作树驱动一系列真实 SSH 行为：`hosts.yaml` `password:` 残留处理、`ssh_exec` 基础调用、`portal_multi_exec(mode="parallel", group_tag=...)` 在真实主机上的 gate（blocked 命令 + 不在 allowlist 的主机均拦截）、`portal_bash` 单命令的 gate、`portal_bash` + `portal_patch` 在远端 `/tmp/` 的 round-trip（含 stale-hash 拒绝路径）、audit.jsonl 是否吃到新加的 operation tag。

```bash
SSH_MCP_AUDIT_FAIL_OPEN=1 \
  TEST_HOST=<your-host> TEST_PORT=22 TEST_USER=<user> \
  TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ 它会在远端 `/tmp/portal-mcp-server-smoke-<pid>.txt` 写一次再删除——只动 `/tmp`。

> 仓库里另有 `examples/phase6_acceptance.py`，是开发期的端到端 demo，**硬编码了 host alias `1810` 和路径 `~/SU2-Quantum/`**，仅作内部回归参考；新用户跑前需要先按代码改 host 与路径。

---

## 本地改代码不生效？

`uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git portal-mcp-server` 在 MCP client 启动那一刻去 GitHub 拉本仓库 main 的最新 commit。所以：

| 你在哪改 | agent 的 MCP server 看得见吗 |
|---|---|
| 本地工作树 (`/home/.../portal-mcp-server/`) | ❌ 看不见。uvx 走的是远端 git，不是本地路径 |
| 已 commit 但没 push | ❌ 看不见 |
| commit + push 到 `TMYTiMidlY/portal-mcp-server` main | ✅ 但需要重启 MCP client（uvx 启动时 fetch；同一进程内不会重拉） |

排错时验证当前启动加载的到底是哪个版本：

```bash
# 必须 cd 到非项目目录再跑，否则 uvx 会优先认本地工作树
cd /tmp && uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git \
  --refresh python -c "
import portal_mcp_server.audit as a
print('audit env var:', getattr(a, '_FAIL_OPEN_ENV',
      'NOT SET — running an OLD/published version'))
"
```

- `audit env var: SSH_MCP_AUDIT_FAIL_OPEN` → 已是含本次安全收紧的新版
- `NOT SET — running an OLD/published version` → uvx 拉到的还是旧 commit（push 没生效，或 uvx 缓存没刷新——加 `--refresh` 即可）

本地调试想让 agent 不 push 也能用上改动，把 `mcp-config.example.json` 里的 `args` 临时改成：
```json
"args": ["--from", "/absolute/path/to/portal-mcp-server", "portal-mcp-server"]
```
（路径必须绝对）。**别把这条本地路径 commit 进 example**。

---

## 致谢与许可

Apache License 2.0（见 [`LICENSE`](LICENSE)）。

衍生关系与 third-party 算法引用见 [`NOTICE`](NOTICE)：

- **[`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0）**——git ancestry，底层模块（asyncssh 引擎、连接池、tunnel 管理、orchestrator、安全策略）沿用；上层 18 个 `portal_*` 工具是新设计
- **[`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT）**——`remote_text_editor.py` 的 SHA-256 hash-protected edit 算法参考来源（clean-room 重写，无源码复制）

> ⚠️ 本工具给 agent 提供对远端系统的程序化 SSH 访问。**仅在你拥有或获得明确书面授权的系统上使用。** 未授权访问在多数司法辖区均属违法。
