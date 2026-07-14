# 0004 — 默认走登录 shell 继承环境；`use_sudo` 即完整变 root，不做"保留用户环境的 sudo"

> 🌐 简体中文 ｜ [English](./0004-login-shell-and-sudo-env.en.md)

状态：已采纳（accepted）

`remote_exec` / `remote_job` 默认 `login=True`：命令跑在**登录 shell**（`bash -lc`）里，
继承"命令最终以谁的身份运行"的登录环境（`/etc/profile` 与 `~/.bash_profile`/`~/.profile`
链），这样 conda / nvm / pyenv / `~/.local/bin` 等 PATH 生效。`use_sudo=True` 表示**完整
变成 root**：sudo 默认的 `env_reset` 会把 `HOME`/`USER`/`LOGNAME`/`SHELL` 按目标用户
（root）重建，因此 `~` 展开为 `/root`、登录用户的环境不保留——处理登录用户的文件必须
写绝对路径。portal **不提供**"有 sudo 权限但保留我的用户环境"这种模式。

## 背景

两件独立、却常被搅在一起的事：

1. **登录 shell 继承。** 非登录、非交互的 `bash -c` 什么 rc 都不读，PATH 只有默认值，用户
   装在 rc/profile 里的工具（conda/nvm/pyenv）全丢。默认 `login=True` 用 `bash -lc` 补上
   登录环境。
   - 诚实的边界：`bash -lc` 是**登录 + 非交互**，读 `/etc/profile` 与
     `~/.bash_profile`/`~/.profile` 链。它**能**加载 `~/.bashrc`，真正的拦路虎是**非交互
     guard**：Ubuntu/Debian 默认 `~/.bashrc` 顶部有 `case $- in *i*) ;; *) return;; esac`，
     非交互下（`$-` 无 `i`）直接 `return`，装在其**下方**的 conda/nvm/pyenv init 被跳过——
     实测 guard 版 `bash -lc` 拿不到，去掉 guard、或把 export 放到 guard 之上 / `~/.profile`
     就拿得到。唯一能让 guard 放行的是**交互 shell**（`bash -ic`），但在无 tty 的 SSH exec 里
     它会往 stdout 灌 `/etc/bash.bashrc` 输出、往 stderr 灌 `no job control` 警告、`-l` 再加
     整段 MOTD，还要 PTY（与 sudo/secret 的 stdin 通道冲突）。**更根本地，portal 尊重
     目标机的 guard**——把那段内容 gate 到交互会话是用户/发行版有意为之、自有其理由，
     不该被自动化强行 `bash -ic` 绕开。所以 portal 走非交互 `bash -lc`：要让工具在此可用，
     把它的 PATH/init 放进 `~/.profile` 或 guard 之上，而非依赖交互式 `.bashrc` 正文。

2. **`use_sudo` 的身份语义。** `sudo <cmd>`（无 `-u`）必然以 root 身份运行。sudo 默认
   `env_reset`（本机 `sudoers(5)`：*"The HOME, MAIL, SHELL, LOGNAME and USER environment
   variables are initialized based on the target user"*）把环境按 root 重建。于是**即便**叠上
   `bash -lc`，加载的也是 **root 的**登录环境、`~` = `/root`——永远不是登录用户的。

## 考虑过的方案

- **另做"保留用户环境 + sudo 权限"模式（`sudo -E` / `--preserve-env`）**——否决。
  - `-E`（"从命令行关掉 `env_reset`"）要的是一项**叠加在普通 sudo 权限之上的额外授权**
    ——sudoers 的 `setenv` 开关，或命令规则上的 `SETENV` tag（`sudoers(5)`：仅当规则的
    命令是 `ALL` 时才自动隐含）。这跟"对面有没有 sudo 权限"是两码事：一台把 sudo 收窄到
    具体命令白名单（最小权限）的机器上，普通 `sudo <cmd>` 照跑，`sudo -E <cmd>` 却会被
    `sorry, you are not allowed to preserve the environment` 拒掉（除非那条规则显式标了
    `SETENV`）。所以 `-E` 能不能用**取决于目标机 sudoers 的授权形态**（宽授 `ALL` 隐含
    放行、命令白名单则否），portal 无从预知——默认开就成了"宽授机上能用、最小权限机上
    静默被拒"的不确定行为，不配当默认。
  - 安全面：`-E` 关掉 `env_reset` 后，除 `env_check`/`env_delete` 黑名单（`LD_*`、`IFS`、
    `BASH_ENV`、`ENV`… 这些经典动态链接向量确实仍被剥掉）以外的变量**全部原样继承进
    root**——`PYTHONPATH` / `PERL5LIB` / `NODE_OPTIONS` / 各类应用自定义变量都在其列。
    `sudoers(5)` 自己就写明"无法屏蔽所有潜在危险变量，故鼓励用默认的 `env_reset`"；何况
    这里的环境还受 agent/LLM 上下文影响。窄口 `--preserve-env=HOME` 能收窄，但仍需上面
    那项 `SETENV` 授权、仍是把用户可写的 rc 交给 root 执行。
  - 语义：`use_sudo=True` 又"不是 root"是个自相矛盾的伪状态；再加 `root=true/false` 只会
    让工具签名更糊。
- **在已提权 shell 里自己 `export HOME=<登录用户 home>` 再 `bash -lc`（免 sudoers）**——
  否决（本 ADR 明确不再考虑）。它绕开了 `setenv` 依赖，但：① 仍是 root 去 `source` 登录
  用户的 rc（同样的提权味道）；② 受上面那条 `~/.bashrc` 非交互 guard 限制，"找回
  conda/nvm"这个主要卖点其实兑现不了；③ 要解析登录用户的 home、拼接注入，复杂度换来的
  收益很薄。
- **`use_sudo` 就是完整变 root，登录 shell 继承默认开**——选中。语义干净（提权 = 变 root，
  跟系统 `sudo` 一致），零 sudoers 依赖，任何主机都能用；"想要我的环境"的诉求由绝对路径
  ＋把要用的工具放进登录安全位置（profile / 系统级）来满足。

## 后果

- `remote_exec` / `remote_job` 默认 `login=True` 走 `bash -lc`；`login=False` 退回裸
  `bash -c`。operator 可用 `PORTAL_LOGIN_SHELL` 或 per-host `login_shell:` 调默认。
- `use_sudo=True` 下 `~`/`$HOME` = `/root`、`$USER`/`$LOGNAME` = root：**处理登录用户文件
  一律写绝对路径**。这是设计如此，不是 bug。
- 文档（工具 docstring / README）不得承诺 login 能加载被 guard 的 `~/.bashrc`；措辞应为
  "读登录环境（profile 链），非交互下不含被 guard 的 `.bashrc` 正文"。
- 需要某工具在提权 / 非交互下可用，正解是把它的 PATH/init 放进 `~/.profile` 或系统级位置，
  而非期待 portal 复刻交互式 shell。
- 词汇——"登录 shell（Login shell）"、"登录环境（Login environment）"、"提权即变 root"——
  定义在 [`CONTEXT.md`](../../CONTEXT.md)。
