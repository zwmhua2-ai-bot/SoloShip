# AI 交接现场（AI_HANDOFF）

> **这是快照，不是事实来源。**
> 下表所有运行状态均带「核实时间」。接手后必须按 `docs/RUNBOOK.md` 重新核实一遍，
> 不得直接相信本文件的旧数字。

---

## 1. 版本与分支

### 1.1 本地 Git 状态（核实时间 2026-10-24 01:53）

| 项 | 值 |
| --- | --- |
| 仓库根 | `E:\Documents\AI-Developer\repositories\SoloShip` |
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
| 远程 | `origin` → `github.com/zwmhua2-ai-bot/SoloShip`（**私有**） |

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
| SS-V01-005 Publisher 发布门禁与幂等性加固 | 完成 | 见交付报告 |

P00–P05B 阶段（交接到 Supervisor 自启）在**纳入 git 之前**完成，没有对应提交，
详见 `docs/DEVLOG.md` 的「未核实/无提交」说明。

## 3. 正在进行的任务

无。SS-DOC-002（交接文档一致性修正）本次交付，等待验收。

## 4. 下一张任务卡

**未指定。** 已知候选（均**未获批**，不得自行开工）：

- P05-C：未收尾/推迟任务恢复、`cmd_start` 存活二次确认、并行扩展。
- 收尾业务侧 PR #1（Draft，AC-04 `partially_covered`，`verified=False`）。
- 补写架构设计说明与数据契约（缺口已登记于 `PROGRESS.md` §7）。

## 5. 最新测试结果与证据来源

| 项 | 值 |
| --- | --- |
| 命令 | `python -m unittest discover -s tests`（**不要加** `-t .`） |
| 结果 | `Ran 374 tests in 416.465s — OK`（SS-V01-005 交付前实测） |
| 时间 | 2026-10-24 03:43（本地） |
| 证据来源 | 本机实际执行输出（`PYTHONDONTWRITEBYTECODE=1`） |
| 上一基线 | `346 OK`（2026-10-24 03:03，SS-V01-004），新增 28 项发布门禁回归后升为 374 |
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

1. **旧目录残留**：`C:\Users\hua\SoloShip-P00` 迁移后无法重命名（Access Denied），内含过期 `.control` 数据库，已放 `_MIGRATED_TO_E.txt` 标记，**不得**再作为运行目录。
2. **Journal 之前的崩溃不可自愈**：Codex/publish 已完成但 Journal 未写时，任务停在 `ACCEPTED`，只能人工介入。
3. **并发恢复无互斥**：同时跑两个 `--recover` 可能产生一次多余写入（内容仍一致）。
4. **历史任务无完成证据清单**：报告若丢失只会进入 `NEEDS_HUMAN`，无法重建。
5. **`NEEDS_HUMAN` 无复位入口**：人工修复后需自行改库状态，暂无命令。
6. **文档中残留旧路径**：`docs/P05B-*.md` 与 `demo/soloship-demo/README.md` 属历史记录，保留原文，不作为操作依据。
7. **Demo 仓库四条本地分支**（`p01/*`、`p02/*` 共 4 条）未推送，功能已被 PR #1 覆盖，仅作过程证据。
8. **孤立 worktree 目录**（核实 2026-10-24 01:53）：`.git/worktrees` 不存在，`git worktree list` 只有主工作区，但 `.worktrees/` 下仍有 6 个目录。**不要复用、不要删除、不要手工注册**，需建 worktree 时由 P02 重新创建。详见 `docs/DEVLOG.md` 迁移条目的「当前状态」段。
9. **测试命令策略是"缺配置即拒绝"**（SS-V01-003）：未写入 `test_command_policy.approved` 的部署/临时根，一律拒绝执行项目测试，任务会以 `P01_TASK_REJECTED` / `P02_TASK_REJECTED`（或执行阶段 `*_BLOCKED_TEST_COMMAND`）收尾。这是设计意图，不是故障；迁移或新建环境时必须同步该段配置。

## 9. 需要人工确认的事项

1. 业务侧 PR #1 是否评审合并（`allow_merge=false`，需显式授权）。
2. 旧目录何时删除（需先解除占用）。
3. 当前分支是否需要推送到 `origin`（**未授权前不得推**）。
4. Supervisor 是否继续常驻。

## 10. 新 AI 接手后的第一步

```bash
cd /e/Documents/AI-Developer/repositories/SoloShip      # 或 Windows 路径
git rev-parse HEAD && git status --short                # 核对版本与工作区
python -m unittest discover -s tests                    # 确认 374 项基线
python bridge/supervisor.py status --root .             # 重新核实运行态
```

然后：

1. 读本篇与 `AGENTS.md`；
2. 读当前任务卡，检索 `docs/DEVLOG.md` 相关条目；
3. 打开与任务相关的 `bridge/*.py`；
4. 若文档与代码冲突，**停止并报告**，不要自行假定哪一方正确。
