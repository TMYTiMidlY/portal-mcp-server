# portal-mcp-server — 术语表（Context）

> 🌐 简体中文 ｜ [English](./CONTEXT.en.md)

这个 MCP server 让 AI agent 像操作本地一样、通过 SSH 驱动远端主机。本文是项目的
术语表：这里的词是项目的**权威用词（canonical vocabulary）**，请保持本文件**只做
术语表**（不写实现细节）。

## 工具家族（Tool families）

MCP 工具分三个家族。家族决定工具名的前缀（见 **工具命名**）。

**远端工具（Remote tool，远端数据面）**：
通过 SSH 作用于**远端主机**的工具——跑命令、编辑文件、搜索、传输、隧道、后台化。
命名为 `remote_*`。
_避免_：叫它"portal 工具"（每个工具都是 portal 工具，那说的是 server，不是家族）。

**本机执行（Local execution）**：
在 MCP server **自己那台机器**上跑命令，不走 SSH。只有一个工具 `local_exec`。刻意
默认关闭、且偏离目标（本项目的目标是驱动**远端**主机）。
_避免_：叫它"remote local exec"（自相矛盾——它不走 SSH）。

**控制面工具（Control-plane tool，内省 / 管理）**：
管理或检视 **portal 自身**——它的主机注册表、策略、自身运行态——而不是在某台主机上
执行任何东西。包括 `hosts`、`policy_check`、`inspect`。用朴素的描述性名词 / 动词命名，
不带 `remote_` 前缀（它们不作用于远端主机）。
_避免_：叫它"审计工具"（太窄——`inspect` 还展示连接池 / 会话 / server 状态）。

## 工具命名（Tool naming）

工具名**不带 `portal_` 前缀**。所有主流 MCP client 本就按客户端侧的配置 key 给工具加
命名空间（Copilot → `portal-<tool>`，Claude/Codex → `mcp__portal__<tool>`，…），所以在
工具名本身再加 `portal_` 前缀是冗余的**口吃（stutter）**（`portal-portal_exec`）。背后
的五客户端调研见 [`docs/adr/0001-tool-naming-scheme.md`](docs/adr/0001-tool-naming-scheme.md)。
远端工具用 `remote_` 前缀是因为它是**语义性**的（标明"作用于远端主机"），不是 server
名字的回声。

## 主机词汇（Host vocabulary）

**主机名（Host name，别名 / 标识符）**：
标识一台主机的字符串——`hosts.yaml` 的 key 或 `~/.ssh/config` 的 `Host` 行（如
`web01`）。永远存在；永不带首尾空白（进入时 trim）。它**不是**网络地址。
_避免_：把"host"含糊地同时指标识符和地址。

**HostName（拨号地址）**：
实际连接到的网络地址（IP / DNS），来自 hosts.yaml 的 `host:` 或 ssh_config 的
`HostName`。是连接参数，不是标识符。

**合并（Merge，hosts.yaml ↔ ssh_config）**：
经 `use_ssh_config: true` 显式开启：以 ssh_config 别名为基底，把 hosts.yaml 里**显式
设置**的字段叠在上面覆盖。区别于默认行为——默认下 hosts.yaml 的主机**完全覆盖**
ssh_config。

## 执行模式（Execution mode）

**前台执行（Foreground execution）**：
同步跑一条命令——工具调用会阻塞到命令退出（`remote_exec`、`remote_shell`、
`local_exec`）。工作跑在 MCP server 进程内，所以 agent（进而 server）停止时它也结束。
_避免_：把它叫"一个 job"（job 是它的后台对应物）。

**后台执行（Background execution）**：
在远端主机上以 `nohup` 分离地跑命令（`remote_job`）：SSH 连接断开或 agent 停止后它仍
继续跑，并被轮询取输出。是前台执行的**持久**对应物。
_避免_：叫它"async exec"（每个工具在传输层都是 async；这个词讲的是**命令**活得比
**调用**久）。

## 凭据路径（Credential path）

**凭据路径（Credential path，统一）**：
每条连接都走的那条唯一的、进程内 asyncssh 认证路径。每种凭据——SSH 密钥、登录密码、
密钥 passphrase、sudo 密码、命名 secret——都在这一条路径上解析，明文只交给它真正的
消费方（asyncssh 握手、`sudo -S` stdin、注入的环境变量）；它永不进入 agent 对话、命令
行、或磁盘。
_避免_：把外壳出去的 `ssh` / `scp` / `sshpass` 子进程当等价物——它不共享这条路径（见
[`docs/adr/0003-credential-unification.md`](docs/adr/0003-credential-unification.md)）。
