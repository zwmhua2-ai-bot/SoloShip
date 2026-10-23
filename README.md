# SoloShip

本地 AI 自动开发流水线：把结构化任务交接给 Codex，在独立 worktree 中实现、自测、审查、产出 Draft PR，并留下可追溯的验收证据。

**当前状态**：P00–P05B 已完成，安全门禁与最终化恢复已加固；业务侧 PR #1 仍为 Draft 待人工评审。
（PID、分支、心跳等实时数字见 `docs/AI_HANDOFF.md`，且必须自行复核。）

## 文档导航

| 我想… | 看这份 |
| --- | --- |
| 立刻上手 / 知道现在该做什么 | [docs/AI_HANDOFF.md](docs/AI_HANDOFF.md) |
| 知道 AI 必须遵守什么规则 | [AGENTS.md](AGENTS.md) |
| 知道以前发生了什么、为什么这么设计 | [docs/DEVLOG.md](docs/DEVLOG.md) |
| 跑命令、排查故障、备份恢复 | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| 看阶段进度、任务进度、报告索引 | [PROGRESS.md](PROGRESS.md) |
| 在 Windows 上安装 | [README-WINDOWS.md](README-WINDOWS.md) |

## 三条最快上手路径

```bash
git rev-parse HEAD && git status --short          # 我在哪个版本、有没有未提交改动
python -m unittest discover -s tests              # 基线：315 项（注意不要加 -t .）
python bridge/supervisor.py status --root .       # 服务是否在跑
```

## 目录结构

```
bridge/     流水线实现（Supervisor、P00–P05 各阶段、安全门禁、恢复）
tests/      回归测试（315 项，Codex 一律用 fake_codex.py）
scripts/    启动脚本与登录自启安装
config/     bridge.json（不含任何 Token）
docs/       交接、过程、操作手册与阶段报告
exchange/   任务、回执、报告、状态索引（运行数据，不入 git）
schemas/    评审数据结构
```

## 明确不做的事

- 不自动合并 PR、不自动部署、不擅自开启沙箱豁免。
- 不删除或改写历史报告与执行证据。
- 未获授权不推送远程。

遇到未知来源的工作区改动，按 `AGENTS.md §9` 停工并报告。
