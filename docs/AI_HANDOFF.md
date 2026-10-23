# AI 交接现场（AI_HANDOFF）

> **这是快照，不是事实来源。**
> 下表所有运行状态均带「核实时间」。接手后必须按 `docs/RUNBOOK.md` 重新核实一遍，
> 不得直接相信本文件的旧数字。

---

## 1. 版本与分支

| 项 | 值 | 核实时间 |
| --- | --- | --- |
| 仓库根 | `E:\Documents\AI-Developer\repositories\SoloShip` | 2026-10-24 01:11 |
| 文档基线 SHA | `b754467`（`chore: 迁移到 E 盘`） | 2026-10-24 01:11 |
| 当前分支 | `ss-v01-002/finalize-recovery` | 2026-10-24 01:11 |
| 工作区 | 干净（0 处未提交改动） | 2026-10-24 01:11 |
| 远程 | `origin` → `github.com/zwmhua2-ai-bot/SoloShip`（**私有**，当前分支未推送） | 2026-10-24 01:11 |

> 说明：本文件本身会随 SS-DOC-001 一起提交，因此**不能**记录提交后的新 SHA。
> 上面写的是文档编写时的基线；实际 HEAD 以 `git rev-parse HEAD` 为准。

## 2. 已完成任务

| 任务 | 结果 | 提交 |
| --- | --- | --- |
| SS-V01-001 统一安全门禁 | 完成 | `b1b5430` |
| SS-V01-002 最终化一致性与报告恢复 | 完成 | `f407d88` + `c63b926` |
| SS-V01-002-R1 完成后报告丢失恢复 | 完成 | `1e3f81e` |
| 仓库迁移至 E 盘 | 完成 | `b754467` |
| SS-DOC-001 交接文档体系 | 本文件所在任务 | 见交付报告 |

P00–P05B 阶段（交接到 Supervisor 自启）在**纳入 git 之前**完成，没有对应提交，
详见 `docs/DEVLOG.md` 的「未核实/无提交」说明。

## 3. 正在进行的任务

无。上一张卡（SS-V01-002-R1）已交付并通过文档验收前检查。

## 4. 下一张任务卡

**未指定。** 已知候选（均**未获批**，不得自行开工）：

- P05-C：未收尾/推迟任务恢复、`cmd_start` 存活二次确认、并行扩展。
- 收尾业务侧 PR #1（Draft，AC-04 `partially_covered`，`verified=False`）。

## 5. 最新测试结果与证据来源

| 项 | 值 |
| --- | --- |
| 命令 | `python -m unittest discover -s tests`（**不要加** `-t .`） |
| 结果 | `Ran 289 tests in 219.229s — OK` |
| 时间 | 2026-10-24 01:14:44（本地） |
| 证据来源 | 本机实际执行输出；文档编写前最后一次全量运行 |

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

## 8. 已知问题与风险

1. **旧目录残留**：`C:\Users\hua\SoloShip-P00` 迁移后无法重命名（Access Denied），内含过期 `.control` 数据库，已放 `_MIGRATED_TO_E.txt` 标记，**不得**再作为运行目录。
2. **Journal 之前的崩溃不可自愈**：Codex/publish 已完成但 Journal 未写时，任务停在 `ACCEPTED`，只能人工介入。
3. **并发恢复无互斥**：同时跑两个 `--recover` 可能产生一次多余写入（内容仍一致）。
4. **历史任务无完成证据清单**：报告若丢失只会进入 `NEEDS_HUMAN`，无法重建。
5. **`NEEDS_HUMAN` 无复位入口**：人工修复后需自行改库状态，暂无命令。
6. **文档中残留旧路径**：`docs/P05B-*.md` 与 `demo/soloship-demo/README.md` 属历史记录，保留原文，不作为操作依据。
7. **Demo 仓库四条本地分支**（`p01/*`、`p02/*` 共 4 条）未推送，功能已被 PR #1 覆盖，仅作过程证据。

## 9. 需要人工确认的事项

1. 业务侧 PR #1 是否评审合并（`allow_merge=false`，需显式授权）。
2. 旧目录何时删除（需先解除占用）。
3. 当前分支是否需要推送到 `origin`（**未授权前不得推**）。
4. Supervisor 是否继续常驻。

## 10. 新 AI 接手后的第一步

```bash
cd /e/Documents/AI-Developer/repositories/SoloShip      # 或 Windows 路径
git rev-parse HEAD && git status --short                # 核对版本与工作区
python -m unittest discover -s tests                    # 确认 289 项基线
python bridge/supervisor.py status --root .             # 重新核实运行态
```

然后：

1. 读本篇与 `AGENTS.md`；
2. 读当前任务卡，检索 `docs/DEVLOG.md` 相关条目；
3. 打开与任务相关的 `bridge/*.py`；
4. 若文档与代码冲突，**停止并报告**，不要自行假定哪一方正确。
