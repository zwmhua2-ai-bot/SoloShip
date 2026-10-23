# SoloShip B-Lite：Windows 11 · P00 本地交接验证
> **当前部署路径**：`E://Documents//AI-Developer//repositories//SoloShip`。
> 下文描述的是压缩包安装方式（解到 `%USERPROFILE%\SoloShip-P00`），
> 与本机现部署位置不同，按文中路径操作前请先对照实际目录。


适用于：Windows 11 + ChatGPT 桌面 Work + Codex 桌面端。**P00 不要求安装 Codex CLI**，只需要 Python 3.11+。

> P00 是可运行的测试接收器，不是完整的开发工作流。它不会运行 Codex、改动 Git 项目、执行传入命令或触发 GitHub PR。

## 1. 解压

把下载的 `SoloShip-P00-Windows.zip` 放到“下载”目录。打开 PowerShell：

```powershell
Expand-Archive -Path "$HOME\Downloads\SoloShip-P00-Windows.zip" -DestinationPath "$HOME" -Force
Set-Location "$HOME\SoloShip-P00"
```

如果下载文件名或路径不一样，将 `-Path` 修改为实际下载位置。

## 2. 检查 Python

```powershell
py -3 --version
```

需要 Python 3.11 或更新版本。如果没有 `py` 启动器，可试 `python --version` 并把后续的 `py -3` 换成 `python`。

## 3. 运行本地测试

```powershell
py -3 -m unittest discover -s tests -v
py -3 -m unittest discover -s demo/soloship-demo/tests -v
```

预期测试全部通过。第二个命令对应后续使用的演示项目。

## 4. 启动 Bridge

在当前 PowerShell 中执行：

```powershell
py -3 -u .\bridge\p00_bridge.py
```

保持窗口运行，它会监测 `exchange\inbox\*.ready.json`。按 Ctrl+C 停止。

## 5. 先做不依赖 ChatGPT Work 的本地自检（可选）

另开一个 PowerShell 窗口：

```powershell
Set-Location "$HOME\SoloShip-P00"
py -3 .\bridge\send_local_test.py
```

回到 Bridge 窗口，应看到 `[ACCEPTED]`。在 `exchange\reports` 可读到对应 JSON 与 Markdown 报告。这只检查 Bridge，不证明 Work 本地写入。

## 6. 用 ChatGPT 桌面 Work 验证真正的自动交接（必要）

1. 在 ChatGPT **桌面应用**切换 Work，而不是网页端的 Work。
2. 仅向 Work 授予这个目录的本地文件访问权限：`%USERPROFILE%\SoloShip-P00\exchange`。不要授权整个用户目录或 `.control`。
3. 在 Work 中发送根目录 `WORK-PROMPT.txt` 的内容（也可从本文件下方复制）。
4. Work 应读取模板并在 `exchange\inbox` 写入 pending 文件，然后重命名为 ready 文件。
5. Bridge 窗口应显示 `[ACCEPTED] demo-app__p00_work_trial_01`。
6. Work 应读取 `exchange\receipts\demo-app__p00_work_trial_01.receipt.json` 和 `exchange\reports\demo-app__p00_work_trial_01.report.md`。

如果 Work 表示本地文件操作不可用：**P00 未通过**。不要把创建下载附件或聊天里的 JSON 当作成功。检查桌面客户端和本地文件授权，之后再决定适配其他入口。

### 直接发给 Work 的指令

```text
我们正在做 SoloShip 的 P00 自动交接测试。请只在我已经授权的本地 exchange 文件夹中操作，不要修改其他文件或调用 Codex。

1. 读取 templates/p00-work-task.json 的 JSON 内容。
2. 将它写入 inbox/p00_work_trial_01.pending.json。
3. 写入完成后，将该文件重命名为 inbox/p00_work_trial_01.ready.json。（必须是真实本地文件操作，不是在聊天中显示 JSON，也不能只生成下载附件。）
4. 如果后台 Bridge 已运行，读取 receipts/demo-app__p00_work_trial_01.receipt.json 和 reports/demo-app__p00_work_trial_01.report.md，报告 job_id、状态和具体证据。
5. 若本地文件访问、写入或重命名缺乏权限，请明确说明卡在哪一步；不要假称已经提交任务。

注意：这一阶段报告 P00_HANDOFF_OK 只代表交接成功，不代表 Codex 已经开发、测试或生成 PR。
```

## 7. P00 验收标准

- Work 确实向 Windows 本地 `inbox` 提交任务，而非返回附件。
- Bridge 验证任务并自动生成 `receipt.json` 与 `report.md`。
- Work 能读回报告并说出实际 job_id。
- `reports/*.report.json` 明确包含 `real_codex_run: false`、`tests_executed: false`、`github_pr_created: false`。
- 不应发生项目代码修改或 GitHub 操作。

## 8. 下一步 P01 / P02 的环境检查

Codex 桌面端能用，不等于操作系统已经安装 `codex` 命令。之后请在 PowerShell 单独检查：

```powershell
codex --version
git --version
gh --version
```

若 `codex` 找不到，可参照官方最新 Codex CLI 文档安装（通常需要 Node.js / npm）：

```powershell
npm install -g @openai/codex
codex login
codex --version
```

`gh` 是后续 P04 提交 PR 才需要的；无需为 P00 提前配置 GitHub Token。完整 B-Lite 会使用隔离 Git 工作区、独立代码审查及真实检查，和这里的模拟回执不同。

## 安全说明

- 本工具只消费 `*.ready.json`，不会执行 JSON 里的代码或命令。
- 只接受 `project_id=demo-app` 与固定 Schema，拒绝未知字段。
- 状态数据库写在 `.control`，不需要向 Work 授权它。
- `request_id` 的重复提交会返回同一任务编号；不同内容重复使用同一 ID 会拒绝。
- 请不要把密钥、真实生产数据或仓库凭据写入交接目录。
