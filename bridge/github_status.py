#!/usr/bin/env python3
"""SoloShip P05-A: GitHub PR 实时状态查询（只读）。

职责边界：
  * 只用 `gh api` 发起 **GET**，不创建/合并/关闭/评论任何 PR，不推送，不改仓库设置。
  * 不修改任何原始执行报告；结果只写 exchange/status/realtime/<task_id>.json。
  * 查询失败时 **不伪造** 实时状态：保留上一次成功观测，并把"本次未确认"如实写进记录。

记录结构与语义：
    state          最近一次 **成功** 观测到的实时状态（draft/open/closed/merged）；
                   从未成功过则为 null。
    confirmed      最近一次 **尝试** 是否成功。False 表示实时状态此刻不可确认。
    checked_at     最近一次尝试的时间（UTC）。
    last_success   最近一次成功观测（state / checked_at / source）。
    history        最近若干次尝试的流水，用于发现"较旧结果不得覆盖较新状态"。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "soloship.realtime/v1"
REALTIME_DIR_REL = Path("exchange") / "status" / "realtime"
HISTORY_LIMIT = 20

STATE_DRAFT = "draft"
STATE_OPEN = "open"
STATE_CLOSED = "closed"
STATE_MERGED = "merged"
STATE_UNKNOWN = "unknown"
KNOWN_STATES = (STATE_DRAFT, STATE_OPEN, STATE_CLOSED, STATE_MERGED)

PR_URL_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")

# 只允许这些子命令；出现任何写操作一律拒绝执行。
ALLOWED_SUBCOMMANDS = (
    ("api",),
    ("pr", "view"),
    ("pr", "list"),
)
FORBIDDEN_FLAGS = ("-X", "--method", "-f", "--field", "--input", "-d", "--raw-field",
                   "--post", "--patch", "--put", "--delete")


class ReadOnlyViolation(RuntimeError):
    """试图通过本模块发起远端写操作。"""


def _now() -> str:
    # 微秒精度：同一次运行里连续查询也能排出先后，避免"同秒即过期"误判。
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_pr_ref(pr_url: str | None = None, repo: str | None = None,
                 number: int | None = None) -> dict | None:
    """从 URL 或 repo+number 得到 {repo, number, url}，拿不全就返回 None。"""
    url = (pr_url or "").strip()
    match = PR_URL_RE.search(url)
    if match:
        return {"repo": match.group(1), "number": int(match.group(2)), "url": url}
    if repo and str(number or "").strip().isdigit():
        num = int(str(number).strip())
        return {"repo": str(repo).strip(), "number": num,
                "url": f"https://github.com/{str(repo).strip()}/pull/{num}"}
    if url and re.fullmatch(r"\d+", url):
        return parse_pr_ref(repo=repo, number=int(url))
    return None


def classify_pr(payload: dict) -> str:
    """GitHub PR 载荷 → draft/open/closed/merged。"""
    if not isinstance(payload, dict):
        return STATE_UNKNOWN
    if payload.get("merged_at") or payload.get("merged"):
        return STATE_MERGED
    state = str(payload.get("state") or "").lower()
    if state == "closed":
        return STATE_CLOSED
    if payload.get("draft") is True:
        return STATE_DRAFT
    if state == "open":
        return STATE_OPEN
    return STATE_UNKNOWN


def endpoint_for(ref: dict) -> str:
    return f"repos/{ref['repo']}/pulls/{ref['number']}"


def assert_readonly(argv: list[str]) -> None:
    """在真正执行前拦截任何写操作。"""
    if not argv:
        raise ReadOnlyViolation("空命令")
    if not any(tuple(argv[:len(prefix)]) == prefix for prefix in ALLOWED_SUBCOMMANDS):
        raise ReadOnlyViolation(f"不允许的 gh 子命令: {argv[:2]}")
    for token in argv:
        text = str(token)
        if text in FORBIDDEN_FLAGS:
            raise ReadOnlyViolation(f"不允许的写操作参数: {text}")
        if text.startswith("--method=") or text.startswith("-X"):
            raise ReadOnlyViolation(f"不允许的写操作参数: {text}")
        lowered = text.lower()
        if lowered in ("post", "patch", "put", "delete") and "method" in " ".join(argv).lower():
            raise ReadOnlyViolation(f"不允许的写操作参数: {text}")


def _failure(reason: str, error: str, *, ref: dict | None, checked_at: str) -> dict:
    return {
        "ok": False,
        "state": None,
        "reason": reason,
        "error": (error or "").strip()[-500:],
        "checked_at": checked_at,
        "pr": ref,
        "source": {"kind": "github", "via": "gh api",
                   "endpoint": endpoint_for(ref) if ref else None},
    }


def probe(pr_url: str | None = None, repo: str | None = None, number: int | None = None,
          *, gh_cmd: list[str] | None = None, timeout_s: int = 20,
          env: dict | None = None) -> dict:
    """只读查询一次 PR 状态。失败一律返回 ok=False，绝不猜测。"""
    checked_at = _now()
    ref = parse_pr_ref(pr_url, repo, number)
    if ref is None:
        result = _failure("NO_PR_REFERENCE",
                          "缺少可解析的 PR 引用（URL 或 repo+number）",
                          ref=None, checked_at=checked_at)
        result["checked_at"] = checked_at
        return result

    cmd = [*(gh_cmd or ["gh"]), "api", endpoint_for(ref)]
    try:
        assert_readonly(cmd[1:] if gh_cmd is None else cmd[len(gh_cmd or []):])
    except ReadOnlyViolation as exc:
        return _failure("READONLY_VIOLATION", str(exc), ref=ref, checked_at=checked_at)

    run_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout_s, env=run_env,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return _failure("GH_MISSING", f"未找到 GitHub CLI: {cmd[0]}", ref=ref,
                        checked_at=checked_at)
    except subprocess.TimeoutExpired:
        return _failure("TIMEOUT", f"查询超过 {timeout_s}s", ref=ref, checked_at=checked_at)
    except OSError as exc:
        return _failure("NETWORK", f"{type(exc).__name__}: {exc}", ref=ref,
                        checked_at=checked_at)

    text = f"{proc.stdout}\n{proc.stderr}".strip()
    if proc.returncode != 0:
        lowered = text.lower()
        if any(marker in lowered for marker in ("not logged in", "gh auth login",
                                                "authentication", "bad credentials",
                                                "http 401", "resource not accessible")):
            reason = "AUTH"
        elif "http 404" in lowered or "not found" in lowered:
            reason = "NOT_FOUND"
        elif "could not resolve host" in lowered or "connection" in lowered:
            reason = "NETWORK"
        else:
            reason = "GH_ERROR"
        return _failure(reason, text, ref=ref, checked_at=checked_at)

    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return _failure("BAD_PAYLOAD", f"响应不是 JSON: {proc.stdout[:200]}", ref=ref,
                        checked_at=checked_at)

    state = classify_pr(payload)
    return {
        "ok": True,
        "state": state,
        "reason": None,
        "error": "",
        "checked_at": checked_at,
        "pr": ref,
        "source": {"kind": "github", "via": "gh api", "endpoint": endpoint_for(ref)},
        "evidence": {
            "state": payload.get("state"),
            "draft": payload.get("draft"),
            "merged_at": payload.get("merged_at"),
            "closed_at": payload.get("closed_at"),
            "updated_at": payload.get("updated_at"),
        },
    }


# ------------------------------------------------------------------ storage
def realtime_path(root: Path, task_id: str) -> Path:
    return Path(root).resolve() / REALTIME_DIR_REL / f"{task_id}.json"


def _write_atomic(path: Path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
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


def read_realtime(root: Path, task_id: str) -> dict | None:
    path = realtime_path(root, task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _strictly_newer(candidate: str | None, existing: str | None) -> bool:
    """候选观测是否比已有观测更新。时间无法解析时按字符串比较。"""
    left, right = _ts(existing), _ts(candidate)
    if left is not None and right is not None:
        return right > left
    return bool(candidate) and bool(existing) and str(candidate) > str(existing)


def refresh_task(root: Path, task_id: str, *, pr_url: str | None = None,
                 repo: str | None = None, number: int | None = None,
                 historical: dict | None = None, gh_cmd: list[str] | None = None,
                 timeout_s: int = 20, probe_fn=None, checked_at: str | None = None) -> dict:
    """查询一次并落盘。较旧的观测不得覆盖较新的状态。"""
    root = Path(root).resolve()
    existing = read_realtime(root, task_id)
    stamp = checked_at or _now()

    if existing and existing.get("checked_at") and not _strictly_newer(stamp, existing.get("checked_at")):
        return {
            "ok": True,
            "task_id": task_id,
            "skipped_stale": True,
            "kept_checked_at": existing.get("checked_at"),
            "candidate_checked_at": stamp,
            "state": existing.get("state"),
            "confirmed": existing.get("confirmed"),
        }

    runner = probe_fn or probe
    if probe_fn is not None:
        result = probe_fn(pr_url, repo, number, checked_at=stamp)
    else:
        result = runner(pr_url, repo, number, gh_cmd=gh_cmd, timeout_s=timeout_s)

    history = list((existing or {}).get("history") or [])
    history.append({
        "checked_at": stamp,
        "ok": bool(result.get("ok")),
        "state": result.get("state"),
        "reason": result.get("reason"),
    })
    history = history[-HISTORY_LIMIT:]

    last_success = (existing or {}).get("last_success")
    if result.get("ok"):
        last_success = {"state": result.get("state"), "checked_at": stamp,
                        "source": result.get("source"), "evidence": result.get("evidence")}

    record = {
        "schema": SCHEMA,
        "task_id": task_id,
        "pr": result.get("pr") or (existing or {}).get("pr"),
        "checked_at": stamp,
        "confirmed": bool(result.get("ok")),
        "state": (last_success or {}).get("state"),
        "source": result.get("source") or (existing or {}).get("source"),
        "last_success": last_success,
        "last_attempt": {
            "ok": bool(result.get("ok")),
            "reason": result.get("reason"),
            "error": result.get("error"),
            "checked_at": stamp,
        },
        "history": history,
    }
    if historical:
        record["historical"] = historical
    _write_atomic(realtime_path(root, task_id), record)

    return {"ok": True, "task_id": task_id, "skipped_stale": False,
            "confirmed": record["confirmed"], "state": record["state"],
            "checked_at": stamp, "reason": result.get("reason")}


# ---------------------------------------------------------------------- CLI
def refresh_all(root: Path, *, gh_cmd: list[str] | None = None, timeout_s: int = 20,
                probe_fn=None, only_missing: bool = False) -> list[dict]:
    import status_index

    root = Path(root).resolve()
    status_index.refresh(root)
    index = status_index.load_index(root)
    results = []
    for task in index.get("tasks") or []:
        if only_missing and task.get("realtime", {}).get("confirmed"):
            continue
        pr = (task.get("pr") or {})
        if not pr.get("url") and not pr.get("number"):
            results.append({"task_id": task.get("task_id"), "skipped": "NO_PR"})
            continue
        results.append(refresh_task(
            root, task["task_id"], pr_url=pr.get("url"), repo=_repo_slug(root, pr),
            number=pr.get("number"), gh_cmd=gh_cmd, timeout_s=timeout_s,
            probe_fn=probe_fn,
            historical={"publish_status": pr.get("status"), "pr_state": pr.get("state")},
        ))
    return results


def _repo_slug(root: Path, pr: dict) -> str | None:
    ref = parse_pr_ref(pr.get("url"))
    return ref["repo"] if ref else None


def _refresh_one(root: Path, task_id: str, *, gh_cmd: list[str] | None = None,
                 timeout_s: int = 20, probe_fn=None, checked_at: str | None = None) -> dict:
    """按 task_id 从索引里取 PR 引用再查询，避免调用方自己拼参数。"""
    import status_index

    task = status_index.find_task(root, task_id) or {}
    pr = task.get("pr") or {}
    if not pr.get("url") and not pr.get("number"):
        return {"task_id": task_id, "skipped": "NO_PR"}
    return refresh_task(
        root, task_id, pr_url=pr.get("url"), repo=_repo_slug(root, pr),
        number=pr.get("number"), gh_cmd=gh_cmd, timeout_s=timeout_s,
        probe_fn=probe_fn, checked_at=checked_at,
        historical={"publish_status": pr.get("status"), "pr_state": pr.get("state")},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SoloShip P05-A GitHub PR 实时状态（只读）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_refresh = sub.add_parser("refresh", help="查询并写入实时状态")
    p_refresh.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    p_refresh.add_argument("--task-id", default=None)
    p_refresh.add_argument("--all", action="store_true")
    p_refresh.add_argument("--only-missing", action="store_true", help="只查未确认过的任务")
    p_refresh.add_argument("--timeout", type=int, default=20)
    p_refresh.add_argument("--rebuild", action="store_true", help="查完重建索引")

    p_show = sub.add_parser("show", help="打印某任务的实时状态记录")
    p_show.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    p_show.add_argument("--task-id", required=True)

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.cmd == "show":
        record = read_realtime(root, args.task_id)
        print(json.dumps(record or {"error": f"没有实时记录: {args.task_id}"},
                         ensure_ascii=False, indent=2))
        return 0 if record else 1

    if args.task_id:
        results = [_refresh_one(root, args.task_id, timeout_s=args.timeout)]
    elif args.all:
        results = refresh_all(root, timeout_s=args.timeout, only_missing=args.only_missing)
    else:
        parser.error("需要 --task-id 或 --all")

    import status_index
    status_index.refresh(root)
    if args.rebuild:
        status_index.build(root)

    confirmed = sum(1 for item in results if item.get("confirmed"))
    print(json.dumps({"ok": True, "checked": len(results), "confirmed": confirmed,
                      "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
