# DEVLOG — 开发过程记录

按时间倒序记录。每条包含：任务编号、提交、原因、关键技术决策、测试结果、遗留问题。
**无法从仓库核实的内容一律标注「未核实」，不做推测。**

---

## SS-DOC-001 · 交接文档体系（本条目）

- **提交**：见交付报告（本文件随该提交一起入库，因此不写自身 SHA）
- **原因**：此前所有上下文只存在于对话记录里，新 AI 无法仅凭仓库接手。
- **决策**：
  - 建立 5 类文档，职责不重叠：`AGENTS.md`（长期约束）/`AI_HANDOFF.md`（现场快照）/`DEVLOG.md`（过程）/`RUNBOOK.md`（操作）/`PROGRESS.md`（进度）。
  - **不建** `REPORTS.md`：报告索引并入 `PROGRESS.md`，避免两份文件对"当前状态"给出不同说法。
  - 运行快照必须带核实时间；快照不等于事实。
- **测试**：文档改动不影响测试，提交前基线 `289 OK`（2026-10-24 01:14）。
- **遗留**：无。

---

## 仓库迁移至 E 盘

- **提交**：`b754467` `chore: 迁移到 E:/Documents/AI-Developer/repositories/SoloShip`
- **原因**：C 盘仅剩 2.4 GB，且原路径 `C:\Users\hua\SoloShip-P00` 位于用户目录下。
- **决策 / 过程**：
  1. 优雅停止 Supervisor（`graceful=true`）；
  2. `robocopy` 复制后做**逐文件 sha256 比对**（437 文件全等）才继续；
  3. 6 个 worktree 用 `git worktree repair` 修正 gitdir（4 个属 `demo/soloship-demo`，2 个属 `D:\SoloShip\repos\soloship-demo`）；
  4. 两个 PS 脚本默认 `$Root` 改成新路径；计划任务重新注册；
  5. 新路径启动并验证 `LastTaskResult=0`、单实例。
- **踩坑**：
  - 目标目录原本**非空**（含 `B-Lite-design-kit` 与旧 zip），robocopy 把无关内容混入仓库；已移到 `repositories\` 根目录后重新比对才全等。
  - bash 工具的工作目录会持久化，shell 停在旧路径会造成目录被占用。
- **测试**：新路径 `289 OK`。
- **遗留**：旧目录 `C:\Users\hua\SoloShip-P00` **无法重命名**（Access Denied，已排除进程与 shell cwd），仅写入 `_MIGRATED_TO_E.txt` 标记，需人工处理。

---

## SS-V01-002-R1 · 已完成任务的报告丢失恢复

- **提交**：`1e3f81e` `fix(p02): 已完成任务的报告可从持久证据清单恢复`
- **原因**：SS-V01-002 成功后删除 Journal，任务进入 `READY` 后若报告丢失，恢复流程没有任何可重放证据，只能标记 `REPORT_MISSING_NO_EVIDENCE`。
- **先证后改**：先写 4 项测试，改动前稳定失败（`report.json/md 应可恢复` 断言为 False），确认缺陷真实存在。
- **决策**：成功后把 Journal **移动**到 `.control/p02/finalized/<job_id>.json` 作为「已完成证据清单」，复用既有结构，不另建证据存储；`recover_finalizing()` 同时扫 `finalizing/`（补交付物+推进状态）与 `finalized/`（只补缺失文件，不改 DB、不移动 inbox、不删清单）。
- **测试**：新增 4 项 → `289 OK`。
- **遗留**：清单只对新增任务生效；清单与报告同盘；无保留策略；孤儿清单需人工。

---

## SS-V01-002 · P02 最终化一致性与报告恢复

- **提交**：`f407d88`（WIP 脚手架）+ `c63b926`（主体）
- **原因**：`finish()` 在报告生成前就写 `finished_at`。进程在最终化中途退出后，数据库显示已完成、报告缺失；再次投递被 `accept()` 判为 `DUPLICATE`，报告永远补不回来；`detect_unfinished` 以 `finished_at IS NULL` 为判据，根本看不见这类任务。
- **决策**：
  1. 顺序改为 **Journal → DB FINALIZING → 交付物 → 归档 → 终端状态 → 收尾**；Journal 先于 DB，故「Journal 已写、DB 未更新」也可恢复。
  2. Journal 记录 evidence / receipt / **三份交付物最终文本** / review 摘要，并自带 `payload_sha256`。存文本是为了恢复后逐字节一致、且能识别冲突。
  3. 恢复时：缺失补写、一致跳过、冲突 → `P02_NEEDS_HUMAN` 且不覆盖。
  4. 异常收窄：只有 `RequestBusy` 映射 `P02_BUSY`；最终化故障为 `P02_FINALIZE_ERROR`；其余异常不吞，保留原始类型与堆栈。
  5. 重复投递不再覆盖原回执，改写 `.duplicate.json`。
- **测试**：新增 12 项故障注入（AC-01..AC-11）→ `285 OK`。
- **遗留**：
  - Journal 写入前崩溃只停在 `ACCEPTED`，不进 `missing_reports`；
  - 并发恢复无互斥；
  - `NEEDS_HUMAN` 无复位入口。
- **外部改动插曲**：`f407d88` 承接的是 2026-10-24 00:22 出现在工作区、并非本会话写入的 34 行脚手架。按 `AGENTS.md §9` 保全现场（patch 导出到仓库外）并逐项核对后才继续。

---

## SS-V01-001 · 统一自动开发安全门禁

- **提交**：`b1b5430` `fix(security): 统一自动开发安全门禁，堵住 Supervisor 绕过路径`
- **原因**：`security.require_auto_dev()` 只在 CLI `main()` 里通过 `guard_or_exit()` 调用；`P01Bridge.process_one` / `P02Bridge.process_one` 没有检查，而 `Supervisor.execute()` 是**同进程直调** `process_one`，因此 `BLOCKED` 时仍可能触发 Codex Builder / Reviewer / 项目测试。
- **决策**：
  - 在 `process_one` 内部（格式校验之后、任何执行动作之前）强制检查；P02 新增 `require_auto_dev()` 每次实时读配置，不复用 `__init__` 缓存。
  - Supervisor 的拦截点放在 `claim()` **之前**：若放到 `execute()` 里失败，`tick()` 会把结果计入 `processed` 并把认领标 `done`，等于「未授权任务被标记成功」。
  - P00 交接不设门禁（文件交接不执行代码）。
- **测试**：新增 `tests/test_security_gate.py` 9 项 → `273 OK`。修复过程中发现 `test_p01/test_p02` 的临时根没有配置，加门禁后 19 项变红，补显式 `windows_test_mode` 授权块后恢复。
- **遗留**：`test_mode`（白名单）只在执行模式变化时刷新，长驻进程改白名单需重启。

---

## P00–P05B 阶段：无提交可追溯（部分未核实）

以下阶段在纳入 git 之前完成，**没有对应提交**，只能依据仓库内的报告与运行产物判断：

| 阶段 | 内容 | 证据 | 提交 |
| --- | --- | --- | --- |
| P00 | ChatGPT Work 交接 | `exchange/reports/demo-app__p00_*.report.md` | 无 |
| P01 | Codex CLI 接入 | `exchange/p01/reports/*.report.md` | 无 |
| P02 | 开发闭环 | `exchange/p02/reports/*.report.md` | 无 |
| P02-B | 模型路由 | `exchange/p02/reports/P02B-DELIVERY.md` | 无 |
| P02-S | 沙箱与 D 盘预检 | `exchange/security/P02S*.md` | 无 |
| P03 / P03-D | Draft PR 发布与首个实任务 | `exchange/p03/P03-DELIVERY.md`、`P03D-*.md` | 无 |
| P04 | 状态索引 / 集成指令 / 验收助手 | `docs/P04-完成报告.md` | 无 |
| P05-A | GitHub 实时状态 | `docs/P05A-完成报告.md` | 无 |
| P05-B | Supervisor 与登录自启 | `docs/P05B-*.md` | 无 |

首次纳入 git 的提交是 `24d0337`（`chore: 初始化 SoloShip 流水线源码（P00–P05B）`，46 个文件），
它把此前所有阶段的代码一次性提交，**不含任何中间历史**——因此这些阶段的具体改动时序**未核实**。

测试规模演进（191 → 233 → 257 → 259 → 262 → 264 → 273 → 285 → 289）来自各阶段报告与提交信息，属**可核实**范围。
