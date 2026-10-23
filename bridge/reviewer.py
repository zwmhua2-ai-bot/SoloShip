#!/usr/bin/env python3
"""SoloShip P02: independent reviewer.

The reviewer is a *separate* `codex exec` session with `-s read-only`, fed only
task + acceptance + diff + test evidence. Its answer must match
schemas/review.schema.json and quote the exact candidate SHA it reviewed.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import codex_adapter as codex

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "review.schema.json"
SEVERITIES = {"BLOCKER", "MAJOR", "MINOR", "INFO"}
BLOCKING = {"BLOCKER", "MAJOR"}


@dataclass
class ReviewResult:
    ok: bool
    status: str = "REVIEW_INVALID"      # REVIEW_APPROVED / REVIEW_CHANGES_REQUESTED / REVIEW_INVALID / REVIEW_TAMPERED
    reason: str = ""
    candidate_sha: str | None = None
    findings: list[dict] = field(default_factory=list)
    summary: str = ""
    raw: str = ""
    thread_id: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    run_kind: str = ""
    model: str | None = None
    effort: str | None = None
    usage: dict | None = None

    @property
    def blocking(self) -> list[dict]:
        return [f for f in self.findings if f.get("severity") in BLOCKING]

    def evidence(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "sandbox": "read-only",
            "candidate_sha": self.candidate_sha,
            "findings": self.findings,
            "summary": self.summary,
            "thread_id": self.thread_id,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "run_kind": self.run_kind,
            "model": self.model,
            "effort": self.effort,
            "usage": self.usage,
        }


def build_prompt(task: dict, base_sha: str, head_sha: str, diff_text: str,
                 test_result: dict, changed_paths: list[str]) -> str:
    acceptance = "\n".join(f"- {ac['id']}: {ac['description']}" for ac in task["acceptance"])
    tests = json.dumps(
        {
            "command": test_result.get("command"),
            "exit_code": test_result.get("exit_code"),
            "stdout_tail": test_result.get("stdout_tail", "")[-3000:],
        },
        ensure_ascii=False,
        indent=2,
    )
    return f"""你是 SoloShip 的独立代码审查员。你没有参与实现，不能修改任何文件。

审查对象：
- 任务：{task['title']}
- 目标：{task['goal']}
- 验收标准：
{acceptance}
- 约束：{'; '.join(task['constraints'])}
- base SHA：{base_sha}
- candidate SHA：{head_sha}
- 变更文件：{', '.join(changed_paths) or '无'}
- Bridge 独立执行的测试结果：
{tests}

完整 diff：
```diff
{diff_text[:60000]}
```

审查要点：
1. 需求是否完整实现，验收标准是否逐条满足；
2. 逻辑与边界条件（空值、重复项、空白输入等）；
3. 安全问题（越界文件访问、命令注入、凭证泄露、路径穿越）；
4. 兼容性（是否破坏原有行为）；
5. 不必要的复杂度或过度设计；
6. 测试覆盖缺口。

规则：
- 只读审查，不要修改、创建、删除任何文件，不要运行会改变状态的命令。
- 每条 finding 必须给出具体证据（引用 diff 里的行）和可执行的修复建议。
- 严重级别：BLOCKER（必须修，否则不能交付）、MAJOR（应当修）、MINOR（可选）、INFO（提示）。
- 只有当你认为不存在 BLOCKER/MAJOR 时才能给 APPROVED。
- candidate_sha 必须原样等于 {head_sha}，不得改写。

只输出一个 JSON 对象，字段严格为 status、candidate_sha、findings、summary：
{{"status": "APPROVED 或 CHANGES_REQUESTED", "candidate_sha": "{head_sha}", "findings": [{{"severity": "BLOCKER|MAJOR|MINOR|INFO", "file": "路径", "line": 0, "description": "问题", "evidence": "证据", "suggested_fix": "修复建议"}}], "summary": "一句话结论"}}
不要输出 JSON 以外的任何文字。
"""


def _validate_payload(payload: Any, head_sha: str) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "审查结果不是 JSON 对象"
    required = {"status", "candidate_sha", "findings", "summary"}
    missing = required - set(payload)
    if missing:
        return False, f"缺少字段: {', '.join(sorted(missing))}"
    extra = set(payload) - required
    if extra:
        return False, f"多余字段: {', '.join(sorted(extra))}"
    if payload["status"] not in ("APPROVED", "CHANGES_REQUESTED"):
        return False, f"status 非法: {payload['status']}"
    candidate = payload["candidate_sha"]
    if not isinstance(candidate, str) or len(candidate) < 7:
        return False, "candidate_sha 无效"
    if not (head_sha.startswith(candidate) or candidate.startswith(head_sha)):
        return False, f"审查版本不一致: review={candidate} head={head_sha[:12]}"
    if not isinstance(payload["summary"], str) or not payload["summary"].strip():
        return False, "summary 为空"
    findings = payload["findings"]
    if not isinstance(findings, list):
        return False, "findings 必须是数组"
    for index, item in enumerate(findings):
        if not isinstance(item, dict):
            return False, f"findings[{index}] 不是对象"
        need = {"severity", "file", "line", "description", "evidence", "suggested_fix"}
        if set(item) != need:
            return False, f"findings[{index}] 字段不符"
        if item["severity"] not in SEVERITIES:
            return False, f"findings[{index}].severity 非法"
        if not isinstance(item["line"], int) or item["line"] < 0:
            return False, f"findings[{index}].line 非法"
        for key in ("file", "description", "evidence", "suggested_fix"):
            if not isinstance(item[key], str) or not item[key].strip():
                return False, f"findings[{index}].{key} 为空"
    return True, ""


def _extract_json(text: str) -> Any:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def review(worktree: Path, task: dict, base_sha: str, head_sha: str, diff_text: str,
           test_result: dict, changed_paths: list[str], *, timeout_s: int = 900,
           codex_cmd: list[str] | None = None, artifacts_dir: Path | None = None,
           model: str | None = None, effort: str | None = None,
           ignore_user_config: bool = False) -> ReviewResult:
    """Run one read-only review session and validate its output."""
    prompt = build_prompt(task, base_sha, head_sha, diff_text, test_result, changed_paths)
    last_path = None
    if artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        last_path = artifacts_dir / "review.last.json"
        if last_path.exists():
            last_path.unlink()

    run = codex.run_codex(
        prompt,
        worktree,
        sandbox="read-only",
        timeout_s=timeout_s,
        codex_cmd=codex_cmd,
        last_message_path=last_path,
        extra_args=["--output-schema", str(SCHEMA_PATH)],
        model=model,
        effort=effort,
        ignore_user_config=ignore_user_config,
    )

    result = ReviewResult(
        ok=False,
        thread_id=run.thread_id,
        exit_code=run.exit_code,
        duration_ms=run.duration_ms,
        raw=(run.last_message or run.stdout or "")[:20000],
        run_kind=run.error_kind,
    )
    result.model = model
    result.effort = effort
    for event in run.events or []:
        if isinstance(event, dict) and event.get('type') == 'turn.completed':
            result.usage = event.get('usage')
    if not run.ok:
        result.status = "REVIEW_INVALID"
        result.reason = f"reviewer 执行失败: {run.error_kind} {run.detail}"
        return result

    payload = _extract_json(run.last_message) or _extract_json(run.stdout)
    if payload is None:
        result.status = "REVIEW_INVALID"
        result.reason = "审查输出不是可解析的 JSON"
        return result

    ok, reason = _validate_payload(payload, head_sha)
    if not ok:
        result.status = "REVIEW_INVALID"
        result.reason = reason
        return result

    result.candidate_sha = payload["candidate_sha"]
    result.findings = payload["findings"]
    result.summary = payload["summary"]

    if payload["status"] == "APPROVED" and result.blocking:
        result.status = "REVIEW_CHANGES_REQUESTED"
        result.reason = "审查自报 APPROVED 但存在 BLOCKER/MAJOR，按阻塞处理"
        return result
    if payload["status"] == "CHANGES_REQUESTED" and not result.blocking:
        result.status = "REVIEW_CHANGES_REQUESTED"
        result.reason = "审查要求修改，但只给出非阻塞项"
        return result

    result.status = "REVIEW_APPROVED" if payload["status"] == "APPROVED" else "REVIEW_CHANGES_REQUESTED"
    result.ok = result.status == "REVIEW_APPROVED"
    return result


def worktree_unchanged(worktree: Path, head_sha: str) -> tuple[bool, str]:
    """Reviewer must not touch the workspace: verify clean tree and same HEAD."""
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if status.returncode != 0 or head.returncode != 0:
        return False, "无法读取 worktree 状态"
    if status.stdout.strip():
        return False, f"审查期间工作区被修改: {status.stdout.strip()[:300]}"
    if head.stdout.strip() != head_sha:
        return False, f"审查期间 HEAD 变化: {head.stdout.strip()[:12]} != {head_sha[:12]}"
    return True, ""
