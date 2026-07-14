# 凭据流 live 测试

本文件供 agent 读取：agent 依据这里的方法、在用户配合（无回显输入凭据、重启 MCP client 等只有人能做的步骤）下，对**已发布产物**跑端到端（e2e）测试，补 `pytest`（单测 / 集成）覆不到的部分——真实远端、真实凭据、无回显 prompt、credential agent 常驻进程等无法进 CI 的行为。测试目标**不限于**下面列出的维度：凡"发布产物真实跑起来才暴露"的凭据 / 连接行为都可纳入，agent 应按 [主动探测](#probe) 一节自行扩展。

当前主要验证 **credential agent 侧信道**的凭据隔离 / 复用不变量：SSH 登录密码 / 私钥 passphrase / sudo 密码是三种独立 kind、默认互不串用、`sudo_password_same_as_ssh` 为 opt-in、`ssh confirm` 成败分支正确、加密私钥能被缓存的 passphrase 解锁。这些不变量的威胁模型出处是 [`SECURITY.md`](../SECURITY.md) 的 [SSH 登录交互式密码](../SECURITY.md#ssh-login-password) 与 [Sudo 认证](../SECURITY.md#sudo-auth) 两节；[ADR 0003](adr/0003-credential-unification.md) 只提供背景前提——所有 kind 共用同一条进程内凭据路径、明文永不进对话 / argv / 磁盘——这也是下面 [用指纹比对而不暴露明文](#fingerprint-compare) 一节能只凭 sha256 指纹判定同值 / 异值的原因。与 [`tests/live_smoke.py`](../tests/live_smoke.py)（自动化、key-auth、跑功能回归）互补——这条链专测**无回显 prompt** 与 **credential agent** 的端到端行为。

## 分工：能自动的都归 agent

原则：版本核对、只读查询、配置编辑、MCP 调用、网络诊断、临时 key 生成与清理，全部由 agent 自主完成。只有两类必须人工，因为它们要么涉及 agent **不该知道明文**的秘密，要么 agent 无法触发。

| 归属 | 事项 |
|---|---|
| agent 自主 | 发布版本预检、`hosts.yaml` / `policies.yaml` 编辑、缓存 `show` / `list` / `clear`、`remote_exec` / `policy_check` / `inspect` / `hosts`、网络可达性诊断与 `proxy_jump` 配置、临时 key 生成、经已认证通道安装公钥、清理与状态还原 |
| 必须人工 | 登录密码的**无回显**输入（`portal ssh set` / `portal ssh confirm` / `portal sudo set`）；**重启 MCP client**（各家命令不同）让 server 换版本 |

agent 自己生成的秘密（例如临时 key 的 passphrase）不必人工：`printf '%s\n' "$secret" | portal passphrase set <alias>` 即可——`getpass` 在无 tty 时回退读 stdin。只有"agent 不该知道明文"的登录密码才走人工无回显输入；秘密永不进对话、argv、日志。

## 预检

发布产物先于任何凭据输入核对：

```bash
uvx portal-mcp-server@<ver> --version        # == <ver>；prerelease 时 @latest 会落到稳定版，必须显式 pin
uvx portal-mcp-server@<ver> passphrase --help # 子命令在位
uv tool install --force --refresh 'portal-mcp-server==<ver>'
which -a portal && portal --version           # 确认 PATH 命中的就是这一版
```

credential agent 是 systemd `--user` socket-activated 服务，`ExecStart` 从 uv tool 的 venv 跑；上面 `--force` 换代码后要 `systemctl --user restart <agent-service>` 让常驻进程加载新版本。**重启会清空所有 TTL 缓存**——先向用户说明再做。`portal agent status` 应报四种 kind（ssh / passphrase / sudo / secret）计数。

MCP 侧：把 client 配置里 portal server 也 pin 到 `@<ver>`，人工重启 client 后 agent 调 `inspect(view="server")`，确认版本一致且工具名是当前一代（`remote_exec` / `inspect` / `hosts` …）。此关通过前不输入任何凭据。

策略两层同时生效（若启用）：`inspect(view="policy")` 看加载值，`policy_check` dry-run 验证 `command_blocklist`（fnmatch）与 `safety_net`（[cc-safety-net](https://github.com/kenryu42/cc-safety-net) 语义分析）**都过才放行**。用一条只命中 blocklist 的（如 `rm -rf /`）和一条 blocklist 无模式、只有语义层能拦的（如 `rm -rf ~`）分别验证两层独立起作用；`safety_net` 取不到裁决时按 `fail_closed` 决定放行或拒绝。

## 网络可达性与 proxy_jump

**现象**：目标 TCP 能秒连却拿不到 SSH banner（连接建立后对端立即 FIN）。先分清是本机出站路径还是远端故障，别直接判 ban 或服务挂了——常见原因是目标 IP 在本机默认直连路径上被屏蔽（对照判据：同域其它主机直连正常，或经一台可达 hop 探测目标 banner 正常）。

**处置**：portal 有 host 级 `proxy_jump`（映射到 asyncssh 的 `tunnel`），给 alias 加 `proxy_jump: <user@jump:port>` 即可经一台能连通目标的 hop 连接，**不动本机自身的代理配置**。合并语义（见 [ADR 0002](adr/0002-ssh-config-merge.md)）：显式 host 只认 `hosts.yaml` 里的 `proxy_jump`；`use_ssh_config` host 不写则沿用 ssh_config 的 `ProxyJump`，写了则覆盖。跳板机自身认证走 ssh-agent / 默认 key。

## 测试 alias 与策略

两个 alias 指向同一台密码登录测试机，差别只在 sudo 复用开关：

```yaml
hosts:
  <default-alias>:
    host: <test-host>
    user: <test-user>
    auth: password              # 无 password_command：每次连接前须先 portal ssh set
  <reuse-alias>:
    host: <test-host>
    user: <test-user>
    auth: password
    sudo_password_same_as_ssh: true   # opt-in：ssh set/confirm 同时预塞 sudo 缓存
```

目标不可达时按上一节给两个 alias 各加 `proxy_jump`。改完让 client 重载，`hosts(action="list")` 确认识别。

## 验证维度

每个维度先按 alias 清掉三种缓存（`portal ssh|sudo|passphrase clear <alias>`）从干净态开始；标 **H** 的操作是人工无回显输入，其余全由 agent 执行并查证。预期结果尽量核到具体信号——缓存计数、指纹关系、CLI 文案、退出行为。

| 维度 | 操作 | 预期结果 |
|---|---|---|
| 默认隔离 | H：`portal ssh set <default-alias>`；A：查三种缓存、`remote_exec(id -un)`、同调用再加 `use_sudo=true` | 仅 `ssh` 有缓存，`sudo`/`passphrase` 无；`id -un` 返回普通登录用户、exit 0；`use_sudo=true` 被拒并明确报"无 sudo 密码、命令未运行"，不回退用 ssh 密码 |
| 显式复用 | H：`portal ssh set <reuse-alias>`；A：查缓存指纹、`remote_exec(id -un, use_sudo=true)` | CLI 额外打印"sudo 也已缓存（同 SSH 登录密码）"；`ssh` 与 `sudo` 指纹相同、`passphrase` 无；sudo 调用 `id -un` = `root`，结果标 `high_risk` |
| 复用反例 | A：承上只 `portal sudo clear <reuse-alias>`，再 `remote_exec(id -un, use_sudo=true)` | `ssh` 缓存仍在（指纹不变）；sudo 调用**失败**、报无 sudo 密码——证明复制发生在 `ssh set` 时，执行阶段不借用 ssh 缓存 |
| confirm 失败分支 | H：`portal ssh confirm <reuse-alias>`，两次输入**不同** | CLI 报 `Values differ; nothing cached`、退出非零；`ssh`/`sudo` 都无缓存 |
| confirm 成功分支 | H：再次 `portal ssh confirm <reuse-alias>`，两次**相同**；A：查缓存、sudo 调用 | `ssh`+`sudo` 都有缓存、同指纹（reuse host 走 opt-in）；`passphrase` 无；sudo `id -un` = `root` |
| passphrase 隔离 | H：`portal passphrase set <reuse-alias>`，值**≠**登录密码；A：查三种指纹，再 `portal passphrase clear <reuse-alias>` 后复查 | `passphrase` 指纹与 `ssh`/`sudo` 不同；set 后 `ssh`/`sudo` 指纹不变；clear `passphrase` 后 `ssh`/`sudo` 仍在——set/clear 都不串到别的 kind |
| 加密私钥端到端 | A 全自主：生成临时加密 Ed25519 key、装公钥到测试账户、`printf '%s\n' "$pp" \| portal passphrase set <key-alias>`，分三阶段连接 | 无 passphrase → 连接**快速失败**（本地解不开私钥，非挂起）；正确 passphrase → 解锁并认证成功、命令返回预期用户；再 `clear` → 再次快速失败 |

sudo 相关调用结果会标 `high_risk`；跑完向用户说明用缓存的 sudo 密码执行了特权命令。

## <a id="fingerprint-compare"></a>用指纹比对而不暴露明文

`portal <kind> show` 只给 secret 的 sha256 指纹 + 剩余 TTL，没有 show-plaintext 动作。比对指纹即可判定"同值 / 异值"——验证复用（ssh 与 sudo 同指纹）、隔离（passphrase 指纹不同）、未被覆盖（多步后指纹不变），全程不接触明文。

## <a id="probe"></a>主动探测：不止照表跑

上表是最小骨架，不是终点。agent 参考本文做测试时应**主动构造变体、边界与反例去逼出潜在问题**，凡预期与实际不符都记录并报告，别只勾"过 / 不过"。可切入的方向（示例，不完备）：

- **kind 串扰的更多反例**：清掉某一 kind 是否误伤其它 kind；同 host 反复 `set` 后指纹是否如实更新为新值；对**没开** opt-in 的 host 确认 `ssh set` 绝不产生 sudo 缓存。
- **TTL 与过期**：缓存到期后再调用应回到"无凭据"路径（拒绝或要求重设），不残留可用；过期边界附近的行为。
- **错误输入的失败信号**：密码 / passphrase 错误时能否清楚区分"无缓存 / 密码错 / 目标无权限"，会不会静默回退到另一凭据来源。
- **依赖链完整性**：在**干净的发布安装**里跑加密私钥端到端，确认 passphrase 真能解锁（而非只是被缓存）——把"缓存成功"与"解锁成功"当两件事分别验。
- **proxy_jump 组合**：加密私钥目标经 `proxy_jump` 时，留意跳板机与目标各自用哪把 key、passphrase 对谁生效；直连与经跳板结果是否一致。
- **passphrase 语义边界**：对**未加密**私钥设 passphrase 会怎样；`use_ssh_agent` 取 auto / 纯 agent / 禁用时，与 key 文件、ssh-agent 的交叉行为。
- **策略层**：`command_blocklist` 与 `safety_net` 各自单独命中；`bash -c` 包壳、解释器单行能否被语义层拆穿；`fail_closed` 下 checker 不可用时是否真拒绝。
- **并发与幂等**：同一 host 并发 `set` / `clear`；`confirm` 与 `set` 混用；重启 credential agent 或 MCP server 后缓存与注册状态是否符合预期。

目的是把"照表通过"变成"主动找茬"：发布产物的真实行为与文档 / ADR 承诺之间的任何偏差都值得记录。

## 清理

- 按 alias `portal <kind> clear`，不做全局 `portal agent clear`（免误清其它临时凭据）。
- 临时 key：按唯一 comment 从远端 `authorized_keys` 撤公钥；本地私钥 / 公钥用 `trash-put`（不用 `rm`）；移除临时 alias。远端若因测试新建了 `~/.ssh` 或 `authorized_keys`，按测前记录的原状还原（原本不存在就整个删掉）。
- 为复现或诊断而临时改动的环境（临时装的依赖、pin 的版本等）测完还原到发布态。
- 测试改动的备份（`hosts.yaml` / MCP 配置 / `known_hosts`）保留为回滚点，去留交用户定。
