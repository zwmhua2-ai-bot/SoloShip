# RUNBOOK — 实际操作手册（Windows）

所有命令均按本仓库当前代码核对（核实时间 2026-10-24 01:15）。
参数可能随版本变化，执行前可用 `<脚本> --help` 确认。

约定：

```powershell
# PowerShell
$REPO = 'E:\Documents\AI-Developer\repositories\SoloShip'
$PY   = 'C:\Users\hua\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location $REPO
```

```bash
# Git Bash
cd /e/Documents/AI-Developer/repositories/SoloShip
PY="/c/Users/hua/AppData/Local/Programs/Python/Python314/python.exe"
```

---

## 1. 环境检查

```powershell
$PY -V                       # 期望 Python 3.14.x
codex --version              # 期望 codex-cli 0.163.0
$PY bridge\supervisor.py doctor --root $REPO
```

`doctor` 是只读自检：报告 Codex 是否可用、登录态、`CODEX_HOME` 是否保留、
`gh_token_in_codex_child` 是否为 False、`publish_gate` 状态、沙箱与执行模式。
它**不会**启动服务，也不会写入凭据。

## 2. Supervisor 启停

```powershell
$PY bridge\supervisor.py status  --root $REPO   # 状态（未运行时退出码 4）
$PY bridge\supervisor.py start   --root $REPO   # 后台启动（已有实例返回 3）
$PY bridge\supervisor.py stop    --root $REPO --timeout 30
$PY bridge\supervisor.py restart --root $REPO
$PY bridge\supervisor.py recovery --root $REPO  # 查看待恢复任务（P05-C 输入）
```

退出码：`0` 成功 / `3` 已在运行 / `4` 未运行 / `5` 启动失败 / `6` 停止超时 / `7` 自检阻断。

登录自启（需显式 `-Approve`）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$REPO\scripts\install_startup.ps1"            # 预览
powershell -NoProfile -ExecutionPolicy Bypass -File "$REPO\scripts\install_startup.ps1" -Approve   # 注册
powershell -NoProfile -ExecutionPolicy Bypass -File "$REPO\scripts\install_startup.ps1" -Uninstall -Approve
```

经任务计划启动/核对：

```powershell
Start-ScheduledTask -TaskName 'SoloShip Supervisor'
Get-ScheduledTaskInfo -TaskName 'SoloShip Supervisor' | Select-Object LastRunTime, LastTaskResult
(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*supervisor.py _run*' }).Count    # 期望 1
```

> `LastTaskResult=0` 不能单独证明进程存活，必须配合 `status` 与进程数。

## 3. 安全门禁检查

```powershell
# 执行模式（SANDBOX_VERIFIED / WINDOWS_TEST_MODE / BLOCKED）
$PY -c "import sys;sys.path.insert(0,'bridge');import security,pathlib;print(security.execution_mode(pathlib.Path('.')));print(security.require_auto_dev(pathlib.Path('.')))"

# 发布相关开关（只读查看）
$PY -c "import json;c=json.load(open('config/bridge.json',encoding='utf-8'));g=c['github'];print({k:g[k] for k in ('enabled','allow_merge','allow_force_push','require_env_token')})"
```

投放前体检：

```powershell
$PY bridge\p03d_guard.py validate-task --help    # 参数以 --help 为准
$PY bridge\p03d_guard.py precheck-inbox --help
```

**不要**为了"让任务跑起来"去打开 `github.enabled` 或临时开启沙箱豁免——那是安全决定，需人工授权。

测试命令授权（SS-V01-003）自查。授权清单在 `config/bridge.json` →
`test_command_policy.approved`，任务 JSON 只能引用：

```powershell
# 看当前允许哪些测试命令
$PY -c "import json;c=json.load(open('config/bridge.json',encoding='utf-8'));print(c['test_command_policy']['approved'])"

# 用真实判定逻辑试一条命令（把列表换成待验证的 argv）
$PY -c "import sys,pathlib;sys.path.insert(0,'bridge');import test_policy;print(test_policy.resolve(pathlib.Path('.'),['py','-3','-m','unittest','discover','-s','tests','-v']))"
```

返回 `(True, argv, '', 'config')` 表示放行；第三项非空即为拒绝原因。
**策略段缺失 / 非法 / `approved` 为空 → 一律拒绝**，这是设计意图，不要用"临时删掉校验"来绕过。

## 4. 测试

```powershell
$PY -m unittest discover -s tests                 # 全量（当前基线 382 项）
$PY -m unittest tests.test_p02_finalize -v        # 单个测试文件
```

- **不要加 `-t .`**：`tests/` 无 `__init__.py`，会报 `Start directory is not importable`。
- 涉及 Codex 的用例使用 `tests/fake_codex.py`，不会调用真实 Codex。

## 5. P02 恢复

```powershell
$PY bridge\p02_bridge.py --root $REPO --recover
```

输出 JSON：`scanned / recovered / needs_human / noop / manifests / missing_reports`。

- 只做本地重放：补报告、补归档、推进状态；**不调用 Codex、不发布 PR**。
- 内容冲突或证据不足 → 进入 `P02_NEEDS_HUMAN`，同时在
  `.control/p02/finalizing/<job_id>.needs_human.json` 留痕，**不会**覆盖已有文件。
- 幂等：重复执行结果收敛。

## 6. Git 状态检查

```powershell
git -C $REPO rev-parse HEAD
git -C $REPO rev-parse --abbrev-ref HEAD
git -C $REPO status --short
git -C $REPO log --oneline -10
git -C $REPO diff --stat
```

提交前必看 `git status --short` 与 `git diff --stat`，确认改动范围与任务卡一致。

## 7. 常见故障

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `[SANDBOX-BLOCKED] ... 仅 P00 文件交接可用` | 执行模式为 `BLOCKED` | 预期行为。需人工授权后才可开发，不要绕过 |
| `start` 返回 3 | 已有实例在运行 | 正常；任务计划场景下脚本会映射为 0 |
| `ALREADY_PROCESSED` 出现在日志 | 同一 `request_id` 重复投递 | 文件被移入 `.control/supervisor/deferred/`，不重跑 |
| `UNFINISHED_CLAIM` | 上次认领未收尾 | 按 P05-C 策略只登记不重跑，需人工处理 |
| `[P02_FINALIZE_ERROR]` | 交付物/归档/状态写入失败 | Journal 已保留，跑 `--recover` |
| `P02_NEEDS_HUMAN` + `.needs_human.json` | 证据冲突或不足 | 人工核对 `.control/p02/finalizing/` 与报告，不要伪造 |
| `P02_BLOCKED_TEST_COMMAND` / `P01_BLOCKED_TEST_COMMAND` | 测试命令未获可信配置授权 | 看报告里的 `test_command_policy.reason`；要放行必须改 `config/bridge.json` 的 `approved`（需人工授权），**不要**改任务 JSON 或删校验 |
| 所有任务都被拒，理由是"策略缺失" | `config/bridge.json` 缺 `test_command_policy` 段 | 迁移/新建环境后忘记同步配置；补上 `approved` 段即可，不要回退代码 |
| 拒绝理由含"解释器所在目录不受信任" | 解释器来自 PATH 靠前的用户目录，或另一套未登记安装 | 这是 PATH 劫持防护（SS-V01-003-R1）。确认该解释器可信后，把它的目录写进 `trusted_interpreter_dirs`；不要退回去信任 PATH |
| `P02_BLOCKED_REVIEW_INVALID`，报告里出现"审查版本不一致/长度非法/非法字符" | Reviewer 回传的 candidate_sha 不是完整一致的对象 ID（前缀、截断、加后缀、其他 SHA、空值） | SS-V01-004 的严格绑定：只有完整一致 SHA 才算数。检查 `reviews/<key>/review.last.json`；若确属模型偶发，重跑任务即可，不要放宽校验 |
| `PUBLISH_BLOCKED_REMOTE`（详情含"无法可靠读取远端 base"） | `ls-remote` 失败、目标分支不存在或返回值不是合法对象 ID | SS-V01-005：读不到 base 一律拒绝。确认远端可达、分支存在后重试；不要为跑通而跳过该校验 |
| `PUBLISH_BLOCKED_PR_STATE` | 已有 PR 不是 OPEN Draft、分支不符、查询失败/结果不完整/有歧义 | 交人工：核对 `.control/p03/publish/<job_id>.json` 与远端 PR 状态，不要自动重开或另建 PR |
| `PUBLISH_BLOCKED_BASE_MOVED` | 远端 base 与预期不一致，或发布过程中发生变化 | 重新评估候选是否仍可干净合入；必要时重跑任务 |
| `PUBLISH_BLOCKED_PR_STATE`（详情含"跨 Fork"/"来源仓库不符"/"来源.*字段缺失"/"达到上限"） | PR 来自其他仓库、来源身份字段缺失，或查询命中数达到 `--limit` 上限 | SS-V01-005-R1：不把其他 Fork 的同名分支 PR 当作自己的 PR；核对远端 PR 归属后由人工处理，不要放宽身份校验 |
| `PermissionError [WinError 5] ... state.json` | 读写与原子替换撞车 | 已由 `_write_json` 退避重试处理；若仍出现，检查是否有外部工具锁文件 |
| 日志中文乱码 | 历史 GBK 段落 | `supervisor-boot.log` 前若干字节是旧记录，新写入均为 UTF-8；不要就地重写历史 |
| `Start directory is not importable` | 测试命令带了 `-t .` | 去掉 `-t .` |
| 计划任务 `LastTaskResult=0` 但没进程 | 曾出现过启动即退 | 以 `status` + 进程数为准，查 `logs/supervisor-boot.log` |

## 8. 备份与恢复

**备份**

- 源码与文档：`git clone` 或 `git bundle`（含历史，推荐）。
- 运行数据（`.control/`、`exchange/`）：**不要**直接复制 SQLite 文件（WAL 模式下可能拿到半写状态），
  用 SQLite 备份 API：

```powershell
$PY -c "import sqlite3;s=sqlite3.connect('file:.control/p02/p02.db?mode=ro',uri=True);d=sqlite3.connect('p02-backup.db');s.backup(d);d.close();s.close()"
```

- 打包时必须排除：`.control/`、`exchange/`、`.worktrees/`、`demo/`、`__pycache__`、`config/*.bak-*`。

**恢复**

1. 先 `stop` Supervisor，避免边写边还原；
2. 恢复代码 → `git status --short` 确认干净；
3. 恢复数据库后跑 `pragma quick_check`；
4. `--recover` 对账（期望 `needs_human`、`missing_reports` 为空）；
5. 全量测试；
6. 再 `start`。

## 9. 迁移/搬迁注意

- worktree 的 gitdir 是绝对路径，搬目录后必须在**母仓库**里执行
  `git worktree repair <新路径...>`，否则工作树失效。
- 计划任务与两个 PS 脚本里的默认 `$Root` 都写死了绝对路径，搬迁后需同步修改并重新注册任务。
