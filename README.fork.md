# ssh-remote-mcp（fork of ssh-shell-mcp）

> 这是 `jaguar999paw-droid/ssh-shell-mcp` 的 fork，新增了 **agent-feels-local** 工具组：
> - `~/.ssh/config` 别名自动解析（无需维护单独的 `hosts.yaml`）
> - **hash 防冲突的远端文件编辑**（`remote_read` / `remote_patch`，灵感来自 [tumf/mcp-text-editor](https://github.com/tumf/mcp-text-editor)）
> - 远端 `ripgrep` / `find` 的结构化输出（`remote_grep` / `remote_glob`）
> - 单 host 单 `bash -i` 持久 session，cwd 与 env 跨调用保留（`remote_bash`）
>
> 上游的 57 个 `ssh_*` 工具仍可用；本 fork 的 `remote_*` 是面向 Claude / Copilot CLI 这类 agent 的更高语义包装。配套 skill 见 [`skills/.curated/remote-cli`](https://github.com/TMYTiMidlY/skills)。

---

## 与上游的差异概览

| 改动 | 文件 | 说明 |
|---|---|---|
| `~/.ssh/config` 别名解析 | `ssh_remote_mcp/connection_manager.py` | `get_connection(host)` 找不到时自动解析 ssh config 注册；asyncssh 原生处理 HostName/User/Port/IdentityFile/ProxyJump |
| Hash 编辑核心 | `ssh_remote_mcp/remote_text_editor.py` | 新增。SHA-256 防并发冲突，原子 tmp+rename，写后再 hash 校验 |
| 远端搜索 | `ssh_remote_mcp/remote_search.py` | 新增。优先用远端 `rg --json`，无则 `grep -rn`；首次连接探测一次缓存 |
| 持久 bash 包装 | `ssh_remote_mcp/remote_bash.py` | 新增。每 host 自动建一个粘性 session，禁掉 PTY echo + bracketed-paste 让 sentinel 完整工作 |
| 6 个 `remote_*` MCP 工具 | `ssh_remote_mcp/cli.py` | 新增 `remote_read` / `remote_patch` / `remote_grep` / `remote_glob` / `remote_bash` / `remote_bash_close` / `remote_bash_status` |
| 打成 PEP 621 包 | `pyproject.toml` | 提供 `ssh-remote-mcp` 入口脚本，可直接 `uvx --from git+https://github.com/TMYTiMidlY/ssh-remote-mcp.git ssh-remote-mcp` 启动 |

---

## 安装与项目级注册

### 1. 选择安装方式

**A. 直接用 `uvx`（推荐，零安装）**

```bash
uvx --from git+https://github.com/TMYTiMidlY/ssh-remote-mcp.git ssh-remote-mcp --help
```

**B. clone 后开发**

```bash
git clone git@github.com:TMYTiMidlY/ssh-remote-mcp.git
cd ssh-remote-mcp
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 2. 验证（在 1810 别名上跑端到端 demo）

```bash
PYTHONPATH=. python examples/phase6_acceptance.py
```

应输出 `🎉 ALL 7 STEPS PASSED — Phase 6 acceptance complete`。包含：
- read 拿 sha256
- 远端 grep 找符号
- patch 修改 sandbox 文件
- **负向测试**：模拟别人改文件 → 我们的旧 hash patch 被拒绝
- 持久 bash 验证 cwd 跨调用保留
- 验证只有 1 条 SSH 连接

### 3. 注册到 Copilot CLI（项目级，原生 `.mcp.json`）

Copilot CLI **原生支持工作区级 `.mcp.json`**（与 Claude Code / Cursor 同格式），优先级独立于用户级 `~/.copilot/mcp-config.json`。最简单的项目级配置（推荐用 uvx）：

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

### 4. 装 skill 引导 agent 怎么用

在 [TMYTiMidlY/skills](https://github.com/TMYTiMidlY/skills) 里安装 `remote-cli`（按 manage-skills skill 的安装流程软链到 `<target>/.agents/skills/`）。
agent 收到 "在 1810 上 ..." 之类指令时会自动遵循 hash check 流程和 `/tmp` 默认沙箱规则。

---

## 安全约束（默认）

ssh-remote-mcp 不强制路径白名单——这事交给配套的 `remote-cli` skill 在 prompt 层强制：

> **默认只可写远端 `/tmp/` 路径；改用户家目录或项目代码目录前必须先问。**

如果你想机器级强制，可以在 `config/policies.yaml` 的 `command_blocklist` 里加自己的规则（比如 `"rm -rf /home/*"`）。

---

## 上游 README

下方为原始 ssh-shell-mcp README 内容（保留以保留上游归属与文档）：

---

