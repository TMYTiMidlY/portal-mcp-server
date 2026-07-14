# 0003 — 唯一的进程内凭据路径；不为持久化另起分离子进程

> 🌐 简体中文 ｜ [English](./0003-credential-unification.en.md)

状态：已采纳（accepted）

portal 建立的每一条连接都只走一条进程内的 asyncssh 认证路径。所有凭据类型——SSH 密钥、
登录密码、密钥 passphrase、sudo 密码、命名 secret——都在这条路径上解析，明文只交给它
真正的消费方：asyncssh 握手、`sudo -S` 的 stdin、注入的环境变量；除此之外，明文永不进入
agent 对话、命令行 / `ps` argv 或磁盘。

正因为这条保证只存在于进程内，portal **不会**为了让某个操作活得比 agent 更久，就另起一个
分离的子进程（`nohup scp` / `rsync` / `ssh`）——那样的子进程根本用不上这条路径，也就守不住
这条保证。

## 背景

agent 常希望一次传输或命令"在 agent 停止后仍继续跑"，最直白的实现就是另起一个分离的
`nohup rsync` / `scp` 子进程。但 portal 的几条凭据保证——连接时现取的 `password_command`、
hosts.yaml ↔ ssh_config 的合并（见 [ADR-0002](0002-ssh-config-merge.md)）、以及"密码不进
argv"——**只**存在于进程内的 asyncssh 路径上。一个外壳出去的 `scp` / `rsync` 一条都享受不到：
它只能回落到 `sshpass`（把密码摆进 `ps` 可见的 argv），或干脆丢掉 `password_command` 与合并。

## 考虑过的方案

- **为持久操作另起分离子进程**——否决。它绕开统一的凭据路径，把本项目立意就是要消除的
  argv / 明文泄漏又请了回来。
- **用 HTTP transport 与 stdio 客户端的生命周期解耦**——作为*持久性方案*推迟：部署更重，
  又不专门解决持久传输，是比问题本身更大的改动。（HTTP transport 后来因其他原因确实加了
  进来，但这不改变本决策——持久性仍交给 `remote_job` / resume。）
- **完全留在进程内，持久性另想办法**——选中。持久工作交给 `remote_job`：命令在**远端**主机上
  `nohup`，凭据在连接建立时就已用完，本地无需任何子进程替它持有；前台传输中断则靠
  `remote_transfer` 的 `resume` 续传恢复。

## 后果

- 前台 `remote_exec` / `remote_shell` / `remote_transfer` 会随 agent / server 一起结束，这是
  **设计如此**——就是那条已写明的 exec-vs-job 区分，而非缺陷。
- "持久"专指 `remote_job`，"恢复中断的上传"专指 `resume`，都不等于前台传输能自主存活。
- 凭据统一这条不变量对每一条代码路径都成立，这正是本项目敢承诺"明文永不进入 LLM / argv /
  磁盘"的底气。
- 词汇——"凭据路径（Credential path）"、"前台 / 后台执行"——定义在
  [`CONTEXT.md`](../../CONTEXT.md)。
