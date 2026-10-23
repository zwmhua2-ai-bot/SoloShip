# AGENTS.md — SoloShip 开发约束（长期有效）

本文件对**所有**接手 SoloShip 的 AI 生效。与任务卡冲突时，以任务卡为准；与代码冲突时，停止并报告（见 §9）。

---

## 1. 项目是什么

SoloShip 是一条**本地自动开发流水线**：接收结构化任务文件 → 在独立 worktree 里调用 Codex 实现 → 自测 → 独立审查 → 产出 Draft PR → 汇总状态与验收证据。

它**不是**产品代码本身。被开发的目标仓库在外部（当前为 `D:\SoloShip\repos\soloship-demo`），本仓库只负责调度与证据。

## 2. 架构与代码入口

```
任务文件 (exchange/**/inbox/*.ready.json)
      │
      ▼
Supervisor（后台常驻、单实例、单并发）
      ├─ P00 交接  → p00_bridge.py    （校验 / 回执 / 入库，不调用 Codex）
      ├─ P02 开发  → p02_bridge.py    （worktree → 实现 → 测试 → 审查 → 修复轮次）
      │                 ├─ codex_adapter.py   Codex CLI 调用与事件解析
      │                 ├─ model_router.py    模型档位选择
      │                 ├─ reviewer.py        审查结论解析
      │                 └─ p02_support.py     沙箱探测 / 测试隔离
      ├─ P03 发布  → github_publisher.py      Draft PR（受安全门禁约束）
      ├─ P04 状态  → status_index.py          索引 / 实时状态合并
      └─ P05 验收  → acceptance.py            AC 与 diff 对照（恒 verified=False）
```

| 模块 | 职责 | 可执行入口 |
| --- | --- | --- |
| `bridge/supervisor.py` | 调度、单实例锁、心跳、恢复重放 | `start/stop/status/restart/doctor/recovery/_run` |
| `bridge/p00_bridge.py` | P00 交接 | `--once` 等 |
| `bridge/p01_bridge.py` | P01 Codex 接入 | 同上 |
| `bridge/p02_bridge.py` | P02 开发闭环、最终化、恢复 | 另有 `--recover` |
| `bridge/github_publisher.py` | Draft PR 发布 | 由 P02 调用 |
| `bridge/github_status.py` | 只读查询 PR 状态 | `refresh/show` |
| `bridge/status_index.py` | 状态索引 | `rebuild/latest/show` |
| `bridge/acceptance.py` | 验收比对 | 命令行 |
| `bridge/p03d_guard.py` | 发布前检查 | `toggle/precheck-inbox/validate-task/check/compare` |
| `bridge/security.py` | 安全门禁判定 | 库 |

跨阶段产物目录：`exchange/`（任务、回执、报告、索引）、`.control/`（数据库、锁、日志）。两者**不入 git**。

## 3. 任务执行与验收流程

1. 读任务卡 → 只读核查现状（不凭记忆断言"已完成"）。
2. 复述动作与影响范围 → 得到明确许可后再改代码。
3. 改动前记录基线（`git rev-parse HEAD`、`git status`）。
4. 先写测试（能复现缺陷的测试），再改实现。
5. 跑全量回归。
6. 提交（本地）→ 输出任务卡要求的交付项。
7. 按 §10 检查文档更新。

验收结论必须区分「已验证」与「未验证」。没有实际运行过的内容，只能写「未验证」。

## 4. Git 规范

- 分支：`main` 为稳定线；任务分支用 `ss-<任务号>/<简短描述>`（例：`ss-v01-002/finalize-recovery`）。
- 提交格式：`<type>(<scope>): <摘要>`，type ∈ `feat/fix/chore/docs/test/refactor`。
- 一个提交只做一件事；WIP 可单独提交并注明 `wip(...)`。
- **不推送远程**，除非任务卡明确授权；`git push --force` 一律禁止。
- 不改写已提交历史（`rebase -i`、`commit --amend` 需明确授权）。

## 5. 代码修改权限

| 允许 | 禁止 |
| --- | --- |
| 修改 `bridge/`、`tests/`、`scripts/`、`docs/`、根目录文档 | 修改 `exchange/` 下的历史报告与回执 |
| 增删测试 | 删除/清空 `.control/` 下数据库与日志 |
| 调整配置（需任务卡授权） | 修改 `demo/` 目标仓库内容（除开发任务本身） |
| — | 自动合并 PR、自动部署、自动开启沙箱豁免 |

## 6. 测试要求

```bash
python -m unittest discover -s tests
```

- **不要加 `-t .`**：`tests/` 没有 `__init__.py`，会报 `Start directory is not importable`。
- 基线：`382` 项全绿（2026-10-24 04:05，SS-V01-005-R1 交付时实测）。新增任务不得让既有用例变红。
- 涉及 Codex 的用例一律用 `tests/fake_codex.py`，**不执行真实开发**。
- 断言要能失败：先证明缺陷存在（测试红），再改到绿。

## 7. 安全门禁（不可绕过、不可回退）

三条硬约束，均有回归测试守护：

1. **执行授权**：`security.require_auto_dev()` 在 `P01Bridge.process_one`、`P02Bridge.process_one`、`Supervisor.tick` 三处强制检查。`BLOCKED` 时不得调用 Codex / 审查 / 项目测试。CLI `main()` 里的检查**不足以**覆盖内部调用。
2. **凭据**：配置内不得出现 Token；发布依赖环境变量 `GH_TOKEN`；子进程环境经 `env_guard.scrub_secrets` 清洗。禁止回退到账号级缓存 Token。
3. **发布**：`github.enabled=false` 时不得发起任何远端写操作；恢复流程不得调用 publisher。
   发布门禁（SS-V01-005）：远端 base 必须可读且合法、与预期一致，且在推送后到建 PR 前不得变化；
   已有 PR 必须为 `OPEN + Draft + 来源仓库=目标仓库 + 分支与 Head SHA 完全一致` 才复用，
   其余（CLOSED/MERGED/非 Draft/跨 Fork/来源身份缺失/Head SHA 不一致/查询失败/结果不完整或有歧义）
   一律拒绝；PR 查询显式带 `--limit 100`，命中数达上限按"可能不完整"拒绝，
   不把"未查到"当作"不存在"；推送或建 PR 超时时必须先重新读取远端对账，
   不得把超时当成"没有创建"而重建。Publisher 源码中不存在强推、合并、部署路径。

**发布相关开关的真实生效方式**（以当前源码为准，三种性质不能混为一谈）：

| 开关 | 性质 | 实际生效位置 |
| --- | --- | --- |
| `github.enabled` | 配置声明 + 运行时检查 | `github_publisher.py:86` 读入，`publish():201` 短路返回 `SKIPPED_DISABLED`，不执行任何远端动作 |
| `require_env_token` / `token_env` | 配置声明 + 运行时检查 | `_credential_args():115` 与 `_token_missing():121`：Token 只经环境变量传入，缺失即拒绝，不回退账号级缓存 |
| `allow_merge` / `allow_force_push` | **仅配置声明** | **无任何 `bridge/*.py` 读取**；`grep "def .*merge\|def .*push" bridge/*.py` 零命中 |

因此：**系统当前不具备合并与强制推送能力**——不是因为 `allow_merge=false` 被运行时校验，
而是因为对应代码路径不存在。新增任何远端写能力前，必须先在本表补上运行时检查位置，
否则视为规范被破坏。

**测试命令授权（SS-V01-003）**：`verify.test_command` 是**请求**，不是授权。

- 唯一判定入口：`bridge/test_policy.py::resolve`。调用点四处——P01 校验（`validate_p01_task`）、P01 执行前、P02 `_run_job` 执行前、P02 `run_tests`。Supervisor 不另写规则，经 `process_one` 继承同一判定。
- 授权清单由 Bridge 管理：`config/bridge.json` → `test_command_policy.approved`。任务 JSON 不能增删。
- 配置缺失、不可读、结构非法或 `approved` 为空 → **全部拒绝**，不回退任何内置白名单。
- 解释器必须解析到受信任目录（当前解释器目录 / `base_prefix` / Windows `SystemRoot`（`py.exe` 固定位置）/ `trusted_interpreter_dirs`）。**PATH 只用于定位，不产生信任**：把同名 `python.exe` 放进 PATH 靠前的用户目录不会因此获得授权；"某个绝对路径存在"也不构成授权。
- 放行后执行的是**验证过的规范化绝对路径**，不是原始命令名，避免"校验一个路径、执行另一个路径"。
- 一律拒绝：`-c` 内联代码、未获批的 `-m` 模块、非 Python 可执行文件、含 `..` 的路径、与 `approved` 不完全相等的参数组合。
- 拒绝语义：不启动进程、不产生"测试通过"、不进修复轮次、不归类为 `P02_BUSY`；状态为 `P01_BLOCKED_TEST_COMMAND` / `P02_BLOCKED_TEST_COMMAND`，原因写入报告与 Journal。
- **允许执行项目测试 ≠ 测试代码有隔离**。本条不改变沙箱声明。

沙箱状态必须如实呈现：`SANDBOX_BLOCKED` 就是未隔离，`WINDOWS_TEST_MODE` 只是"显式授权的本机测试模式"，**不得**描述为沙箱隔离。

## 8. 证据与历史

- 任务完成后删除 Journal 是**错误**的：应转为 `.control/p02/finalized/<job_id>.json` 作为可重放证据。
- 报告、回执、验收明细、数据库行都属于证据，不得删除、不得为了"好看"重写。
- Reviewer 的 `candidate_sha` 必须与 Bridge 的真实 `head_sha` **完整一致**（40 或 64 位十六进制）。前缀、截断、附加字符、其他合法 SHA 一律视为 `REVIEW_INVALID` → `P02_BLOCKED_REVIEW_INVALID`，不得进入 READY。
- 需要修正历史时，只能**追加**新文件（例如 `.duplicate.json`），不能覆盖原件。

## 9. 遇到未知来源的工作区修改：立即停止并报告

若发现工作区出现**自己没做过**的改动：

1. 停止一切编码；
2. 记录 `git status`、`git diff`、文件 mtime，导出 patch 到仓库外；
3. 检查是否有并行进程/会话在写入（短窗口哈希对比）；
4. 报告给人类，等待指示；**不得** `git reset --hard`、不得覆盖、不得"顺手合并"。

> 真实案例：2026-10-24 00:22，`bridge/p02_bridge.py` 出现 34 行并非本会话写入的改动。当时按上述流程保全现场（patch 存到仓库外），核对后才继续。

## 10. 文档更新规则（强制）

每张任务卡**结束前**必须逐项检查，并在交付报告里写明检查结果（改了哪些、为什么没改）：

| 文件 | 何时更新 |
| --- | --- |
| `PROGRESS.md` | 阶段或任务进度变化 |
| `docs/AI_HANDOFF.md` | 当前状态、下一任务、风险、运行快照变化 |
| `docs/DEVLOG.md` | 出现了值得记录的开发过程或技术决策 |
| `docs/RUNBOOK.md` | 运行/排障操作发生变化 |
| `AGENTS.md` | 开发规范发生变化 |

没有变化的文件不必改，但**必须说明"已检查、无需变更"**。

## 11. 敏感信息禁令

文档与提交中不得出现：Token/密钥、`hosts.yml` 等凭据文件内容、个人邮箱与账号、与本机无关的私人路径。运行快照只记录必要的绝对路径（项目根、目标仓库），且必须注明核实时间。

## 12. 文档导航

- 现在该做什么 → `docs/AI_HANDOFF.md`
- 以前发生了什么 → `docs/DEVLOG.md`
- 怎么跑、怎么修 → `docs/RUNBOOK.md`
- 阶段进度与报告索引 → `PROGRESS.md`
- 上手入口 → `README.md`
