#!/usr/bin/env python3
"""SoloShip P04-C: AI 验收助手。

只读任务原件与报告，产出可核对的验收结论：
  exchange/p04/acceptance/<task_id>.acceptance.md / .json

硬约束：
  * 不发起任何网络请求，不合并、不关闭、不评论任何 PR。
  * 不修改原始报告；只写 p04 目录下的新文件。
  * 结论必须标明依据；无法从真实数据得出的，一律标为 unverified。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import status_index

P04_REL = Path("exchange") / "p04"

STOPWORDS = {"the", "and", "for", "with", "return", "returns", "must", "should",
             "true", "false", "test", "able"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=".write-", suffix=".tmp",
                                         dir=str(path.parent), delete=False) as fh:
            tmp = fh.name
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
    return {word.lower() for word in words if word.lower() not in STOPWORDS}


def _changed_files(diff_text: str) -> list[str]:
    files = []
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            path = line[6:].strip()
            if path and path not in files:
                files.append(path)
    return files


def _read_diff(root: Path, report: dict) -> str:
    for meta in (report.get("artifacts") or {}).values():
        path = Path(str(meta.get("path", "")))
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return ""


def _test_names(report: dict) -> list[str]:
    tail = ((report.get("tests") or {}).get("stderr_tail") or "")
    names = []
    for line in tail.splitlines():
        match = re.match(r"^(test_[A-Za-z0-9_]+)", line.strip())
        if match:
            names.append(match.group(1))
    return names


AC_COVERED = "covered_by_test_names"
AC_PARTIAL = "partially_covered"
AC_UNVERIFIED = "unverified"


AC_COVERED = "covered_by_test_names"          # 已废弃：仅保留常量兼容旧报告读取
AC_ASSERTION_SEEN = "assertion_seen_in_diff"   # 有测试名线索 + diff 里看到断言（仍非语义证明）
AC_NAME_ONLY = "test_name_only"                # 只有测试名线索
AC_PARTIAL = "partially_covered"               # 关键词只命中一部分
AC_UNVERIFIED = "unverified"
AC_MANUAL = "manual_pending"                   # 人工验收项：不得由自动证据判定

STATUS_ORDER = (AC_ASSERTION_SEEN, AC_NAME_ONLY, AC_PARTIAL, AC_MANUAL, AC_UNVERIFIED)


def _review_binding(report: dict, candidate_sha: str | None) -> dict:
    """Reviewer 是否审查了同一个 candidate（SS-V01-AC-001）。

    Reviewer 只有总体结论时，不能伪造逐条 AC 审查证据；这里只回答一个问题：
    这条审查结论能不能绑定到当前 candidate SHA 上。
    """
    reviews = report.get("reviews") or []
    last = reviews[-1] if reviews and isinstance(reviews[-1], dict) else {}
    review_sha = last.get("candidate_sha")
    base = {"status": last.get("status"), "candidate_sha": review_sha,
            "source": "report.reviews[-1]", "bound": False, "reason": ""}
    if not review_sha:
        base["reason"] = "Reviewer 未回报 candidate_sha，无法确认审查对象"
        return base
    if not candidate_sha:
        base["reason"] = "任务没有 candidate SHA，无法核对审查对象"
        return base
    if review_sha != candidate_sha:
        base["reason"] = (f"Reviewer 审查的是 {review_sha[:12]}，"
                          f"与 candidate {candidate_sha[:12]} 不一致")
        return base
    base["bound"] = True
    return base


def _test_bodies(diff_text: str) -> dict[str, dict]:
    """从 diff 的 + 行里抽出测试函数体，统计断言条数。

    只做"有没有断言"的事实统计，不做语义匹配——命名相同不代表测的是同一件事。
    """
    bodies: dict[str, dict] = {}
    current = None
    for raw in (diff_text or "").splitlines():
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        line = raw[1:]
        head = re.match(r"\s*def (test_[A-Za-z0-9_]+)", line)
        if head:
            current = head.group(1)
            bodies[current] = {"assertions": 0}
            continue
        if current is None:
            continue
        if re.match(r"\s*(class |def )", line):
            current = None
            continue
        if re.search(r"\bassert", line):
            bodies[current]["assertions"] += 1
    return bodies


def _possible_tests(names: list[str], missing: list[str]) -> list[str]:
    """缺失关键词是否有"名字相近"的用例：这是线索，不是覆盖。"""
    hints = []
    for token in missing:
        parts = [p for p in token.split("_") if len(p) >= 3]
        for name in names:
            lowered = name.lower()
            if name in hints:
                continue
            if any(part in lowered for part in parts):
                hints.append(name)
    return hints


def _ac_review(task: dict, report: dict, diff_text: str = "",
               candidate_sha: str | None = None, review_binding: dict | None = None,
               report_path: str | None = None) -> list[dict]:
    names = _test_names(report)
    joined = " ".join(names).lower()
    bodies = _test_bodies(diff_text)
    diff_available = bool((diff_text or "").strip())
    tests_ok = (report.get("tests") or {}).get("exit_code") == 0
    tests = report.get("tests") or {}
    binding = review_binding or {"bound": False, "reason": "没有审查绑定信息"}

    results = []
    for item in task.get("acceptance") or []:
        if not isinstance(item, dict):
            continue
        tokens = _tokens(str(item.get("description", "")))
        matched = sorted(token for token in tokens if token in joined)
        missing = sorted(token for token in tokens if token not in joined)
        matched_tests = [name for name in names
                         if any(token in name.lower() for token in matched)]
        assertions = sum((bodies.get(name) or {}).get("assertions", 0)
                         for name in matched_tests)
        verification = str(item.get("verification") or "unspecified")
        is_manual = verification not in ("automated", "unspecified")

        if is_manual:
            status = AC_MANUAL
            basis = "人工验收项：不得由自动证据判定"
            uncertainty = "必须由人按原始描述逐条确认"
        elif not tests_ok:
            status = AC_UNVERIFIED
            basis = "测试未通过，无法据此判定该项已实现"
            uncertainty = "测试结论为失败或缺失"
        elif not tokens:
            status = AC_UNVERIFIED
            basis = "描述中没有可自动匹配的标识符，无法据此核对"
            uncertainty = "只能由人读代码或手测确认"
        elif matched and missing:
            status = AC_PARTIAL
            basis = ("测试名只命中 " + ", ".join(matched) + "，未命中 "
                     + ", ".join(missing) + "；需人工确认")
            uncertainty = "未命中可能是命名差异，也可能确实没覆盖"
        elif not matched:
            status = AC_UNVERIFIED
            basis = "测试输出里没有任何用例名命中该项关键词，无法据此核对"
            uncertainty = "只能由人读代码或手测确认"
        elif matched and assertions:
            status = AC_ASSERTION_SEEN
            basis = (f"测试名命中 {', '.join(matched)}，且 diff 中对应用例有 {assertions} 处断言；"
                     "断言是否覆盖该验收项的语义仍需人工判定")
            uncertainty = "断言只证明代码在断言某些性质，不证明与本条描述等价"
        elif matched and not diff_available:
            status = AC_NAME_ONLY
            basis = "仅测试名命中 " + ", ".join(matched) + "，diff 不可用，无法核对断言"
            uncertainty = "只有命名线索；不能据此认定行为已验证"
        else:
            status = AC_NAME_ONLY
            basis = "仅测试名命中 " + ", ".join(matched) + "，未见断言证据"
            uncertainty = "只有命名线索；不能据此认定行为已验证"

        # ---- 证据矩阵（SS-V01-AC-001）：每条 AC 都能回答"证据在哪、缺什么" ----
        missing_evidence: list[str] = []
        if not candidate_sha:
            missing_evidence.append("缺少 candidate SHA")
        if not tests_ok:
            missing_evidence.append("测试未通过或没有测试记录")
        if not names:
            missing_evidence.append("没有可引用的测试用例名（report.tests.stderr_tail）")
        if not diff_available:
            missing_evidence.append("diff 证据不可用，无法核对断言")
        if not matched and not is_manual:
            missing_evidence.append("没有测试名命中本条关键词")
        if not binding.get("bound"):
            missing_evidence.append(f"Reviewer 未绑定同一 candidate SHA：{binding.get('reason')}")

        if is_manual:
            human_action = f"人工验收：{item.get('description')}"
        elif status == AC_UNVERIFIED:
            human_action = "人工读代码或手测，确认本条是否满足"
        elif status in (AC_PARTIAL, AC_NAME_ONLY):
            human_action = "人工确认现有测试是否覆盖本条语义"
        else:
            human_action = "人工确认断言语义是否等价于本条描述"

        entry = {
            "id": item.get("id"),
            "description": item.get("description"),
            "status": status,
            # 关键约束：任何自动判定都不构成"已验证"
            "verified": False,
            "verdict": "needs_human_review",
            "matched_tokens": matched,
            "missing_tokens": missing,
            "matched_tests": matched_tests,
            "assertions_in_diff": assertions,
            "possible_tests": _possible_tests(names, missing),
            "basis": basis,
            "uncertainty": uncertainty,
            # ---- 以下为证据矩阵字段 ----
            "ac_id": item.get("id"),
            "verification": verification,
            "candidate_sha": candidate_sha,
            "coverage_status": status,
            "test_evidence": {
                "command": tests.get("command"),
                "exit_code": tests.get("exit_code"),
                "test_names": names,
                "matched_tests": matched_tests,
                "assertions_in_diff": assertions,
                "report_path": report_path,
            },
            "review_evidence": {
                "status": binding.get("status"),
                "candidate_sha": binding.get("candidate_sha"),
                "bound": bool(binding.get("bound")),
                "reason": binding.get("reason", ""),
                "source": binding.get("source"),
            },
            "missing_evidence": missing_evidence,
            "human_action": human_action,
        }
        results.append(entry)
    return results


def _risks(record: dict, report: dict, acs: list[dict],
           review_binding: dict | None = None) -> list[str]:
    risks = []
    sandbox = report.get("sandbox_status")
    if sandbox and sandbox != "SANDBOX_ENFORCED":
        risks.append(f"执行环境非强制隔离：`{sandbox}`，"
                     f"isolation_claim=`{report.get('isolation_claim', 'NONE')}`；"
                     "路径白名单只是事后检测。")
    if record.get("pr", {}).get("state") == "created":
        risks.append("PR 为 Draft，未合并、未部署；合并需人工决定。")
    if record.get("pr", {}).get("state") == "none":
        risks.append("没有 PR 记录：改动仅存在于本地分支。")
    if (record.get("review") or {}).get("status") in (None, ""):
        risks.append("Review 状态缺失，独立审查证据不完整。")
    if (record.get("review") or {}).get("blocking_findings"):
        risks.append(f"Review 报出 {record['review']['blocking_findings']} 项阻塞问题。")
    if review_binding is not None and not review_binding.get("bound"):
        risks.append(f"Reviewer 未绑定同一 candidate SHA（{review_binding.get('reason')}），"
                     "审查证据不可采信。")
    name_only = [ac["id"] for ac in acs if ac["status"] == AC_NAME_ONLY]
    partial = [ac["id"] for ac in acs if ac["status"] == AC_PARTIAL]
    pending = [ac["id"] for ac in acs if ac["status"] == AC_UNVERIFIED]
    if acs:
        risks.append("验收项结论均为线索级（verified=False）：测试名/断言只能提示，"
                     "语义是否正确必须人工判定。")
    if partial:
        risks.append("以下验收项只被测试名部分覆盖，需人工确认："
                     + ", ".join(str(x) for x in partial))
    if name_only:
        risks.append("以下验收项只有测试名线索、未见断言证据："
                     + ", ".join(str(x) for x in name_only))
    if pending:
        risks.append("以下验收项未能自动核对：" + ", ".join(str(x) for x in pending))
    models = {call.get("actual_model") for call in report.get("calls") or []}
    if models and models <= {"UNVERIFIED", None}:
        risks.append("实际使用的模型无法从运行记录验证（请求模型≠实际模型）。")
    return risks


def _manual_checks(record: dict, files: list[str]) -> list[str]:
    checks = ["打开 PR 页面逐行读 diff，确认改动范围只落在允许路径内。"]
    if record.get("pr", {}).get("url"):
        checks.append(f"在 PR 上本地复跑测试：`py -3 -m unittest discover -s tests -v`。")
    for path in files[:5]:
        checks.append(f"人工检查 `{path}` 的边界行为（空输入、重复项、异常路径）。")
    if not files:
        checks.append("diff 未解析出文件清单，需人工确认实际改动。")
    return checks


def _draft_fix(record: dict, report: dict, task: dict, acs: list[dict],
               root: Path) -> dict | None:
    blocking = (record.get("review") or {}).get("blocking_findings") or 0
    tests_ok = (record.get("tests") or {}).get("ok")
    pending = [ac for ac in acs if ac["status"] == AC_UNVERIFIED]
    weak = [ac for ac in acs if ac["status"] in (AC_PARTIAL, AC_NAME_ONLY)]
    needs_fix = blocking > 0 or tests_ok is False or record.get("state") in (
        status_index.STATE_BLOCKED, status_index.STATE_FAILED)
    if not needs_fix:
        return None

    base_id = record.get("task_id") or "task"
    index = 1
    while True:
        candidate = f"{base_id}_fix{index}"
        if len(candidate) <= 80 and not (
                Path(root) / "exchange" / "p03" / f"{candidate}.task.json").exists():
            break
        index += 1

    symptoms = []
    if blocking:
        symptoms.append(f"Review 阻塞项 {blocking} 条")
    if tests_ok is False:
        symptoms.append("测试未通过")
    if pending:
        symptoms.append("未覆盖验收项 " + ", ".join(str(ac["id"]) for ac in pending))
    elif weak:
        symptoms.append("证据不足的验收项 " + ", ".join(str(ac["id"]) for ac in weak))

    draft = {
        "schema_version": "1.0",
        "request_id": candidate,
        "project_id": task.get("project_id") or record.get("project_id"),
        "title": f"修复：{task.get('title') or record.get('task_id')}",
        "goal": "修复上一轮未通过项：" + "；".join(symptoms) + "。保持既有行为不变。",
        "requested_paths": task.get("requested_paths") or ["src/**", "tests/**"],
        "constraints": task.get("constraints") or [],
        "non_goals": ["扩大功能范围", "修改 Bridge 或任务协议"],
        "acceptance": task.get("acceptance") or [],
        "workspace": task.get("workspace") or {},
        "verify": task.get("verify") or {},
        "codex": task.get("codex") or {},
        "draft_of": record.get("task_id"),
        "generated_by": "p04-acceptance-assistant",
        "generated_at": _now(),
    }
    return draft


def build(root: Path, request_id: str | None = None) -> dict:
    root = Path(root).resolve()
    status_index.refresh(root)
    if request_id is None:
        latest = status_index.load_latest(root)
        record = latest.get("task")
        if not record:
            return {"ok": False, "error": "索引里没有真实开发任务记录"}
    else:
        record = status_index.find_task(root, request_id)
        if not record:
            return {"ok": False, "error": f"索引里未找到任务 {request_id}"}

    task_path = root / "exchange" / "p03" / f"{record['task_id']}.task.json"
    task = _load(task_path) if task_path.is_file() else {}
    report = {}
    if record.get("report_path"):
        try:
            report = _load(root / record["report_path"])
        except (ValueError, OSError):
            report = {}

    diff_text = _read_diff(root, report)
    files = _changed_files(diff_text)
    candidate_sha = record.get("head_commit") or report.get("head_commit")
    review_binding = _review_binding(report, candidate_sha)
    acs = _ac_review(task, report, diff_text, candidate_sha, review_binding,
                     record.get("report_path"))
    risks = _risks(record, report, acs, review_binding)
    tests = report.get("tests") or {}
    tail = tests.get("stderr_tail") or ""
    test_count = None
    match = re.search(r"Ran (\d+) tests?", tail)
    if match:
        test_count = int(match.group(1))

    summary = {
        "ok": True,
        "generated_at": _now(),
        "task_id": record["task_id"],
        "job_id": record.get("job_id"),
        "state": record.get("state"),
        "status": record.get("status"),
        "title": task.get("title") or record.get("title"),
        "goal": task.get("goal") or record.get("goal"),
        "pr": record.get("pr"),
        "report_path": record.get("report_path"),
        "fix_rounds": record.get("fix_rounds"),
        "changed_files": files,
        "tests": {"ok": tests.get("exit_code") == 0, "exit_code": tests.get("exit_code"),
                  "passed": test_count, "command": tests.get("command")},
        "review": record.get("review"),
        "acceptance": acs,
        # 自动判定永远不构成"已验证"：本字段恒为 False，防止下游误读。
        "acceptance_verified": False,
        "acceptance_note": ("验收结论只是线索（测试名 / diff 中的断言），"
                            "语义正确性需人工判定"),
        # ---- 证据矩阵（SS-V01-AC-001）----
        "candidate_sha": candidate_sha,
        "review_binding": review_binding,
        "evidence_matrix": {
            "candidate_sha": candidate_sha,
            "report_path": record.get("report_path"),
            "report_md_path": record.get("report_md_path"),
            "receipt_path": record.get("receipt_path"),
            "tests": {"ok": tests.get("exit_code") == 0,
                      "exit_code": tests.get("exit_code"),
                      "passed": test_count, "command": tests.get("command")},
            "review_bound": review_binding.get("bound"),
            "acs": {ac["ac_id"]: ac["coverage_status"] for ac in acs},
            "generated_by": "p04-acceptance-assistant",
        },
        "risks": risks,
        "manual_checks": _manual_checks(record, files),
        "auto_merge": False,
    }
    summary["draft_fix"] = _draft_fix(record, report, task, acs, root)
    return summary


def render_markdown(data: dict) -> str:
    if not data.get("ok"):
        return f"# 验收失败\n\n{data.get('error')}\n"
    lines = [
        f"# AI 验收报告 · {data['task_id']}", "",
        f"- 生成时间：`{data['generated_at']}`",
        f"- 任务状态：`{data['status']}` → `{data['state']}`",
        f"- Job：`{data['job_id']}`",
        f"- 原始报告：`{data.get('report_path')}`",
        f"- PR：{('`' + data['pr']['url'] + '`') if data['pr'].get('url') else '无'}",
        f"- 自动合并：**否**（本助手不执行合并）", "",
        "## 完成的功能", "",
        f"- 标题：{data.get('title') or '-'}",
        f"- 目标：{data.get('goal') or '-'}",
        f"- 改动文件：{', '.join('`' + f + '`' for f in data['changed_files']) or '未从 diff 解析出'}",
        f"- 测试：退出码 `{data['tests']['exit_code']}`，用例数 `{data['tests'].get('passed')}`",
        f"- 修复轮次：`{data.get('fix_rounds', 0)}`", "",
        "## 验收标准核对", "",
        "> 下表的结论都是**线索级**：`assertion_seen_in_diff` 只表示 diff 里对应用例有断言，",
        "> 不等于该验收项的语义已被验证；`test_name_only` / `partially_covered` 更弱。",
        "> 每条都标 `verified=False`，最终判定必须由人完成。", "",
        "| 编号 | 描述 | 结论 | 依据 |",
        "|---|---|---|---|",
    ]
    for ac in data["acceptance"]:
        lines.append(f"| {ac['id']} | {ac['description']} | `{ac['status']}` | {ac['basis']} |")
    hints = [ac for ac in data["acceptance"] if ac.get("possible_tests")]
    if hints:
        lines += ["", "可能相关但未直接命中的用例（仅供人工追踪，不作为证据）：", ""]
        for ac in hints:
            lines.append(f"- `{ac['id']}` ← " + ", ".join(f"`{n}`" for n in ac["possible_tests"]))

    # ---- 证据矩阵：每条 AC 的证据位置、缺失项与人工动作 ----
    matrix = data.get("evidence_matrix") or {}
    binding = data.get("review_binding") or {}
    lines += ["", "## 证据矩阵", "",
              f"- Candidate SHA：`{data.get('candidate_sha') or '缺失'}`",
              f"- 测试证据：退出码 `{matrix.get('tests', {}).get('exit_code')}`，"
              f"用例数 `{matrix.get('tests', {}).get('passed')}`"
              f"（证据位置：`{matrix.get('report_path') or '缺失'}`）",
              f"- Reviewer 绑定：{'是' if binding.get('bound') else '否'}"
              f"（{binding.get('reason') or ('已绑定到同一 candidate' if binding.get('bound') else '未提供原因')}）",
              f"- 自动合并：**否**；`acceptance_verified=False` 恒成立", "",
              "| AC | verification | 覆盖结论 | Candidate | 测试证据 | Reviewer 绑定 | 缺失证据 | 人工动作 |",
              "|---|---|---|---|---|---|---|---|"]
    for ac in data["acceptance"]:
        test_ev = ac.get("test_evidence") or {}
        review_ev = ac.get("review_evidence") or {}
        tests_ref = (f"退出码 {test_ev.get('exit_code')}；"
                     f"命中 {len(test_ev.get('matched_tests') or [])} 个用例")
        lines.append(
            f"| {ac.get('ac_id')} | {ac.get('verification')} | `{ac.get('coverage_status')}` "
            f"| `{(ac.get('candidate_sha') or '缺失')[:12]}` | {tests_ref} "
            f"| {'是' if review_ev.get('bound') else '否'} "
            f"| {'；'.join(ac.get('missing_evidence') or []) or '—'} "
            f"| {ac.get('human_action')} |")
    lines += ["", "## 未验证事项与剩余风险", ""]
    lines += [f"- {item}" for item in data["risks"]] or ["- 无已识别风险（不代表无风险）"]
    lines += ["", "## 需要人工体验的检查项", ""]
    lines += [f"{index}. {item}" for index, item in enumerate(data["manual_checks"], 1)]
    draft = data.get("draft_fix")
    lines += ["", "## 修复任务草案", ""]
    if draft:
        lines += [f"- 已生成（未执行）：`{draft['request_id']}`",
                  f"- 目标：{draft['goal']}"]
    else:
        lines.append("- 未生成：本轮没有测试失败、审查阻塞或阻断状态。")
    lines += ["", "## 边界", "",
              "- 本报告只依据 `exchange/` 下的真实文件生成，未推测聊天历史。",
              "- 未发起任何 GitHub 写入操作。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SoloShip P04-C AI 验收助手")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--request-id", default=None, help="缺省读取 latest.json 的最新开发任务")
    parser.add_argument("--print", dest="do_print", action="store_true", help="只打印不落盘")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    data = build(root, args.request_id)
    if args.do_print:
        print(render_markdown(data))
        return 0 if data.get("ok") else 1

    if not data.get("ok"):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 1

    out_dir = root / P04_REL / "acceptance"
    md_path = out_dir / f"{data['task_id']}.acceptance.md"
    json_path = out_dir / f"{data['task_id']}.acceptance.json"
    _write_atomic(md_path, render_markdown(data))
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    if data.get("draft_fix"):
        draft_path = root / P04_REL / "drafts" / f"{data['draft_fix']['request_id']}.task.json"
        _write_atomic(draft_path, json.dumps(data["draft_fix"], ensure_ascii=False, indent=2) + "\n")
        data["draft_path"] = str(draft_path)

    print(json.dumps({"ok": True, "task_id": data["task_id"], "state": data["state"],
                      "pr_url": data["pr"].get("url"), "acceptance_md": str(md_path),
                      "acceptance_json": str(json_path),
                      "draft_fix": (data.get("draft_fix") or {}).get("request_id")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
