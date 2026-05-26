# 贡献指南

> 🌐 [English version](./CONTRIBUTING.en.md)

欢迎以 issue 与 PR 形式参与 `portal-mcp-server`。本文整理常见流程；如对方向有疑问，先开 issue 讨论再写代码。

## 开发环境

```bash
git clone git@github.com:TMYTiMidlY/portal-mcp-server.git
cd portal-mcp-server
uv sync --all-extras           # 准备 .venv + 所有 dev 依赖
source .venv/bin/activate
pytest                         # 应全绿（live SSH 测试默认 skip）
```

要让 MCP client 直接跑这个本地 checkout，可安装成固定可执行文件：

```bash
uv tool install --force .      # --force 覆盖旧 tool，确保用当前 checkout
```

不想用 uv 也可以走标准 pip：

```bash
pip install -e ".[dev]"       # -e/--editable 指向当前源码
```

## 代码规范

- **Python 3.10+**，新代码尽量补全类型注解；公共函数 / MCP 工具的注解必须完整
- I/O 全部 `async/await`——**不允许**在 server 进程里出现阻塞调用（包括 `time.sleep`、`subprocess.run`、同步 `socket.recv` 等）
- 不出现硬编码 hostname / username / IP / 路径，一律从 `config/hosts.yaml` 或环境变量读
- 命令拼接走 `shlex.quote` / `quote_shell`，**永远不要**直接 f-string 拼接用户输入到 shell 命令里——参考 `safety.py` 已有的 validators
- 安全策略相关改动（`security.py`、`cli.py:_gate*`）必须配套写测试，覆盖 happy path 与 reject path

## 新工具开发流程

新加一个 `@mcp.tool()` 时按下面步骤来：

1. **想清楚是否真的需要新工具**——优先扩展现有 `portal_*` 的 `mode` / `action` 字段，避免工具数量膨胀（README 第一节解释了为什么"18 vs 57"是核心设计取舍）
2. **写完整 docstring**——FastMCP 直接把 docstring 当 MCP description 暴露给 agent，写得好坏决定 agent 用得对不对
3. **接入安全闸门**——任何状态变更都必须 `_gate(host, command)`；多机操作走 `_gate_many` / `_gate_playbook`
4. **写 audit**——状态变更的 happy path 末尾写 `audit_log(host, op_str, result, operation="...")`；只读工具显式不写
5. **更新 [`docs/tools.md`](./docs/tools.md)**——是用户能看到的工具索引，新增 / 修改 mode 时一定要同步
6. **测试**——至少 1 个 happy path + 1 个 policy reject path；用现有的 `recorder` 模式（参考 `tests/test_pool_leak_regression.py`）

## 测试要求

- 提交前 `pytest tests/ -v` 必须**全绿**（live SSH 测试默认 skip，不需要真实 host）
- 修复 bug 时**先写测试**重现问题，再修代码——这样能防止 regression
- 安全 / 资源生命周期相关的修复，配套测试要进入 `tests/test_pool_leak_regression.py` 或 `tests/test_gate_coverage_fixes.py` 系列
- 跑端到端验证用 `tests/live_smoke.py`（需要真实 SSH host，详见 README "测试" 节）

## 安全 & 隐私

- **不 commit secret**——`hosts.yaml`、含真实主机名 / 用户名 / 私钥路径的任何文件都已在 `.gitignore`，永远别绕过
- 提交 PR 前用 `git diff` 自查一遍，确认没把本地路径、IP、token 写进 docstring 或测试 fixture
- `config/hosts.example.yaml` 是**唯一** schema 模板；新加配置字段时同步更新它

## Commit message

走 [Conventional Commits](https://www.conventionalcommits.org/) 规范，类型前缀必须正确：

| 前缀 | 用途 |
|---|---|
| `feat:` | 新功能（新工具、新 mode、新 CLI 选项 ...） |
| `fix:` | bug 修复 |
| `security:` | 安全漏洞修复或加固 |
| `docs:` | 仅文档改动 |
| `test:` | 仅测试改动 |
| `refactor:` | 不改变行为的重构 |
| `chore:` | 构建脚本、CI、依赖、example 等周边 |

在 `feat:` / `fix:` / `security:` 后面适合时加 scope：`fix(remote-edit): ...`、`security(audit): ...`。

如果一个 commit 同时做多件事，**拆开**——每个 commit 一件事更易 review、更易 revert。

## PR 流程

1. Fork → 在 feature branch 上开发
2. PR title 用 Conventional Commits 风格作为概括
3. PR description 至少包含：
   - **What**：这个 PR 做了什么
   - **Why**：为什么要做（关联 issue / 用户场景 / 上游 advisory）
   - **How**：关键设计决策（如有）
   - **Test plan**：跑了什么测试，结果是什么
4. CI 必须通过；ruff lint 与 pytest 都不能 fail
5. 涉及行为变更或安全相关的 PR 期待 maintainer review；纯文档 / 测试改动可以更快合入

## 漏洞披露

**不要**在公开 issue 里报告安全漏洞，请按 [`SECURITY.md`](./SECURITY.md) 走 GitHub Security Advisories 私下提交。

## 协议

提交 PR 即表示你同意贡献内容以 Apache License 2.0 释出（见 [`LICENSE`](./LICENSE)）。
