#!/usr/bin/env python3
"""SoloShip P03-D 操作守卫（只做本地文件操作，不读 Token、不发起任何远端请求）。

三个子命令：

  toggle           只改 config/bridge.json 的 github.enabled；用结构化 JSON 读写，
                   深比较确认除 github.enabled 外没有任何字段变化。
  precheck-inbox   确认 exchange/p02/inbox 里没有本次授权之外的任务。
  check            判定一次任务结果是否真的形成了 Draft PR。

退出码：0 = 通过，1 = 不通过，2 = 用法/环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

READY_STATUS = "P02_READY_FOR_HUMAN"
BRANCH_PREFIX = "soloship/"
# 与 github_publisher 保持同步；由 tests/test_p03d_guard.py 中的一致性测试兜底。
PR_SUCCESS_STATUSES = ("PUBLISH_PR_CREATED", "PUBLISH_PR_EXISTS", "PUBLISH_PR_RECOVERED")

CONFIG_REL = Path("config") / "bridge.json"


# ------------------------------------------------------------------ plumbing
def _read_text(path: Path) -> str:
    # utf-8-sig 兼容可能存在的 BOM，但写回时一律不带 BOM。
    return path.read_text(encoding="utf-8-sig")


def load_json(path: Path):
    return json.loads(_read_text(path))


def _write_atomic(path: Path, payload: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=".write-",
            suffix=".tmp", dir=str(path.parent), delete=False,
        ) as fh:
            tmp_name = fh.name
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _encode_like(obj, raw: bytes) -> bytes:
    """沿用原文件的换行风格与结尾换行与否，使开→关往返后字节完全一致。"""
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if newline != "\n":
        text = text.replace("\n", newline)
    if raw.endswith(b"\n"):
        text += newline
    return text.encode("utf-8")


def deep_diff(before, after, prefix: tuple = ()) -> list[tuple]:
    """返回两份 JSON 结构之间所有不同的路径。"""
    if type(before) is not type(after):
        return [prefix]
    if isinstance(before, dict):
        diffs: list[tuple] = []
        for key in sorted(set(before) | set(after)):
            if key not in before or key not in after:
                diffs.append(prefix + (key,))
            else:
                diffs.extend(deep_diff(before[key], after[key], prefix + (key,)))
        return diffs
    if isinstance(before, list):
        if len(before) != len(after):
            return [prefix]
        diffs = []
        for index, (left, right) in enumerate(zip(before, after)):
            diffs.extend(deep_diff(left, right, prefix + (index,)))
        return diffs
    return [] if before == after else [prefix]


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


# -------------------------------------------------------------------- toggle
def toggle(root: Path, value: bool, *, keep_backup: bool = True) -> dict:
    """只切换 github.enabled，并证明没有动别的字段。"""
    config_path = Path(root).resolve() / CONFIG_REL
    result = {"config": str(config_path), "target": bool(value), "changed": False,
              "before": None, "after": None, "diff": [], "backup": None}
    if not config_path.is_file():
        return result | {"error": f"配置不存在: {config_path}"}

    raw_bytes = config_path.read_bytes()
    before = load_json(config_path)
    if not isinstance(before, dict):
        return result | {"error": "配置根节点不是对象"}
    github = before.get("github")
    if not isinstance(github, dict):
        return result | {"error": "配置缺少 github 对象"}
    if "enabled" not in github:
        return result | {"error": "github 对象缺少 enabled 字段"}

    result["before"] = github.get("enabled")
    if bool(github.get("enabled")) == bool(value):
        result["after"] = github.get("enabled")
        return result

    after = json.loads(_dump(before))
    after["github"]["enabled"] = bool(value)

    diff = deep_diff(before, after)
    result["diff"] = [list(item) for item in diff]
    if result["diff"] != [["github", "enabled"]]:
        return result | {"error": f"改动范围超出 github.enabled: {result['diff']}"}

    if keep_backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = config_path.with_name(f"{config_path.name}.bak-{stamp}")
        shutil.copy2(config_path, backup)
        result["backup"] = str(backup)

    _write_bytes_atomic(config_path, _encode_like(after, raw_bytes))

    written = load_json(config_path)
    if deep_diff(after, written):
        return result | {"error": "写回后重新读取内容不一致"}
    if config_path.read_bytes()[:3] == b"\xef\xbb\xbf":
        return result | {"error": "写回文件带有 UTF-8 BOM"}

    result["changed"] = True
    result["after"] = written["github"]["enabled"]
    return result


# ------------------------------------------------------------ precheck-inbox
def precheck_inbox(root: Path, allowed: list[str]) -> dict:
    inbox = Path(root).resolve() / "exchange" / "p02" / "inbox"
    allowed_stems = {f"{name}.ready.json" for name in allowed}
    result = {"inbox": str(inbox), "allowed": sorted(allowed_stems),
              "found": [], "foreign": [], "ok": True}
    if not inbox.is_dir():
        result["ok"] = False
        result["error"] = f"inbox 目录不存在: {inbox}"
        return result

    pending = sorted(p.name for p in inbox.iterdir() if p.is_file())
    result["found"] = pending
    foreign = [name for name in pending if name not in allowed_stems]
    result["foreign"] = foreign
    if foreign:
        result["ok"] = False
        result["error"] = f"inbox 存在非本次授权任务: {', '.join(foreign)}"
    return result


# --------------------------------------------------------------- task file
def validate_task_file(root: Path, request_id: str) -> dict:
    """按 request_id 找到任务文件，并核对它自己声明的 id 与授权参数一致。"""
    root = Path(root).resolve()
    task_path = root / "exchange" / "p03" / f"{request_id}.task.json"
    result = {"task": str(task_path), "requested": request_id, "ok": False,
              "task_request_id": None, "project_id": None,
              "repo": None, "base_ref": None, "errors": []}
    errors: list[str] = result["errors"]

    if not task_path.is_file():
        errors.append(f"任务文件不存在: {task_path}")
        return result
    try:
        obj = load_json(task_path)
    except (ValueError, OSError) as exc:
        errors.append(f"任务文件不可解析: {type(exc).__name__}: {exc}")
        return result
    if not isinstance(obj, dict):
        errors.append("任务文件根节点不是对象")
        return result

    declared = obj.get("request_id")
    result["task_request_id"] = declared
    result["project_id"] = obj.get("project_id")
    workspace = obj.get("workspace") if isinstance(obj.get("workspace"), dict) else {}
    result["repo"] = workspace.get("repo")
    result["base_ref"] = workspace.get("base_ref")

    if declared != request_id:
        errors.append(f"任务文件 request_id={declared!r} 与参数 {request_id!r} 不一致")
    if obj.get("schema_version") != "1.0":
        errors.append(f"schema_version 不是 '1.0'（{obj.get('schema_version')!r}）")

    config = {}
    config_path = root / CONFIG_REL
    if config_path.is_file():
        try:
            config = load_json(config_path)
        except (ValueError, OSError) as exc:
            errors.append(f"配置不可解析: {type(exc).__name__}: {exc}")
    mode = (config.get("windows_test_mode") or {}) if isinstance(config, dict) else {}
    approved_projects = mode.get("approved_projects") or []
    if result["project_id"] not in approved_projects:
        errors.append(f"project_id {result['project_id']!r} 不在 approved_projects {approved_projects}")

    approved_repos = []
    for item in mode.get("approved_absolute_repos") or []:
        try:
            approved_repos.append(str(Path(item).resolve()))
        except (OSError, RuntimeError):
            continue
    repo_value = result["repo"]
    if not isinstance(repo_value, str) or not Path(repo_value).is_absolute():
        errors.append(f"workspace.repo 必须是绝对路径（{repo_value!r}）")
    else:
        try:
            resolved_repo = str(Path(repo_value).resolve())
        except (OSError, RuntimeError):
            resolved_repo = repo_value
        if resolved_repo not in approved_repos:
            errors.append(f"workspace.repo {resolved_repo} 不在 approved_absolute_repos")

    github = (config.get("github") or {}) if isinstance(config, dict) else {}
    allowed_bases = github.get("allowed_base_branches") or ["main"]
    if result["base_ref"] not in allowed_bases:
        errors.append(f"workspace.base_ref {result['base_ref']!r} 不在 allowed_base_branches {allowed_bases}")

    result["ok"] = not errors
    return result


# --------------------------------------------------------------------- check
def check_result(report_path: Path, request_id: str | None = None,
                 require_pr: bool = True) -> dict:
    path = Path(report_path)
    result = {"report": str(path), "ok": False, "reasons": [],
              "task_status": None, "publish_status": None,
              "branch": None, "pr_url": None}
    if not path.is_file():
        result["reasons"].append(f"报告不存在: {path}")
        return result
    try:
        evidence = load_json(path)
    except (ValueError, OSError) as exc:
        result["reasons"].append(f"报告不可读: {type(exc).__name__}: {exc}")
        return result
    if not isinstance(evidence, dict):
        result["reasons"].append("报告根节点不是对象")
        return result

    task_status = evidence.get("status")
    publish = evidence.get("publish") or {}
    if not isinstance(publish, dict):
        result["reasons"].append("publish 字段不是对象")
        publish = {}
    result["task_status"] = task_status
    result["publish_status"] = publish.get("status")
    result["branch"] = publish.get("branch")
    result["pr_url"] = publish.get("pr_url")

    if request_id and evidence.get("request_id") != request_id:
        result["reasons"].append(
            f"任务不匹配: report request_id={evidence.get('request_id')} expected={request_id}")
    if task_status != READY_STATUS:
        result["reasons"].append(f"任务未通过闸门: status={task_status}")

    if not publish:
        result["reasons"].append("没有发布记录：Publisher 未执行")
        return result

    publish_status = publish.get("status")
    if require_pr:
        if publish_status not in PR_SUCCESS_STATUSES:
            result["reasons"].append(
                f"发布状态不是有效的 Draft PR: {publish_status}（允许 {', '.join(PR_SUCCESS_STATUSES)}）")
        if not str(publish.get("pr_url") or "").strip():
            result["reasons"].append("缺少 PR 链接")
        if not str(publish.get("branch") or "").startswith(BRANCH_PREFIX):
            result["reasons"].append(
                f"分支不在授权前缀 {BRANCH_PREFIX}: {publish.get('branch')}")
        if publish.get("candidate_sha") != evidence.get("head_commit"):
            result["reasons"].append(
                "candidate SHA 与报告 head_commit 不一致")

    result["ok"] = not result["reasons"]
    return result


# -------------------------------------------------------------------- compare
def compare_files(left: Path, right: Path) -> dict:
    result = {"left": str(left), "right": str(right), "identical": False, "diff": []}
    try:
        first = load_json(Path(left))
        second = load_json(Path(right))
    except (ValueError, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    diff = deep_diff(first, second)
    result["diff"] = [list(item) for item in diff]
    result["identical"] = not diff
    return result


# ---------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SoloShip P03-D 操作守卫")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_toggle = sub.add_parser("toggle", help="切换 github.enabled")
    p_toggle.add_argument("--root", required=True)
    p_toggle.add_argument("--value", required=True, choices=["true", "false"])
    p_toggle.add_argument("--no-backup", action="store_true")

    p_pre = sub.add_parser("precheck-inbox", help="确认 inbox 只有授权任务")
    p_pre.add_argument("--root", required=True)
    p_pre.add_argument("--allow", action="append", required=True)

    p_task = sub.add_parser("validate-task", help="按 request_id 校验任务文件")
    p_task.add_argument("--root", required=True)
    p_task.add_argument("--request-id", required=True)

    p_check = sub.add_parser("check", help="判定发布结果")
    p_check.add_argument("--report", required=True)
    p_check.add_argument("--request-id", default=None)

    p_cmp = sub.add_parser("compare", help="比较两份 JSON 是否语义相同")
    p_cmp.add_argument("--left", required=True)
    p_cmp.add_argument("--right", required=True)

    for item in (p_toggle, p_pre, p_task, p_check, p_cmp):
        item.add_argument("--out", default=None, help="把结果 JSON 写到该文件")

    args = parser.parse_args(argv)

    if args.cmd == "compare":
        payload = compare_files(Path(args.left), Path(args.right))
        code = 2 if "error" in payload else (0 if payload.get("identical") else 1)
    elif args.cmd == "toggle":
        payload = toggle(Path(args.root), args.value.lower() == "true",
                         keep_backup=not args.no_backup)
        code = 0 if not payload.get("error") else 2
    elif args.cmd == "precheck-inbox":
        payload = precheck_inbox(Path(args.root), args.allow)
        if payload.get("ok"):
            code = 0
        else:
            missing = "不存在" in str(payload.get("error") or "")
            code = 2 if missing else 1
    elif args.cmd == "validate-task":
        payload = validate_task_file(Path(args.root), args.request_id)
        if payload.get("ok"):
            code = 0
        else:
            missing = any("任务文件不存在" in item for item in payload.get("errors") or [])
            code = 2 if missing else 1
    else:
        payload = check_result(Path(args.report), args.request_id)
        code = 0 if payload.get("ok") else 1

    text = _dump(payload)
    if args.out:
        _write_atomic(Path(args.out), text)
    sys.stdout.write(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
