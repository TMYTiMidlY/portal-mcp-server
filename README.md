# portal-mcp-server

> **Now you're thinking with portals.**
> Agent-feels-local SSH orchestration for coding agents (Claude Code, Copilot CLI, Cursor, …).
> **18 MCP tools** over AsyncSSH + FastMCP. 持久 bash session、SHA-256 防冲突的远端文件编辑、远端 ripgrep/find、`~/.ssh/config` 别名自动解析、policy-gated multi-host orchestration、fail-closed audit。

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen)](https://modelcontextprotocol.io/)

> **Origin & attribution**: Fork 自 [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0），底层 SSH/asyncssh 引擎、连接池、安全策略、tunnel 管理、多机编排算法等沿用上游模块；上层重新设计了**面向 agent 的 18 个工具**——8 个 hash-protected 的 `portal_*` 核心工具（read / patch / grep / glob / bash / bash_close / bash_status / cleanup_tmps），加上 10 个用 `mode` 字段合并的高层工具（host / transfer / tunnel_open|close|list / multi_exec / playbook / ping / audit / check）。`portal_read` / `portal_patch` 的 hash-protected 编辑算法参考了 [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT，clean-room 重写）。详见 [`NOTICE`](./NOTICE) 与 [`SECURITY.md`](./SECURITY.md)。

---

## Why 18 tools instead of 70+

Anthropic 的 [_Writing Tools for Agents_](https://www.anthropic.com/engineering/writing-tools-for-agents) 指南明确说："More tools don't always lead to better outcomes... Tools that merely wrap existing software functionality is a common error... Too many tools or overlapping tools can also distract agents from pursuing efficient strategies."

上游 `ssh-shell-mcp` 沿袭"SSH shell over MCP"思路，把每种 ergonomic 都做成单独 tool（`ssh_run` / `ssh_run_batch` / `ssh_run_script` / `ssh_run_with_env` / `ssh_session_exec` / `ssh_ps` / `ssh_kill` / `ssh_df` / `ssh_free` / `ssh_journalctl` / `ssh_docker` / `ssh_tmux_*`…），共 57+ 个。这些工具大部分是 bash 一行命令的包装，**`portal_bash`（持久 bash session）一个工具就能覆盖**。

portal-mcp-server 的设计取舍：

- **8 个 `portal_*` 核心** —— `portal_bash` 替代了 ~20 个 thin-wrapper 工具；`portal_read` + `portal_patch` 用 hash 保护取代裸 cat/write 的并发漏洞；`portal_grep` / `portal_glob` 提供结构化搜索结果。
- **10 个 `portal_*` 高层** —— 用 `mode` 字段合并行为同质的工具（`portal_tunnel_open(mode=local|reverse|socks)` 取代 3 个独立 tool；`portal_multi_exec(mode=parallel|rolling|broadcast)` 取代 4 个等等）。
- **27 个工具被砍** —— 全部被 `portal_bash` 或 `portal_*` 高层覆盖，每个删除都附原因（命令执行族 5 个、多 session 族 6 个、系统检查族 7 个、进程管理族 5 个、tmux 族 4 个；见 CHANGELOG）。

结果：context 占用从 ~7.5k tokens 降到 ~2.5k；agent 不再需要在多个语义重复的工具里选择。

---

## What this gives an AI agent

### 8 个 portal core 工具（首选入口）

| 工具 | 给 agent 的能力 |
|---|---|
| `portal_read` / `portal_patch` | 读远端文件并拿 SHA-256；patch 用 `file_hash` + per-range hash 防并发覆盖，写入走 tmp+`posix_rename` 原子替换，写后再 hash 校验 |
| `portal_grep` / `portal_glob` | 远端 `rg --json` / `find` 结构化输出；首次连接探测一次缓存 |
| `portal_bash` / `portal_bash_close` / `portal_bash_status` | 每个 host 一个粘性 `bash -i`，cwd / env 跨调用保留；PTY echo + bracketed-paste 关掉以让 sentinel 完整工作 |
| `portal_cleanup_tmps` | patch 中断后留下的孤儿 `*.mcp_tmp.*` 清理 |

### 10 个 portal 高层工具（mode 切换）

| 工具 | mode/参数 | 用途 |
|---|---|---|
| `portal_host` | `action=list\|register\|remove` | 主机注册（用于 tag 分组；`~/.ssh/config` 别名自动解析无需登记） |
| `portal_transfer` | `direction=upload\|download\|sync` | SFTP 文件传输（二进制安全） |
| `portal_tunnel_open` / `portal_tunnel_close` / `portal_tunnel_list` | `mode=local\|reverse\|socks` | SSH 隧道（端口转发 / 反向 / SOCKS5） |
| `portal_multi_exec` | `mode=parallel\|rolling\|broadcast`，`hosts_json\|group_tag` | 多机命令编排 |
| `portal_playbook` | `host\|group_tag` | 多步骤剧本 |
| `portal_ping` | optional `hosts_json` | 健康检查（单机或全 fleet） |
| `portal_audit` | `view=snapshot\|history\|stats\|policy` | 审计日志 + 服务器内部状态 introspection |
| `portal_check` | `host`, optional `command` | 安全策略 dry-run |

`~/.ssh/config` 别名**自动解析**——`get_connection("1810")` 找不到时自动从 `~/.ssh/config` 注册；asyncssh 原生处理 HostName / User / Port / IdentityFile / ProxyJump。

配套的 [`remote` skill](https://github.com/TMYTiMidlY/skills) 教 agent 怎么按 read → patch 流程改远端代码、何时用 `/tmp` 沙箱、何时该问。

---

## SSH Connection Reuse Performance

portal-mcp-server 在 server 进程内部维护 asyncssh 连接池——所有工具调用（`portal_bash`、`portal_read`、`portal_transfer` 等）共享同一条 TCP，**几乎所有除第一次连接外的操作都摊销到 channel 创建（~10-30ms）**。

下表是与"裸 ssh + ControlMaster"方案的对比（即 `~/.ssh/config` 里启用 `ControlMaster auto / ControlPersist 10m` 的最佳 plain 方案）：

| 维度 | portal-mcp-server | plain ssh + ControlMaster |
|---|---|---|
| 复用机制 | asyncssh 进程内连接池（每 host 最多 5 条 TCP） | OpenSSH master 进程 + Unix domain socket |
| 复用粒度 | **进程级**（MCP server 活着就持续） | 会话级（默认 10min `ControlPersist`） |
| 第一次连接成本 | TCP + auth（~200-500ms） | TCP + auth（~200-500ms） |
| 后续命令 | 复用现有连接，开新 channel（**~10-30ms**） | 复用 master，开新 channel（**~10-30ms**） |
| 跨工具复用 | ✅ `portal_bash` 和 `portal_read` 共享同一 TCP | ❌ `ssh` 和 `scp` 复用要求两边 `ControlPath` 一致 |
| 持久 shell 状态 | ✅ `portal_bash` 维护 `bash -i` 会话，cwd/env 跨调用保留 | ❌ 每次 `ssh host cmd` 是新 shell，cwd/env 不留 |
| 并发 | asyncio 多 channel 真并发 | 多 ssh 进程串行启动（共享 master） |
| 失活检测 | `is_alive` 主动剔除 + asyncssh 心跳 | TCP keepalive |

性能上**纯连接复用部分两边持平**——优势在 (1) 跨工具复用粒度更粗、(2) `portal_bash` 提供 ControlMaster 做不到的 stateful shell、(3) 跨平台一致（见下节）。

> 测试方法（脱敏）：在同一 LAN（< 1ms RTT）的 host 上跑 100 次 `echo pong`：plain ssh 复用 ControlMaster 平均 23ms/次；portal-mcp-server 通过 `portal_bash` 平均 18ms/次（省了 ssh 客户端进程启动）。第一次连接两边都 ~280ms（auth dominated）。

---

## Why MCP > plain ssh on Windows

`ControlMaster` 在 Windows OpenSSH 上**不工作**——它依赖 Unix domain socket 实现 master 进程与子 ssh 进程之间共享文件描述符，而 Windows OpenSSH 默认编译选项不带这个机制（Win 10/11 实验性 named-pipe 支持也常出问题）。

portal-mcp-server **完全不依赖** OS 的 socket 共享：连接池放在 MCP server 自己的 Python 进程内存里（asyncssh 是纯 Python），任何能跑 Python 的平台（Windows / macOS / Linux）都能享受**与 Linux 完全一致**的复用性能：

```text
┌─────────────────────────────────────────────────────────────┐
│  Plain ssh on Windows:                                       │
│   ssh host cmd1   ─→  full TCP+auth  ─→  cmd1                │
│   ssh host cmd2   ─→  full TCP+auth  ─→  cmd2  (no master!)  │
│   每次都新连接，每次 ~300ms                                       │
├─────────────────────────────────────────────────────────────┤
│  portal-mcp-server on Windows:                               │
│   first call    ─→  TCP+auth (one-time, ~280ms)              │
│   subsequent    ─→  reuse pool, new channel (~20ms)          │
│   每次都复用，平均 ~20ms                                          │
└─────────────────────────────────────────────────────────────┘
```

副作用红利：池连接随 MCP server 进程持续（小时级），不是 `ControlPersist` 默认的 10 分钟——长时间会话里的 reconnect 抖动也省了。

### Why asyncssh

portal-mcp-server 的 SSH 引擎用 [asyncssh](https://github.com/ronf/asyncssh)（EPL-2.0/GPL-2.0 双许可），不是把命令行 OpenSSH `subprocess.run` 起来。asyncssh 是 SSHv2 协议的**独立纯 Python 实现**（与 OpenSSH 是协议层等价的两个 implementation），核心特性：

- **单进程多连接、单连接多 session**：连接池就是 Python dict，没有进程边界，没有 fd 共享需求
- **协议层完整覆盖**：local/remote/dynamic 端口转发、SFTP、SCP、X11 fwd、TUN/TAP——OpenSSH 能干的协议层动作 asyncssh 全都能干
- **OpenSSH 兼容**：原生解析 `~/.ssh/config`、`known_hosts`、`authorized_keys`、ssh-agent / Pageant，与 OpenSSH 客户端互通
- **仅依赖 PyCA `cryptography`**：装上 Python 就能跑，无 C 依赖、无 OS 特定 IPC

对比"用 subprocess 调 `ssh`/`scp`"的方案：
- 不需要每次 `ssh host cmd` fork 一个新进程（启动 ~50-100ms 没了）
- 不需要协调多个 OS 进程之间共享 SSH 复用（这正是 ControlMaster 在 Win 上挂的地方）
- 错误处理、重试、超时都是 Python 异步原语，不是解析 stderr 字符串

---

## Install

按身份选路径：

### 给 agent / 终端用户（用 MCP server，不动源码）

不需要 clone，让 MCP client 通过 `uvx` 直接从 GitHub 拉运行——见下方 [Register with your agent](#register-with-your-agent)。`uvx` 第一次启动时会缓存依赖，后续重启秒级。

要在 shell 里手动跑一下试探：

```bash
uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git portal-mcp-server --help
```

### 给开发者（要改代码 / 跑测试）

推荐 `uv sync`，它会按 `pyproject.toml` + `uv.lock` 一次到位准备好 `.venv` 和所有 dev 依赖：

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras           # → .venv with prod + [dev] deps
source .venv/bin/activate
pytest                         # 129 passed, 22 skipped
```

不想用 uv 的话，标准 pip editable install 也可以（自己负责 venv）：

```bash
pip install -e ".[dev]"        # prod + dev (pytest etc.)
# 或纯运行时
pip install -e .
```

### 验证（在远端跑端到端 demo）

```bash
PYTHONPATH=. python examples/phase6_acceptance.py
```

应输出 `🎉 ALL 7 STEPS PASSED — Phase 6 acceptance complete`。覆盖：read 拿 sha256 → 远端 grep 找符号 → patch 修改 sandbox 文件 → **负向**：模拟别人改文件后旧 hash patch 被拒 → 持久 bash 验证 cwd 跨调用保留 → 验证只有 1 条 SSH 连接。

---

## Register with your agent

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

VS Code 用不同的 schema（顶层 key 是 `servers` 不是 `mcpServers`）：

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

> 两种格式不兼容。如果同时用 Copilot CLI 和 VS Code，需要各维护一份。

### 配套 skill

在 [TMYTiMidlY/skills](https://github.com/TMYTiMidlY/skills) 安装 `remote` skill（按 `manage-skills` 流程软链到 `<target>/.agents/skills/`）。Agent 收到「在 1810 上 ...」之类指令时会自动遵循 hash-check 流程和 `/tmp` 默认沙箱规则。

---

## Configuration

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `SSH_HOSTS_YAML` | 主机注册 YAML | `./config/hosts.yaml` 若存在，否则 `$XDG_CONFIG_HOME/portal-mcp-server/hosts.yaml` |
| `SSH_POLICIES_YAML` | 安全策略 YAML | `./config/policies.yaml` 若存在，否则 `$XDG_CONFIG_HOME/portal-mcp-server/policies.yaml` |
| `SSH_MCP_LOG_DIR` | audit + server log 目录 | `./logs/` 若存在，否则 `$XDG_STATE_HOME/portal-mcp-server/logs/` |
| `SSH_MCP_AUDIT_FAIL_OPEN` | 设 `1` → audit 写盘失败时仅 warning 并继续；默认（未设）→ **fail-closed**，audit 写不进则操作 raise 中止 | _(unset)_ |
| `MCP_AUTH_TOKEN` | HTTP transport 的 Bearer token | _(none)_ |

`hosts.example.yaml` 给了完整 schema 模板。**`hosts.yaml` 含真实凭据，已在 `.gitignore`，永远别 commit**。

---

## Security

### 默认安全约束

portal-mcp-server 不强制路径白名单——这事交给配套的 `remote` skill 在 prompt 层强制：

> **默认只可写远端 `/tmp/` 路径；改用户家目录或项目代码目录前必须先问。**

如果想做机器级强制，在 `config/policies.yaml` 的 `command_blocklist` 加规则（如 `"rm -rf /home/*"`）。

### Policy gate

`SecurityPolicy` 检查：host allowlist（fnmatch）、command blocklist/allowlist（fnmatch）、per-host rate limit（sliding window）。所有命令执行类工具走 `_gate(host, command)`；多主机编排（`portal_multi_exec` 的 parallel/rolling/broadcast 模式、`portal_playbook` 的 group 路径）走 `_gate_many(hosts, command)`，playbook 还会遍历 `steps` 逐条过 blocklist。`portal_bash` 也对每条命令 gate（持久 session 不等于授权一切命令）。

### 认证

**仅支持 key-based auth**。`HostConfig` 不带 `password` 字段，`portal_host(action="register", ...)` 没有 `password` 参数。`hosts.yaml` 里若残留 `password:` 键会被启动时 ERROR 日志提示并忽略。

### Audit

所有改状态的工具写 `logs/audit.jsonl`（exec / file write / patch / register / tunnel / playbook / multi-host orchestration）。只读类（`portal_read` / `portal_grep` / `portal_glob` / `portal_audit` / `portal_check` / `portal_tunnel_list`）显式不审计以减噪音。

**默认 fail-closed**——audit 写盘失败时操作 raise 中止；设 `SSH_MCP_AUDIT_FAIL_OPEN=1` 切到 fail-open（仅 warning 后继续，适合 dev/test）。

更详细的算法依据与设计 diff 见 [SECURITY.md](SECURITY.md)。漏洞披露请走 GitHub Security Advisories。

---

## Testing

### 单元 + 安全（不需要真实 SSH）

```bash
pytest tests/ -v
# 129 passed, 22 skipped (live SSH tests gated by SSH_TEST_LIVE)
```

覆盖：command injection regression、safety validators、hash-protected editor、concurrency、resource lifecycle、multi-host policy enforcement、no-password-auth invariants、audit fail mode。

### 端到端 live smoke

`tests/live_smoke.py` 直接 import 本地工作树驱动一系列真实 SSH 行为：hosts.yaml `password:` 残留处理、`ssh_exec` 基础调用、`portal_multi_exec(mode="parallel", group_tag=...)` 在真实主机上的 gate（blocked 命令 + 不在 allowlist 的主机均拦截）、`portal_bash` 单命令的 gate、`portal_bash` + `portal_patch` 在远端 `/tmp/` 的 round-trip（含 stale-hash 拒绝路径）、audit.jsonl 是否吃到新加的 operation tag。

```bash
SSH_MCP_AUDIT_FAIL_OPEN=1 \
  TEST_HOST=10.144.18.10 TEST_PORT=2222 TEST_USER=timidly \
  TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ 它会在远端 `/tmp/portal-mcp-server-smoke-<pid>.txt` 写一次再删除——只动 `/tmp`。

---

## ⚠️ "我改了代码，但 agent 调 MCP 时为什么还是旧行为？"

`uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git portal-mcp-server` 在 MCP client 启动那一刻去 GitHub 拉本仓库 main 的最新 commit。所以：

| 你在哪改 | agent 的 MCP server 看得见吗 |
|---|---|
| 本地工作树 (`/home/.../portal-mcp-server/`) | ❌ 看不见。uvx 走的是远端 git，不是本地路径 |
| 已 commit 但没 push | ❌ 看不见 |
| commit + push 到 `TMYTiMidlY/portal-mcp-server` main | ✅ 但需要重启 MCP client（uvx 启动时 fetch；同一进程内不会重拉） |

排错时验证当前启动加载的到底是哪个版本：

```bash
# 必须 cd 到一个非项目目录再跑，否则 uvx 会优先认本地工作树
cd /tmp && uvx --from git+https://github.com/TMYTiMidlY/portal-mcp-server.git \
  --refresh python -c "
import portal_mcp_server.audit as a
print('audit env var:', getattr(a, '_FAIL_OPEN_ENV',
      'NOT SET — running an OLD/published version'))
"
```

- `audit env var: SSH_MCP_AUDIT_FAIL_OPEN` → 已经是含本次安全收紧的新版
- `NOT SET — running an OLD/published version` → uvx 拉到的还是旧 commit（push 没生效，或 uvx 缓存没刷新——加 `--refresh` 即可）

本地调试想让 agent 不 push 也能用上改动，把 `mcp-config.example.json` 里的 `args` 临时改成：
```json
"args": ["--from", "/home/agony/TiMidlY-projects/portal-mcp-server", "portal-mcp-server"]
```
（路径必须绝对）。这样 uvx 从本地工作树 install，每次重启 MCP client 都会拿到最新代码。**别把这条本地路径 commit 进 example**。

---

## License & Attribution

Apache License 2.0（见 [`LICENSE`](LICENSE)）。

衍生关系与 third-party 算法引用见 [`NOTICE`](NOTICE)：
- 上游 [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0）—— git ancestry，底层模块（asyncssh 引擎、连接池、tunnel 管理、orchestrator、安全策略）沿用；上层 18 个 `portal_*` 工具是新设计的
- [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT）—— `remote_text_editor.py` 的 SHA-256 hash-protected edit 算法参考来源（clean-room 重写，无源码复制）

> ⚠️ This tool gives programmatic SSH access to remote systems. **Use only on systems you own or have explicit written authorization to access.** Unauthorized access is illegal in most jurisdictions.
