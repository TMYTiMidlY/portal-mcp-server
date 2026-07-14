# 0003 — 唯一的进程内凭据路径；不为"活得久"而起分离子进程

> 🌐 简体中文 ｜ [English](./0003-credential-unification.en.md)

状态：已采纳（accepted）

portal 建立的每条连接都走唯一一条进程内 asyncssh 认证路径。所有凭据类型——SSH 密钥、
登录密码、密钥 passphrase、sudo 密码、命名 secret——都在这条路径上解析，明文只交给它
真正的消费方（asyncssh 握手、`sudo -S` 的 stdin、注入的环境变量）；明文永不进入 agent
对话、命令行 / `ps` argv、或磁盘。portal **不会**为了让某操作活得比 agent 久而起一个
分离的子进程（`nohup scp` / `rsync` / `ssh`），因为那个子进程用不上这条路径。

## 背景

agent 常想让一次传输或命令"在 agent 停止后仍继续"。最直白的实现是起一个分离的
`nohup rsync` / `scp` 子进程。但 portal 的凭据保证——连接时现取的 `password_command`、
hosts.yaml ↔ ssh_config 合并（见 [ADR-0002](0002-ssh-config-merge.md)）、以及"密码不进
argv"——只存在于进程内的 asyncssh 路径上。外壳出去的 `scp` / `rsync` 一个都没有：它会
回落到 `sshpass`、把密码放进 argv（`ps` 可见），或彻底丢掉 `password_command` / 合并。

## 考虑过的方案

- **为持久操作起分离子进程**——否决：绕开统一凭据路径，重新引入本项目存在的意义所要
  避免的 argv / 明文泄漏。
- **用 HTTP transport 与 stdio 客户端生命周期解耦**——推迟*作为持久性方案*：部署更重，
  且不专门解决持久传输，是比问题本身更大的改动。（HTTP transport 后来因其他原因加了
  进来；这并不改变本决策——持久性仍交给 `remote_job` / resume。）
- **完全留在进程内，持久性另想办法**——选中。持久工作交给 `remote_job`（命令 `nohup`
  在**远端**主机上，凭据在连接建立时就已用完、无需本地子进程持有）；前台传输中断则靠
  `remote_transfer` 的 `resume` 续传恢复。

## 后果

- 前台 `remote_exec` / `remote_shell` / `remote_transfer` 随 agent / server 一起死，这是
  设计如此——就是那条已写明的 exec-vs-job 区分，不是缺陷。
- "持久"指 `remote_job`；"恢复中断的上传"指 `resume`，而非前台传输的自主存活。
- 凭据统一不变量对每条代码路径都成立，这正是本项目敢承诺"明文永不进入 LLM / argv /
  磁盘"的依据。
- 词汇——"凭据路径（Credential path）"、"前台 / 后台执行"——定义在
  [`CONTEXT.md`](../../CONTEXT.md)。
