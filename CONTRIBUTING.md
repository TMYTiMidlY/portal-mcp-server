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
- 不出现硬编码 hostname / username / IP / 路径，一律从 `hosts.yaml` 或环境变量读（解析顺序见 README §文件路径）
- 命令拼接走 `shlex.quote` / `quote_shell`，**永远不要**直接 f-string 拼接用户输入到 shell 命令里——参考 `safety.py` 已有的 validators
- 安全策略相关改动（`security.py`、`cli.py:_gate*`）必须配套写测试，覆盖 happy path 与 reject path

## 新工具开发流程

新加一个 `@mcp.tool()` 时按下面步骤来：

1. **想清楚是否真的需要新工具**——优先扩展现有 `portal_*` 的 `mode` / `action` 字段，避免工具数量膨胀（README 第一节解释了为什么"14 vs 57"是核心设计取舍）
2. **写完整 docstring**——FastMCP 直接把 docstring 当 MCP description 暴露给 agent，写得好坏决定 agent 用得对不对
3. **接入安全闸门**——任何状态变更都必须 `_gate(host, command)`；多机操作走 `_gate_many` / `_gate_playbook`
4. **写 audit**——状态变更的 happy path 末尾写 `audit_log(host, op_str, result, operation="...")`；只读工具显式不写
5. **更新 README 工具节**——[`README.md`](./README.md) / [`README.en.md`](./README.en.md) 的「工具列表」是用户能看到的工具索引（含折叠的完整签名 + 源码位置表），新增 / 修改 mode 时一定要同步
6. **测试**——至少 1 个 happy path + 1 个 policy reject path；用现有的 `recorder` 模式（参考 `tests/test_pool_leak_regression.py`）

## 测试要求

- 提交前 `pytest tests/ -v` 必须**全绿**（live SSH 测试默认 skip，不需要真实 host）
- 修复 bug 时**先写测试**重现问题，再修代码——这样能防止 regression
- 安全 / 资源生命周期相关的修复，配套测试要进入 `tests/test_pool_leak_regression.py` 或 `tests/test_gate_coverage_fixes.py` 系列
- 跑端到端验证用 `tests/live_smoke.py`（需要真实 SSH host，详见 README "测试" 节）

## 安全 & 隐私

- **不 commit secret**——真实凭据**只**放 `$XDG_CONFIG_HOME/portal-mcp-server/`（默认 `~/.config/portal-mcp-server/`）
- 提交 PR 前用 `git diff` 自查一遍，确认没把本地路径、IP、token 写进 docstring 或测试 fixture
- `examples/hosts.yaml` 是**唯一** schema 模板；新加配置字段时同步更新它

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

> 📌 **本项目用 [Commitizen](https://commitizen-tools.github.io/commitizen/) 托管版本号与 CHANGELOG**（`pyproject.toml` 的 `[tool.commitizen]`）。`cz bump` 直接读这些 conventional commit 前缀来决定语义化版本的递增方向：`feat` → minor、`fix` / `security` → patch、带 `BREAKING CHANGE` 脚注或 `!` → major。所以前缀写错会让自动定版出错——务必准确。`docs` / `test` / `refactor` / `chore` 不触发版本递增。

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

## CI & Release 自动化

仓库走两条 GitHub Actions 流水线，正常发布动作都不需要在本地手跑构建。

### CI — `.github/workflows/ci.yml`

触发：push / PR 到 `main`。
矩阵：Python **3.10 / 3.11 / 3.12 / 3.13**（ubuntu-latest）。

每个矩阵 job 做四件事：

1. `pip install -e ".[dev]" && pip install ruff`
2. `ruff check portal_mcp_server/` —— 只 lint 产品代码，不 lint 测试
3. import smoke：`python -c "import portal_mcp_server; assert portal_mcp_server.main"` + `portal-mcp-server --help`
4. `pytest tests/ -v --tb=short`（live SSH 测试 fixture 默认 skip，CI 不需要真实 host）

PR 必须四个 Python 版本全绿才能 merge。

### Release — `.github/workflows/release.yml`

触发：
- push tag 匹配 `v*.*.*`（**生产路径**）
- `workflow_dispatch`（手动兜底）

三个 job 顺序执行：

1. **`release-build`**：`python -m build` 出 wheel + sdist，传成 artifact `release-dists`。
2. **`create-release`**：下载 artifact → 从 `CHANGELOG.md` awk 抽出当前版本段（详见下面"CHANGELOG 格式约束"）→ 用 `softprops/action-gh-release@v1` 建 GitHub Release，把 `.whl` / `.tar.gz` 当 asset 一并上传。
3. **`pypi-publish`**：用 PyPA 官方 action + **OIDC trusted publishing**（无 token、无 secret）发到 PyPI；`skip-existing: true` 防止同版本重发硬失败。

GH environment `pypi` 在仓库 Settings → Environments 里绑到 https://pypi.org/p/portal-mcp-server/ 的 trusted publisher。**不要往里塞 `PYPI_API_TOKEN`**——trusted publishing 比静态 token 安全得多。

### CHANGELOG 格式约束

`release.yml` 用 awk 抓「以 `## ` 开头、且首行字串包含目标版本号」的那段，到下一个 `## ` 之前为止：

```markdown
## v1.1.0 (2026-05-26)

### BREAKING CHANGES
- ...

### Fix
- ...

## v1.0.1 (2026-05-15)
...
```

> ⚠️ 每个版本头行必须包含纯版本号（如 `1.1.0`），否则 awk 抓不到，GH Release body 会是空字符串。`cz bump` 生成的 `## v<x.y.z> (<日期>)` 头行天然满足这条约束。

### 发布新版本步骤

> ## ⚠️ 发版铁律（TL;DR）
> 1. **不要手改** `pyproject.toml` 的 `version`
> 2. **不要手写** `CHANGELOG.md`
> 3. **不用手动** `uv lock`
>
> 发版**只一条命令**：`uv run cz bump` —— `version_provider = "uv"` 会让 `cz bump` 把 `pyproject.toml`、`uv.lock` 自身版本号、`CHANGELOG.md` 全部更新并打进同一个 bump commit + 同一个 annotated tag。

**版本号、CHANGELOG、`uv.lock` 全部由 [Commitizen](https://commitizen-tools.github.io/commitizen/) 托管**——`pyproject.toml` 里配了 `version_provider = "uv"`，`cz bump` 会把 `uv.lock` 里的自身版本号一起更新并纳入同一个 bump commit。

1. 确保要发版的 commit 都已 merge 进 `main`，本地在干净的 `main` HEAD 上
2. （可选 shift-left）本地预跑一遍 hooks：`uv run ruff check portal_mcp_server/ && uv run pytest tests/ -q`——不跑也行，下一步 `cz bump` 配的 `pre_bump_hooks` 会自动跑同样两条；先跑只是为了在 cz 改 `pyproject.toml` / `uv.lock` / `CHANGELOG.md` 之前就发现 lint / 测试问题
3. 预览将要发的版本和 CHANGELOG：`uv run cz bump --dry-run`
4. 正式发版：`uv run cz bump` —— 先跑 `pre_bump_hooks`（ruff + pytest，任一非零退出整个 bump 中止、不留半成品），再按 commit 历史递增 `pyproject.toml` 的 `version`、更新 `uv.lock`、在 `CHANGELOG.md` 顶部生成 `## v<x.y.z> (<日期>)` 段、建 bump commit、打 annotated `v<x.y.z>` tag（`annotated_tag = true`）
5. 推送触发 release：`git push origin main --follow-tags`
6. 在 [Actions 页](https://github.com/TMYTiMidlY/portal-mcp-server/actions) 等三个 job 全绿
7. 验收：https://github.com/TMYTiMidlY/portal-mcp-server/releases/tag/v\<x.y.z\> + https://pypi.org/project/portal-mcp-server/\<x.y.z\>/

> `cz bump` 不会自己 push（给你留了反悔的机会）。`--follow-tags` 会把 annotated tag 跟 `main` 一起推上去；release.yml 由 `push: tags: ['v*.*.*']` 触发。

### Release 出错怎么办

| 哪个 job 红 | 多半原因 | 处理 |
|---|---|---|
| `release-build` | `pyproject.toml` 写错 / build backend 报错 | 看构建日志，本地 `python -m build` 复现 |
| `create-release` | CHANGELOG 段抽不到（版本号串不在头行 / 上一段头行被吞了） | 一般是手改 CHANGELOG 破坏了格式——优先回到 `cz bump` 生成的内容；删 tag → 修好 → 重打 tag |
| `pypi-publish` | trusted publisher 没配 / PyPI 上已有同版本 | 配 trusted publishing；同版本已发就接受，无须重传 |

> 历史教训：v1.1.0 之前 release.yml 曾从 `pyproject.toml` 直接读 version，导致 tag 和包版本号不一致；commit `8e33ea3 fix(ci): derive release version from tag name instead of pyproject.toml` 改成从 `GITHUB_REF_NAME` 拿，更可靠。

## 漏洞披露

**不要**在公开 issue 里报告安全漏洞，请按 [`SECURITY.md`](./SECURITY.md) 走 GitHub Security Advisories 私下提交。

## 协议

提交 PR 即表示你同意贡献内容以 Apache License 2.0 释出（见 [`LICENSE`](./LICENSE)）。
