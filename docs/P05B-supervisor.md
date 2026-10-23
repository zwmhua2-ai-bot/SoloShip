# SoloShip Supervisor（P05-B）

用户登录后在后台接收 ChatGPT Work 投递的任务，单实例、单并发、失败隔离。
**不常驻 Codex**——只有任务到达时才启动开发流程。

## 1. 命令

```bash
# 启动（默认后台；已有实例时退出码 3）
py -3 bridge/supervisor.py start --root C:\Users\hua\SoloShip-P00

# 前台运行（排障用；--max-cycles 便于测试）
py -3 bridge/supervisor.py start --foreground --max-cycles 1

# 状态（--json 供脚本读取；未运行时时退出码 4）
py -3 bridge/supervisor.py status --root C:\Users\hua\SoloShip-P00
py -3 bridge/supervisor.py status --json

# 优雅停止（默认 30s 等待；--force 才强杀，不推荐）
py -3 bridge/supervisor.py stop --timeout 30

# 重启
py -3 bridge/supervisor.py restart

# 后台环境与凭据自检（只读）
py -3 bridge/supervisor.py doctor

# 查看未收尾任务（P05-C 的输入）
py -3 bridge/supervisor.py recovery
```

退出码：`0` 成功 / `3` 已在运行 / `4` 未运行 / `5` 启动失败 / `6` 停止超时 / `7` 自检阻断。

> `scripts/soloship_start.ps1` 会把 `start` 的退出码 `3`（已有实例）映射为 `0`，
> 否则任务计划程序会把「登录时重复触发」记成失败。手动执行 `supervisor.py start`
> 仍返回 `3`，语义不变。

## 2. 单实例控制

两层，都不依赖 PID 文件：

1. **Windows 命名互斥体** `Local\SoloShipSupervisor-<root 哈希>`（`CreateMutexW`，`ERROR_ALREADY_EXISTS` 即拒绝）。
2. **文件字节锁** `.control/supervisor/supervisor.lock`（`msvcrt.locking` / POSIX `flock`）。

`state.json` 里的 PID 只用于展示与排障；系统可能复用 PID，所以**不作为**运行判据。
`status.running` 直接等于「锁是否被人持有」。

存活探测用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess`，
**不用** `os.kill(pid, 0)`——Windows 上后者等价于 `TerminateProcess`，
且对任务计划程序以受限令牌启动的进程会返回"不存在"（实测踩过）。

> `counts` 是**本次进程**的累计值，重启后归零；跨进程的完整历史看
> `logs/supervisor-YYYYMMDD.log`。

### 状态文件写入重试

`_write_json` 对 `os.replace` 的 `PermissionError` 做退避重试（8 次，20ms→400ms）。
原因：Windows 上 Python 的 `open()` 不带 `FILE_SHARE_DELETE`，`start` 轮询
`state.json` 的瞬间会让守护进程的原子替换失败（`WinError 5`）。不重试时守护进程会
在**第一次心跳**就退出，而任务计划仍报 `0` —— P05-B 重启时实测踩到过。

### 日志编码

任务计划程序拉起时 stdout 被重定向到 `logs/supervisor-boot.log`。Python 默认按本地
代码页写字节（zh-CN 为 cp936），而日志消费方按 UTF-8 读，中文就会乱码——P05-B
实测过同一文件里 UTF-8 与 GBK 段落混排。

现在 `main()` 入口调用 `force_utf8_stdio()`：**只**把被重定向（非终端）的
stdout/stderr 重配为 UTF-8，交互式控制台仍用本地代码页；不使用
`PYTHONIOENCODING`，因此不改变任何子进程能继承的环境变量。

`scripts/soloship_start.ps1` 侧同样两处：把 `[Console]::OutputEncoding` 设为 UTF-8
（正确解码子进程输出），并**弃用 `*>>`**（PS 5.1 的文件重定向固定写 UTF-16LE，
会与脚本自己 `Out-File -Encoding utf8` 的行混成编码大杂烩），改为逐行
`Out-File -Append -Encoding utf8`，保证增量写入且整段新增内容都是 UTF-8。
历史日志段落不重写。

## 3. 调度

- 扫描两处 inbox：`exchange/inbox/*.ready.json`（P00 交接）与
  `exchange/p02/inbox/*.ready.json`（P02 开发）。
- P00 优先（快、不消耗 Codex 额度）；每轮最多执行 **一个** 任务（`MAX_PARALLEL = 1`）。
- 认领文件 `.control/supervisor/claims/<kind>__<task_id>.json`：
  - `running` 且持有者存活 → 跳过（另一个 worker 在跑）；
  - `running` 但持有者已死 → **推迟**，交给 P05-C，不自动重跑；
  - `done` / `failed` → 视为已处理，文件移入 `deferred/`（原因 `ALREADY_PROCESSED`），不再执行。
- 任务异常只记 `SUPERVISOR_ERROR`，循环继续。
- 每完成一个任务刷新 P04 状态索引（`status_index.refresh`）。
- 启动时把 `p02_jobs.finished_at IS NULL` 的历史任务写入
  `.control/supervisor/recovery_pending.json`，并**不重跑**。

## 4. 目录与文件

```
.control/supervisor/
  state.json               运行状态、PID、启动时间、最近心跳、计数、当前任务
  supervisor.lock          字节锁（勿删）
  queue.json               队列快照（审计）
  claims/*.json            单任务认领
  recovery_pending.json    未收尾任务（P05-C）
  deferred/                被推迟的任务文件
  logs/supervisor-YYYYMMDD.log
  logs/supervisor-boot.log 后台启动时的 stdout/stderr
```

## 5. 凭据与环境

- 不读取、不写入、不传递 GitHub PAT；脚本与计划任务里都没有 Token。
- 发布仍走既有门禁：`github.enabled` + `require_env_token`；缺 `GH_TOKEN` 时
  `publish_gate=blocked_missing_token`，**不回退**到 `gh` 账号级缓存 Token。
- Codex 子进程环境仍由 `env_guard.scrub_secrets(keep_paths=("CODEX_HOME",))` 生成：
  `CODEX_HOME` 保留（登录态所在），`GH_TOKEN` 等凭据被剥离。
- `SANDBOX_BLOCKED` / `WINDOWS_TEST_MODE` 原样透出，Supervisor 不改写。

## 6. 登录自启安装（需单独批准）

```powershell
# 1) 先预览（默认不注册任何东西）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_startup.ps1

# 2) 确认后注册（普通用户权限 RunLevel=Limited）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Approve

# 3) 验证
Get-ScheduledTask -TaskName 'SoloShip Supervisor' | Get-ScheduledTaskInfo
py -3 bridge\supervisor.py status

# 4) 撤销
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Uninstall -Approve
```

安装后的行为：用户登录 → `scripts\soloship_start.ps1` → `supervisor.py start` →
后台子进程持锁运行；重复触发被 `MultipleInstances=IgnoreNew` 与互斥体双重挡住。

## 7. 并行扩展点

首版 `MAX_PARALLEL = 1`。要支持并行：重写 `Supervisor.select_batch()` 返回多个候选，
并把 `tick()` 的串行执行换成 worker 池。认领与队列已是任务粒度，存储层不用改。
