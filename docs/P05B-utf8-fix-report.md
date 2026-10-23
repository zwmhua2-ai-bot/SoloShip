# P05-B 日志编码修复报告

- 任务：修复 `supervisor-boot.log` 的 UTF-8 / GBK 混合编码，并重启 Supervisor
- 时间：2026-10-23 22:30 – 22:42（本地时间）
- 结论：**编码缺陷已修复，日志新增内容全部为严格 UTF-8；Supervisor 由任务计划程序重启并保持后台运行（PID 20400）。** 修复过程中暴露并修掉一个更严重的启动竞态（见 §3）。

---

## Result

1. `supervisor-boot.log` 与 `start-script.log` 的**新增内容**均为严格 UTF-8（0 个替换字符、0 个 NUL 字节）。
2. Supervisor 经任务计划程序重启成功，单实例、心跳、任务接收、状态查询全部正常。
3. 过程发现：修复后第一次重启时守护进程在**第一次心跳**就因 `WinError 5` 退出，而任务计划仍报 `0`。已定位为 `state.json` 原子替换与 `start` 轮询的竞态，并加退避重试修掉。

## Changes

| 文件 | 变更 | 依据 |
| --- | --- | --- |
| `bridge/supervisor.py` | 新增 `force_utf8_stdio()`，在 `main()` 入口把**非终端** stdout/stderr 重配为 UTF-8 | 批准项 1、3 |
| `bridge/supervisor.py` | `_write_json` 对 `os.replace` 的 `PermissionError` 做退避重试（8 次，20ms→400ms） | 见 §3，完成批准项 7 所必需 |
| `scripts/soloship_start.ps1` | 设 `[Console]::OutputEncoding = UTF8`；`*>>` 改为逐行 `Out-File -Append -Encoding utf8` | 批准项 1、3、8 |
| `tests/test_p05b_supervisor.py` | 新增 `TestStdioEncoding`（3 项）、`TestStateWriteRace`（2 项） | 回归覆盖 |
| `docs/P05B-supervisor.md` | 补「状态文件写入重试」「日志编码」两节 | 文档同步 |
| `docs/P05B-utf8-fix-report.md` | 本报告 | — |

未使用 `PYTHONIOENCODING`：环境变量一个都没改，代码子进程可继承的变量范围不变。

## Verification

### 1. 修复前检查（批准项 4）

- PID **44224**，`state=running`，`lock=True`，心跳 1.1s 前
- `current_task = null`，队列 `P00=0 / P02=0`，inbox 空，`recovery_pending=0`，进程数 1
- 判定：无正在执行的任务，可以安全停止（批准项 5）

### 2. 停止 → 修改 → 重启

- 优雅停止：`ok=true, graceful=true, stopped=true`，退出码 0，停止后进程数 0
- 首次重启（22:35:35）：**失败** —— 见 §3，已修复
- 二次重启（22:36:31）：pid 9648 正常，用作编码初验
- 最终重启（22:40:33，`Start-ScheduledTask`）：`LastTaskResult = 0`，新 PID **20400**，进程创建于 22:40:34

### 3. 意外缺陷：`state.json` 原子替换竞态（已修）

首次重启后守护进程 20ms 内退出，`supervisor-boot.log` 记录：

```
PermissionError: [WinError 5] 拒绝访问。: '.control\supervisor\.write-*.pending' -> '.control\supervisor\state.json'
  at supervisor.py:131 _write_json -> p00_bridge.py:61 atomic_text -> os.replace
```

- 根因：Windows 上 Python 的 `open()` 不带 `FILE_SHARE_DELETE`。`start` 的 `_wait_ready`
  每 0.2s 读 `state.json`，读窗口内的原子替换必被拒绝。
- 危害：守护进程在第一次心跳即退出，而 `start` 已经打印 `ok:true / ready:true`、
  任务计划 `LastTaskResult=0` —— **「任务计划 0」不能单独用作「进程活着」的证据**。
- 修复：`_write_json` 退避重试（8 次）。
- 确定性证据：`TestStateWriteRace.test_single_attempt_conflicts` 把 `WRITE_ATTEMPTS` 压到 1，
  在持有读句柄时**必然**抛 `PermissionError`；恢复默认值后写入成功。

### 4. 日志 UTF-8 验证（批准项 8）

按重启前记录的字节偏移，只校验**新增段**（旧内容未改，批准项 3）：

| 文件 | 新增字节 | 严格 UTF-8 解码 | U+FFFD | NUL 字节 |
| --- | --- | --- | --- | --- |
| `logs/supervisor-boot.log` | 303 | 通过 | 0 | 0 |
| `logs/start-script.log` | 955 | 通过 | 0 | 0 |

新增内容示例：

```
2026-10-23 22:40:27,664 INFO 等待期间收到停止请求
2026-10-23 22:40:27,673 INFO Supervisor 已停止 pid=9648 cycles=47 counts={...}
2026-10-23 22:40:35,400 INFO Supervisor 启动 pid=20400 root=C:\Users\hua\SoloShip-P00 codex_ready=True
```

对照组（未修复时同样写法）：`b'\xc6\xf4\xb6\xaf\r\n'` —— GBK 字节，严格 UTF-8 解码失败
（`invalid continuation byte`）。说明该验证确实能发现问题，不是空跑。

> 附带发现：PS 5.1 的 `*>>` 固定写 UTF-16LE，之前 `start-script.log` 实为
> 「脚本自己写的 UTF-8 行 + 重定向的 UTF-16LE 行」混排。按 UTF-8 读时 NUL 字节是合法字符，
> 所以早期「0 个替换字符」是假阴性。本次改用 `Out-File -Append -Encoding utf8` 逐行写入，
> 新增段 NUL 字节为 0。

### 5. 单实例控制（批准项 9）

| 手段 | 结果 |
| --- | --- |
| 任务计划重复触发（22:40:54） | `LastTaskResult=0`，PID 仍为 20400，进程数 **1** |
| 锁文件独占 | 外部 `ReadAllBytes` 读取 `supervisor.lock` 失败（被持有） |
| `status` | `running=True, lock=True` |

### 6. 任务接收（批准项 9）

为不写入数据库，采用「重复投递已处理任务」验证接收链路：

- 把已归档的 `p00_p05b_login_01.ready.json` 重新放入 inbox
- 结果：日志 `推迟任务 p00/p00_p05b_login_01：ALREADY_PROCESSED: 任务已处理过（state=done），不重复执行`
- `counts.deferred = 1`，文件进入 `deferred/`，inbox 清空
- **`p00.db jobs = 4 → 4`，未产生新任务**

### 7. 数据与配置（安全要求）

| 项目 | 结果 |
| --- | --- |
| `p00.db` | `quick_check=ok`，`jobs=4`（与修复前一致） |
| `p02.db` | `quick_check=ok`，`p02_jobs=5`、`p02_rounds=9`、`p02_worktrees=6`（与修复前一致） |
| 文件基线 | 290 → 313：新增 23、缺失 **0**、变化 5 |
| 变化明细 | `.control/p00.db`（上轮 P00 任务的 WAL 落盘，jobs 未变）、`supervisor.py`、`soloship_start.ps1`、`test_p05b_supervisor.py`、`P05B-supervisor.md` |
| `github.enabled` | `false`（`allow_merge=false`、`allow_force_push=false`、`require_env_token=true`） |
| `config/bridge.json` 含 token | 否（本轮未改动该文件） |
| `WINDOWS_TEST_MODE` / `isolation_claim` | `true` / `NONE`，未改写 |
| PR / 合并 / 部署 | 无 |

### 8. 全量回归

`python -m unittest discover -s tests` → **Ran 264 tests in 172.828s — OK**
（259 → 264：`TestStdioEncoding` 3 项、`TestStateWriteRace` 2 项）

## Remaining

1. **`start` 的 ready 判定有窗口**：守护进程可能在 `start` 打印 `ok:true` 之后立刻退出（§3）。
   建议 P05-C 在 `cmd_start` 里加一次「ready 后 0.5s 复核 lock + pid_alive」，或让
   `start` 返回前做存活二次确认。本轮未改，避免超出批准范围。
2. `counts.deferred = 1` 是 §6 去重测试的产物，不是失败；`deferred/` 里有 1 个文件。
3. `supervisor-boot.log` 前 1311 字节仍是历史 GBK 段落（按要求不重写）；需要统一时
   应做归档而非就地改写。
4. `--force` 强杀、推迟任务不自动恢复，仍属 P05-C。
5. `bridge/__pycache__/supervisor.cpython-314.pyc` 为 `py_compile` 语法检查产物。

## 当前状态

Supervisor **后台运行中**：PID **20400**，由任务计划程序于 22:40:33 拉起，
心跳正常（4.7s 前），`running=True / lock=True`，队列 `P00=0 / P02=0`，
`recovery_pending=0`，`Codex 就绪=True`，执行模式 `WINDOWS_TEST_MODE`。
未完成或等待恢复的任务：**无**。

## Evidence

```
.control/supervisor/state.json                             pid=20400, state=running
.control/supervisor/logs/supervisor-boot.log               新增 303B 严格 UTF-8
.control/supervisor/logs/start-script.log                  新增 955B 严格 UTF-8
.control/supervisor/logs/supervisor-20261023.log           ALREADY_PROCESSED 记录
.control/supervisor/deferred/p00__p00_p05b_login_01.ready.json  去重产物
bridge/supervisor.py                                       force_utf8_stdio + 写入重试
scripts/soloship_start.ps1                                 编码设置 + 逐行 UTF-8 落盘
tests/test_p05b_supervisor.py                              +5 用例
```
