#!/usr/bin/env python3
"""SoloShip P00: local file handoff *only* (no Codex, shell execution or GitHub access).

This is a smoke-test receiver for a deliberately non-privileged task contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQ_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,79}$")
PROJECT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
AC_PATTERN = re.compile(r"^AC-[0-9]{2,3}$")
TASK_KEYS = {
    "schema_version", "request_id", "project_id", "title", "goal",
    "requested_paths", "constraints", "non_goals", "acceptance",
}
AC_KEYS = {"id", "description", "verification"}
MAX_FILE_BYTES = 64 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def canonical_sha(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def atomic_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=".write-",
            suffix=".pending", dir=target.parent, delete=False
        ) as fh:
            tmp_name = fh.name
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    finally:
        if tmp_name is not None:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_task(obj: Any) -> list[str]:
    """Minimal strict validation of the supplied schema using Python stdlib."""
    if not isinstance(obj, dict):
        return ["任务内容必须是 JSON 对象"]
    errors: list[str] = []
    for key in sorted(TASK_KEYS):
        if key not in obj:
            errors.append(f"缺少字段: {key}")
    extras = set(obj) - TASK_KEYS
    if extras:
        errors.append(f"未知字段: {', '.join(sorted(extras))}")
    if obj.get("schema_version") != "1.0":
        errors.append("schema_version 必须是 '1.0'")
    req = obj.get("request_id")
    if not isinstance(req, str) or not REQ_PATTERN.fullmatch(req):
        errors.append("request_id 格式无效")
    project = obj.get("project_id")
    if not isinstance(project, str) or not PROJECT_PATTERN.fullmatch(project):
        errors.append("project_id 格式无效")
    elif project != "demo-app":
        errors.append("P00 只允许 demo-app 项目")
    for key in ("title", "goal"):
        if not nonempty_string(obj.get(key)):
            errors.append(f"{key} 必须为非空文本")
    for key in ("requested_paths", "constraints", "non_goals"):
        arr = obj.get(key)
        if not isinstance(arr, list) or (key == "requested_paths" and not arr):
            errors.append(f"{key} 必须是{'非空' if key == 'requested_paths' else ''}数组")
        elif any(not nonempty_string(item) for item in arr):
            errors.append(f"{key} 只能包含非空文本")
    acceptance = obj.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        errors.append("acceptance 必须是非空数组")
    else:
        ids = set()
        for index, ac in enumerate(acceptance):
            if not isinstance(ac, dict) or set(ac) != AC_KEYS:
                errors.append(f"acceptance[{index}] 字段有误")
                continue
            ac_id = ac.get("id")
            if not isinstance(ac_id, str) or not AC_PATTERN.fullmatch(ac_id):
                errors.append(f"acceptance[{index}].id 格式无效")
            elif ac_id in ids:
                errors.append(f"重复验收 ID: {ac_id}")
            else:
                ids.add(ac_id)
            if not nonempty_string(ac.get("description")):
                errors.append(f"acceptance[{index}].description 必须为非空文本")
            if ac.get("verification") not in ("automated", "manual"):
                errors.append(f"acceptance[{index}].verification 必须是 automated 或 manual")
    return errors


class SoloShipP00:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.exchange = self.root / "exchange"
        self.inbox = self.exchange / "inbox"
        self.receipts = self.exchange / "receipts"
        self.reports = self.exchange / "reports"
        self.archive = self.exchange / "archive"
        self.rejected = self.exchange / "rejected"
        self.control = self.root / ".control"
        for path in (self.inbox, self.receipts, self.reports, self.archive, self.rejected, self.control):
            path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.control / "p00.db"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                spec_sha TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (project_id, request_id)
            )""")

    @contextmanager
    def connect(self):
        """Yield a connection that commits on success, rolls back on error, always closes."""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def stash(self, source: Path, target_dir: Path) -> None:
        target = target_dir / f"{source.name}.{uuid.uuid4().hex[:8]}"
        shutil.move(str(source), str(target))

    def reject(self, source: Path, message: str) -> None:
        report = {
            "status": "REJECTED",
            "source_file": source.name,
            "reason": message,
            "timestamp": utc_now(),
            "note": "P00 拒收不代表 Codex 运行失败；本阶段从未调用 Codex。",
        }
        atomic_text(self.receipts / f"{source.name}.error.json", dump_json(report))
        self.stash(source, self.rejected)
        print(f"[REJECTED] {source.name}: {message}", flush=True)

    def process_one(self, source: Path) -> str:
        if source.is_symlink():
            self.reject(source, "不接受符号链接")
            return "rejected"
        if source.stat().st_size > MAX_FILE_BYTES:
            self.reject(source, "任务文件超过 64 KiB")
            return "rejected"
        try:
            obj = json.loads(source.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            self.reject(source, f"JSON 无法解析: {exc}")
            return "rejected"
        errors = validate_task(obj)
        if errors:
            self.reject(source, "; ".join(errors))
            return "rejected"
        if source.name != f"{obj['request_id']}.ready.json":
            self.reject(source, "文件名必须是 <request_id>.ready.json")
            return "rejected"
        project_id = obj["project_id"]
        req_id = obj["request_id"]
        spec_sha = canonical_sha(obj)
        key = f"{project_id}__{req_id}"
        with self.connect() as db:
            row = db.execute(
                "SELECT job_id, spec_sha, created_at FROM jobs WHERE project_id=? AND request_id=?",
                (project_id, req_id),
            ).fetchone()
            if row:
                job_id, old_sha, created_at = row
                if old_sha != spec_sha:
                    self.reject(source, "同一 project_id/request_id 已存在不同内容；需使用新 request_id")
                    return "rejected"
                outcome = "DUPLICATE"
            else:
                job_id = "p00_" + uuid.uuid4().hex[:12]
                created_at = utc_now()
                db.execute(
                    "INSERT INTO jobs (job_id, project_id, request_id, spec_sha, created_at) VALUES (?, ?, ?, ?, ?)",
                    (job_id, project_id, req_id, spec_sha, created_at),
                )
                outcome = "ACCEPTED"
        receipt = {
            "phase": "P00", "status": outcome, "job_id": job_id,
            "request_id": req_id, "project_id": project_id,
            "spec_sha256": spec_sha, "created_at": created_at,
            "report_path": f"reports/{key}.report.json",
        }
        report = {
            "phase": "P00", "status": "P00_HANDOFF_OK", "job_id": job_id,
            "request_id": req_id, "project_id": project_id,
            "spec_sha256": spec_sha, "created_at": created_at,
            "received_title": obj["title"],
            "real_codex_run": False, "code_changed": False,
            "tests_executed": False, "github_pr_created": False,
            "acceptance": [
                {**ac, "result": "NOT_VERIFIED"} for ac in obj["acceptance"]
            ],
            "message": "本地自动任务交接成功，仅为模拟结果，尚未连接 Codex、运行测试或创建 PR。",
        }
        readable = (
            f"# SoloShip P00 交接报告\n\n"
            f"- Job: `{job_id}`\n"
            f"- Request: `{req_id}`\n"
            f"- Status: `P00_HANDOFF_OK`\n"
            f"- Task SHA-256: `{spec_sha}`\n\n"
            f"**已经证实**：Bridge 接收了结构化任务并写回了本地报告。\n\n"
            f"**尚未运行**：Codex、代码修改、实际项目测试、GitHub PR。\n\n"
            f"下一阶段才能接入 Codex CLI。\n"
        )
        atomic_text(self.receipts / f"{key}.receipt.json", dump_json(receipt))
        atomic_text(self.reports / f"{key}.report.json", dump_json(report))
        atomic_text(self.reports / f"{key}.report.md", readable)
        self.stash(source, self.archive)
        print(f"[{outcome}] {key} => {job_id}; report ready", flush=True)
        return outcome.lower()

    def poll(self) -> int:
        count = 0
        for path in sorted(self.inbox.glob("*.ready.json")):
            try:
                self.process_one(path)
                count += 1
            except OSError as exc:
                print(f"[RETRYABLE-IO] {path.name}: {exc}", file=sys.stderr, flush=True)
            except sqlite3.Error as exc:
                print(f"[RETRYABLE-DB] {path.name}: {exc}", file=sys.stderr, flush=True)
        return count


def main() -> None:
    parser = argparse.ArgumentParser(description="SoloShip P00 本地交接模拟 Bridge")
    parser.add_argument("--once", action="store_true", help="只处理当前已提交的文件并退出")
    parser.add_argument("--root", type=Path, default=ROOT, help="测试用：切换工作根目录")
    args = parser.parse_args()
    bridge = SoloShipP00(args.root)
    print("SoloShip P00 已启动：仅处理 exchange/inbox/*.ready.json", flush=True)
    print(f"交接目录：{bridge.exchange}", flush=True)
    print("本阶段不会启动 Codex、运行 shell 命令、修改仓库或创建 PR。", flush=True)
    if args.once:
        print(f"本次处理 {bridge.poll()} 个文件。", flush=True)
        return
    print("保持窗口打开；按 Ctrl+C 停止。", flush=True)
    try:
        while True:
            bridge.poll()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("已退出。", flush=True)


if __name__ == "__main__":
    main()
