# ssh-remote-mcp

> **AI-native SSH orchestration for AI coding agents (Claude Code, Copilot CLI, Cursor, …).**
> 71 MCP tools over AsyncSSH + FastMCP. 持久 bash session、SHA-256 防冲突的远端文件编辑、远端 ripgrep/find、`~/.ssh/config` 别名自动解析、policy-gated multi-host orchestration、fail-closed audit。

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-brightgreen)](https://modelcontextprotocol.io/)

> **Origin & attribution**: 本项目源自 [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0），延续其 65 个 `ssh_*` 工具与整体架构，新增 6 个面向 agent 的 `remote_*` 工具、`~/.ssh/config` 别名解析、输入校验集中化、以及对上游若干安全描述与实现不一致的修复。`remote_read` / `remote_patch` 的 hash-protected 编辑算法参考了 [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT，clean-room 重写）。详见 [`NOTICE`](./NOTICE) 与 [`SECURITY.md`](./SECURITY.md)。

---

## What this gives an AI agent

| 工具组 | 给 agent 的能力 |
|---|---|
| `remote_read` / `remote_patch` | 读远端文件并拿 SHA-256；patch 用 file_hash + per-range hash 防并发覆盖，写入走 tmp+`posix_rename` 原子替换，写后再 hash 校验 |
| `remote_grep` / `remote_glob` | 远端 `rg --json` / `find` 结构化输出；首次连接探测一次缓存 |
| `remote_bash` / `remote_bash_close` / `remote_bash_status` | 每个 host 一个粘性 `bash -i`，cwd / env 跨调用保留；PTY echo + bracketed-paste 关掉以让 sentinel 完整工作 |
| `~/.ssh/config` 别名自动解析 | `get_connection("1810")` 找不到时自动从 `~/.ssh/config` 注册；asyncssh 原生处理 HostName / User / Port / IdentityFile / ProxyJump |
| 65 个 `ssh_*` 工具（exec / file / session / process / system / orchestration / tunnel / security / observability / tmux） | 沿用上游，详见模块 docstring 与 MCP tool 列表；其中多主机编排与 session 已补齐 policy gate（见下方 Security） |

配套的 [`remote-cli` skill](https://github.com/TMYTiMidlY/skills) 教 agent 怎么按 read → patch 流程改远端代码、何时用 `/tmp` 沙箱、何时该问。

---

## Install

按身份选路径：

### 给 agent / 终端用户（用 MCP server，不动源码）

不需要 clone，让 MCP client 通过 `uvx` 直接从 GitHub 拉运行——见下方 [Register with your agent](#register-with-your-agent)。`uvx` 第一次启动时会缓存依赖，后续重启秒级。

要在 shell 里手动跑一下试探：

```bash
uvx --from git+https://github.com/TMYTiMidlY/ssh-remote-mcp.git ssh-remote-mcp --help
```

### 给开发者（要改代码 / 跑测试）

推荐 `uv sync`，它会按 `pyproject.toml` + `uv.lock` 一次到位准备好 `.venv` 和所有 dev 依赖：

```bash
git clone git@github.com:TMYTiMidlY/ssh-remote-mcp.git
cd ssh-remote-mcp
uv sync --all-extras           # → .venv with prod + [dev] deps
source .venv/bin/activate
pytest                         # 131 passed, 22 skipped
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
    "ssh-remote": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/TMYTiMidlY/ssh-remote-mcp.git",
        "ssh-remote-mcp"
      ]
    }
  }
}
```

验证：

```bash
cd <project>
copilot mcp list                # → Workspace servers: ssh-remote (local)
copilot mcp get ssh-remote      # → Source: Workspace (<project>/.mcp.json)
```

> ⚠️ 不要用 `copilot mcp add ssh-remote -- ...`——它默认写到 user-level `~/.copilot/mcp-config.json`，会污染所有项目。直接编辑 `.mcp.json` 才能保持项目级。

### VS Code（`.vscode/mcp.json`）

VS Code 用不同的 schema（顶层 key 是 `servers` 不是 `mcpServers`）：

```json
{
  "servers": {
    "ssh-remote": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/TMYTiMidlY/ssh-remote-mcp.git",
        "ssh-remote-mcp"
      ]
    }
  }
}
```

> 两种格式不兼容。如果同时用 Copilot CLI 和 VS Code，需要各维护一份。

### 配套 skill

在 [TMYTiMidlY/skills](https://github.com/TMYTiMidlY/skills) 安装 `remote-cli`（按 `manage-skills` 流程软链到 `<target>/.agents/skills/`）。Agent 收到「在 1810 上 ...」之类指令时会自动遵循 hash-check 流程和 `/tmp` 默认沙箱规则。

---

## Configuration

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `SSH_HOSTS_YAML` | 主机注册 YAML | `./config/hosts.yaml` 若存在，否则 `$XDG_CONFIG_HOME/ssh-remote-mcp/hosts.yaml` |
| `SSH_POLICIES_YAML` | 安全策略 YAML | `./config/policies.yaml` 若存在，否则 `$XDG_CONFIG_HOME/ssh-remote-mcp/policies.yaml` |
| `SSH_MCP_LOG_DIR` | audit + server log 目录 | `./logs/` 若存在，否则 `$XDG_STATE_HOME/ssh-remote-mcp/logs/` |
| `SSH_MCP_AUDIT_FAIL_OPEN` | 设 `1` → audit 写盘失败时仅 warning 并继续；默认（未设）→ **fail-closed**，audit 写不进则操作 raise 中止 | _(unset)_ |
| `MCP_AUTH_TOKEN` | HTTP transport 的 Bearer token | _(none)_ |

`hosts.example.yaml` 给了完整 schema 模板。**`hosts.yaml` 含真实凭据，已在 `.gitignore`，永远别 commit**。

---

## Security

### 默认安全约束

ssh-remote-mcp 不强制路径白名单——这事交给配套的 `remote-cli` skill 在 prompt 层强制：

> **默认只可写远端 `/tmp/` 路径；改用户家目录或项目代码目录前必须先问。**

如果想做机器级强制，在 `config/policies.yaml` 的 `command_blocklist` 加规则（如 `"rm -rf /home/*"`）。

### Policy gate

`SecurityPolicy` 检查：host allowlist（fnmatch）、command blocklist/allowlist（fnmatch）、per-host rate limit（sliding window）。所有命令执行类工具走 `_gate(host, command)`；多主机编排（`ssh_group_exec` / `ssh_rolling` / `ssh_broadcast_batch` / `ssh_playbook_on_group`）走 `_gate_many(hosts, command)`，playbook 还会遍历 `steps` 逐条过 blocklist。`ssh_session_exec` 也对每条命令 gate（创建 session 不等于授权一切命令）。

### 认证

**仅支持 key-based auth**。`HostConfig` 不带 `password` 字段，`ssh_register_host` 没有 `password` 参数。`hosts.yaml` 里若残留 `password:` 键会被启动时 ERROR 日志提示并忽略。

### Audit

所有改状态的工具写 `logs/audit.jsonl`（exec / file write / patch / session / register / tunnel / playbook / multi-host orchestration）。只读类（`remote_read` / `remote_grep` / `remote_glob`）显式不审计以减噪音。

**默认 fail-closed**——audit 写盘失败时操作 raise 中止；设 `SSH_MCP_AUDIT_FAIL_OPEN=1` 切到 fail-open（仅 warning 后继续，适合 dev/test）。

更详细的算法依据与设计 diff 见 [SECURITY.md](SECURITY.md)。漏洞披露请走 GitHub Security Advisories。

---

## Testing

### 单元 + 安全（不需要真实 SSH）

```bash
pytest tests/ -v
# 131 passed, 22 skipped (live SSH tests gated by SSH_TEST_LIVE)
```

覆盖：command injection regression、safety validators、hash-protected editor、concurrency、resource lifecycle、multi-host policy enforcement、no-password-auth invariants、audit fail mode。

### 端到端 live smoke

`tests/live_smoke.py` 直接 import 本地工作树驱动一系列真实 SSH 行为：hosts.yaml `password:` 残留处理、`ssh_exec` 基础调用、`ssh_group_exec` / `ssh_session_exec` 在真实主机上的 gate（blocked 命令 + 不在 allowlist 的主机均拦截）、`remote_bash` + `remote_patch` 在远端 `/tmp/` 的 round-trip（含 stale-hash 拒绝路径）、audit.jsonl 是否吃到新加的 operation tag。

```bash
SSH_MCP_AUDIT_FAIL_OPEN=1 \
  TEST_HOST=10.144.18.10 TEST_PORT=2222 TEST_USER=timidly \
  TEST_KEY_PATH=$HOME/.ssh/id_ed25519 \
  uv run --with-editable . --with pytest --with pytest-asyncio \
    python tests/live_smoke.py
```

⚠️ 它会在远端 `/tmp/ssh-remote-mcp-smoke-<pid>.txt` 写一次再删除——只动 `/tmp`。

---

## ⚠️ "我改了代码，但 agent 调 MCP 时为什么还是旧行为？"

`uvx --from git+https://github.com/TMYTiMidlY/ssh-remote-mcp.git ssh-remote-mcp` 在 MCP client 启动那一刻去 GitHub 拉本仓库 main 的最新 commit。所以：

| 你在哪改 | agent 的 MCP server 看得见吗 |
|---|---|
| 本地工作树 (`/home/.../ssh-remote-mcp/`) | ❌ 看不见。uvx 走的是远端 git，不是本地路径 |
| 已 commit 但没 push | ❌ 看不见 |
| commit + push 到 `TMYTiMidlY/ssh-remote-mcp` main | ✅ 但需要重启 MCP client（uvx 启动时 fetch；同一进程内不会重拉） |

排错时验证当前启动加载的到底是哪个版本：

```bash
# 必须 cd 到一个非项目目录再跑，否则 uvx 会优先认本地工作树
cd /tmp && uvx --from git+https://github.com/TMYTiMidlY/ssh-remote-mcp.git \
  --refresh python -c "
import ssh_remote_mcp.audit as a
print('audit env var:', getattr(a, '_FAIL_OPEN_ENV',
      'NOT SET — running an OLD/published version'))
"
```

- `audit env var: SSH_MCP_AUDIT_FAIL_OPEN` → 已经是含本次安全收紧的新版
- `NOT SET — running an OLD/published version` → uvx 拉到的还是旧 commit（push 没生效，或 uvx 缓存没刷新——加 `--refresh` 即可）

本地调试想让 agent 不 push 也能用上改动，把 `mcp-config.example.json` 里的 `args` 临时改成：
```json
"args": ["--from", "/home/agony/TiMidlY-projects/ssh-remote-mcp", "ssh-remote-mcp"]
```
（路径必须绝对）。这样 uvx 从本地工作树 install，每次重启 MCP client 都会拿到最新代码。**别把这条本地路径 commit 进 example**。

---

## License & Attribution

Apache License 2.0（见 [`LICENSE`](LICENSE)）。

衍生关系与 third-party 算法引用见 [`NOTICE`](NOTICE)：
- 上游 [`jaguar999paw-droid/ssh-shell-mcp`](https://github.com/jaguar999paw-droid/ssh-shell-mcp)（Apache 2.0）—— git ancestry，65 个 `ssh_*` 工具与整体架构沿用
- [`tumf/mcp-text-editor`](https://github.com/tumf/mcp-text-editor)（MIT）—— `remote_text_editor.py` 的 SHA-256 hash-protected edit 算法参考来源（clean-room 重写，无源码复制）

> ⚠️ This tool gives programmatic SSH access to remote systems. **Use only on systems you own or have explicit written authorization to access.** Unauthorized access is illegal in most jurisdictions.
