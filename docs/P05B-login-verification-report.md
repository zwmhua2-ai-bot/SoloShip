# P05-B 登录自启验证报告

- 验证对象：Windows 任务计划任务 `SoloShip Supervisor`（普通用户、`RunLevel=Limited`）
- 验证时间：2026-10-23 21:55 – 22:18（本地时间）
- 结论：**注册成功，自启链路可用，单实例生效，P00–P04 数据未被污染。** 发现 1 项待批准修复的日志编码缺陷。

---

## Result

注册该任务，并用任务计划程序真实拉起后台 Supervisor，完成「自动接收任务 / 单实例 / 停止 / 重启 / 数据隔离」五项验证，全部通过。

## Changes

本轮新增/修改（相对 P05-B 审批前代码）：

| 文件 | 变更 |
| --- | --- |
| `bridge/supervisor.py` | 修复 `pid_alive` 误判（受限令牌进程被当成已死） |
| `scripts/soloship_start.ps1` | `start` 退出码 3 → `exit 0`，避免登录重复触发被记成失败 |
| `tests/test_p05b_supervisor.py` | 新增 `TestPidProbe` 2 项回归用例 |
| `docs/P05B-supervisor.md` | 记录退出码 3→0 映射、`pid_alive` 实现、`counts` 单进程语义 |
| `docs/P05B-login-verification-report.md` | 本报告 |

注册产生的系统侧变更：任务计划任务 `SoloShip Supervisor`（1 个），无其他系统改动。

## Verification

### 1. 注册结果

| 项目 | 实测值 |
| --- | --- |
| TaskName | `SoloShip Supervisor` |
| State | `Ready` |
| RunLevel | `Limited`（普通用户） |
| LogonType | `InteractiveToken` |
| UserId | `hua` |
| MultipleInstances | `IgnoreNew` |
| StartWhenAvailable | `true` |
| ExecutionTimeLimit | `PT0S`（不限时） |
| Action | `powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\Users\hua\SoloShip-P00\scripts\soloship_start.ps1 -Root C:\Users\hua\SoloShip-P00` |
| XML 大小 | 1481 bytes |
| XML 含 `Password` / `GITHUB_TOKEN` / `GH_TOKEN` | 否 / 否 / 否 |

> XML 中唯一匹配到 `Token` 字样的地方是 `LogonType=InteractiveToken`，不含任何凭据。

### 2. 经任务计划程序实际启动

- 触发：`Start-ScheduledTask`（2026-10-23 22:17:58）
- 结果：`LastTaskResult = 0`，守护进程 pid **44224**（创建时间 22:17:59）
- 心跳：`2026-10-23T14:18:05+00:00`（查询时 3.5s 前），`running=True, lock=True`
- 进程数：`supervisor.py _run` 恰为 **1**
- Codex 就绪 `True`，执行模式 `WINDOWS_TEST_MODE`（未被改写）

### 3. 自动接收（新 P00 任务）

- 测试请求：`p00_p05b_login_01`（标题「P05-B 登录自启验证」）
- 由任务计划启动的实例（pid 31192）在 2026-10-23 22:06:08 完成接收
- 回执：`ACCEPTED` / `P00_HANDOFF_OK` / `job_id=p00_890ef0313579`
- `real_codex_run=false`、`code_changed=false`、`tests_executed=false`、`github_pr_created=false`
- 认领文件：`claims/p00__p00_p05b_login_01.json` → `state=done, detail=accepted`
- inbox 已清空，原文件归档为 `exchange/archive/p00_p05b_login_01.ready.json.51274097`

### 4. 单实例（两重证据）

| 手段 | 现象 |
| --- | --- |
| 手动 `supervisor.py start` | 退出码 **3**（`ALREADY_RUNNING`） |
| 任务计划重复触发（22:18:13） | `LastTaskResult=0`（脚本把 3 映射为 0），pid 仍为 44224，进程数 **1** |
| 操作系统级锁 | 文件字节锁被守护进程持有（外部进程读取 `supervisor.lock` 报共享冲突）；命名互斥体 `Local\SoloShipSupervisor-<root sha1>` 兜底 |

`state.json` 的 PID 仅用于展示，运行判据是「锁是否被持有」。

### 5. 停止与重启

- 优雅停止 pid 5600：`ok=true, graceful=true, stopped=true`，退出码 0，停止后进程数 0
- 重启由任务计划完成（pid 44224），未残留多实例
- 停止日志：`2026-10-23 22:17:52,730 INFO Supervisor 已停止 pid=5600 cycles=131`

### 6. P00–P04 数据完整性

基线快照 `baseline_p05b.json`（290 个文件，注册前采集）：

| 类别 | 数量 | 明细 |
| --- | --- | --- |
| 变化 | 5 | `.control/p00.db`（+1 job）、`bridge/supervisor.py`、`scripts/soloship_start.ps1`、`docs/P05B-supervisor.md`、`tests/test_p05b_supervisor.py` |
| 新增 | 20 | 8 个 `.control/supervisor/` 运行时文件；4 个 P00 测试任务产物（archive/receipt/report×2）；5 个 P04 状态索引；1 个 pyc；2 个 SQLite WAL 边车文件 |
| 缺失 | 0 | `p00.db-shm/-wal` 曾因 WAL checkpoint 消失、后又随连接重建，属 SQLite 正常行为 |

数据库校验（只读）：

- `.control/p00.db`：`quick_check=ok`，`jobs=4`（基线 3 + 本次 P00 测试任务 1）
- `.control/p02/p02.db`：`quick_check=ok`，`p02_jobs=5`、`p02_rounds=9`、`p02_worktrees=6` —— 与基线一致，**未新增任何开发任务**
- `recovery_pending=0`，`.control/supervisor/deferred/` 为空
- P04 索引仍指向开发任务：`latest.json → task_id=p03d_remove_002, state=pr_created, state_basis=github_realtime`

### 7. 安全限制逐条核对

| 限制 | 实际 |
| --- | --- |
| 不自动提交新的 P02 开发任务 | `p02_jobs` 5→5，无新增 |
| 不修改 GitHub 凭据 | `config/bridge.json` 哈希与基线一致，且无 `token` 字段 |
| 保持 `github.enabled=false` | `enabled=false`、`allow_merge=false`、`allow_force_push=false`、`require_env_token=true` |
| 不创建 PR / 不合并 / 不部署 | 无新 PR；测试回执 `github_pr_created=false` |
| 保持 `SANDBOX_BLOCKED` / `WINDOWS_TEST_MODE` | `execution_mode=WINDOWS_TEST_MODE`，`isolation_claim=NONE`，Supervisor 未改写 |
| 不删除、不恢复历史 worktree | `.worktrees/` 6 个目录齐全 |
| 不重跑历史中断任务 | `recovery_pending=0`，仅登记不执行 |

### 8. 全量回归

`python -m unittest discover -s tests` → **Ran 259 tests in 168.110s — OK**（22:17 重跑，此后代码未再变动）

## Remaining

### 待批准修复（1 项，本轮未擅自改动）

**缺陷：`logs/supervisor-boot.log` 在真实自启路径下编码不一致。**

- 证据：文件内 pid 1488 那段是 UTF-8 字节（`\xe5\x90\xaf\xe5\x8a\xa8` = 启动），pid 31192 / 44224 两段是 GBK 字节（`\xc6\xf4\xb6\xaf` = 启动）；按 UTF-8 打开得 25 个替换字符，按 GBK 解码末尾完全正确。
- 根因：`_spawn_background` 把日志文件直接作为守护进程 stdout，Python 在任务计划环境（控制台代码页 936）下用 cp936 写字节；而 `start-script.log` 由 `Out-File -Encoding utf8` 写入，正常。
- 影响：仅诊断可读性，不影响调度、锁、心跳与数据。主日志 `logs/supervisor-YYYYMMDD.log` 用 `encoding="utf-8"`，正常。
- 建议补丁（2 处，非凭据变更）：

```python
# bridge/supervisor.py::_spawn_background
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
proc = subprocess.Popen(cmd, ..., env=env)
```

```powershell
# scripts/soloship_start.ps1（调用 python 之前）
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
$env:PYTHONIOENCODING = 'utf-8'
```

生效需停启一次，历史 GBK 段落保留不重写。

### 已知风险

1. **长驻未证明**：本沙箱在命令结束时回收 DETACHED 子进程，只能证明「任务计划拉起 → 心跳连续」；「连续驻留数小时」需跨重启观察。
2. `counts` 为单进程累计，重启归零（文档已注明）。
3. `stop --force` 强杀可能留下未收尾任务，恢复策略属 P05-C。
4. 推迟任务（`UNFINISHED_CLAIM` / `ALREADY_PROCESSED`）不会自动恢复，等 P05-C。
5. `supervisor-boot.log` 的句柄由守护进程持有（外部 `ReadAllBytes` 会共享冲突，`Get-Content` 可读）；因 `cmd_start` 先判锁再开文件，不影响重复触发。
6. `gh` Token 权限含 `repo`（大于只读所需），代码仅发 GET 且有 `assert_readonly` 守卫。

### 当前状态

Supervisor **运行中**：pid 44224，由任务计划程序于 22:17:58 启动，心跳正常，队列 0/0，无待恢复任务。

## Evidence

```
.control/supervisor/state.json                          运行状态与心跳
.control/supervisor/supervisor.lock                     字节锁（被持有）
.control/supervisor/claims/p00__p00_p05b_login_01.json  认领 done/accepted
.control/supervisor/logs/supervisor-20261023.log        主日志（UTF-8）
.control/supervisor/logs/start-script.log               启动脚本日志（含 3→0 映射）
.control/supervisor/logs/supervisor-boot.log            守护进程 stdout（编码不一致）
.control/supervisor/baseline_p05b.json                  注册前 290 文件基线
exchange/reports/demo-app__p00_p05b_login_01.report.json P00 回执报告
exchange/status/latest.json                             P04 索引指向 p03d_remove_002
Get-ScheduledTask -TaskName 'SoloShip Supervisor'       任务注册信息
```
