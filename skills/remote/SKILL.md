---
name: remote
description: 在已知 SSH 主机（~/.ssh/config 中的 Host 别名）上做远端操作时使用。提供两种模式——MCP 模式（默认，通过 ssh-remote-mcp 让 agent 像在本地一样用 hash 防冲突的 remote_read/remote_patch、远端 ripgrep 的 remote_grep/remote_glob、持久 cwd+env 的 remote_bash）和 Plain 模式（基于直接 ssh/scp 拼字符串，主要服务于交互式 sudo 场景以及 ssh-remote-mcp 未注册的环境）。涵盖触发判断、安全规则、模式切换、典型工作流与边界。
---

# Remote

## 何时触发

用户说"在 `<host>` 上 ..."、"去远端 ..."、"远端 grep / 改 / 跑 ..."、"在 `<host>` 上装/改 / 起服务"等任何操作发生在远端机器的请求。

**先确认 host**：去 `~/.ssh/config` 找匹配的 Host 别名。如果找不到，**不要继续**，直接问用户：是不是名字说错了、是不是想表达别的意思、还是需要先添加 SSH 配置。

---

## 选模式

| 场景 | 模式 |
|---|---|
| 普通文件读 / 改 / 搜索 / 跑命令 | **MCP** |
| 需要 cwd 在多次命令间保留 | **MCP**（remote_bash 自动持久） |
| 需要并发安全（多 agent / 用户同时改同一文件） | **MCP**（hash 校验） |
| 需要交互式 sudo（弹密码） | **Plain** |
| 装包 / 改 /etc / systemd / 改其他用户的文件 | **Plain**（agent 准备脚本，用户自己跑 `ssh -t`） |
| ssh-remote-mcp 未注册到当前项目 | **Plain** |
| 一次性查个东西、不需要 cwd 持久 | 任意（plain 更轻） |

判断"MCP 是否可用"：在 cwd 下跑 `copilot mcp list`，看 Workspace servers 里是否有 `ssh-remote`。没有的话先按下方"安装"指引引导用户。

---

## MCP 模式（默认）

### 关键不变量（理解后再用）

- **写之前必须先 read**：`remote_read` 返回 `file_hash` 和每段 `range_hash`；`remote_patch` 必须带回。中途别人改了文件，patch 会被服务端拒绝并返回当前 hash + 内容。
- **patch 是事务的**：一次 patches_json 内多段全成或全败；overlapping patches 直接拒。
- **bash session 粘性**：第一次 `remote_bash` 自动建一个 `bash -i`，cwd / export 跨调用保留；想清空 `remote_bash_close`。
- **文件 IO 与 bash 共用同一 SSH 连接**（连接池复用），不会因为多工具就开多条 TCP。

### 工具速查

| 工具 | 用途 | 必需参数 | 返回关键字段 |
|---|---|---|---|
| `remote_read` | 读文件或行范围 | `host, path, [start, end]` | `content, file_hash, range_hash, total_lines` |
| `remote_patch` | 改文件（hash 防冲突） | `host, path, file_hash, patches_json` | `result=ok\|error, file_hash`（成功）/ `current_file_hash`（hash mismatch） |
| `remote_grep` | 远端 rg / grep | `host, path, pattern, [glob, file_type, ignore_case, max_count]` | `engine, matches[{file,line,text}]` |
| `remote_glob` | 远端文件列表 | `host, pattern, [path]` | `engine, files[]` |
| `remote_bash` | 持久 shell | `host, command, [timeout]` | `host, session_id, output` |
| `remote_bash_close` | 关闭主机 session | `host` | 状态字符串 |
| `remote_bash_status` | 查看缓存的 session | — | host→session_id 映射 |

### 安全规则（MCP 模式强制）

1. **默认只可写远端 `/tmp/` 路径**。改用户家目录、项目代码目录、`/etc`、`/usr` 等任何非 `/tmp` 位置之前，**必须先问用户**并明确得到许可。
2. 真实项目目录上做实验，先建议复制到 `/tmp/<task-name>/` 沙箱，确认无误后让用户决定是否合并回真实路径。
3. patch 失败（hash mismatch）后**不要立刻覆盖式重试**——先 `remote_read` 看新 hash 和新内容，识别是不是别的 agent / 用户在并发改；是的话向用户汇报并等指示。
4. `remote_bash` cwd 是粘性的——执行任何命令前先 `pwd` 确认，不要假定还在上次目录（被 timeout 关 / 用户在另一会话里 close 都会重置）。
5. 不要直接调用 `ssh-shell-mcp` 上游的 `ssh_write` / `ssh_run` 等等价工具——它们没 hash 校验，会绕开本 skill 并发安全模型。优先用 `remote_*` 系列。

### 典型工作流

#### A. 在远端项目里做小改动

```
1. remote_grep host=<H> path=<dir> pattern="<symbol>" glob="*.py"
   → 找到目标 file:line
2. remote_read  host=<H> path=<file> start=<n-5> end=<n+10>
   → 拿到 file_hash + range_hash
3. remote_patch host=<H> path=<file> file_hash=<...>
                patches_json='[{"start":<n>,"end":<n>,"contents":"<new>\n","range_hash":"<from 2>"}]'
4. 如 error 且 reason 含 "hash mismatch"：回到 step 2 重读，识别冲突再决定
```

#### B. 在远端跑命令并保留上下文

```
1. remote_bash host=<H> command="cd /tmp/sandbox && python -m venv .venv && source .venv/bin/activate"
2. remote_bash host=<H> command="pip install ..."   # 仍在 /tmp/sandbox 且 venv 已激活
3. remote_bash host=<H> command="python script.py"
4. 完成后 remote_bash_close host=<H>
```

#### C. 多 patch 一次性改完同一文件

把多段 patch 放在一个 `patches_json` 里——服务端从下到上应用避免行号漂移。重叠会被拒绝。改动跨度太大宁可拆成多个 read+patch 循环，也不要试图用一个超长 patch。

---

## Plain 模式（sudo 与兜底）

### 前置检查

每次开始远端操作前：

1. 确认 `SSH_AUTH_SOCK` 已设置（某些环境不会 source `~/.bashrc`）：

```bash
[ -z "$SSH_AUTH_SOCK" ] && export SSH_AUTH_SOCK=/run/user/$(id -u)/ssh-agent.socket
```

2. 确认本地 SSH config 包含连接复用（ControlMaster）。Windows 本机 OpenSSH 通常不支持 ControlMaster，跳过这一步；只在 Linux / macOS / WSL 检查：

```bash
grep -q "ControlMaster" ~/.ssh/config && echo "OK" || echo "MISSING"
```

缺失时提醒用户在 `~/.ssh/config` 的 `Host *` 下添加：

```text
Host *
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 10m
```

### 执行远端命令（无 sudo）

```bash
ssh <name> "<command>"
```

### 需要 sudo 的命令（核心场景）

agent 在本地 `/tmp/` 写脚本，自己 `scp` 上传，**只让用户**执行最终的 `ssh -t`：

```bash
ssh -t <name> "sudo bash /tmp/<script>.sh"
```

`-t` 强制分配伪终端，使 sudo 能正常读取密码。**不要让用户跑 `scp`**——上传脚本、上传临时文件、拉取日志这些准备动作都该 agent 做。

脚本开头加 `exec > >(tee /tmp/<name>.log) 2>&1`，用户看终端的同时留下日志；跑完 agent 自己 `scp` 拉回读，不要让用户手贴。

### 复杂文件修改（无法用 MCP 时）

需要做非平凡编辑时**不要**通过 ssh 内联 sed/awk，而是：

1. 复制到本地临时目录：`scp <name>:/remote/path/file /tmp/file`
2. 在本地用 view/edit 工具修改
3. 复制回远程：`scp /tmp/file <name>:/remote/path/file`

简单的追加或单行替换可以直接 ssh 执行。

> 这套流程现在主要是给"MCP 还没注册" 或"目标在别的用户家目录"等场景兜底；正常情况下走 MCP 模式的 `remote_read` + `remote_patch` 比这个安全也方便。

---

## 安装 ssh-remote-mcp（项目级注册）

ssh-remote-mcp 项目根有 `mcp-config.example.json`。Copilot CLI **原生支持工作区级 `.mcp.json`**（与 Claude Code / Cursor 同格式）：

```bash
cp <ssh-remote-mcp>/mcp-config.example.json <project>/.mcp.json
# 编辑里面的绝对路径指向你 clone 的位置
```

之后在该项目目录下启动 `copilot`，会自动加载。验证：

```bash
copilot mcp list                # → Workspace servers: ssh-remote (local)
copilot mcp get ssh-remote      # → Source: Workspace (.../.mcp.json)
```

> ⚠️ 别用 `copilot mcp add` 走交互——它默认写到 user-level `~/.copilot/mcp-config.json`，会污染所有项目。**直接编辑 `.mcp.json`** 才能保持项目级。

---

## 边界

- **新装服务器 / 配 Caddy / EasyTier / BBR / error-pages**：交给 `vps-maintenance` skill
- **多机 fleet 编排**（上百台并发）：直接用上游 ssh-shell-mcp 的 `ssh_parallel` / `ssh_rolling`，不在本 skill 范围
- **真要 sudo 但又想自动化**：要么让用户在自己终端先跑一次 `sudo true` 给 sudo 缓存计时器续命（5 分钟内 MCP 可用 `sudo -n`），要么干脆走 Plain 模式
