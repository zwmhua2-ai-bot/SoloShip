# AI 交接现场（AI_HANDOFF）

> **这是快照，不是事实来源。**
> 下表所有运行状态均带「核实时间」。接手后必须按 `docs/RUNBOOK.md` 重新核实一遍，
> 不得直接相信本文件的旧数字。

---

> **发布说明（本同步分支）**：本分支 `docs/ss-v01-handoff-public` 仅同步 v0.1 收尾的 4 份交接文档（本文件 + `PROGRESS.md` / `DEVLOG.md` / `RUNBOOK.md`），基于真实 `origin/main`（`d524991`）。文中多次引用的详细交付报告（`docs/SS-V01-RELEASE-002-*.md`、`docs/SS-V01-FINAL-*-DELIVERY.md`）与外置验收工具目录 `scripts/release002_acceptance/`（F-01/F-02 修复）**仅存在于本地分支、尚未发布到 `main`**；仓库路径与 Python 解释器路径均因部署而异。请勿据此认为上述内容已公开发布。

## 新 AI 接手：60 秒快速了解

> 只看本节也能判断下一步。完整证据见本文件各节（尤其是末尾 `SS-V01-RELEASE-002-R1/R2`、`SS-V01-FINAL-*` 登记）+ `docs/DEVLOG.md` + `PROGRESS.md` §0；任务编号与 Commit 见各文末。

**1. 这是什么项目？** SoloShip 是一条**本地自动开发流水线**（调度 Codex 实现 → 自测 → 独立审查 → 产出 Draft PR），不是产品代码本身。当前处于 **v0.1 收尾验收**阶段。

**2. 当前目标是什么？** 完成 v0.1 的**真实端到端验收**，然后**安全升级生产 Supervisor** → **验证 GitHub Draft PR** → **冻结并发布 v0.1.0**。当前**不开发 v0.2**、不扩充新功能。

**3. 最新稳定代码和 RC 分别在哪里？**
- 生产 `main`：`d524991f3aceadcbd361b75d19ce08499e27f20a`（v0.1.0 **未发布**）。
- R2 发布候选：`release/v0.1.0-rc1` @ `3b8c153a3650dd659b042f2e46f7ac6c11cf6c23`（尚未部署）。
- 外置验收工具修复：`ss-v01-release-002-r2/observer-gate` @ `d4c87bc2457d526c9dc0867c1503ea8354d9de59`（本地分支，尚未发布到 `main`）。
- 本交接文档分支：`docs/ss-v01-handoff-public`（基于真实 `origin/main` `d524991f3aceadcbd361b75d19ce08499e27f20a` 的独立文档工作区，**未推送**）。

**4. 哪些事情已经完成？**
- v0.1 核心开发（P00–P05B + SS-V01-001～AC-001：安全门禁、测试授权、Reviewer SHA 绑定、Publisher 门禁、P05-C 恢复、AC 证据矩阵）。
- `SS-V01-FINAL-FIX-001`：Windows Supervisor Mutex 所有权 + 恢复 Manifest 持久化（435 项全量 OK）。
- `SS-V01-FINAL-CONC-002` R2：并发恢复互斥 / 终态保护 / Manifest 顺序（455 项，453 PASS / 2 SKIP）。
- `SS-V01-RELEASE-002-R1` 阶段一：CLI `-c` 参数布局修复（454 PASS / 1 SKIP）；阶段二确认真实 Builder 的 Windows 沙箱权限**正确**。
- `SS-V01-RELEASE-002-R2`：外置验收工具 **F-01 / F-02** 最小修复 + 28 项无模型测试 PASS。
- 安全审计已收尾，不再无限扩展审核。

**5. 现在卡在哪里？** 完整 **真实 Builder → 测试 → Reviewer E2E 仍未通过**。F-01/F-02 外置工具已修，但真实 E2E 尚未在**全新授权**下重跑；v0.1.0 **不能发布**。

**6. 下一张任务卡是什么？** `SS-V01-RELEASE-002-R2`（已启动，工具修复已就位）。其剩余工作：**等待用户重新批准一次全新真实 Codex E2E**（新 `runtime` / `request_id` / `Job`），跑通后再按「§0 后续发布顺序」推进。

**7. 有哪些操作绝对不能擅自执行？**
- **不得**重跑 / 恢复 / 改写 `release002_r1_real_codex_001`（见 §0 异常任务）。
- **不得**修改历史数据库伪造终态、删除原始证据、让新 Supervisor 自动接管中断的测试运行根。
- **不得**擅自启动真实 Codex 模型或重跑历史 E2E；每次真实 E2E 必须用**新授权、独立运行根、独立任务 ID**。
- **不得**推送 `main` / 合并 R2 / 创建 GitHub PR / 创建发布 Tag / 停止或升级生产 Supervisor / 开始 v0.2。
- `WINDOWS_TEST_MODE` ≠ 已验证的完整 OS 沙箱；真实模型、GitHub 写入、生产操作**都不是默认授权**。

**8. 应该从哪里查看完整证据？** 本文件各节（含末尾 `SS-V01-RELEASE-002-R1/R2`、`SS-V01-FINAL-*` 登记）+ `docs/DEVLOG.md` + `PROGRESS.md` §0；关键交付报告：`docs/SS-V01-RELEASE-002-R1-STAGE-2.md`、`docs/SS-V01-RELEASE-002-R2.md`、`docs/SS-V01-FINAL-FIX-001-DELIVERY.md`、`docs/SS-V01-FINAL-CONC-002-R2-DELIVERY.md`。

## 0. 接手必读：当前目标、阻断与下一步

### 当前目标
完成 SoloShip v0.1 的**真实端到端验收**，然后**安全升级生产 Supervisor** → **验证 GitHub Draft PR** → **发布 v0.1.0**。当前**不开发 v0.2**、不扩充新功能。

### 已经解决的问题
- Windows Supervisor Mutex 所有权问题（`SS-V01-FINAL-FIX-001` / A，`63eac82`）。
- 恢复竞争与 Manifest 持久化问题（`SS-V01-FINAL-FIX-001` / B，`d77ae62`；`SS-V01-FINAL-CONC-002` R2，`cdcbc3d` / `ffec835`）。
- Git Push 审计器已确认的绕过路径已封堵（统一自动开发安全门禁 `SS-V01-001`，`b1b5430`）。
- Codex CLI `-c` 参数位置导致的 Windows 沙箱配置丢失（`SS-V01-RELEASE-002-R1` 阶段一，`af5177b`）。

### 当前真正的阻断问题（F-01 / F-02）
> 两项外置验收工具的缺陷**已在 `SS-V01-RELEASE-002-R2`（`d4c87bc`）修复并通过 28 项无模型测试**；但**真实 E2E 尚未重跑验证**，因此 v0.1.0 发布仍被阻断。

- **F-01：观察器误报** —— 观察器对整条命令字符串做 `\bcodex\b` 匹配，把合法 PowerShell 可执行文件目录 `codex-runtimes` 误判为启动额外 Codex。需正确区分可执行文件、路径与嵌套命令，同时保留真实违规调用的拦截能力。修复见 `scripts/release002_acceptance/command_guard.py` + `parse_command.ps1`（有界 AST 解析；修复脚本位于本地分支 `ss-v01-release-002-r2/observer-gate` 的 `scripts/release002_acceptance/`，尚未发布到 `main`）。
- **F-02：预检未安全失败** —— 预检以默认编码读取 UTF-8 JSON 异常后，PowerShell 外层控制流仍启动执行器。需确保任何预检异常、非零退出码或无效结果都**阻止执行器启动**。修复见 `scripts/release002_acceptance/precheck.py` + `launch.ps1`（同上，位于本地分支 `ss-v01-release-002-r2/observer-gate`，尚未发布到 `main`）。

### 特别重要的异常任务（历史中断，禁止触碰）
- **Request ID**：`release002_r1_real_codex_001`
- **Job**：`p02_f98f3ad93e91`
- **最后报告状态**：`ACCEPTED`（由外置观察器中断后的未最终化状态；无 Receipt、Report 或 Manifest）
- **铁律**：
  1. 禁止自行重新执行该任务；
  2. 禁止修改历史数据库来伪造终态；
  3. 禁止删除原始证据；
  4. 禁止让新的 Supervisor 自动接管该独立测试运行根；
  5. 后续 E2E 必须使用**新的 runtime、request_id 和授权**，不复用旧 `launch-once` / `scope` / 历史硬编码 runner。
- 完整证据：`docs/SS-V01-RELEASE-002-R1-STAGE-2.md`。

### 下一步唯一优先任务
`SS-V01-RELEASE-002-R2`（已启动，外置工具修复已就位）：
- 已做：F-01/F-02 工具修复 + 28 项无模型测试 + Windows 启动控制流实测。
- 待做（需用户授权）：**重新批准一次全新真实 Codex E2E**（新 `runtime` / `request_id` / `Job`），跑通 Builder → 项目测试 → Reviewer。
- 通过并授权后停止，等待用户决定发布。

### 后续发布顺序
1. 完成观察器和预检修复（已完成工具层，待真实 E2E 验证）。
2. 真实 Builder → 项目测试 → Reviewer E2E（全新授权）。
3. 安全合并 R2 到本地 `main`。
4. 经用户授权升级生产 Supervisor。
5. 经用户授权完成 GitHub Draft PR 真实验收。
6. 冻结并发布 v0.1.0。

## 1. 版本与分支

### 1.1 本地 Git 状态（核实时间 2026-10-24 01:53）

| 项 | 值 |
| --- | --- |
| 仓库根 | 本仓库根目录（路径因部署而异，不固定） |
| 当前分支 | `main` |
| HEAD 完整 SHA | `29401afda3ea629dac4f5cb1a333ba56f81825db` |
| 工作区 | 干净（`git status --porcelain` 无输出） |
| 上游跟踪 | `origin/main` |
| 本地与上游 | ahead 6 / behind 0（**仅比较本地引用**，见 1.3） |

### 1.2 分支现状（同次核实）

| 分支 | 指向 | 说明 |
| --- | --- | --- |
| `main` | `29401af` | 当前分支。2026-10-24 01:39:17 由 `b1b5430` 快进至此（reflog：`ff: main -> 29401af`，CAS 记录） |
| `ss-v01-002/finalize-recovery` | `29401af` | 与 `main` **同一 SHA**，不再单独领先；已不是"待合并开发分支" |
| `backup/main-before-ff-20261024` | `b1b5430` | 快进前的备份点，2026-10-24 01:39:17 创建 |

### 1.3 远端状态：UNKNOWN（未实时确认）

| 项 | 值 |
| --- | --- |
| 已知 `origin/main` 引用 | `24d03375fc874d28a24e115edff22d5ee7a8cd1d` |
| 该引用最近更新时间 | 2026-10-23 23:26:58（`.git/refs/remotes/origin/main` mtime；`.git/FETCH_HEAD` 为 2026-10-23 23:26:46） |
| 此后是否 fetch 过 | **否**。远端在该时刻之后有无新提交，本地无法确定 |
| 远程 | `origin` → `github.com/zwmhua2-ai-bot/SoloShip`（**公开 Public** 仓库） |

> 1.1 的 ahead/behind 是**本地引用**的比较结果，不等于远端真实状态。
> 要确认远端必须先 `git fetch` 并取得授权；在那之前一律按 **UNKNOWN** 处理，不得写成"已同步"。

> 说明：本文件本身会随任务提交，因此**不能**记录提交后的新 SHA。
> 实际 HEAD 以 `git rev-parse HEAD` 为准；上面所有值都带核实时间，过期即失效。

## 2. 已完成任务

| 任务 | 结果 | 提交 |
| --- | --- | --- |
| SS-V01-001 统一安全门禁 | 完成 | `b1b5430` |
| SS-V01-002 最终化一致性与报告恢复 | 完成 | `f407d88` + `c63b926` |
| SS-V01-002-R1 完成后报告丢失恢复 | 完成 | `1e3f81e` |
| 仓库迁移至 E 盘 | 完成 | `b754467` |
| SS-DOC-001 交接文档体系 | 完成 | `29401af` |
| SS-DOC-002 交接文档一致性修正 | 完成 | 见交付报告 |
| SS-V01-003 测试命令授权加固 | 完成 | `dc25135` |
| SS-V01-003-R1 测试解释器可信路径验证 | 完成 | `66cb2ef` |
| SS-V01-004 Reviewer Candidate SHA 严格绑定 | 完成 | `0db54fc` |
| SS-V01-005 Publisher 发布门禁与幂等性加固 | 完成 | `0fa7f45` |
| SS-V01-005-R1 PR 来源仓库身份验证 | 完成 | `7eb8988` |
| SS-V01-006（P05-C）Supervisor 中断恢复与安全重调度 | 完成 | `6970901` |
| SS-V01-AC-001 逐条 AC 验收证据矩阵 | 完成 | 见交付报告 |

P00–P05B 阶段（交接到 Supervisor 自启）在**纳入 git 之前**完成，没有对应提交，
详见 `docs/DEVLOG.md` 的「未核实/无提交」说明。

## 3. 正在进行的任务

- `SS-V01-HANDOFF-SYNC-001`（本卡，仅文档同步）：在独立分支 `docs/ss-v01-handoff-public` 更新交接文档，不修改代码、不触碰 `main`/RC，不推送。
- `SS-V01-RELEASE-002-R2`（已启动、进行中）：外置验收工具 F-01/F-02 修复已完成（`d4c87bc`，28 项无模型测试 PASS）；真实 E2E 待用户重新授权，**未执行**。
- 核心开发任务均已完成，仅剩 v0.1 验收收尾。

## 4. 下一张任务卡

**实际下一步（需用户重新批准）**：一次**全新真实 Codex E2E**（新 `runtime` / `request_id` / `Job` 与独立授权），承接 `SS-V01-RELEASE-002-R2` 已就位的 F-01/F-02 工具修复，跑通 Builder → 项目测试 → Reviewer 后，再按「§0 后续发布顺序」推进。

已知候选（均**未获批**，不得自行开工）：

- `SS-V01-RELEASE-002-R2` 的真实 E2E 阶段（依赖用户重新授权，见 §0）。
- P05-C：未收尾/推迟任务恢复、`cmd_start` 存活二次确认、并行扩展。
- 收尾业务侧 PR #1（Draft，AC-04 `partially_covered`，`verified=False`）。
- 补写架构设计说明与数据契约（缺口已登记于 `PROGRESS.md` §7）。
- v0.2 方向讨论备忘录：`docs/V02_ROADMAP_DRAFT.md`（讨论稿，未获批，不得据此开工）。

## 5. 最新测试结果与证据来源

| 项 | 值 |
| --- | --- |
| 命令 | `python -m unittest discover -s tests`（**不要加** `-t .`） |
| 结果 | `Ran 414 tests in 439.779s — OK`（SS-V01-AC-001 交付前实测） |
| 时间 | 2026-10-24 09:21（本地） |
| 证据来源 | 本机实际执行输出（`PYTHONDONTWRITEBYTECODE=1`） |
| 上一基线 | `402 OK`（2026-10-24 04:38，SS-V01-006），新增 12 项证据矩阵回归后升为 414 |
| 之后是否复跑 | 无。接手后须自行复跑；不得把上面这行当成"我刚跑过" |

## 6. 运行状态（必须重新核实）

| 项 | 值 | 核实时间 |
| --- | --- | --- |
| Supervisor | `running=True, lock=True`，PID **41140** | 2026-10-24 01:11 |
| 启动时间 | `2026-10-23T17:04:38+00:00` | 同上 |
| 最近心跳 | `2026-10-23T17:11:10+00:00`（2.8s 前） | 同上 |
| 队列 | P00=0 / P02=0，当前任务 `-` | 同上 |
| 待恢复 | 0 | 同上 |
| 执行模式 | `WINDOWS_TEST_MODE`（`isolation_claim=NONE`） | 同上 |
| 沙箱状态 | `SANDBOX_BLOCKED` | 同上 |
| 计划任务 | `LastRunTime=2026-10-24 01:04:36`，`LastTaskResult=0` | 同上 |
| 进程数 | 1 | 同上 |

`LastTaskResult=0` **不能**单独证明进程还活着——历史上出现过「任务计划报 0 但守护进程已退出」。必须另行 `status` + 进程数复核。

## 7. 安全边界（当前生效值）

| 配置 | 值 |
| --- | --- |
| `github.enabled` | `false` |
| `allow_merge` / `allow_force_push` | `false` / `false` |
| `require_env_token` / `token_env` | `true` / `GH_TOKEN`（环境变量，配置内无 Token） |
| `windows_test_mode.enabled` | `true`（`risk_accepted=true`） |
| `isolation_claim` | `NONE`（明示无操作系统级隔离） |
| `test_command_policy.approved` | 仅 2 条 `-m unittest discover -s tests`（含 `-v`）；缺失/非法即拒绝执行测试 |
| 解释器信任来源 | 固定目录（解释器目录 / `base_prefix` / `SystemRoot` / `trusted_interpreter_dirs`），**PATH 不参与**；执行用验证后的绝对路径 |

> **上表是"配置声明"，不等于"运行时检查"。**
> 真实生效方式见 `AGENTS.md` §7 的开关表：`github.enabled` 与 `require_env_token`
> 有对应的运行时短路/校验；`allow_merge` / `allow_force_push` **没有任何代码读取**，
> 系统根本不存在合并与强制推送能力。不要把后者的"`false`"当成一道被执行的权限校验。

## 8. 已知问题与风险

1. **旧目录残留**：迁移前位于用户目录下的 `SoloShip-P00` 迁移后无法重命名（Access Denied），内含过期 `.control` 数据库，已放 `_MIGRATED_TO_E.txt` 标记，**不得**再作为运行目录。
2. **Journal 之前的崩溃不可自愈**：Codex/publish 已完成但 Journal 未写时，任务停在 `ACCEPTED`，只能人工介入。
3. **并发恢复无互斥**：同时跑两个 `--recover` 可能产生一次多余写入（内容仍一致）。
4. **历史任务无完成证据清单**：报告若丢失只会进入 `NEEDS_HUMAN`，无法重建。
5. **`NEEDS_HUMAN` 无复位入口**：人工修复后需自行改库状态，暂无命令。
6. **文档中残留旧路径**：`docs/P05B-*.md` 与 `demo/soloship-demo/README.md` 属历史记录，保留原文，不作为操作依据。
7. **Demo 仓库四条本地分支**（`p01/*`、`p02/*` 共 4 条）未推送，功能已被 PR #1 覆盖，仅作过程证据。
8. **孤立 worktree 目录**（核实 2026-10-24 01:53）：`.git/worktrees` 不存在，`git worktree list` 只有主工作区，但 `.worktrees/` 下仍有 6 个目录。**不要复用、不要删除、不要手工注册**，需建 worktree 时由 P02 重新创建。详见 `docs/DEVLOG.md` 迁移条目的「当前状态」段。
9. **测试命令策略是"缺配置即拒绝"**（SS-V01-003）：未写入 `test_command_policy.approved` 的部署/临时根，一律拒绝执行项目测试，任务会以 `P01_TASK_REJECTED` / `P02_TASK_REJECTED`（或执行阶段 `*_BLOCKED_TEST_COMMAND`）收尾。这是设计意图，不是故障；迁移或新建环境时必须同步该段配置。
10. **恢复是分级而非全自动**（SS-V01-006）：`needs_human/` 下的任务必须人工核对后自行决定后续动作，本期没有强制成功命令；SAFE_TO_RESUME 只覆盖有明确持久证据的情形。

## 9. 需要人工确认的事项

1. 业务侧 PR #1 是否评审合并（`allow_merge=false`，需显式授权）。
2. 旧目录何时删除（需先解除占用）。
3. 当前分支是否需要推送到 `origin`（**未授权前不得推**）。
4. Supervisor 是否继续常驻。

## 10. 新 AI 接手后的第一步

```bash
cd <仓库根目录>      # 替换为实际仓库路径（Windows / Linux 均适用）
git rev-parse HEAD && git status --short                # 核对版本与工作区
python -m unittest discover -s tests                    # 确认 414 项基线
python bridge/supervisor.py status --root .             # 重新核实运行态
```

然后：

1. 读本篇与 `AGENTS.md`；
2. 读当前任务卡，检索 `docs/DEVLOG.md` 相关条目；
3. 打开与任务相关的 `bridge/*.py`；
4. 若文档与代码冲突，**停止并报告**，不要自行假定哪一方正确。

## SS-V01-FINAL-FIX-001 开发快照补充（2026-10-24，A 完成）

本节覆盖上文旧的“无正在进行任务”说明。本次在全新 `SoloShip-final-fix-001` worktree、`fix/ss-v01-final-fix-001` 分支开发，基线 `d524991`；生产运行快照未重新核实，也未启停生产。

A 已自测：Windows Mutex 正确获取/释放所有权，8 项新增测试与 89 项既有 P05 回归通过，10/10 次立即启停成功。下一步仅实施任务卡 B（恢复 Journal 保留 Finalized Manifest），整卡待独立验收。AGENTS.md 已检查，无开发规则变更。

## SS-V01-FINAL-FIX-001 开发快照补充（2026-10-24，B 完成）

A/B 两项代码已完成自测；B 新增 13 项和既有最终化 16 项通过。真实隔离子进程 FINALIZING 退出 → Manifest 保留 → 再次丢报告 → 逐字节重建已通过。当前仅剩全量回归、证据审计和最终交付；不进入 v0.2，不自动验收或发布。

新增存储限制：finalizing/finalized 必须在同一支持 hard link 的文件系统。本机测试盘为 NTFS，已实际验证；不支持时保留 Journal 并报告失败。生产 Supervisor、数据库与历史证据未接触，旧运行快照仍未核实。

## SS-V01-FINAL-FIX-001 最终交接（2026-10-24）

A/B 修复已分别本地提交（完整 SHA 见交付报告），分支 `fix/ss-v01-final-fix-001`，独立 worktree `SoloShip-final-fix-001`。原 main 仍为 `d524991f3aceadcbd361b75d19ce08499e27f20a`，工作区干净；生产运行状态未核实、未操作。

全量 435 项，446.276s，OK (skipped=2)，无失败/错误。PATH 环境条件跳过项已单独补测通过；真实 GitHub 网络负例未执行。新增 21 项通过，全量后 E2E-07/08/10 均再次通过。最终 PARTIAL，A/B 验收项开发自测 PASS，等待独立验收。无新的开发任务获批，禁止据此进入 v0.2、推送、合并、部署或重启生产。

审计：3,701 次子进程启动尝试中，真实 Codex、真实 gh、生产 Supervisor 命令、Fake 路径违规均为 0。证据在同级 `SoloShip-final-fix-001-evidence/`，详见 `docs/SS-V01-FINAL-FIX-001-DELIVERY.md`。存储 hard link 限制仍有效；断电与其他文件系统未验证。AGENTS.md 已检查，无需变更。

## SS-V01-FINAL-CONC-002 R2 开发交接（2026-10-24）

本节覆盖此前“无开发任务获批”的旧快照。本卡批准处理 F-00～F-03，开发分支为 `fix/ss-v01-final-concurrency-r2`，独立 worktree 为 `codex-data/worktrees/ss-v01-final-concurrency-r2/SoloShip`。基线完整 SHA 为 `301886d204be3d5f86565ec9d8e294adf51f9438`；未切换主工作区、未操作生产 Supervisor、数据库、配置或历史证据。生产运行快照未重新核实。

新版本的 P02 任务执行、正常最终化和全部恢复入口共用 `.control/p02/recovery.lock` 文件字节锁。持久载体不删除、不记 PID 所有权；进程退出由 OS 释放。嵌套调用仅同一 Bridge 的同线程重入。CLI 争锁返回 JSON `busy` 和退出码 3；直接调用抛出 `RecoveryBusy`。Supervisor 通过现有错误摘要报告恢复争锁；其锁和恢复入口未改，tick 仅增加 P02_BUSY → requeued 分支，防止未执行任务被误记 done，保留认领历史后重试。

历史 READY/BLOCKED 任务检查失败只产生人工诊断，不覆盖终态、原 Manifest 或报告。新完成顺序为 Journal → FINALIZING → 交付物/归档 → Manifest 安装与读回验证 → DB 条件确认终态 → Journal 清理。Manifest 已安装而 DB 未完成仍能恢复。无需数据库迁移或任务协议变更。

定向回归、10 轮进程并发、异常退出解锁及 `-I -S` 清洁导入均通过；完整结果和逐 AC 矩阵见 `SS-V01-FINAL-CONC-002-R2-DELIVERY.md`。外部原始证据位于本 worktree 同级 `../evidence-r2/`。AGENTS.md 已检查，无需变更。

下一步仅独立代码审查和人工验收。新旧版本不得混用同一运行根；本任务没有升级生产运行实例。断电级持久性、其他文件系统、真实 GitHub 网络负例仍未验证。不推送、不合并、不部署、不重启生产、不进入 v0.2。

最终自测：455 项/428.506s，OK (skipped=2)，453 PASS；PATH 项单独补测通过，真实网络负例 NOT_TESTED。完整分进程审计 5,070 条启动尝试无真实 Codex/gh 放行、Fake 路径违规、生产 Supervisor 或真实 Git push；32 个恢复进程调用均为 0。实现提交 `cdcbc3d943030c8ad88c541456d44f22c91d07ff`，Supervisor 兼容性提交 `ffec8359487537da85294ab630d53be38d5cc36f`；测试/文档最终提交及干净状态见交付消息、证据 git-final.txt。结论 PARTIAL：本卡开发自测 PASS，独立审查待进行，已停止开发。


## SS-V01-RELEASE-002-R1 第一阶段（2026-10-24）

核实 2026-10-24T21:28:29.726092+08:00：分支 ss-v01-release-002-r1/cli-layout 基于 R2 3b8c153，独立启动器组件与无模型探针完成。生产 main d524991 / PID41140 空闲，1544 保护文件未变。原 release002_real_codex_001 终态保留，不能重跑；下一步仅在新明确授权后创建新 runtime/request_id 并核对实际 exec 策略；未授权真实模型。

完整结果见 [第一阶段交付](SS-V01-RELEASE-002-R1-STAGE-1.md)。


## SS-V01-RELEASE-002-R1 阶段二停止（2026-10-24）

核实2026-10-24T21:46:10.760357+08:00：request release002_r1_real_codex_001 / Job p02_f98f3ad93e91 已由观察器停止；本次授权已消费。runtime 在本地临时目录（路径略），留存ACCEPTED job、Ready和launch-once；禁止接入Supervisor、自行恢复、改库或重新process_one。真实CLI权限已确认正确；功能E2E仍BLOCKED。2,136保护文件不变，生产PID41140同一实例空闲；旧失败任务和第一阶段证据未动。

完整证据见 [停止交付报告](SS-V01-RELEASE-002-R1-STAGE-2.md)。


## SS-V01-RELEASE-002-R2（2026-10-24）

历史 release002_r1_real_codex_001 / p02_f98f3ad93e91 登记为人工中断验收，DB保持ACCEPTED且未最终化；禁止恢复、process_one、改库、删除Ready/Worktree/日志及Supervisor接管。本卡只修外置工具副本，28测试PASS、2282保护文件不变；下一次真实验收必须新runtime/request_id/Job及新授权。

完整报告：[SS-V01-RELEASE-002-R2.md](SS-V01-RELEASE-002-R2.md)。
