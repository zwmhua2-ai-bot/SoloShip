#!/usr/bin/env python3
"""SoloShip P02 bridge: implement -> validate -> review -> fix loop.

States: IMPLEMENTING -> VALIDATING -> REVIEWING -> READY_FOR_HUMAN
        REVIEWING (blocking findings or red tests) -> FIXING -> VALIDATING -> REVIEWING
        max two automatic fix rounds, then BLOCKED.

Hard limits on this host: the Codex sandbox is not enforced on Windows, so only
the dedicated demo repository is accepted. Nothing is pushed, no PR is created.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_adapter as codex  # noqa: E402
import github_publisher  # noqa: E402
import github_status  # noqa: E402
import model_router  # noqa: E402
import p02_support as support  # noqa: E402
import reviewer as reviewer_mod  # noqa: E402
import security  # noqa: E402
import status_index  # noqa: E402
from p00_bridge import atomic_text, dump_json, utc_now  # noqa: E402
from p01_bridge import build_prompt, validate_p01_task  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_REPO = "demo/soloship-demo"
MAX_FIX_ROUNDS = 2

READY = "P02_READY_FOR_HUMAN"
BLOCKED_REVIEW = "P02_BLOCKED_REVIEW"
BLOCKED_REVIEW_INVALID = "P02_BLOCKED_REVIEW_INVALID"
BLOCKED_TESTS = "P02_BLOCKED_TESTS"
BLOCKED_LIMIT = "P02_BLOCKED_FIX_LIMIT"
BLOCKED_TAMPER = "P02_BLOCKED_REVIEW_TAMPER"
BLOCKED_PATH = "P02_BLOCKED_PATH_VIOLATION"
BLOCKED_CODEX = "P02_BLOCKED_CODEX"
BLOCKED_TIMEOUT = "P02_BLOCKED_TIMEOUT"
BLOCKED_CLI = "P02_BLOCKED_CLI_MISSING"
BLOCKED_AUTH = "P02_BLOCKED_AUTH"
BLOCKED_MODEL = "P02_BLOCKED_MODEL"
REPO_NOT_ALLOWED = "P02_REPO_NOT_ALLOWED"
DUPLICATE = "P02_DUPLICATE"
REJECTED = "P02_TASK_REJECTED"
BUSY = "P02_BUSY"
FINALIZING = "P02_FINALIZING"      # 最终化进行中：DB 可见、证据尚未齐全
NEEDS_HUMAN = "P02_NEEDS_HUMAN"    # 无法安全恢复：明确交人工，绝不假装成功
ERROR = "P02_ERROR"                # 未预期的执行异常（与 BUSY 严格区分）
FINALIZE_ERROR = "P02_FINALIZE_ERROR"   # 最终化阶段的故障（报告/归档/状态写入）
JOURNAL_SCHEMA = "soloship.p02.finalizing/v1"

# 进入这些状态即视为已有终态结论，恢复流程只核对、不改写
TERMINAL_STATUSES = {
    READY, BLOCKED_REVIEW, BLOCKED_REVIEW_INVALID, BLOCKED_TESTS, BLOCKED_LIMIT,
    BLOCKED_TAMPER, BLOCKED_PATH, BLOCKED_CODEX, BLOCKED_TIMEOUT, BLOCKED_CLI,
    BLOCKED_AUTH, BLOCKED_MODEL, DUPLICATE, REJECTED, NEEDS_HUMAN,
}


class RequestBusy(RuntimeError):
    """同一 request 的互斥锁冲突。只有它才映射为 P02_BUSY。

    历史上 `process_one` 用 `except RuntimeError` 兜住整个 `_run_job`，
    于是 git 失败、路由错误等任何 RuntimeError 都被报成 P02_BUSY，日志失真。
    """


class FinalizeError(RuntimeError):
    """最终化（Journal/报告/归档/状态）故障。只映射 P02_FINALIZE_ERROR。

    抛出后 Journal 保留，供 `recover_finalizing()` 重放，不重复执行 Codex。
    """


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json_file(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return None


class P02Bridge:
    def __init__(self, root: Path = ROOT, codex_cmd: list[str] | None = None,
                 sandbox_status: str | None = None, isolation: str = support.ISOLATION_PARTIAL,
                 publish_gh_cmd: list[str] | None = None):
        self.root = Path(root).resolve()
        self.exchange = self.root / "exchange" / "p02"
        self.inbox = self.exchange / "inbox"
        self.receipts = self.exchange / "receipts"
        self.reports = self.exchange / "reports"
        self.artifacts = self.reports / "artifacts"
        self.archive = self.exchange / "archive"
        self.rejected = self.exchange / "rejected"
        self.control = self.root / ".control"
        self.state_dir = self.control / "p02"
        self.locks = self.state_dir / "locks"
        self.finalizing_dir = self.state_dir / "finalizing"
        self.finalized_dir = self.state_dir / "finalized"
        self.worktrees = self.root / ".worktrees"
        for path in (self.inbox, self.receipts, self.reports, self.artifacts, self.archive,
                     self.rejected, self.control, self.state_dir, self.locks,
                     self.finalizing_dir, self.finalized_dir, self.worktrees):
            path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "p02.db"
        self.codex_cmd = codex_cmd
        self.publish_gh_cmd = publish_gh_cmd
        self.realtime_probe = None  # 测试可注入；None 表示用真实只读 gh api
        self.sandbox_status = sandbox_status or self._cached_sandbox_status()
        self.isolation = isolation
        self.execution_mode = security.execution_mode(self.root)
        self.test_mode = security.windows_test_mode(self.root)
        self.router = model_router.Router.from_root(self.root)
        self.publisher = github_publisher.Publisher.from_root(self.root, gh_cmd=publish_gh_cmd)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS p02_jobs (
                job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                spec_sha TEXT NOT NULL,
                status TEXT NOT NULL,
                repo TEXT, worktree TEXT, base_commit TEXT, head_commit TEXT,
                fix_rounds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, finished_at TEXT,
                UNIQUE (project_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS p02_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                round_no INTEGER NOT NULL,
                kind TEXT NOT NULL,
                thread_id TEXT,
                head_sha TEXT,
                diff_sha TEXT,
                test_exit INTEGER,
                review_status TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS p02_worktrees (
                path TEXT PRIMARY KEY,
                job_id TEXT,
                request_id TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT
            );
            """)

    # ------------------------------------------------------------------ db
    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _cached_sandbox_status(self) -> str:
        cache = self.state_dir / "sandbox_probe.json"
        if cache.is_file():
            try:
                return json.loads(cache.read_text(encoding="utf-8"))["status"]
            except (ValueError, KeyError):
                pass
        return support.SANDBOX_UNVERIFIED

    # ------------------------------------------------------------------ git
    def git(self, *args: str, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )

    def changed_paths(self, worktree: Path) -> list[str]:
        proc = self.git("status", "--porcelain", cwd=worktree)
        return [line[3:].replace("\\", "/").strip().strip('"') for line in proc.stdout.splitlines() if line.strip()]

    def path_allowed(self, changed: list[str], allow_paths: list[str]) -> tuple[bool, list[str]]:
        allow = [item.replace("\\", "/").strip().rstrip("/") for item in allow_paths]
        bad = [p for p in changed
               if not any(p == a or p.startswith(a + "/") for a in allow)]
        return not bad, bad

    def commit(self, worktree: Path, request_id: str, message: str) -> str | None:
        if self.git("add", "-A", cwd=worktree).returncode != 0:
            return None
        proc = self.git("-c", "user.name=SoloShip Bridge", "-c", "user.email=bridge@soloship.local",
                        "commit", "-m", message[:200], cwd=worktree)
        if proc.returncode != 0:
            return None
        return self.git("rev-parse", "HEAD", cwd=worktree).stdout.strip()

    # ------------------------------------------------------------- registry
    def register_worktree(self, path: Path, job_id: str, request_id: str, state: str, note: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO p02_worktrees (path, job_id, request_id, state, created_at, note)"
                " VALUES (?,?,?,?,?,?)",
                (str(path), job_id, request_id, state, utc_now(), note),
            )

    def set_worktree_state(self, path: Path, state: str, note: str = "") -> None:
        with self.connect() as db:
            db.execute("UPDATE p02_worktrees SET state=?, note=? WHERE path=?", (state, note, str(path)))

    # ----------------------------------------------------------------- lock
    @contextmanager
    def request_lock(self, request_id: str):
        lock = self.locks / f"{request_id}.lock"
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RequestBusy("同一 request 正在处理中") from None
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    # ---------------------------------------------------------------- task
    def reject(self, source: Path, message: str) -> str:
        payload = {"phase": "P02", "status": REJECTED, "source_file": source.name,
                   "reason": message, "timestamp": utc_now()}
        atomic_text(self.receipts / f"{source.name}.error.json", dump_json(payload))
        shutil.move(str(source), str(self.rejected / f"{source.name}.{uuid.uuid4().hex[:8]}"))
        print(f"[{REJECTED}] {source.name}: {message}", flush=True)
        return REJECTED

    def accept(self, obj: dict) -> tuple[str, str]:
        spec_sha = sha256_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        with self.connect() as db:
            row = db.execute("SELECT job_id, spec_sha FROM p02_jobs WHERE project_id=? AND request_id=?",
                             (obj["project_id"], obj["request_id"])).fetchone()
            if row:
                if row[1] != spec_sha:
                    raise ValueError("同一 request_id 已存在不同内容；需使用新 request_id")
                return row[0], DUPLICATE
            job_id = "p02_" + uuid.uuid4().hex[:12]
            db.execute("INSERT INTO p02_jobs (job_id, project_id, request_id, spec_sha, status, created_at)"
                       " VALUES (?,?,?,?,?,?)",
                       (job_id, obj["project_id"], obj["request_id"], spec_sha, "ACCEPTED", utc_now()))
            return job_id, "ACCEPTED"

    def finish(self, job_id: str, *, mark_finished: bool = True, **fields: Any) -> None:
        """更新任务行。`mark_finished=False` 用于 FINALIZING，避免提前写完成时间。"""
        allowed = {"status", "repo", "worktree", "base_commit", "head_commit", "fix_rounds"}
        sets, values = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                values.append(value)
        if mark_finished:
            sets.append("finished_at=?")
            values.append(utc_now())
        if not sets:
            return
        values.append(job_id)
        with self.connect() as db:
            db.execute(f"UPDATE p02_jobs SET {', '.join(sets)} WHERE job_id=?", values)

    def record_meta(self, job_id: str, **fields: Any) -> None:
        """只记录 repo/worktree/commit 等元数据，不改状态、不写完成时间。"""
        self.finish(job_id, mark_finished=False,
                    **{k: v for k, v in fields.items() if k != "status"})

    # --------------------------------------------------- finalize journal
    def _journal_path(self, job_id: str) -> Path:
        return self.finalizing_dir / f"{job_id}.json"

    def write_journal(self, *, job_id: str, key: str, request_id: str, project_id: str,
                      status: str, source_name: str, evidence: dict, receipt: dict,
                      artifacts: dict, review_payload: dict | None) -> Path:
        """最终化日志：**先于**任何交付物、也先于 DB 状态变更落盘。

        除结构化证据外，还记录三份交付物的**最终文本**，因此重启后写出的报告
        与崩溃前逐字节一致，既能补齐缺失文件，也能识别内容冲突。
        全程不含进程内对象，不需要再次调用 Codex、也不会重复发布 PR。
        """
        body = {
            "schema": JOURNAL_SCHEMA, "job_id": job_id, "key": key,
            "request_id": request_id, "project_id": project_id, "status": status,
            "source_name": source_name, "evidence": evidence, "receipt": receipt,
            "artifacts": artifacts, "review": review_payload, "created_at": utc_now(),
        }
        body["payload_sha256"] = sha256_text(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        path = self._journal_path(job_id)
        atomic_text(path, dump_json(body))
        return path

    def load_journal(self, path: Path) -> tuple[dict | None, str]:
        """读取并校验 Journal 完整性。校验失败一律不采信。"""
        data = _read_json_file(path)
        if not isinstance(data, dict):
            return None, "JOURNAL_UNREADABLE: 无法解析 Journal"
        if data.get("schema") != JOURNAL_SCHEMA:
            return None, f"JOURNAL_SCHEMA_MISMATCH: {data.get('schema')!r}"
        recorded = data.pop("payload_sha256", None)
        actual = sha256_text(
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        data["payload_sha256"] = recorded
        if not recorded or recorded != actual:
            return None, "JOURNAL_INTEGRITY: payload_sha256 不匹配"
        return data, ""

    def begin_finalizing(self, job_id: str) -> None:
        """推进到 FINALIZING，且**不写** finished_at（避免提前看起来已完成）。"""
        self.finish(job_id, mark_finished=False, status=FINALIZING)

    @staticmethod
    def review_payload(review) -> dict | None:
        """把 ReviewResult 压成可 JSON 化的证据摘要（仅供审计与一致性校验）。"""
        if review is None:
            return None
        payload = dict(review.evidence())
        payload["ok"] = bool(getattr(review, "ok", True))
        return payload

    def artifact_paths(self, key: str) -> dict[str, Path]:
        return {
            "receipt": self.receipts / f"{key}.receipt.json",
            "report_json": self.reports / f"{key}.report.json",
            "report_md": self.reports / f"{key}.report.md",
        }

    def build_artifacts(self, key: str, evidence: dict, receipt: dict, review) -> dict[str, str]:
        """三份交付物的最终文本。写入与核对共用同一份定义。"""
        return {
            "receipt": dump_json(receipt),
            "report_json": dump_json(evidence),
            "report_md": self.render_report(evidence, receipt, review),
        }

    def _materialize(self, key: str, artifacts: dict[str, str]) -> None:
        paths = self.artifact_paths(key)
        for name, text in artifacts.items():
            atomic_text(paths[name], text)

    def _archive_source(self, source: Path) -> None:
        """幂等归档：源文件已不在（上次已完成归档）就不动。"""
        if not source.exists():
            return
        shutil.move(str(source), str(self.archive / f"{source.name}.{uuid.uuid4().hex[:8]}"))

    def check_journal_evidence(self, journal: dict) -> list[str]:
        """跨证据一致性校验：HEAD / Review candidate / 测试结论必须自洽。"""
        problems: list[str] = []
        evidence = journal.get("evidence")
        receipt = journal.get("receipt")
        job_id, request_id = journal.get("job_id"), journal.get("request_id")
        project_id = journal.get("project_id")

        if not (job_id and request_id and project_id):
            return ["MISSING_IDS: Journal 缺少 job_id/request_id/project_id"]
        if journal.get("status") not in (TERMINAL_STATUSES - {NEEDS_HUMAN}):
            problems.append(f"STATUS_NOT_TERMINAL: {journal.get('status')}")
        if not isinstance(evidence, dict) or not evidence:
            problems.append("MISSING_EVIDENCE: 缺少 evidence")
        if not isinstance(receipt, dict) or not receipt:
            problems.append("MISSING_RECEIPT: 缺少 receipt")
        artifacts = journal.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != {"receipt", "report_json", "report_md"}:
            problems.append("MISSING_ARTIFACTS: Journal 未记录完整交付物文本")
        elif any(not isinstance(v, str) or not v for v in artifacts.values()):
            problems.append("EMPTY_ARTIFACTS: 交付物文本为空")
        if problems:
            return problems

        if evidence.get("job_id") != job_id or evidence.get("request_id") != request_id:
            problems.append("EVIDENCE_ID_MISMATCH")
        if receipt.get("job_id") != job_id or receipt.get("request_id") != request_id:
            problems.append("RECEIPT_ID_MISMATCH")
        if journal.get("key") != f"{project_id}__{request_id}":
            problems.append("KEY_MISMATCH")

        head = evidence.get("head_commit")
        if receipt.get("head_commit") != head:
            problems.append(
                f"HEAD_COMMIT_CONFLICT: evidence={head} receipt={receipt.get('head_commit')}")

        review = journal.get("review") or {}
        candidate = review.get("candidate_sha")
        if candidate and head and candidate != head:
            problems.append(f"REVIEW_CANDIDATE_SHA_CONFLICT: {candidate} != {head}")

        tests = evidence.get("tests") or {}
        if isinstance(tests, dict) and "exit_code" in tests:
            expected_ok = tests.get("exit_code") == 0
            if receipt.get("tests_ok") is not None and bool(receipt["tests_ok"]) != expected_ok:
                problems.append(
                    f"TESTS_RESULT_CONFLICT: exit={tests.get('exit_code')} "
                    f"receipt.tests_ok={receipt['tests_ok']}")
        return problems

    def _job_status(self, job_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT status FROM p02_jobs WHERE job_id=?", (job_id,)).fetchone()
        return row[0] if row else None

    def _needs_human(self, job_id: str, reason: str, *, mark_db: bool = True) -> dict:
        """无法安全恢复：留痕 + 明确状态，绝不假装成功。"""
        marker = self.finalizing_dir / f"{job_id}.needs_human.json"
        atomic_text(marker, dump_json({"job_id": job_id, "reason": reason,
                                       "at": utc_now()}))
        if mark_db:
            with self.connect() as db:
                db.execute("UPDATE p02_jobs SET status=? WHERE job_id=?", (NEEDS_HUMAN, job_id))
        print(f"[{NEEDS_HUMAN}] {job_id}: {reason}", flush=True)
        return {"state": "needs_human", "job_id": job_id, "reason": reason}

    def _recover_one(self, path: Path, *, from_manifest: bool = False) -> dict:
        """重放单条证据：补交付物、补归档、推进状态；冲突一律交人工。

        from_manifest=True 表示任务已正常完成，证据清单在 finalized/ 下：
        此时只补缺失文件，不改数据库状态、不移动 inbox 文件、也不删除清单，
        因此可以反复重放且不会改写历史。
        """
        fallback_id = path.stem
        journal, problem = self.load_journal(path)
        if problem:
            return self._needs_human(fallback_id, problem,
                                     mark_db=self._job_status(fallback_id) is not None)

        job_id = journal["job_id"]
        problems = self.check_journal_evidence(journal)
        if problems:
            return self._needs_human(job_id, "; ".join(problems),
                                     mark_db=self._job_status(job_id) is not None)

        key, target_status = journal["key"], journal["status"]
        artifacts = journal["artifacts"]
        row_status = self._job_status(job_id)
        if row_status is None:
            return self._needs_human(job_id, "JOB_ROW_MISSING: 数据库无对应任务行", mark_db=False)

        if from_manifest:
            return self._repair_from_manifest(job_id, key, artifacts, row_status)

        # 1) 交付物：缺失补写；已存在且逐字节一致则保留；冲突绝不覆盖
        paths = self.artifact_paths(key)
        for name, expected in artifacts.items():
            target = paths[name]
            if target.is_file():
                try:
                    current = target.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    return self._needs_human(job_id, f"ARTIFACT_UNREADABLE: {target.name} ({exc})")
                if current != expected:
                    return self._needs_human(job_id, f"ARTIFACT_CONFLICT: {target.name}")
            else:
                atomic_text(target, expected)

        # 2) 归档：源文件仍在 inbox 就补一次（幂等，不产生重复副本）
        source_name = journal.get("source_name")
        if source_name:
            self._archive_source(self.inbox / source_name)

        # 3) 状态：Journal 可能是唯一真相（AC-10：DB 尚未进入 FINALIZING）
        if row_status != target_status:
            self.finish(job_id, status=target_status)

        path.unlink(missing_ok=True)
        print(f"[{target_status}] 恢复最终化 {job_id}（原状态 {row_status}）", flush=True)
        return {"state": "recovered", "job_id": job_id, "status": target_status,
                "from": row_status}

    def _repair_from_manifest(self, job_id: str, key: str, artifacts: dict,
                              row_status: str | None) -> dict:
        """已完成任务只补缺失交付物：不改状态、不覆盖冲突、不删除清单。"""
        if row_status == NEEDS_HUMAN:
            return {"state": "skipped", "job_id": job_id, "reason": "已标记人工处理"}
        paths = self.artifact_paths(key)
        repaired: list[str] = []
        for name, expected in artifacts.items():
            target = paths[name]
            if target.is_file():
                try:
                    current = target.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    return self._needs_human(
                        job_id, f"ARTIFACT_UNREADABLE: {target.name} ({exc})", mark_db=False)
                if current != expected:
                    # 完成后被改写：不覆盖、不改状态，只留人工标记
                    return self._needs_human(
                        job_id, f"ARTIFACT_CONFLICT: {target.name}", mark_db=False)
            else:
                atomic_text(target, expected)
                repaired.append(target.name)
        if not repaired:
            return {"state": "noop", "job_id": job_id}
        print(f"[RECOVERED] 依已完成证据补齐 {key}: {', '.join(repaired)}", flush=True)
        return {"state": "recovered", "job_id": job_id, "from_manifest": True,
                "status": row_status, "repaired": repaired}

    def recover_job(self, job_id: str) -> dict:
        path = self._journal_path(job_id)
        if not path.is_file():
            return {"state": "none", "job_id": job_id}
        return self._recover_one(path)

    def scan_missing_reports(self, with_journal: set[str] | None = None) -> list[dict]:
        """终态任务报告缺失、且没有 Journal 可重放 → 只能交人工（不写库，保历史）。"""
        with_journal = with_journal or set()
        found: list[dict] = []
        with self.connect() as db:
            rows = db.execute(
                "SELECT job_id, project_id, request_id, status FROM p02_jobs").fetchall()
        for job_id, project_id, request_id, status in rows:
            if status not in TERMINAL_STATUSES or status == NEEDS_HUMAN:
                continue
            if job_id in with_journal:
                continue
            key = f"{project_id}__{request_id}"
            if (self.reports / f"{key}.report.md").is_file():
                continue
            marker = self.finalizing_dir / f"{job_id}.needs_human.json"
            if not marker.is_file():
                atomic_text(marker, dump_json({
                    "job_id": job_id, "at": utc_now(),
                    "reason": "REPORT_MISSING_NO_EVIDENCE: 报告缺失且无 Journal 可重放",
                }))
            found.append({"job_id": job_id, "request_id": request_id,
                          "status": status, "reason": "REPORT_MISSING_NO_EVIDENCE"})
        return found

    def recover_finalizing(self) -> dict:
        """扫描全部证据并与数据库对账。多次调用结果收敛（幂等）。

        两处来源：
          finalizing/*.json —— 崩溃未收尾，需要补交付物 + 推进状态；
          finalized/*.json  —— 已完成任务的证据清单，只在文件缺失时补齐。
        """
        self.finalizing_dir.mkdir(parents=True, exist_ok=True)
        self.finalized_dir.mkdir(parents=True, exist_ok=True)
        summary: dict[str, Any] = {"scanned": 0, "recovered": [], "needs_human": [],
                                   "noop": 0, "manifests": 0}
        pending: set[str] = set()

        for path in sorted(self.finalizing_dir.glob("*.json")):
            if path.name.endswith(".needs_human.json"):
                continue
            pending.add(path.stem)
            summary["scanned"] += 1
            outcome = self._recover_one(path)
            if outcome["state"] == "recovered":
                summary["recovered"].append(outcome)
            elif outcome["state"] == "needs_human":
                summary["needs_human"].append(outcome)
            else:
                summary["noop"] += 1

        for path in sorted(self.finalized_dir.glob("*.json")):
            if path.name.endswith(".needs_human.json"):
                continue
            pending.add(path.stem)
            summary["manifests"] += 1
            outcome = self._recover_one(path, from_manifest=True)
            if outcome["state"] == "recovered":
                summary["recovered"].append(outcome)
            elif outcome["state"] == "needs_human":
                summary["needs_human"].append(outcome)
            else:
                summary["noop"] += 1

        summary["missing_reports"] = self.scan_missing_reports(pending)
        return summary

    def record_round(self, job_id: str, round_no: int, kind: str, **fields: Any) -> None:
        payload = {
            "job_id": job_id, "round_no": round_no, "kind": kind,
            "thread_id": fields.get("thread_id"), "head_sha": fields.get("head_sha"),
            "diff_sha": fields.get("diff_sha"), "test_exit": fields.get("test_exit"),
            "review_status": fields.get("review_status"), "note": fields.get("note"),
            "created_at": utc_now(),
        }
        with self.connect() as db:
            db.execute("INSERT INTO p02_rounds (job_id, round_no, kind, thread_id, head_sha, diff_sha,"
                       " test_exit, review_status, note, created_at)"
                       " VALUES (:job_id,:round_no,:kind,:thread_id,:head_sha,:diff_sha,"
                       " :test_exit,:review_status,:note,:created_at)", payload)

    def update_round(self, job_id: str, round_no: int, kind: str, **fields: Any) -> None:
        allowed = {"head_sha", "diff_sha", "test_exit", "note"}
        sets, values = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                values.append(value)
        if not sets:
            return
        values.extend([job_id, round_no, kind])
        with self.connect() as db:
            db.execute(
                f"UPDATE p02_rounds SET {', '.join(sets)} WHERE job_id=? AND round_no=? AND kind=?",
                values,
            )

    # ------------------------------------------------------------- validate
    def run_tests(self, cmd: list[str], cwd: Path, timeout_s: int) -> dict:
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout_s,
                                  stdin=subprocess.DEVNULL, env=support.sanitized_env())
        except subprocess.TimeoutExpired:
            return {"command": cmd, "exit_code": None, "timed_out": True,
                    "stdout_tail": "", "stderr_tail": f"测试超过 {timeout_s}s"}
        return {
            "command": cmd,
            "exit_code": proc.returncode,
            "timed_out": False,
            "stdout_tail": "\n".join(proc.stdout.strip().splitlines()[-40:]),
            "stderr_tail": "\n".join(proc.stderr.strip().splitlines()[-15:]),
        }

    # --------------------------------------------------------------- main
    def require_auto_dev(self) -> tuple[bool, str]:
        """统一安全门禁：每次调用都实时读取配置与沙箱状态。

        不复用 `__init__` 缓存，避免长驻进程（Supervisor）拿到旧配置；
        同时把 `execution_mode` / `test_mode` 刷新为实时值，保证后续证据与
        白名单判断用的是同一份状态。
        """
        live_mode = security.execution_mode(self.root)
        if live_mode != self.execution_mode:
            self.execution_mode = live_mode
            self.test_mode = security.windows_test_mode(self.root)
        return security.require_auto_dev(self.root)

    def process_one(self, source: Path) -> str:
        if source.is_symlink():
            return self.reject(source, "不接受符号链接")
        if source.stat().st_size > 256 * 1024:
            return self.reject(source, "任务文件超过 256 KiB")
        try:
            obj = json.loads(source.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            return self.reject(source, f"JSON 无法解析: {exc}")

        errors = validate_p01_task(obj, self.root)
        if errors:
            return self.reject(source, "; ".join(errors))
        if source.name != f"{obj['request_id']}.ready.json":
            return self.reject(source, "文件名必须是 <request_id>.ready.json")
        # 安全门禁：在执行任何 Codex / 测试动作之前强制检查。
        # 不能只依赖 CLI main() 里的 guard_or_exit —— Supervisor 是同进程直调。
        allowed, gate_message = self.require_auto_dev()
        if not allowed:
            return self.reject(source, gate_message)
        repo_key = self._repo_key(obj["workspace"]["repo"])
        if repo_key not in self.allowed_repos():
            return self.reject(source, f"P02 未授权仓库: {repo_key}")
        if self.execution_mode == "WINDOWS_TEST_MODE":
            approved_projects = (self.test_mode or {}).get("approved_projects") or []
            approved_repos = (self.test_mode or {}).get("approved_repos") or []
            if obj["project_id"] not in approved_projects:
                return self.reject(source, f"WINDOWS_TEST_MODE 未注册该项目: {obj['project_id']}")
            if not Path(obj["workspace"]["repo"]).is_absolute() and repo_key not in approved_repos:
                return self.reject(source, f"WINDOWS_TEST_MODE 未注册该仓库: {obj['workspace']['repo']}")

        try:
            with self.request_lock(obj["request_id"]):
                return self._run_job(source, obj)
        except RequestBusy as exc:
            print(f"[{BUSY}] {source.name}: {exc}", flush=True)
            return BUSY
        except FinalizeError as exc:
            # 最终化故障：Journal 已保留，可由 recover_finalizing() 重放
            print(f"[{FINALIZE_ERROR}] {source.name}: {exc}", flush=True)
            return FINALIZE_ERROR
        # 其余异常一律不吞：保留原始类型与堆栈，避免被误标成 P02_BUSY

    def _run_job(self, source: Path, obj: dict) -> str:
        try:
            job_id, outcome = self.accept(obj)
        except ValueError as exc:
            return self.reject(source, str(exc))
        request_id, project_id = obj["request_id"], obj["project_id"]
        key = f"{project_id}__{request_id}"
        if outcome == DUPLICATE:
            # 上次可能停在最终化中途：先按 Journal 补齐；恢复成功就返回真实终态。
            recovered = self.recover_job(job_id)
            if recovered.get("state") == "recovered":
                self._archive_source(source)
                print(f"[RECOVERED] {key} => {job_id}; status={recovered.get('status')}",
                      flush=True)
                return str(recovered.get("status") or DUPLICATE)
            payload = {"phase": "P02", "status": DUPLICATE, "job_id": job_id, "request_id": request_id,
                       "project_id": project_id, "note": "同一任务已处理过，未重复调用 Codex",
                       "finished_at": utc_now()}
            # 不覆盖已有回执：它是历史交付证据，重复投递只追加旁证文件。
            target = self.receipts / f"{key}.receipt.json"
            if target.exists():
                atomic_text(self.receipts / f"{key}.duplicate.json", dump_json(payload))
            else:
                atomic_text(target, dump_json(payload))
            self._archive_source(source)
            print(f"[{DUPLICATE}] {key} => {job_id}", flush=True)
            return DUPLICATE

        workspace = obj["workspace"]
        verify = obj["verify"]
        spec = obj.get("codex") or {}
        repo = (self.root / workspace["repo"]).resolve()
        allow_paths = workspace["allow_paths"]
        test_cmd = [str(p) for p in verify["test_command"]]
        test_timeout = int(verify.get("timeout_seconds", 300))
        sandbox = spec.get("sandbox", codex.DEFAULT_SANDBOX)
        codex_timeout = int(spec.get("timeout_seconds", 900))

        evidence: dict[str, Any] = {
            "phase": "P02", "job_id": job_id, "request_id": request_id, "project_id": project_id,
            "title": obj["title"], "goal": obj["goal"],
            "repo": str(repo), "sandbox_mode": sandbox,
            "sandbox_status": self.sandbox_status,
            "execution_mode": self.execution_mode,
            "risk_accepted": bool(self.test_mode and self.test_mode.get("risk_accepted")),
            "isolation_claim": (self.test_mode or {}).get("isolation_claim", "NONE"),
            "test_isolation": self.isolation,
            "codex_version": codex.codex_version(self.codex_cmd),
            "started_at": utc_now(),
            "rounds": [],
            "calls": [],
            "artifacts": {},
            "evidence_status": {},
        }
        if self.sandbox_status != support.SANDBOX_ENFORCED:
            evidence["evidence_status"]["sandbox"] = (
                f"UNAVAILABLE_ENFORCEMENT: {self.sandbox_status}（-s {sandbox} 在本机不限制文件系统）"
            )

        worktree = self.worktrees / request_id
        try:
            base_sha = self.git("rev-parse", workspace.get("base_ref", "HEAD"), cwd=repo).stdout.strip()
            if not base_sha:
                raise ValueError("无法解析 base_ref")
            proc = self.git("worktree", "add", "-b", f"p02/{request_id}", str(worktree),
                            workspace.get("base_ref", "HEAD"), cwd=repo)
            if proc.returncode != 0:
                raise ValueError(f"git worktree add 失败: {proc.stderr.strip()}")
        except Exception as exc:  # noqa: BLE001
            evidence["fatal"] = f"{type(exc).__name__}: {exc}"
            self.record_meta(job_id, repo=str(repo))
            return self._finalize(source, key, job_id, BLOCKED_CODEX, evidence, request_id, project_id)

        self.register_worktree(worktree, job_id, request_id, "ACTIVE")
        evidence.update({"worktree": str(worktree), "base_commit": base_sha,
                         "branch": f"p02/{request_id}"})

        # ---- optional read-only exploration (Fast tier)
        if spec.get("explore"):
            try:
                explore = self._explore_round(obj, worktree, evidence, sandbox)
                evidence["explore_ok"] = explore.ok
            except model_router.RouterError as exc:
                evidence.setdefault("errors", []).append({"stage": "explore_route", "error": str(exc)})
                self.record_meta(job_id, repo=str(repo), worktree=str(worktree),
                                 base_commit=base_sha)
                evidence.update({"status": BLOCKED_MODEL, "head_commit": None, "fix_rounds": 0,
                                 "finished_at": utc_now()})
                return self._finalize(source, key, job_id, BLOCKED_MODEL, evidence, request_id, project_id)

        # ---- round 0: implement
        status, head_sha, diff_text, changed, test_result, review = self._implement_round(
            job_id, obj, worktree, base_sha, 0, test_cmd, test_timeout, codex_timeout,
            allow_paths, evidence, key, sandbox,
        )

        fix_rounds = 0
        final_status = status
        while final_status in (BLOCKED_TESTS, BLOCKED_REVIEW) and fix_rounds < MAX_FIX_ROUNDS:
            fix_rounds += 1
            findings = review.findings if review else []
            test_snapshot = test_result or {}
            final_status, head_sha, diff_text, changed, test_result, review = self._fix_round(
                job_id, obj, worktree, base_sha, head_sha, fix_rounds, test_cmd, test_timeout,
                codex_timeout, allow_paths, evidence, key, sandbox, findings, test_snapshot,
            )

        if final_status in (BLOCKED_TESTS, BLOCKED_REVIEW) and fix_rounds >= MAX_FIX_ROUNDS:
            final_status = BLOCKED_LIMIT

        state = READY == final_status
        self.set_worktree_state(
            worktree,
            "READY_FOR_CLEANUP" if state else "RETAINED",
            "任务通过，可待人工确认后回收" if state else "失败现场保留，供排查",
        )
        if state:
            evidence["publish"] = self._publish_draft(
                job_id, obj, worktree, head_sha, base_sha, evidence, changed, test_result, review,
            )
        self.record_meta(job_id, repo=str(repo), worktree=str(worktree),
                         base_commit=base_sha, head_commit=head_sha, fix_rounds=fix_rounds)
        evidence.update({"status": final_status, "head_commit": head_sha,
                         "fix_rounds": fix_rounds, "finished_at": utc_now()})
        return self._finalize(source, key, job_id, final_status, evidence, request_id, project_id,
                              head_sha=head_sha, fix_rounds=fix_rounds, review=review,
                              changed=changed, test_result=test_result, diff_text=diff_text)

    # --------------------------------------------------------- round logic
    def _repo_key(self, repo: str) -> str:
        value = Path(repo)
        return str(value.resolve()) if value.is_absolute() else repo.replace("\\", "/")

    def allowed_repos(self) -> set[str]:
        allowed = {ALLOWED_REPO}
        allowed.update(security.approved_absolute_repos(self.root))
        return allowed

    def _publish_draft(self, job_id, obj, worktree, head_sha, base_sha, evidence,
                       changed, test_result, review) -> dict:
        """Publish gate + draft PR. Never let a publisher failure corrupt the task result."""
        job = {
            "job_id": job_id,
            "request_id": obj["request_id"],
            "status": READY,
            "head_commit": head_sha,
            "base_commit": base_sha,
            "tests_ok": bool(test_result and test_result.get("exit_code") == 0),
            "review_status": review.status if review else None,
            "fix_rounds": evidence.get("fix_rounds", 0),
            "changed_paths": changed,
            "execution_mode": evidence.get("execution_mode"),
            "sandbox_status": evidence.get("sandbox_status"),
            "test_isolation": evidence.get("test_isolation"),
            "isolation_claim": evidence.get("isolation_claim", "NONE"),
            "title": obj["title"],
        }
        repo = self.publisher.cfg.get("target_repo") or (self.publisher.allowed_repos or [""])[0]
        base_branch = self.publisher.cfg.get("target_branch") or self.publisher.allowed_bases[0]
        try:
            result = self.publisher.publish(
                job, worktree=worktree, candidate_sha=head_sha, repo=repo,
                base_branch=base_branch, expected_base_sha=base_sha,
                cancelled=bool(evidence.get("cancelled")),
            )
            return result.to_dict()
        except Exception as exc:  # noqa: BLE001 - publish must not break the job record
            return {"status": "PUBLISH_FAILED_EXCEPTION", "detail": f"{type(exc).__name__}: {exc}",
                    "candidate_sha": head_sha, "stage": "exception"}

    def _route(self, role: str, obj: dict, evidence: dict, fix_round: int = 1):
        spec = obj.get("codex") or {}
        override = {key: spec[key] for key in ("tier", "model", "effort") if key in spec}
        decision = self.router.decide(role, task=obj, fix_round=fix_round, override=override)
        evidence.setdefault("routes", []).append(decision.to_dict())
        return decision

    def _call_codex(self, role, obj, prompt, worktree, sandbox, timeout_s, last_name, evidence,
                    fix_round: int = 1):
        decision = self._route(role, obj, evidence, fix_round)
        run = codex.run_codex(
            prompt, worktree, sandbox=sandbox, timeout_s=timeout_s,
            codex_cmd=self.codex_cmd,
            last_message_path=self.state_dir / last_name,
            model=decision.model, effort=decision.effort,
            ignore_user_config=self.router.ignore_user_config,
        )
        evidence["calls"].append(self.router.telemetry(decision, run))
        return run

    def _explore_round(self, obj, worktree, evidence, sandbox):
        prompt = (
            "只读勘察任务：列出仓库根目录结构与 tests/ 下的测试文件，指出与本任务最相关的 2-3 个文件，"
            "并给出建议的测试命令。不要修改、创建或删除任何文件。"
        )
        decision = self._route("explorer", obj, evidence)
        run = codex.run_codex(
            prompt, worktree, sandbox="read-only", timeout_s=300,
            codex_cmd=self.codex_cmd,
            last_message_path=self.state_dir / f"{obj['request_id']}.explore.last.txt",
            model=decision.model, effort=decision.effort,
            ignore_user_config=self.router.ignore_user_config,
        )
        evidence["calls"].append(self.router.telemetry(decision, run))
        evidence["rounds"].append({"round": -1, "kind": "EXPLORE", **run.evidence()})
        return run

    def _implement_round(self, job_id, obj, worktree, base_sha, round_no, test_cmd, test_timeout,
                         codex_timeout, allow_paths, evidence, key, sandbox):
        prompt = build_prompt(obj)
        prompt += (
            "\n注意：本机 Codex 沙箱不在操作系统层生效，你必须只修改上面列出的允许路径；"
            "任何越界写入都会被判为失败。\n"
        )
        try:
            run = self._call_codex("builder", obj, prompt, worktree, sandbox, codex_timeout,
                                   f"{key}.r{round_no}.last.txt", evidence)
        except model_router.RouterError as exc:
            evidence.setdefault("errors", []).append({"stage": "build_route", "error": str(exc)})
            return BLOCKED_MODEL, None, "", [], {}, None
        evidence["rounds"].append({"round": round_no, "kind": "IMPLEMENT", **run.evidence()})
        self.record_round(job_id, round_no, "IMPLEMENT", thread_id=run.thread_id, note=run.error_kind)
        if not run.ok:
            return self._map_codex_failure(run), None, "", [], {}, None
        return self._verify_and_review(job_id, obj, worktree, base_sha, round_no, test_cmd,
                                       test_timeout, allow_paths, evidence, key, "IMPLEMENT")

    def _fix_round(self, job_id, obj, worktree, base_sha, head_sha, round_no, test_cmd, test_timeout,
                   codex_timeout, allow_paths, evidence, key, sandbox, findings, test_snapshot):
        prompt = self._build_fix_prompt(obj, findings, test_snapshot, head_sha)
        role = "fixer_2" if round_no >= 2 else "fixer_1"
        try:
            run = self._call_codex(role, obj, prompt, worktree, sandbox, codex_timeout,
                                   f"{key}.f{round_no}.last.txt", evidence, fix_round=round_no)
        except model_router.RouterError as exc:
            evidence.setdefault("errors", []).append({"stage": "fix_route", "error": str(exc)})
            return BLOCKED_MODEL, head_sha, "", [], {}, None
        evidence["rounds"].append({"round": round_no, "kind": "FIX", **run.evidence()})
        self.record_round(job_id, round_no, "FIX", thread_id=run.thread_id, note=run.error_kind)
        if not run.ok:
            return self._map_codex_failure(run), head_sha, "", [], {}, None
        return self._verify_and_review(job_id, obj, worktree, base_sha, round_no, test_cmd,
                                       test_timeout, allow_paths, evidence, key, "FIX")

    def _map_codex_failure(self, run) -> str:
        return {
            codex.CLI_MISSING: BLOCKED_CLI,
            codex.AUTH_FAILED: BLOCKED_AUTH,
            codex.TIMEOUT: BLOCKED_TIMEOUT,
        }.get(run.error_kind, BLOCKED_CODEX)

    def _verify_and_review(self, job_id, obj, worktree, base_sha, round_no, test_cmd,
                           test_timeout, allow_paths, evidence, key, kind):
        changed = self.changed_paths(worktree)
        allowed, bad = self.path_allowed(changed, allow_paths)
        evidence.setdefault("path_checks", []).append({"round": round_no, "changed": changed, "outside": bad})
        if not allowed:
            self.record_round(job_id, round_no, kind, note=f"path violation: {bad}")
            return BLOCKED_PATH, None, "", changed, {}, None
        if not changed:
            self.record_round(job_id, round_no, kind, note="no diff")
            return BLOCKED_CODEX, None, "", changed, {}, None

        head_sha = self.commit(worktree, obj["request_id"], f"p02({obj['request_id']}) r{round_no} {kind}")
        if not head_sha:
            return BLOCKED_CODEX, None, "", changed, {}, None

        changed = [p for p in self.git("diff", "--name-only", f"{base_sha}..{head_sha}", cwd=worktree)
                   .stdout.splitlines() if p.strip()]
        diff_text = self.git("diff", f"{base_sha}..{head_sha}", cwd=worktree).stdout
        diff_sha = sha256_text(diff_text)
        artifact = support.write_artifact(self.artifacts / f"{key}.r{round_no}.patch", diff_text)
        evidence["artifacts"][f"diff_r{round_no}"] = {**artifact, "sha256_of_diff_text": diff_sha}

        test_result = self.run_tests(test_cmd, worktree, test_timeout)
        evidence["tests"] = test_result
        evidence["changed_paths"] = changed
        self.update_round(job_id, round_no, kind, head_sha=head_sha, diff_sha=diff_sha,
                          test_exit=test_result.get("exit_code"))
        if test_result["exit_code"] != 0:
            return BLOCKED_TESTS, head_sha, diff_text, changed, test_result, None

        try:
            review_decision = self._route("reviewer", obj, evidence)
        except model_router.RouterError as exc:
            evidence.setdefault("errors", []).append({"stage": "review_route", "error": str(exc)})
            return BLOCKED_MODEL, head_sha, diff_text, changed, test_result, None

        review = reviewer_mod.review(
            worktree, obj, base_sha, head_sha, diff_text, test_result, changed,
            codex_cmd=self.codex_cmd, artifacts_dir=self.state_dir / "reviews" / key,
            model=review_decision.model, effort=review_decision.effort,
            ignore_user_config=self.router.ignore_user_config,
        )
        evidence.setdefault("review_calls", []).append(
            self.router.telemetry(review_decision, review, "reviewer"))
        unchanged, why = reviewer_mod.worktree_unchanged(worktree, head_sha)
        evidence.setdefault("reviews", []).append(
            {"round": round_no, **review.evidence(), "worktree_unchanged": unchanged, "unchanged_reason": why}
        )
        self.record_round(job_id, round_no, "REVIEW", thread_id=review.thread_id,
                          review_status=review.status, note=why or review.reason)
        if not unchanged:
            return BLOCKED_TAMPER, head_sha, diff_text, changed, test_result, review
        if review.status == "REVIEW_INVALID":
            return BLOCKED_REVIEW_INVALID, head_sha, diff_text, changed, test_result, review
        if review.status == "REVIEW_APPROVED":
            return READY, head_sha, diff_text, changed, test_result, review
        return BLOCKED_REVIEW, head_sha, diff_text, changed, test_result, review

    def _build_fix_prompt(self, obj, findings, test_snapshot, head_sha) -> str:
        items = []
        for index, finding in enumerate(findings, 1):
            items.append(
                f"{index}. [{finding['severity']}] {finding['file']}:{finding['line']} "
                f"{finding['description']}\n   证据: {finding['evidence']}\n   建议: {finding['suggested_fix']}"
            )
        if not items and test_snapshot:
            items.append("测试失败输出（节选）：\n" + (test_snapshot.get("stdout_tail", "")[-2000:]))
        return f"""你正在修复 SoloShip P02 的审查/测试问题。当前 candidate SHA：{head_sha}

原始任务（不得扩大范围）：{obj['title']}
目标：{obj['goal']}
允许修改的路径：{', '.join(obj['workspace']['allow_paths'])}

需要修复的具体问题：
{chr(10).join(items) or '测试未通过，请查看测试输出并修复'}

要求：
1. 只修上面列出的问题，不新增功能、不重构无关代码；
2. 不要修改测试来迁就实现；如果测试本身有错，请在最终说明里指出而不是删断言；
3. 修改完运行 `{' '.join(obj['verify']['test_command'])}` 并确保通过；
4. 最后 5 行内说明改了哪些文件、测试结果。
"""

    # ----------------------------------------------------------- finalize
    def _refresh_realtime(self, request_id: str, publish: dict) -> dict:
        """P05-A：任务结束后刷新一次 PR 实时状态（只读）。

        没有 PR、显式关闭、或任何异常都只是跳过——绝不改变任务结论，
        也绝不把"查不到"写成"没变化"（记录里会标 confirmed=false）。
        """
        if not (publish or {}).get("pr_url"):
            return {"skipped": "NO_PR"}
        try:
            config = security.bridge_config(self.root) or {}
            realtime_cfg = config.get("realtime") or {}
            if realtime_cfg.get("after_task", True) is False:
                return {"skipped": "DISABLED"}
            return github_status.refresh_task(
                self.root, request_id, pr_url=publish.get("pr_url"),
                number=publish.get("pr_number"), gh_cmd=self.publish_gh_cmd,
                timeout_s=int(realtime_cfg.get("timeout_seconds", 20)),
                probe_fn=self.realtime_probe,
                historical={"publish_status": publish.get("status")},
            )
        except Exception as exc:  # noqa: BLE001 - 实时查询是旁路
            return {"skipped": "ERROR", "error": f"{type(exc).__name__}: {exc}"}

    def _finalize(self, source: Path, key: str, job_id: str, status: str, evidence: dict,
                  request_id: str, project_id: str, **kwargs) -> str:
        head_sha = kwargs.get("head_sha")
        fix_rounds = kwargs.get("fix_rounds", 0)
        review = kwargs.get("review")
        changed = kwargs.get("changed") or []
        test_result = kwargs.get("test_result") or {}
        diff_text = kwargs.get("diff_text") or ""

        evidence["evidence_status"].setdefault(
            "diff", "OK" if diff_text else "UNAVAILABLE: 未产生 diff"
        )
        evidence["evidence_status"].setdefault(
            "tests", "OK" if test_result else "UNAVAILABLE: 未执行测试"
        )
        evidence["evidence_status"].setdefault(
            "review", "OK" if review else "UNAVAILABLE: 未执行审查"
        )
        if review is not None and review.status == "REVIEW_INVALID":
            evidence["evidence_status"]["review"] = f"INVALID: {review.reason}"

        receipt = {
            "phase": "P02", "status": status, "job_id": job_id,
            "request_id": request_id, "project_id": project_id,
            "base_commit": evidence.get("base_commit"), "head_commit": head_sha,
            "fix_rounds": fix_rounds,
            "review_status": review.status if review else None,
            "blocking_findings": len(review.blocking) if review else 0,
            "tests_ok": test_result.get("exit_code") == 0 if test_result else None,
            "changed_paths": changed,
            "worktree": evidence.get("worktree"),
            "sandbox_status": self.sandbox_status,
            "execution_mode": self.execution_mode,
            "risk_accepted": evidence.get("risk_accepted"),
            "isolation_claim": evidence.get("isolation_claim", "NONE"),
            "test_isolation": self.isolation,
            "routing": evidence.get("routes", []),
            "calls": evidence.get("calls", []) + evidence.get("review_calls", []),
            "publish": evidence.get("publish"),
            "artifacts": evidence["artifacts"],
            "evidence_status": evidence["evidence_status"],
            "report_path": f"reports/{key}.report.md",
            "created_at": evidence.get("started_at"), "finished_at": utc_now(),
        }
        review_payload = self.review_payload(review)
        artifacts = self.build_artifacts(key, evidence, receipt, review)
        # 1) Journal 先落盘：此后任何一步崩溃，都能在重启后重放，不必再跑 Codex。
        journal = self.write_journal(
            job_id=job_id, key=key, request_id=request_id, project_id=project_id,
            status=status, source_name=source.name, evidence=evidence,
            receipt=receipt, artifacts=artifacts, review_payload=review_payload)
        try:
            # 2) DB 进入 FINALIZING（不写 finished_at，避免提前显示完成）
            self.begin_finalizing(job_id)
            # 3) 交付物（文本取自 Journal，保证与恢复结果逐字节一致）
            self._materialize(key, artifacts)
            # 4) 归档 inbox 任务文件（幂等）
            self._archive_source(source)
            # 5) 终端状态：此时报告与归档都已就绪，DB 才敢说完成
            self.finish(job_id, status=status)
        except Exception as exc:  # noqa: BLE001 - 保留 Journal，交恢复流程
            raise FinalizeError(f"{type(exc).__name__}: {exc}") from exc

        # 6) 收尾：Journal 转为「已完成证据清单」（SS-V01-002-R1）
        #    保留而不是删除——完成后报告若丢失，仍能据此逐字节重建，且可反复重放。
        try:
            journal.replace(self.finalized_dir / journal.name)
        except OSError as exc:
            print(f"[{ERROR}] 固化最终化证据失败 {journal.name}: {exc}", flush=True)

        # P05-A：实时查询与索引都是旁路，失败不影响任务结论
        self._refresh_realtime(request_id, evidence.get("publish") or {})
        status_index.refresh(self.root)
        print(f"[{status}] {key} => {job_id}; fixes={fix_rounds}; head={head_sha}", flush=True)
        return status

    def render_report(self, evidence: dict, receipt: dict, review) -> str:
        lines = [
            "# SoloShip P02 执行报告", "",
            f"- Job: `{evidence['job_id']}`",
            f"- Request: `{evidence['request_id']}`",
            f"- Status: `{evidence['status']}`",
            f"- Codex CLI: `{evidence.get('codex_version')}`",
            f"- Sandbox: `{evidence.get('sandbox_mode')}` → `{evidence.get('sandbox_status')}`",
            f"- 执行模式: `{evidence.get('execution_mode')}`（isolation_claim=`{evidence.get('isolation_claim', 'NONE')}`）",
            f"- 风险接受: `{evidence.get('risk_accepted')}`（非沙箱隔离，路径检查仅为事后检测）",
            f"- 测试隔离: `{evidence.get('test_isolation')}`",
            f"- Base SHA: `{evidence.get('base_commit')}`",
            f"- Head SHA: `{evidence.get('head_commit')}`",
            f"- 修复轮次: `{receipt.get('fix_rounds')}`",
            "", "## 证据可用性", "",
        ]
        for key, value in evidence.get("evidence_status", {}).items():
            lines.append(f"- {key}: `{value}`")
        lines += ["", "## 模型路由", ""]
        for route in evidence.get("routes", []):
            lines.append(
                f"- {route['role']}: `{route['tier']}` → `{route['model']}` / `{route['effort']}`"
                f"（{route['source']}，{route['reason']}）"
            )
        lines += ["", "## 调用记录", "",
                  "| 角色 | 请求模型 | 强度 | 实际模型 | 耗时ms | 退出码 | 用量 |",
                  "|---|---|---|---|---|---|---|"]
        for call in evidence.get("calls", []) + evidence.get("review_calls", []):
            usage = call.get("usage") or {}
            lines.append(
                f"| {call.get('role')} | {call.get('requested_model')} | {call.get('requested_effort')} | "
                f"{call.get('actual_model')} | {call.get('duration_ms')} | {call.get('exit_code')} | "
                f"in={usage.get('input_tokens', '-')} out={usage.get('output_tokens', '-')} |"
            )
        lines += ["", "## 产物（含 SHA-256）", ""]
        for name, meta in evidence.get("artifacts", {}).items():
            lines.append(f"- {name}: `{meta['path']}` · {meta['bytes']} bytes · sha256 `{meta['sha256']}`")
        lines += ["", "## 轮次记录", ""]
        for item in evidence.get("rounds", []):
            lines.append(
                f"- round {item['round']} {item['kind']}: exit=`{item.get('exit_code')}` "
                f"thread=`{item.get('thread_id')}` {item.get('duration_ms')}ms "
                f"{'TIMEOUT' if item.get('timed_out') else ''}"
            )
        for item in evidence.get("reviews", []):
            lines.append(
                f"- round {item['round']} REVIEW: status=`{item.get('status')}` "
                f"thread=`{item.get('thread_id')}` sandbox=`{item.get('sandbox', 'read-only')}` "
                f"{item.get('duration_ms')}ms unchanged=`{item.get('worktree_unchanged')}`"
            )
        if evidence.get("codex_events_path") or evidence.get("rounds"):
            lines += ["", "## Codex 事件与最终输出", ""]
            lines.append(f"- 事件文件: 每轮记录于 `.control/p02/` 下对应 `*.last.txt` 与 DB `p02_rounds`")
            lines.append(f"- 最终输出状态: {'OK' if any(r.get('last_message') for r in evidence.get('rounds', [])) else 'EMPTY'}")
        if review is not None:
            lines += ["", "## 审查结论", "",
                      f"- 状态: `{review.status}`",
                      f"- candidate SHA: `{review.candidate_sha}`",
                      f"- 结论: {review.summary or review.reason}",
                      f"- 阻塞项: `{len(review.blocking)}`"]
            for finding in review.findings:
                lines.append(
                    f"  - [{finding['severity']}] `{finding['file']}:{finding['line']}` {finding['description']}"
                )
        tests = evidence.get("tests") if evidence.get("tests") else None
        if tests:
            lines += ["", "## 测试", "", f"- 命令: `{' '.join(tests.get('command', []))}`",
                      f"- 退出码: `{tests.get('exit_code')}`"]
        publish = evidence.get("publish") or {}
        if publish:
            lines += ["", "## 发布", "",
                      f"- 状态: `{publish.get('status')}`",
                      f"- 分支: `{publish.get('branch')}`",
                      f"- PR: `{publish.get('pr_url') or '-'}`",
                      f"- 说明: {publish.get('detail') or '-'}"]
        lines += ["", "## 结论边界", "",
                  "- 本报告只覆盖隔离 worktree 内的实现、测试与审查。"]
        lines.append(self._boundary_publish_line(publish))
        lines.append(
            f"- 沙箱状态 `{evidence.get('sandbox_status')}`：Windows 下未在 OS 层强制，"
            "本次仅靠路径白名单与事后核验约束。"
            if evidence.get("sandbox_status") != support.SANDBOX_ENFORCED
            else "- 沙箱边界探测未发现越界。"
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _boundary_publish_line(publish: dict) -> str:
        """结论边界必须与实际发布状态一致，不能硬编码未发布。"""
        if not publish:
            return "- 本次未产生发布记录：未推送远端、未创建 PR、未合并、未部署。"
        status = publish.get("status")
        branch = publish.get("branch") or "(未知分支)"
        if status in github_publisher.PR_SUCCESS_STATUSES:
            return (f"- 已按授权推送分支 `{branch}` 并创建/复用 Draft PR；"
                    "未合并、未部署、未修改仓库设置。")
        if status == github_publisher.PUSHED:
            return (f"- 已推送分支 `{branch}`，但未创建 PR（`{status}`）；未合并、未部署。")
        return (f"- 发布未完成（`{status}`）：未创建 PR、未合并、未部署。"
                f"详情：{publish.get('detail') or '-'}")

    # ---------------------------------------------------------------- poll
    def cleanup_plan(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT path, request_id, state, note FROM p02_worktrees ORDER BY created_at").fetchall()
        return [{"path": r[0], "request_id": r[1], "state": r[2], "note": r[3]} for r in rows]

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
    parser = argparse.ArgumentParser(description="SoloShip P02 Bridge（实现-审查-修复闭环）")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--selfcheck", action="store_true")
    parser.add_argument("--probe-sandbox", action="store_true", help="实测 Codex 是否能越界读写")
    parser.add_argument("--cleanup-plan", action="store_true", help="列出 worktree 回收计划（不删除）")
    parser.add_argument("--recover", action="store_true",
                        help="扫描最终化 Journal 并与数据库对账（不执行 Codex、不发布 PR）")
    parser.add_argument("--codex-cmd", default=None)
    args = parser.parse_args()

    cmd = [args.codex_cmd] if args.codex_cmd else None
    if args.recover:
        # 恢复只做本地重放：补报告、补归档、推进状态。整条路径不引用 codex 与
        # publisher，所以既不能借它执行开发，也不构成对 SS-V01-001 门禁的绕过。
        bridge = P02Bridge(args.root, codex_cmd=cmd)
        print(json.dumps(bridge.recover_finalizing(), ensure_ascii=False, indent=2), flush=True)
        return
    if args.selfcheck:
        security.guard_or_exit(args.root, "selfcheck")
    elif args.probe_sandbox:
        security.guard_or_exit(args.root, "probe")
    elif args.cleanup_plan:
        security.guard_or_exit(args.root, "cleanup")
    else:
        security.guard_or_exit(args.root, "once" if args.once else "watch")
    bridge = P02Bridge(args.root, codex_cmd=cmd)

    if args.probe_sandbox:
        probe = support.probe_sandbox(cmd, base_dir=bridge.state_dir / "sandbox_probe")
        atomic_text(bridge.state_dir / "sandbox_probe.json", dump_json(probe.as_dict() | {"evidence": probe.evidence}))
        print(json.dumps(probe.as_dict(), ensure_ascii=False, indent=2), flush=True)
        return

    if args.cleanup_plan:
        print(json.dumps(bridge.cleanup_plan(), ensure_ascii=False, indent=2), flush=True)
        return

    if args.selfcheck:
        info = {
            "codex_version": codex.codex_version(cmd),
            "sandbox_status": bridge.sandbox_status,
            "test_isolation": bridge.isolation,
            "allowed_repo": ALLOWED_REPO,
            "max_fix_rounds": MAX_FIX_ROUNDS,
        }
        instr = support.inspect_instructions(bridge.root / ALLOWED_REPO, cmd)
        info["instructions"] = instr
        print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
        return

    print("SoloShip P02 已启动：监测 exchange/p02/inbox/*.ready.json", flush=True)
    print(f"沙箱状态：{bridge.sandbox_status}", flush=True)
    print(f"只允许仓库：{ALLOWED_REPO}；最多自动修复 {MAX_FIX_ROUNDS} 轮", flush=True)
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
