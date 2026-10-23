# SoloShip 进度记录（PROGRESS）

> 本文件是**阶段与任务进度**的主记录。
> 现场状态（PID、心跳、当前分支）看 `docs/AI_HANDOFF.md`；过程与技术决策看 `docs/DEVLOG.md`。

---

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
| SS-V01-004 | Reviewer Candidate SHA 严格绑定与审查证据校验 | 完成 | 见交付报告 |
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
| SS-V01-004 | **346**（当前） |

当前基线：`346 OK`（2026-10-24 03:03，`python -m unittest discover -s tests`，SS-V01-004 实测）。

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

## 7. 文档缺口登记（SS-DOC-002 登记，未修复）

以下缺口已确认存在。本次**只登记不补写**，避免从旧文档臆造最新实现：

| 缺口 | 影响 | 现状证据 |
| --- | --- | --- |
| 缺少当前版本的架构设计说明（无 `DESIGN.md` / `IMPLEMENTATION.md`） | 新接手者只能靠 `AGENTS.md` §2 的模块表并逐个读 `bridge/*.py` 重建架构、调用时序与边界 | 仓库根目录无这两份文件；`README.md` 与 `AGENTS.md` 也未引用 |
| 缺少任务 JSON、数据库与 Journal 的数据契约说明 | 任务字段、`p00.db` / `p02.db` 表结构、`soloship.p02.finalizing/v1` Journal 结构只存在于代码里 | `schemas/` 仅含 `review.schema.json` |

补写规范：以**当前源码**为唯一依据，逐项与 `bridge/` 实现核对；
不得照抄历史报告，也不得凭记忆补全字段。
