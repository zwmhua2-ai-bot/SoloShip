# SoloShip 进度记录（PROGRESS）

> 本文件是**阶段与任务进度**的主记录。
> 现场状态（PID、心跳、当前分支）看 `docs/AI_HANDOFF.md`；过程与技术决策看 `docs/DEVLOG.md`。

> **发布说明（本同步分支）**：本分支 `docs/ss-v01-handoff-public` 仅同步 4 份交接文档（含本文件），基于真实 `origin/main`（`d524991`）。文中引用的详细交付报告（`docs/SS-V01-RELEASE-002-*.md`、`docs/SS-V01-FINAL-*-DELIVERY.md`）与外置验收工具目录 `scripts/release002_acceptance/`（F-01/F-02 修复）**仅存在于本地分支、尚未发布到 `main`**；仓库路径与解释器路径均因部署而异。请勿据此认为上述内容已公开发布。

---

## 0. v0.1 发布状态（2026-10-24 文档同步更新 · SS-V01-HANDOFF-SYNC-001）

> 本节为 v0.1 收尾的权威状态摘要；详细证据见 `docs/AI_HANDOFF.md`、`docs/DEVLOG.md` 与各交付报告。

- **v0.1 核心功能开发基本完成**：P00–P05B 与 SS-V01-001～AC-001（安全门禁、测试授权、Reviewer SHA 绑定、Publisher 门禁、P05-C 恢复、AC 证据矩阵）均已交付。
- **R2 发布候选已建立，但尚未部署**：`release/v0.1.0-rc1` @ `3b8c153a3650dd659b042f2e46f7ac6c11cf6c23`；生产 `main` 仍停在 `d524991f3aceadcbd361b75d19ce08499e27f20a`，v0.1.0 **未发布**。
- **最后已报告的 R2 回归结果**：RELEASE-002-R1 第一阶段（CLI 修复，`af5177b`）＝ **454 PASS / 1 SKIPPED**（455 项）；并行卡 FINAL-CONC-002 R2（`cdcbc3d` / `ffec835`）＝ **453 PASS / 2 SKIPPED**（455 项）。主产品全量基线仍是 **414 OK**（`main`，SS-V01-AC-001）。**以上均为历史本机实证，本次文档任务（SS-V01-HANDOFF-SYNC-001）未执行任何测试，不得当作新结果。**
- **CLI 参数布局修复已完成**：`-c` 统一置于 `exec` 之后（`af5177b`），真实 Builder 会话已验证获得正确的 Windows `workspace-write` 沙箱权限（unelevated / never / network=false），详见 `docs/SS-V01-RELEASE-002-R1-STAGE-2.md`。
- **完整 Builder → 测试 → Reviewer E2E 仍未通过**：RELEASE-002-R1 阶段二被外置观察器误报中断（`6ceae33f`）；RELEASE-002-R2（`d4c87bc`）仅完成外置验收工具（F-01/F-02）修复与 28 项无模型测试，**真实 E2E 仍 `NOT_TESTED`**。
- **当前尚不能宣布 v0.1.0 正式发布**：发布前置条件（真实 E2E 通过、经授权升级生产 Supervisor、经授权完成 GitHub Draft PR 真实验收）均未满足。

## 1. 阶段进度

| 阶段 | 内容 | 状态 | 证据 |
| --- | --- | --- | --- |
| P00 | ChatGPT Work 交接（ready.json → 回执） | 完成 | `exchange/reports/demo-app__p00_*.report.md` |
| P01 | Codex CLI 接入与环境隔离 | 完成 | `exchange/p01/reports/*.report.md` |
| P02 | 开发闭环（worktree → 实现 → 测试 → 审查 → 修复轮次） | 完成 | `exchange/p02/reports/*.report.md` |
| P02-B | 模型路由（fast / balanced / deep） | 完成 | `exchange/p02/reports/P02B-DELIVERY.md` |
| P02-S | 沙箱与 D 盘预检 | 完成 | `exchange/security/P02S*.md` |
| P03 | Draft PR 发布（只读守卫 + 配置恢复） | 完成 | `exchange/p03/P03-DELIVERY.md` |
| P03-D | 实任务：TodoList `remove()` + Draft PR #1 | 完成 | `exchange/p03/P03D-*.md` |
| P04 | 状态索引 / 集成指令 / 验收助手 | 完成 | `docs/P04-完成报告.md` |
| P05-A | GitHub 实时状态（只读） | 完成 | `docs/P05A-完成报告.md` |
| P05-B | 后台 Supervisor + 登录自启 + 单实例 | 完成 | `docs/P05B-*.md` |
| P05-C | 未收尾任务恢复 / 并行扩展 / `start` 存活复核 | **未开始** | — |

> P00–P05B 在纳入 git 之前完成，没有独立提交，详见 `docs/DEVLOG.md` 的说明。

## 2. 任务卡进度

| 任务卡 | 内容 | 状态 | 提交 |
| --- | --- | --- | --- |
| SS-V01-001 | 统一自动开发安全门禁 | 完成 | `b1b5430` |
| SS-V01-002 | P02 最终化一致性与报告恢复 | 完成 | `f407d88` + `c63b926` |
| SS-V01-002-R1 | 已完成任务的报告丢失恢复 | 完成 | `1e3f81e` |
| （迁移） | 仓库迁至 E 盘并修正路径依赖 | 完成 | `b754467` |
| SS-DOC-001 | AI 开发交接与过程记录体系 | 完成 | `29401af` |
| SS-DOC-002 | 交接文档一致性修正（Git/安全/worktree/缺口） | 完成 | 见交付报告 |
| SS-V01-003 | 测试命令授权加固（`test_command_policy`） | 完成 | `dc25135` |
| SS-V01-003-R1 | 测试解释器可信路径验证（PATH 劫持 / 执行路径一致性） | 完成 | `66cb2ef` |
| SS-V01-004 | Reviewer Candidate SHA 严格绑定与审查证据校验 | 完成 | `0db54fc` |
| SS-V01-005 | GitHub Publisher 发布门禁与幂等性加固 | 完成 | `0fa7f45` |
| SS-V01-005-R1 | GitHub PR 来源仓库身份验证 | 完成 | `7eb8988` |
| SS-V01-006 / P05-C | Supervisor 中断恢复与安全重调度 | 完成 | `6970901` |
| SS-V01-AC-001 | 逐条 AC 验收证据矩阵 | 完成 | 见交付报告 |
| P05-C | 未收尾任务恢复 / 并行扩展 / `start` 存活复核 | 未开工，未获批 | — |

## 3. 测试基线演进

| 节点 | 用例数 |
| --- | --- |
| P04 收尾 | 191 |
| P05-A | 233 |
| P05-B | 257 → 259（`pid_alive`、退出码修复） |
| 日志编码修复 | 262 |
| SS-V01-001 | 273 |
| SS-V01-002 | 285 |
| SS-V01-002-R1 | 289 |
| SS-V01-003 | 315 |
| SS-V01-003-R1 | 323 |
| SS-V01-004 | 346 |
| SS-V01-005 | 374 |
| SS-V01-005-R1 | 382 |
| SS-V01-006（P05-C） | 402 |
| SS-V01-AC-001 | **414**（主产品基线，`main`） |
| RELEASE-002-R1 STAGE-1（CLI 修复，`af5177b`） | 455 项 / **454 PASS / 1 SKIPPED** |
| FINAL-CONC-002 R2（`cdcbc3d` / `ffec835`） | 455 项 / **453 PASS / 2 SKIPPED** |

当前基线：`414 OK`（2026-10-24 09:21，`python -m unittest discover -s tests`，SS-V01-AC-001 实测，主产品 `main`）。
R2 线在独立分支各自实测：RELEASE-002-R1 STAGE-1 = 454 PASS / 1 SKIPPED（`af5177b`）；FINAL-CONC-002 = 453 PASS / 2 SKIPPED（`cdcbc3d` / `ffec835`）。
RELEASE-002-R2（`d4c87bc`）只改外置验收工具，**未重跑产品全量**；其 28 项工具回归（17 新 + 11 原）为外置脚本测试，不计入产品基线。
本次文档任务（SS-V01-HANDOFF-SYNC-001）未运行任何测试，上述数字均为历史实证，不得当作新结果。

## 4. 报告索引

**模块总报告（docs/）**

| 报告 | 内容 |
| --- | --- |
| `docs/P04-完成报告.md` | 状态索引、集成指令、验收助手三态判定 |
| `docs/P05A-完成报告.md` | 只读实时状态、`state_basis`、防降级 |
| `docs/P05B-supervisor.md` | Supervisor 命令、单实例、调度、目录 |
| `docs/P05B-login-verification-report.md` | 登录自启真机验证 |
| `docs/P05B-utf8-fix-report.md` | 日志编码修复与重启验证 |

**任务级报告（exchange/）**

| 路径 | 内容 |
| --- | --- |
| `exchange/reports/demo-app__p00_*.report.md` | P00 交接回执（4 份） |
| `exchange/p01/reports/*.report.md` | P01 执行报告 |
| `exchange/p02/reports/*.report.md` | P02 / P02-B / P03-D 执行报告 |
| `exchange/p03/P03-DELIVERY.md`、`P03D-*.md` | 发布流程与首个实任务 |
| `exchange/p04/acceptance/p03d_remove_002.acceptance.md` | 验收明细（AC-04 `partially_covered`） |
| `exchange/security/P02S*.md` | 沙箱与环境评估 |
| `exchange/status/WORK-INSTRUCTIONS.md` | 面向 ChatGPT Work 的消费规则 |

**机器可读状态**

`exchange/status/latest.json`、`index.json`、`projects/demo-app.json`、
`realtime/p03d_remove_002.json`、`.control/p00.db`、`.control/p02/p02.db`。

## 5. 当前交付状态

- 业务侧 PR #1（`soloship/p03d_remove_002`）为 **Draft、未合并**；
  P04 验收结论 AC-04 = `partially_covered`、`verified=False`，需人工复核。
- `github.enabled=false`：发布被 `github_publisher.publish()` 短路，不会发起远端写操作。
  自动合并与强制推送**不是被开关拦住，而是系统没有实现**
  （`allow_merge` / `allow_force_push` 无任何代码读取，详见 `AGENTS.md` §7）。
- 测试命令自 SS-V01-003 起由 `config/bridge.json` 的 `test_command_policy.approved` 授权：
  任务 JSON 只能引用，不能扩权；策略缺失或非法一律拒绝执行测试（fail closed）。
- 流水线侧提交位于本地 `main`（HEAD `29401af`，核实 2026-10-24 01:53），
  比本地 `origin/main` 引用（`24d0337`）领先 6 个提交，**未推送**。
  远端在此期间是否有新提交**未实时确认**，按 UNKNOWN 处理；分支细节见 `docs/AI_HANDOFF.md` §1。

## 6. 后续候选（均未获批）

1. P05-C：未收尾/推迟任务的恢复策略、`cmd_start` 存活二次确认、并行扩展。
2. 收尾 PR #1：补 AC-04 证据 → 转 Ready → 人工评审。
3. 清理历史遗留：`NEEDS_HUMAN` 复位入口、旧目录删除、4 条未推送的 demo 分支处置。
4. 补写架构设计说明与数据契约（见 §7 缺口登记）。
5. v0.2 方向讨论备忘录已入库 `docs/V02_ROADMAP_DRAFT.md`（讨论稿，**不构成开发授权**，待 v0.1 验收后重新评审）。

## 7. 文档缺口登记（SS-DOC-002 登记，未修复）

以下缺口已确认存在。本次**只登记不补写**，避免从旧文档臆造最新实现：

| 缺口 | 影响 | 现状证据 |
| --- | --- | --- |
| 缺少当前版本的架构设计说明（无 `DESIGN.md` / `IMPLEMENTATION.md`） | 新接手者只能靠 `AGENTS.md` §2 的模块表并逐个读 `bridge/*.py` 重建架构、调用时序与边界 | 仓库根目录无这两份文件；`README.md` 与 `AGENTS.md` 也未引用 |
| 缺少任务 JSON、数据库与 Journal 的数据契约说明 | 任务字段、`p00.db` / `p02.db` 表结构、`soloship.p02.finalizing/v1` Journal 结构只存在于代码里 | `schemas/` 仅含 `review.schema.json` |

补写规范：以**当前源码**为唯一依据，逐项与 `bridge/` 实现核对；
不得照抄历史报告，也不得凭记忆补全字段。

## SS-V01-FINAL-FIX-001 / A（2026-10-24）

- 独立 worktree、分支 `fix/ss-v01-final-fix-001`，基线 `d524991f3aceadcbd361b75d19ce08499e27f20a`。
- Supervisor Mutex 修复已自测，B 尚未实施，等待整卡独立验收。
- 新增 8 项锁/真实隔离进程回归通过（35.419s），P05-B/P05-C/实时状态既有 89 项通过（45.487s）。
- 10/10 轮立即启停成功，每轮第二实例返回 3；并发 `_run` 与并发 `start` 只确认一个有效实例。
- 证据：开发 worktree 同级 `SoloShip-final-fix-001-evidence/A-before.log`、`A-final.log`、`A-p05-regression.log`。

## SS-V01-FINAL-FIX-001 / B（2026-10-24）

- 恢复路径删除唯一 Journal 的缺陷已复现并修复；与正常最终化共用持久化规则，不改变 Journal schema。
- 新增 13 项回归通过（41.081s）；既有最终化/完成后恢复 16 项通过（44.552s）。
- E2E-07/08 组合实测：Fake Codex 完成执行与审查后，真实子进程在 FINALIZING 以 73 退出；恢复生成 Manifest；再删除隔离 report.json 后逐字节重建；恢复 Codex/Publisher 调用均为 0。
- B 修复前 11 项：9 FAIL、1 ERROR（清理异常未被处理），1 PASS；完整全量回归待两个修复提交后执行。
- 证据：同级 `SoloShip-final-fix-001-evidence/B-before.log`、`B-final.log`、`B-existing-regression.log`、`E2E-07-08/`。

## SS-V01-FINAL-FIX-001 最终自测收尾（2026-10-24）

- A 提交 `63eac82a526f6dc9b5bc913fba995287e028f14d`；B 提交 `d77ae6267eb30fe9df42a8ef5accab402305b791`。
- 全量实测 `Ran 435 tests in 446.276s — OK (skipped=2)`，退出码 0：433 PASS、2 skip，无失败/错误。PATH 条件跳过项在当前可信解释器目录的测试 PATH 下补测 PASS（1 项，0.061s），只剩真实 GitHub 网络负例 NOT_TESTED。
- 全量后复验 E2E-07/08 PASS（3.372s），E2E-10 再次 10/10 次立即启停 PASS（29.486s）。新增测试总计 21 项。
- A/B 全部验收项开发自测 PASS；整卡结论 PARTIAL（真实网络负例未验证），等待独立验收。未推送、未合并、未发布，不进入 v0.2。
- 完整报告：`docs/SS-V01-FINAL-FIX-001-DELIVERY.md`。前文 414“当前”与待全量描述均为旧快照；本节为最新结果。

## SS-V01-FINAL-CONC-002 R2（2026-10-24）

- 从 `301886d204be3d5f86565ec9d8e294adf51f9438` 的独立 worktree 实施，分支 `fix/ss-v01-final-concurrency-r2`；原 main 与生产运行目录未修改。
- F-00～F-03 均复现：共享 P02 文件字节锁、SQLite 条件终态保护、Manifest 安装并验证后再确认 DB、显式子进程导入路径已实现。
- 定向自测：P02 既有 16 项、Manifest 15 项、新增一致性与进程锁 17 项、P05 90 项通过；`python -I -S` 清洁环境 15 项通过；双进程并发 10/10 轮通过。
- Supervisor 仅增加 P02_BUSY → requeued 分支，保留历史且不计任务完成；争锁后可正常重试，未改变单实例/恢复锁或锁顺序。
- 全量及最终安全审计结果见 `docs/SS-V01-FINAL-CONC-002-R2-DELIVERY.md`；等待独立代码审查，不构成人工验收或发布许可。
- 最终全量 `455 tests / 428.506s — OK (skipped=2)`，退出 0：453 PASS、2 SKIPPED；PATH 条件项另行 0.061s 补测 PASS，真实 GitHub 网络负例 NOT_TESTED。比 435 基线新增 20 项。
- 最终完整分进程审计 5,070 条启动尝试，真实 Codex/gh 放行、Fake 路径违规、生产 Supervisor、真实 Git push 均 0；32 个恢复 worker 调用均 0，17 场景（含 10 轮）PASS。开发自测 PASS，整卡 PARTIAL（独立审查及未授权外部验证待进行），完成后停止。


## SS-V01-RELEASE-002-R1 第一阶段（2026-10-24）

第一阶段 PASS；5 项新启动器回归 PASS、R2 全量 455 项（454 PASS/1 SKIP）。全部 -c 统一 exec 后，未改核心/路由/凭据/全局配置。真实模型0；整卡真实 E2E NOT_TESTED，第二阶段等待新授权。

完整结果见 [第一阶段交付](docs/SS-V01-RELEASE-002-R1-STAGE-1.md)。


## SS-V01-RELEASE-002-R1 阶段二停止（2026-10-24）

阶段二一次性真实E2E已执行并停止，整体BLOCKED。首个Builder原生权限正确（unelevated/workspace-write/never/network=false），但外置观察器把pwsh路径codex-runtimes误匹配为Codex命令，主动终止exit125；外层即时预检亦有编码异常后继续启动的控制缺陷。模型1次，Reviewer/Fixer/Explorer0，无Diff/Candidate，DB实际ACCEPTED，原生交付物未生成；不得重跑。

完整证据见 [停止交付报告](docs/SS-V01-RELEASE-002-R1-STAGE-2.md)。


## SS-V01-RELEASE-002-R2（2026-10-24）

外置验收工具 F-01/F-02 最小修复 PASS，28项工具回归实际通过，Windows PowerShell 启动控制流实测。真实模型0，真实E2E本轮NOT_TESTED且仍BLOCKED；测试通过后停止。

完整报告：[docs/SS-V01-RELEASE-002-R2.md](docs/SS-V01-RELEASE-002-R2.md)。
