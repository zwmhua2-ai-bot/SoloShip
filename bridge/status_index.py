#!/usr/bin/env python3
"""SoloShip P04-A: 统一任务状态索引。

只读现有记录（P00/P01/P02 回执、P02 报告、P03 发布日志），不修改任何原始报告，
在 exchange/status/ 下生成三份便于外部（ChatGPT Work）读取的索引：

    latest.json               最新一次「真实开发任务」
    index.json                全部任务索引（按时间倒序）
    projects/<project>.json   单项目摘要

写盘一律先写临时文件再 os.replace，避免读取方拿到半份 JSON。
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

SCHEMA = "soloship.status/v1"
STATUS_DIR_REL = Path("exchange") / "status"

KIND_DEVELOPMENT = "development"
KIND_DEMO = "demo"
KIND_HANDOFF = "handoff"
KIND_UNKNOWN = "unknown"

STATE_PR_CREATED = "pr_created"
STATE_PR_MERGED = "pr_merged"
STATE_PR_CLOSED = "pr_closed"
STATE_WAITING_REVIEW = "waiting_human_review"
STATE_DEV_DONE = "dev_done"
STATE_BLOCKED = "blocked"
STATE_FAILED = "failed"
STATE_HANDOFF_OK = "handoff_ok"
STATE_HANDOFF_FAILED = "handoff_failed"
STATE_UNKNOWN = "unknown"

PR_OK_STATUSES = ("PUBLISH_PR_CREATED", "PUBLISH_PR_EXISTS", "PUBLISH_PR_RECOVERED")
PR_MERGED_STATUSES = ("PUBLISH_PR_MERGED",)

# 给读取方（ChatGPT Work / 人）直接可用的一句话状态，避免把 pr_created 误读成"已上线"。
STATE_LABELS = {
    STATE_PR_MERGED: "PR 已合并",
    STATE_PR_CREATED: "PR 已创建（Draft，未合并）",
    STATE_PR_CLOSED: "PR 已关闭（未合并）",
    STATE_WAITING_REVIEW: "等待人工验收（未发布）",
    STATE_DEV_DONE: "开发完成（未发布）",
    STATE_BLOCKED: "被阻塞，需人工介入",
    STATE_FAILED: "失败，需人工介入",
    STATE_HANDOFF_OK: "交接完成",
    STATE_HANDOFF_FAILED: "交接失败",
    STATE_UNKNOWN: "状态未知",
}

# 这些状态下"下一步动作在人手上"，而不是在流水线里。
HUMAN_ACTION_STATES = (STATE_WAITING_REVIEW, STATE_PR_CREATED, STATE_PR_CLOSED,
                       STATE_BLOCKED, STATE_FAILED)

# P05-A：GitHub 实时状态 → 任务状态。实时观测只做"向上修正"，
# 历史执行记录（publish 日志）始终原样保留在 pr.state 里。
REALTIME_STATE_MAP = {
    "merged": STATE_PR_MERGED,
    "draft": STATE_PR_CREATED,
    "open": STATE_PR_CREATED,
    "closed": STATE_PR_CLOSED,
}

REALTIME_DIR_REL = Path("exchange") / "status" / "realtime"

# Codex 会话里没有可验证的模型标识，索引里如实保留报告的原始值。
UNVERIFIED = "UNVERIFIED"


# ------------------------------------------------------------------ plumbing
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_atomic(path: Path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=".write-", suffix=".tmp",
                                         dir=str(path.parent), delete=False) as fh:
            tmp_name = fh.name
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


# --------------------------------------------------------------- discovery
def _config(root: Path) -> dict:
    path = Path(root).resolve() / "config" / "bridge.json"
    if not path.is_file():
        return {}
    try:
        data = _load(path)
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def approved_repos(root: Path) -> set[str]:
    mode = (_config(Path(root)).get("windows_test_mode") or {})
    resolved = set()
    for item in mode.get("approved_absolute_repos") or []:
        try:
            resolved.add(str(Path(item).resolve()).lower())
        except (OSError, RuntimeError):
            continue
    return resolved


def classify_kind(phase: str, repo: str | None, approved: set[str]) -> str:
    """交接测试 / 演示任务 / 真实开发任务的判定，不依赖文件名。

    仓库未知时归为 unknown：既不冒充开发任务，也不冒充演示任务。
    """
    if phase == "P00":
        return KIND_HANDOFF
    if not (isinstance(repo, str) and repo.strip()):
        return KIND_UNKNOWN
    try:
        if str(Path(repo).resolve()).lower() in approved:
            return KIND_DEVELOPMENT
    except (OSError, RuntimeError):
        return KIND_UNKNOWN
    return KIND_DEMO


def _publish_events(root: Path) -> dict[str, dict]:
    """按 job_id 归集 P03 发布日志。"""
    journal_dir = Path(root).resolve() / ".control" / "p03" / "publish"
    result: dict[str, dict] = {}
    if not journal_dir.is_dir():
        return result
    for path in sorted(journal_dir.glob("*.json")):
        try:
            data = _load(path)
        except (ValueError, OSError):
            continue
        if isinstance(data, dict) and data.get("job_id"):
            result[str(data["job_id"])] = data
    return result


def _state(phase: str, status: str, publish: dict) -> str:
    publish_status = (publish or {}).get("status")
    if publish_status in PR_MERGED_STATUSES:
        return STATE_PR_MERGED
    if publish_status in PR_OK_STATUSES:
        return STATE_PR_CREATED
    if phase == "P00":
        return STATE_HANDOFF_OK if status == "ACCEPTED" else STATE_HANDOFF_FAILED
    if status == "P02_READY_FOR_HUMAN":
        return STATE_WAITING_REVIEW
    if status in ("P01_CODEX_OK",):
        return STATE_DEV_DONE
    if status.startswith("P01_") or status.startswith("P02_"):
        return STATE_BLOCKED if "BLOCKED" in status else STATE_FAILED
    return STATE_UNKNOWN


def _pr_state(publish: dict) -> str:
    status = (publish or {}).get("status")
    if status in PR_MERGED_STATUSES:
        return "merged"
    if status in PR_OK_STATUSES:
        return "created"
    return "none"


def _pr_number(publish: dict) -> int | None:
    """发布日志常只记 URL，编号从 URL 反解，避免索引里留空。"""
    value = (publish or {}).get("pr_number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    match = re.search(r"/pull/(\d+)", str((publish or {}).get("pr_url") or ""))
    return int(match.group(1)) if match else None


def _pr_number_with_source(publish: dict) -> tuple[int | None, str | None]:
    """编号来源要可追溯：原始日志写了就是 publish，靠 URL 推的就标 derived。"""
    value = (publish or {}).get("pr_number")
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        return _pr_number(publish), "publish"
    match = re.search(r"/pull/(\d+)", str((publish or {}).get("pr_url") or ""))
    if match:
        return int(match.group(1)), "derived_from_url"
    return None, None


def _realtime(root: Path, task_id: str) -> dict:
    """读取 P05-A 的实时状态记录；读不到就是"没查过"，不是"没变化"。"""
    path = Path(root) / REALTIME_DIR_REL / f"{task_id}.json"
    summary = {
        "available": False, "confirmed": False, "state": None,
        "checked_at": None, "last_success_at": None,
        "source": None, "reason": None, "error": None, "note": None,
    }
    if not path.is_file():
        summary["note"] = "尚未查询 GitHub 实时状态（不代表状态未变化）"
        return summary
    try:
        data = _load(path)
    except (ValueError, OSError):
        summary.update({"available": True, "reason": "UNREADABLE",
                        "note": "实时记录存在但无法解析"})
        return summary
    last = data.get("last_success") or {}
    summary.update({
        "available": True,
        "confirmed": bool(data.get("confirmed")),
        "state": data.get("state"),
        "checked_at": data.get("checked_at"),
        "last_success_at": last.get("checked_at"),
        "source": data.get("source"),
        "reason": (data.get("last_attempt") or {}).get("reason"),
        "error": (data.get("last_attempt") or {}).get("error"),
    })
    if not summary["confirmed"]:
        if summary["state"]:
            summary["note"] = (f"实时不可确认（{summary['reason']}）：显示的是最后一次成功观测 "
                               f"{summary['last_success_at']}")
        else:
            summary["note"] = "实时状态不可确认；以下状态来自历史执行记录"
    elif summary["last_success_at"] and summary["last_success_at"] != summary["checked_at"]:
        summary["note"] = "实时状态取自更早的一次成功查询"
    return summary


def _report_paths(root: Path, phase: str, key: str) -> tuple[str | None, str | None]:
    base = {"P00": Path("exchange"), "P01": Path("exchange") / "p01",
            "P02": Path("exchange") / "p02"}.get(phase)
    if base is None:
        return None, None
    json_path = Path(root) / base / "reports" / f"{key}.report.json"
    md_path = Path(root) / base / "reports" / f"{key}.report.md"
    json_rel = str((base / "reports" / f"{key}.report.json").as_posix()) if json_path.is_file() else None
    md_rel = str((base / "reports" / f"{key}.report.md").as_posix()) if md_path.is_file() else None
    return json_rel, md_rel


def _acceptance_path(root: Path, request_id: str) -> str | None:
    """P04-C 验收报告：存在才给路径，不存在不要编。"""
    rel = Path("exchange") / "p04" / "acceptance" / f"{request_id}.acceptance.md"
    return rel.as_posix() if (Path(root) / rel).is_file() else None


def _record(root: Path, receipt: dict, phase: str, receipt_path: Path,
            publish: dict, approved: set[str]) -> dict:
    project = receipt.get("project_id") or "unknown"
    request_id = receipt.get("request_id") or receipt_path.stem.split("__")[-1]
    key = f"{project}__{request_id}"
    report = {}
    report_rel, report_md_rel = _report_paths(root, phase, key)
    if report_rel:
        try:
            report = _load(Path(root) / report_rel)
        except (ValueError, OSError):
            report = {}
    # 发布日志目录缺失时，回退到回执/报告里内嵌的 publish 快照（同一次运行的原始记录）。
    if not publish:
        publish = receipt.get("publish") or report.get("publish") or {}

    workspace = {}
    task_file = Path(root) / "exchange" / "p03" / f"{request_id}.task.json"
    if task_file.is_file():
        try:
            workspace = (_load(task_file).get("workspace") or {})
        except (ValueError, OSError):
            workspace = {}

    repo = report.get("repo") or workspace.get("repo")
    kind = classify_kind(phase, repo, approved)
    status = receipt.get("status") or report.get("status") or "UNKNOWN"

    tests = report.get("tests") or {}
    tests_ok = receipt.get("tests_ok")
    if tests_ok is None and tests:
        tests_ok = tests.get("exit_code") == 0

    review_status = receipt.get("review_status")
    if review_status is None:
        reviews = report.get("reviews") or []
        if reviews and isinstance(reviews[-1], dict):
            review_status = reviews[-1].get("status")

    instants = [
        ("finished_at", _parse_time(receipt.get("finished_at") or report.get("finished_at"))),
        ("publish_updated_at", _parse_time((publish or {}).get("updated_at"))),
        ("created_at", _parse_time(receipt.get("created_at") or report.get("started_at"))),
    ]
    chosen, chosen_at = "file_mtime", None
    for source, value in instants:
        if value is not None and (chosen_at is None or value > chosen_at):
            chosen, chosen_at = source, value
    if chosen_at is None:
        try:
            chosen_at = datetime.fromtimestamp(receipt_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            chosen_at = None

    state = _state(phase, status, publish or {})
    state_basis = "publish_log" if publish else "receipt"
    realtime = _realtime(root, request_id)
    realtime_state = REALTIME_STATE_MAP.get(realtime.get("state") or "")
    if realtime["confirmed"] and realtime_state:
        state = realtime_state
        state_basis = "github_realtime"
    elif realtime_state and realtime.get("last_success_at"):
        # 本次查询失败，但有过成功观测：保留最后一次已知状态，并标记为过期，
        # 不能因为一次瞬时失败就退回更旧的执行记录（那等于把 merged 降级回 created）。
        state = realtime_state
        state_basis = "github_realtime_stale"
    pr_number, pr_number_source = _pr_number_with_source(publish or {})
    return {
        "task_id": request_id,
        "job_id": receipt.get("job_id") or report.get("job_id"),
        "phase": phase,
        "project_id": project,
        "kind": kind,
        "status": status,
        "state": state,
        "state_label": STATE_LABELS.get(state, STATE_UNKNOWN),
        "state_basis": state_basis,
        "human_action_required": state in HUMAN_ACTION_STATES,
        "title": report.get("title"),
        "goal": report.get("goal"),
        "repo": repo,
        "base_commit": receipt.get("base_commit") or report.get("base_commit"),
        "head_commit": receipt.get("head_commit") or report.get("head_commit"),
        "fix_rounds": receipt.get("fix_rounds"),
        "tests": {
            "ok": tests_ok,
            "exit_code": tests.get("exit_code"),
            "command": tests.get("command"),
        },
        "review": {
            "status": review_status,
            "blocking_findings": receipt.get("blocking_findings"),
        },
        "pr": {
            "state": _pr_state(publish or {}),
            "status": (publish or {}).get("status"),
            "url": (publish or {}).get("pr_url"),
            "number": pr_number,
            "number_source": pr_number_source,
            "branch": (publish or {}).get("branch"),
        },
        "realtime": realtime,
        "report_path": report_rel,
        "report_md_path": report_md_rel,
        "acceptance_path": _acceptance_path(root, request_id),
        "receipt_path": str(receipt_path.resolve().relative_to(Path(root).resolve()).as_posix())
        if _is_inside(receipt_path, root) else str(receipt_path),
        "created_at": _iso(_parse_time(receipt.get("created_at") or report.get("started_at"))),
        "finished_at": _iso(_parse_time(receipt.get("finished_at") or report.get("finished_at"))),
        "updated_at": _iso(chosen_at),
        "timestamp_source": chosen,
        "model_verification": UNVERIFIED if report.get("calls") else None,
    }


def _is_inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


RECEIPT_DIRS = (
    ("P00", Path("exchange") / "receipts"),
    ("P01", Path("exchange") / "p01" / "receipts"),
    ("P02", Path("exchange") / "p02" / "receipts"),
)


def collect_tasks(root: Path) -> list[dict]:
    root = Path(root).resolve()
    approved = approved_repos(root)
    publishes = _publish_events(root)
    records: list[dict] = []
    for phase, rel in RECEIPT_DIRS:
        directory = root / rel
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.receipt.json")):
            try:
                receipt = _load(path)
            except (ValueError, OSError):
                continue
            if not isinstance(receipt, dict):
                continue
            publish = publishes.get(str(receipt.get("job_id")), {})
            records.append(_record(root, receipt, phase, path, publish, approved))

    records.sort(key=lambda item: (item.get("updated_at") or "", item.get("task_id") or ""),
                 reverse=True)
    return records


# ------------------------------------------------------------------- writers
def _counts(records: list[dict]) -> dict:
    kinds: dict[str, int] = {}
    states: dict[str, int] = {}
    for item in records:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        states[item["state"]] = states.get(item["state"], 0) + 1
    return {"total": len(records), "by_kind": kinds, "by_state": states}


def _latest(records: list[dict], kind: str | None) -> dict | None:
    pool = [item for item in records if kind is None or item["kind"] == kind]
    return pool[0] if pool else None


def build(root: Path) -> dict:
    root = Path(root).resolve()
    records = collect_tasks(root)
    status_dir = root / STATUS_DIR_REL
    generated = _now()

    latest_dev = _latest(records, KIND_DEVELOPMENT)
    latest_any = _latest(records, None)

    latest_payload = {
        "schema": SCHEMA,
        "generated_at": generated,
        "scope": KIND_DEVELOPMENT,
        "has_development_task": latest_dev is not None,
        "task": latest_dev,
        "latest_any": latest_any,
    }
    index_payload = {
        "schema": SCHEMA,
        "generated_at": generated,
        "counts": _counts(records),
        "latest_development": (latest_dev or {}).get("task_id"),
        "latest_any": (latest_any or {}).get("task_id"),
        "tasks": records,
    }

    _write_atomic(status_dir / "latest.json", latest_payload)
    _write_atomic(status_dir / "index.json", index_payload)

    projects: dict[str, list[dict]] = {}
    for item in records:
        projects.setdefault(item["project_id"], []).append(item)
    for project_id, items in projects.items():
        project_latest = _latest(items, KIND_DEVELOPMENT)
        _write_atomic(status_dir / "projects" / f"{project_id}.json", {
            "schema": SCHEMA,
            "generated_at": generated,
            "project_id": project_id,
            "counts": _counts(items),
            "latest_development": project_latest,
            "latest_any": _latest(items, None),
            "tasks": items,
        })

    return {
        "ok": True,
        "generated_at": generated,
        "tasks": len(records),
        "latest_development": (latest_dev or {}).get("task_id"),
        "latest_any": (latest_any or {}).get("task_id"),
        "status_dir": str(status_dir),
    }


def refresh(root: Path) -> dict:
    """给 Bridge 调用：绝不因索引失败影响任务结果。"""
    try:
        return build(root)
    except Exception as exc:  # noqa: BLE001 - 索引是旁路，不能污染主流程
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------- CLI
def load_latest(root: Path) -> dict:
    path = Path(root).resolve() / STATUS_DIR_REL / "latest.json"
    return _load(path) if path.is_file() else {}


def load_index(root: Path) -> dict:
    path = Path(root).resolve() / STATUS_DIR_REL / "index.json"
    return _load(path) if path.is_file() else {}


def find_task(root: Path, request_id: str) -> dict | None:
    index = load_index(root)
    for item in index.get("tasks", []):
        if item.get("task_id") == request_id:
            return item
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SoloShip P04-A 任务状态索引")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("rebuild", help="扫描数据库/回执/报告重建索引")
    p_build.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))

    p_latest = sub.add_parser("latest", help="打印最新开发任务")
    p_latest.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))

    p_show = sub.add_parser("show", help="按 task_id 查询")
    p_show.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    p_show.add_argument("--request-id", required=True)

    args = parser.parse_args(argv)
    if args.cmd == "rebuild":
        print(json.dumps(build(Path(args.root)), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "latest":
        payload = load_latest(Path(args.root))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("task") else 1

    record = find_task(Path(args.root), args.request_id)
    print(json.dumps(record or {"error": f"未找到任务 {args.request_id}"},
                     ensure_ascii=False, indent=2))
    return 0 if record else 1


if __name__ == "__main__":
    sys.exit(main())
