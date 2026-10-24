# RUNBOOK — 实际操作手册（Windows）

所有命令均按本仓库当前代码核对（核实时间 2026-10-24 01:15）。
参数可能随版本变化，执行前可用 `<脚本> --help` 确认。

> **发布说明（本同步分支）**：本分支 `docs/ss-v01-handoff-public` 仅同步 4 份交接文档（含本文件），基于真实 `origin/main`（`d524991`）。文中引用的详细交付报告与外置验收工具目录 `scripts/release002_acceptance/`（F-01/F-02 修复）**仅存在于本地分支、尚未发布到 `main`**；下文 `$REPO` / `$PY` 等路径与解释器均因部署而异，请按实际环境替换。

约定：

```powershell
# PowerShell
$REPO = '<本仓库根目录，请按实际部署替换>'
$PY   = '<本机 Python 解释器路径，期望 3.14.x>'
Set-Location $REPO
```

```bash
# Git Bash
cd <仓库根目录>
PY="<本机 Python 解释器路径>"
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
$PY -m unittest discover -s tests                 # 全量（当前基线 414 项）
$PY -m unittest tests.test_p02_finalize -v        # 单个测试文件
```

- **不要加 `-t .`**：`tests/` 无 `__init__.py`，会报 `Start directory is not importable`。
- 涉及 Codex 的用例使用 `tests/fake_codex.py`，不会调用真实 Codex。

## 5. P05-C：Supervisor 中断恢复（SS-V01-006）

恢复由 Supervisor 启动路径自动执行（`start --foreground` 与后台 `_run` 都会在
`detect_unfinished()` 之后调用 `recover_on_start()`），也可手动只读查看。

```powershell
# 只读查看：待恢复清单 + 恢复摘要 + 需人工处理明细（不修改任何状态）
$PY bridge\supervisor.py recovery --root $REPO

# 启动即恢复（前台，便于观察）
$PY bridge\supervisor.py start --foreground --max-cycles 1 --root $REPO
```

分级规则（证据越弱越保守，**宁可停止**）：

| 分类 | 判定 | 动作 |
| --- | --- | --- |
| ACTIVE | 认领持有者进程仍存活 | 不接管、不重复认领 |
| SAFE_TO_RESUME | P00 文件交接；或未入库且无 worktree；或 `finalizing/` Journal 完整；或 `finalized/` 清单完整；或终态且报告在位 | 认领转 `requeued`、任务文件回 inbox 由 P02 幂等处理；历史认领留在 `history` |
| NEEDS_HUMAN | 未入库但有 worktree；有 job 无 Journal；发布结果不可对账；终态但报告缺失；worktree 状态不一致 | 写 `.control/supervisor/needs_human/<kind>__<task>.json`，认领转 `needs_human`，**拒绝自动执行** |
| DEFERRED | 安全门禁 / Codex 不可用等停放任务 | 保持停放，不因重启自动放行或重新入队 |

互斥与就绪确认：

- 恢复持 `.control/supervisor/recovery.lock`（O_EXCL）；持锁进程失联才接管，
  两个恢复进程不会同时处理同一任务。
- 启动就绪 = 单实例锁被持有 + PID 存活 + 心跳 ≤ 30s；不满足则 `start` 明确失败，
  不显示虚假的 RUNNING。
- `status` 输出新增 `recovery` 摘要（分类计数、`needs_human` 数量、更新时间）。

**没有**“标记成功 / 强制放行”命令。人工处理完 `needs_human/` 下的条目后，需自行决定
后续动作并留下记录；不得伪造成功状态。

## 6. P04 验收证据矩阵（SS-V01-AC-001）

```powershell
# 生成/刷新某个任务的验收证据矩阵（只读原始证据，写 exchange/p04/acceptance/）
$PY bridge\acceptance.py --root $REPO --request-id <request_id>

# 只打印不落盘
$PY bridge\acceptance.py --root $REPO --request-id <request_id> --print
```

每条 AC 的证据矩阵字段：

| 字段 | 含义 |
| --- | --- |
| `ac_id` / `description` / `verification` | 来自原始任务，矩阵不得增删 |
| `candidate_sha` | 本条证据对应的代码版本（取任务的 `head_commit`） |
| `test_evidence` | 命令、退出码、用例名、命中用例、diff 中断言数、报告路径 |
| `review_evidence` | Reviewer 状态及其 `candidate_sha`、是否绑定同一提交 |
| `coverage_status` | `assertion_seen_in_diff` / `test_name_only` / `partially_covered` / `manual_pending` / `unverified` |
| `verified` | **恒为 False**：自动判定只是线索，不是验收通过 |
| `missing_evidence` | 明确列出缺什么（无 candidate SHA、测试未过、无用例名、diff 不可用、Reviewer 未绑定…） |
| `human_action` | 人工还需要做什么 |

硬规则：

- Reviewer 未回报 `candidate_sha` 或与 candidate 不一致 → `review_evidence.bound=False`，
  并在 risks 明示“审查证据不可采信”；**不伪造逐条 AC 审查结论**。
- `verification != automated` 的 AC 恒为 `manual_pending`，保留人工验收要求。
- `acceptance_verified` / `auto_merge` 恒为 False；矩阵不会触发任何合并或发布。
- 只写 `exchange/p04/acceptance/`，不改 P02 报告、回执与 Journal。

## 7. P02 恢复

```powershell
$PY bridge\p02_bridge.py --root $REPO --recover
```

输出 JSON：`scanned / recovered / needs_human / noop / manifests / missing_reports`。

- 只做本地重放：补报告、补归档、推进状态；**不调用 Codex、不发布 PR**。
- 内容冲突或证据不足 → 进入 `P02_NEEDS_HUMAN`，同时在
  `.control/p02/finalizing/<job_id>.needs_human.json` 留痕，**不会**覆盖已有文件。
- 幂等：重复执行结果收敛。

## 8. Git 状态检查

```powershell
git -C $REPO rev-parse HEAD
git -C $REPO rev-parse --abbrev-ref HEAD
git -C $REPO status --short
git -C $REPO log --oneline -10
git -C $REPO diff --stat
```

提交前必看 `git status --short` 与 `git diff --stat`，确认改动范围与任务卡一致。

## 9. 常见故障

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

## 10. 备份与恢复

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

## 11. 迁移/搬迁注意

- worktree 的 gitdir 是绝对路径，搬目录后必须在**母仓库**里执行
  `git worktree repair <新路径...>`，否则工作树失效。
- 计划任务与两个 PS 脚本里的默认 `$Root` 都写死了绝对路径，搬迁后需同步修改并重新注册任务。

## SS-V01-FINAL-FIX-001 / A 补充（2026-10-24）

Windows 单实例判定使用 Mutex 所有权和文件字节锁；Mutex 对象仍有句柄、锁文件仍存在，都不能单独证明 Supervisor 运行。探测只临时获取并配对释放，不改写持有者元数据。启动仍须同时满足锁、PID 和新鲜心跳。无需增加停止后的等待时间。

锁回归命令：`python -m unittest discover -s tests -p test_supervisor_mutex.py -v`。测试包装器显式传入绝对 Fake Codex；本任务执行时额外启用同级证据目录中的 `sitecustomize.py`（PYTHONPATH），拒绝缺失配置与真实 Codex/gh 子进程，并记录 commands.jsonl。它是测试保护和审计，不是操作系统级沙箱。

## SS-V01-FINAL-FIX-001 / B 补充（2026-10-24）

FINALIZING 恢复完成后必须保留 `.control/p02/finalized/<job_id>.json`。正常最终化与恢复均按：核对已有 Manifest → 补报告/归档/最终状态 → 原子安装 Manifest → 读回验证 → 清理临时 Journal。内容相同不改写；冲突、关键证据缺失或安装/读回/清理失败保留 Journal，并明确报告错误或 NEEDS_HUMAN。恢复不启动 Codex，不调用 Publisher。

存储要求：finalizing 与 finalized 应位于同一支持 hard link 的本地文件系统（本次 NTFS 已实测）。不支持 hard link、权限不足等错误不得用覆盖式 move/replace 绕过；应保留现场并人工处理。安装后、清理前中断会留下两个名字，这是可重放状态，不要手工删除其中任一证据。

组合回归：`python -m unittest discover -s tests -p test_finalizing_manifest.py -v`。设置 `SOLOSHIP_EVIDENCE_DIR` 可在指定测试证据目录保存组合 E2E 的 Manifest、报告与结果摘要。全量命令仍为 `python -m unittest discover -s tests`；本次真实网络凭据负例通过外置测试保护明确 skip，不能据此宣称真实 GitHub 凭据边界已复验。

### 本次回归的 PATH 条件（2026-10-24）

全量的裸 `python` 规范化测试若因 WindowsApps 等目录不受信任而 skip，不应修改白名单或放松策略。可在**独立补测进程**的 PATH 首位指定当前 `sys.executable` 所在目录，再运行原测试；该目录本来已受信任，PATH 仅改变定位。本次这样补测成功，原 skip 与补测日志均保留。真实 GitHub 网络负例继续 NOT_TESTED，无额外真实网络授权。

## SS-V01-FINAL-CONC-002 R2：P02 共享互斥与终态保护

本节仅适用于 R2 修复版本。P02 的 CLI、Bridge 直接恢复（包括缺失报告扫描/低层重放）、Supervisor 启动时两处 P02 重放，以及正常任务/最终化共用 `.control/p02/recovery.lock`。Windows 使用 `msvcrt.locking` 文件字节锁；同线程嵌套重入，其他进程/Bridge/线程争锁立即失败。载体是持久文件，存在不表示锁被持有，**不得删除它来解锁**；进程退出后由 OS 释放。

锁顺序：Supervisor 单实例锁 →（若在 recover_on_start 内）Supervisor recovery.lock → P02 锁；正常任务 request lock → P02 锁。P02 不获取前述外层锁，不形成反向依赖。正常任务在入库前取得 P02 锁，并持有到完成，避免执行后争锁失败且无 Journal。运行中的任务会使外部恢复返回 busy。

CLI `--recover` 争锁时输出 `{"state":"busy","reason":"P02_RECOVERY_BUSY: ..."}`，退出码 **3**，表示本次没有恢复。直接调用抛 `RecoveryBusy`；Supervisor 的原有恢复错误摘要会带此原因，可稍后重试；不得把 busy 当成功。退出码 0 的摘要仍需核对 `needs_human/missing_reports`，不等于自动验收通过。

正常调度遇到 P02_BUSY 时，Supervisor tick 将 claim 转回现有 requeued 状态并保留 history、inbox，增加 skipped 而不增加 processed；后续周期可重试。这里不需要倒序获取锁或把忙碌状态算作任务完成，单实例锁与启动恢复入口均未改变。

最终化顺序固定为 Journal → DB FINALIZING → 交付物与归档 → 安装 Manifest 并读回核验 → DB 条件确认终态 → 清理 Journal。安装或核验失败保留 Journal；Manifest 成功但 DB 失败时两份证据保留；终态已提交后的清理失败只诊断，不降级 READY/BLOCKED。Journal 消失时，只有完整校验 Manifest、身份/Candidate、Reviewer/测试与交付物后才返回 `already_recovered`；缺失或冲突仍明确交人工。

历史终态扫描异常不会改库，也不覆盖 Manifest/报告；人工诊断 Marker 保留原件，新发现的原因在本次摘要/日志中返回。合法 Manifest 仍可逐字节补齐缺失交付物。仅有 Manifest 而 DB 尚未完成时会继续最终化；没有证据不伪造成功。

测试复现命令（在隔离开发 worktree 执行，不指定生产根）：

```powershell
python -m unittest discover -s tests -p 'test_recovery*.py' -v
python -m unittest discover -s tests -p test_finalizing_manifest.py -v
python tests/recovery_concurrency_probe.py --evidence <全新且位于源码目录外的证据目录>
```

探针由 CONC-001 移植，使用独立 spawn 进程和 Pipe 屏障；正常夹具显式 Fake Codex、禁用发布，恢复子进程禁止 Codex/Publisher/网络/子进程启动。双 CLI 为独立 OS 进程调用真实 CLI main；Supervisor 组合调用 detect_unfinished/recover_on_start，不启动生产常驻实例。10 轮交替测试 CLI/CLI 与 Supervisor/CLI，证据目录必须全新。

生产升级需单独授权，并确保旧版与新版不并行操作同一运行根：旧进程不会遵守新增锁。保持原 hard link 同文件系统限制；本轮只验证进程异常退出，断电级目录项持久性、其他文件系统和 Linux 未验证。


## SS-V01-RELEASE-002-R1 第一阶段（2026-10-24）

仅本卡隔离验收：scripts/release002_r1_launcher.py 的 adapter_options(cli, options) 通过现有 R2 extra_args 生成全部后置 -c；保留 ignore，不传前置配置对。先做 Mock argv、exec --help、空 home 无 turn/start 配置初始化、原生文件边界核验。完整 exec 需另获真实模型授权；必须新 runtime/request_id，禁止删旧 launch-once/重置数据库。再现 read-only/blocked by policy 就停，不扩大权限。

完整结果见 [第一阶段交付](SS-V01-RELEASE-002-R1-STAGE-1.md)。


## SS-V01-RELEASE-002-R1 阶段二停止（2026-10-24）

本次停止runtime只能读证据；DB真实ACCEPTED、无Receipt/Report/Manifest，不得以恢复/强改终态补造交付。原生acceptance.build因无回执索引返回找不到任务；evidence/stopped-ac-matrix为明确标记的缺失证据表，4 unverified + 1 manual_pending。命令扫描和即时预检控制缺陷需另行授权修复，剩余模型预算不得用作重试授权。

完整证据见 [停止交付报告](SS-V01-RELEASE-002-R1-STAGE-2.md)。


## SS-V01-RELEASE-002-R2（2026-10-24）

仅无模型回归入口：python -B -m unittest discover -s scripts/release002_acceptance -p 'test*.py' -v。launch.ps1实际Windows测试仅调用临时Python探针；其存在不构成真实E2E授权。外置runner副本未绑定新运行根，不得直接运行或复用旧任务；先为未来卡准备全新runtime/request_id/Job。解析器只支持有界字面量PowerShell子集，遇不支持调用须安全失败。（注：`scripts/release002_acceptance/` 为本地分支 `ss-v01-release-002-r2/observer-gate` 中的外置验收工具副本，尚未发布到 `main`，请勿按已发布内容对待。）

完整报告：[SS-V01-RELEASE-002-R2.md](SS-V01-RELEASE-002-R2.md)。

## 12. 验收工具与真实 E2E 安全注意事项（SS-V01-RELEASE-002-R2，2026-10-24）

下列为已证实的规则与安全边界，仅记录事实，不推测：

1. **Windows 下 Codex CLI 的安全参数必须按已验证方式统一放在 `exec` 之后**（R2 实测：`af5177b` 后所有 `-c` 均在 `exec` 后，真实 Builder 获得 `workspace-write`）。跨子命令放置会导致 Windows 沙箱配置丢失、权限降为 read-only。
2. **使用真实模型前必须检查实际生效的沙箱策略，而不是仅检查请求参数**：R2 阶段二以原生日志 `Windows sandbox selection: unelevated` 与 `sandbox_policy` 实测为准，而非只看请求字段。
3. **外部观察器不能简单扫描整条命令字符串判断进程身份**：对整条命令做 `\bcodex\b` 匹配会把合法 PowerShell 可执行文件目录 `codex-runtimes` 误判为启动额外 Codex（F-01）。应区分可执行文件、路径与嵌套命令（有界 AST 解析）。
4. **预检异常必须阻止执行器启动**：预检以默认编码读 UTF-8 JSON 异常后，PowerShell 控制流仍启动 runner 属控制缺陷（F-02）。任何预检异常、非零退出码或无效结果都必须阻止执行器启动。
5. **异常中断的独立测试根不得直接交给 Supervisor 恢复**：`release002_r1_real_codex_001` / `p02_f98f3ad93e91` 中断后留有一个未最终化 `ACCEPTED` 任务；禁止让新 Supervisor 自动接管该运行根、改终态或重跑。
6. **每次真实 E2E 都要使用全新授权、独立运行根及独立任务 ID**：禁止复用旧 `launch-once` / `scope` / 历史硬编码 runner；后续验收必须新 `runtime` / `request_id` / `Job`。
7. **`WINDOWS_TEST_MODE` 不等于经过验证的完整 OS 沙箱**：`isolation_claim=NONE`，观察/取消机制不是 OS 级隔离，也不能保证事件到达前命令尚未执行（见 `AGENTS.md` §7）。
8. **不得将真实模型、GitHub 写入和生产操作视为默认授权**：`github.enabled=false` 且系统无合并/强推代码路径；真实模型、GitHub 写入、生产 Supervisor 升级均须显式人工授权。

仅无模型回归入口（不触发真实 E2E；`scripts/release002_acceptance/` 为本地分支 `ss-v01-release-002-r2/observer-gate` 中的外置验收工具副本，尚未发布到 `main`）：

```powershell
# scripts/release002_acceptance/ 尚未发布到 main，请勿按已发布内容对待
python -B -m unittest discover -s scripts/release002_acceptance -p 'test*.py' -v
```
