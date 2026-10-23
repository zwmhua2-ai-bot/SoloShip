#!/usr/bin/env python3
"""SS-V01-006（P05-C）：Supervisor 中断恢复与安全重调度回归。

覆盖 AC-01..AC-14。全部使用临时根、Fake Codex 与 Mock Publisher，
**不触碰真实 Supervisor、不执行真实 GitHub 写操作**。
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "tests"))

import p02_bridge as p02  # noqa: E402
import supervisor as sup  # noqa: E402
import policy_fixture as fx  # noqa: E402

FAKE_CODEX = str(ROOT / "tests" / "fake_codex.py")
PREFLIGHT_OK = {
    "codex": {"found": True, "cmd": [sys.executable, FAKE_CODEX], "version": "codex-cli 0.0.0-fake"},
    "codex_home": {"present": True},
    "login": {"logged_in": True},
    "env": {"kept_for_codex": ["CODEX_HOME"], "gh_token_in_codex_child": False},
    "github": {"enabled": False, "publish_gate": "ok"},
    "sandbox": {"execution_mode": "WINDOWS_TEST_MODE", "sandbox_status": "SANDBOX_BLOCKED"},
    "blocking": [], "degraded": [], "ok": True, "codex_ready": True,
}


def sh(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check,
                          encoding="utf-8", errors="replace")


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p05c-"))
        self.root = self.tmp
        (self.tmp / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "config" / "bridge.json", self.tmp / "config" / "bridge.json")
        (self.tmp / "exchange" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.tmp / "exchange" / "p02" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.tmp / ".control").mkdir(parents=True, exist_ok=True)
        self.repo = self.tmp / "demo" / "soloship-demo"
        self.repo.mkdir(parents=True)
        sh(["git", "-C", str(self.repo), "init", "-q"])
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "tests" / "test_app.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n", encoding="utf-8")
        sh(["git", "-C", str(self.repo), "add", "-A"])
        sh(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-q", "-m", "base"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def supervisor(self, **kwargs):
        kwargs.setdefault("codex_cmd", [sys.executable, FAKE_CODEX])
        kwargs.setdefault("preflight_info", dict(PREFLIGHT_OK))
        return sup.Supervisor(self.root, **kwargs)

    def bridge(self):
        return p02.P02Bridge(self.root, codex_cmd=[sys.executable, FAKE_CODEX],
                             sandbox_status="SANDBOX_UNENFORCED")

    def drop_p02(self, request_id="p02_rec_0001"):
        obj = {
            "schema_version": "1.0", "request_id": request_id, "project_id": "demo-app",
            "title": "恢复测试任务", "goal": "验证中断恢复",
            "requested_paths": ["src/**"], "constraints": [], "non_goals": [],
            "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
            "workspace": {"repo": "demo/soloship-demo", "base_ref": "HEAD",
                          "allow_paths": ["src", "tests"]},
            "verify": {"test_command": [sys.executable, "-m", "unittest", "discover",
                                        "-s", "tests", "-v"], "timeout_seconds": 120},
            "codex": {"sandbox": "workspace-write", "timeout_seconds": 120},
        }
        path = self.root / "exchange" / "p02" / "inbox" / f"{request_id}.ready.json"
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return path

    def stale_claim(self, kind="p02", task_id="p02_rec_0001", pid=999999, state="running"):
        path = sup.claims_dir(self.root) / f"{kind}__{task_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"kind": kind, "task_id": task_id, "pid": pid,
                                    "state": state, "at": "2026-10-23T10:00:00+00:00"}),
                        encoding="utf-8")
        return path

    def insert_job(self, job_id, request_id, status="ACCEPTED"):
        db = self.root / ".control" / "p02" / "p02.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE IF NOT EXISTS p02_jobs (
            job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
            spec_sha TEXT NOT NULL, status TEXT NOT NULL, repo TEXT, worktree TEXT,
            base_commit TEXT, head_commit TEXT, fix_rounds INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, finished_at TEXT, UNIQUE (project_id, request_id))""")
        conn.execute("INSERT OR REPLACE INTO p02_jobs (job_id, project_id, request_id, spec_sha,"
                     " status, created_at) VALUES (?,?,?,?,?,?)",
                     (job_id, "demo-app", request_id, "x", status, "2026-10-23T10:00:00+00:00"))
        conn.commit()
        conn.close()
        return job_id

    def run_p02_to_ready(self, request_id="p02_rec_done"):
        """用 Fake Codex 跑完一个任务，返回 (job_id, receipt)。"""
        self.drop_p02(request_id)
        with mock.patch.dict(os.environ, {"FAKE_BUILD_MODE": "strict", "FAKE_REVIEW_MODE": "approve"}):
            self.bridge().poll()
        receipt = json.loads((self.root / "exchange" / "p02" / "receipts"
                              / f"demo-app__{request_id}.receipt.json").read_text(encoding="utf-8"))
        return receipt["job_id"], receipt

    def needs_human_files(self):
        return sorted(sup.needs_human_dir(self.root).glob("*.json"))


class StartupConfirmationTests(RecoveryFixture):
    """AC-01 / AC-02：启动必须确认真实存活，重复启动保持单实例。"""

    def _engine_with_lock(self):
        """启动确认的前提是单实例锁被持有：测试里显式持锁。"""
        lock = sup.SingleInstanceLock(self.root)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        engine = self.supervisor()
        engine.heartbeat()
        return engine

    def test_ac01_ready_requires_lock_pid_and_fresh_heartbeat(self):
        self._engine_with_lock()
        ok, why = sup.startup_ready(self.root, os.getpid())
        self.assertTrue(ok, why)

    def test_ac01_stale_heartbeat_is_not_ready(self):
        self._engine_with_lock()
        with mock.patch.object(sup.time, "time", return_value=sup.time.time() + 3600):
            ok, why = sup.startup_ready(self.root, os.getpid())
        self.assertFalse(ok)
        self.assertIn("心跳", why)

    def test_ac01_pid_mismatch_is_not_ready(self):
        self._engine_with_lock()
        ok, why = sup.startup_ready(self.root, 999999)
        self.assertFalse(ok)
        self.assertIn("pid", why.lower())

    def test_ac02_duplicate_start_keeps_single_instance(self):
        with sup.SingleInstanceLock(self.root):
            code = sup.cmd_start(argparse.Namespace(root=str(self.root)))
        self.assertEqual(code, sup.EXIT_ALREADY_RUNNING)


class ClassificationTests(RecoveryFixture):
    """AC-03/04/05/06/07/08/10/13：按证据分类，不盲目重跑。"""

    def candidate(self, kind="p02", task_id="p02_rec_0001"):
        return {"kind": kind, "task_id": task_id,
                "path": str(self.root / "exchange" / "p02" / "inbox" / f"{task_id}.ready.json"),
                "mtime": 0}

    def test_ac04_live_holder_is_active(self):
        self.stale_claim(pid=os.getpid())
        verdict = self.supervisor().classify_interrupted(self.candidate())
        self.assertEqual(verdict["category"], "ACTIVE")
        self.assertIn("存活", verdict["reason"])

    def test_ac10_no_side_effects_is_safe_to_resume(self):
        self.drop_p02()
        self.stale_claim()
        verdict = self.supervisor().classify_interrupted(self.candidate())
        self.assertEqual(verdict["category"], "SAFE_TO_RESUME")
        self.assertIn("副作用", verdict["reason"])

    def test_ac07_job_without_journal_is_needs_human(self):
        self.insert_job("p02_job_x", "p02_rec_0001")
        self.stale_claim()
        verdict = self.supervisor().classify_interrupted(self.candidate())
        self.assertEqual(verdict["category"], "NEEDS_HUMAN")
        self.assertIn("证据", verdict["reason"])

    def test_ac08_unconfirmable_publish_is_needs_human(self):
        self.insert_job("p02_job_pub", "p02_rec_0001", status="P02_READY_FOR_HUMAN")
        publish = self.root / ".control" / "p03" / "publish"
        publish.mkdir(parents=True, exist_ok=True)
        (publish / "p02_job_pub.json").write_text(json.dumps(
            {"status": "PUBLISH_PUSHED", "stage": "pr_creating"}), encoding="utf-8")
        self.stale_claim()
        verdict = self.supervisor().classify_interrupted(self.candidate())
        self.assertEqual(verdict["category"], "NEEDS_HUMAN")
        self.assertIn("发布", verdict["reason"])

    def test_ac13_stale_worktree_is_needs_human_and_not_deleted(self):
        self.insert_job("p02_job_wt", "p02_rec_0001")
        worktree = self.root / ".worktrees" / "p02_rec_0001"
        worktree.mkdir(parents=True)
        (worktree / "junk.txt").write_text("not a git worktree\n", encoding="utf-8")
        self.stale_claim()
        verdict = self.supervisor().classify_interrupted(self.candidate())
        self.assertEqual(verdict["category"], "NEEDS_HUMAN")
        self.assertIn("worktree", verdict["reason"].lower())
        self.assertTrue(worktree.is_dir(), "失效 worktree 不得被删除")

    def test_ac05_finalizing_journal_is_safe_to_resume(self):
        job_id, _receipt = self.run_p02_to_ready("p02_rec_fin")
        # 把已完成证据清单搬回 finalizing/ 并删掉报告：模拟"最终化中途崩溃"
        manifest = self.root / ".control" / "p02" / "finalized" / f"{job_id}.json"
        self.assertTrue(manifest.is_file())
        target = self.root / ".control" / "p02" / "finalizing" / f"{job_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(manifest), str(target))
        report = self.root / "exchange" / "p02" / "reports" / f"demo-app__p02_rec_fin.report.md"
        report.unlink()
        self.drop_p02("p02_rec_fin")
        self.stale_claim(task_id="p02_rec_fin")
        verdict = self.supervisor().classify_interrupted(self.candidate(task_id="p02_rec_fin"))
        self.assertEqual(verdict["category"], "SAFE_TO_RESUME")
        self.assertIn("Journal", verdict["reason"])

    def test_ac06_missing_report_restored_from_manifest(self):
        job_id, _receipt = self.run_p02_to_ready("p02_rec_rep")
        report = self.root / "exchange" / "p02" / "reports" / f"demo-app__p02_rec_rep.report.md"
        report.unlink()
        self.assertFalse(report.is_file())
        summary = self.bridge().recover_finalizing()
        self.assertTrue(report.is_file(), "已完成任务的报告应从证据清单恢复")
        self.assertIn(job_id, json.dumps(summary, ensure_ascii=False))


class RecoverOnStartTests(RecoveryFixture):
    """AC-03/09/10/11/12：启动恢复的调度、互斥与幂等。"""

    def candidate(self, kind="p02", task_id="p02_rec_0001"):
        return {"kind": kind, "task_id": task_id,
                "path": str(self.root / "exchange" / "p02" / "inbox" / f"{task_id}.ready.json"),
                "mtime": 0}

    def test_ac10_safe_task_is_requeued_and_then_processed(self):
        self.drop_p02()
        self.stale_claim()
        engine = self.supervisor()
        summary = engine.recover_on_start()
        item = [i for i in summary["items"] if i["task_id"] == "p02_rec_0001"][0]
        self.assertEqual(item["category"], "SAFE_TO_RESUME")
        self.assertEqual(item["action"], "requeued")
        inbox = self.root / "exchange" / "p02" / "inbox" / "p02_rec_0001.ready.json"
        self.assertTrue(inbox.is_file(), "安全任务应回到 inbox 等待重调度")
        claim = json.loads((sup.claims_dir(self.root) / "p02__p02_rec_0001.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(claim["state"], "requeued")
        self.assertTrue(claim.get("history"), "原认领记录必须保留为证据")

        with mock.patch.dict(os.environ, {"FAKE_BUILD_MODE": "strict",
                                          "FAKE_REVIEW_MODE": "approve"}):
            result = engine.tick()["executed"]
        self.assertEqual(result["status"], p02.READY)
        receipt = json.loads((self.root / "exchange" / "p02" / "receipts"
                              / "demo-app__p02_rec_0001.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], p02.READY)

    def test_ac03_needs_human_task_is_parked_with_evidence(self):
        self.insert_job("p02_job_nh", "p02_rec_0001")
        self.drop_p02()
        self.stale_claim()
        engine = self.supervisor()
        with mock.patch("codex_adapter.run_codex") as run_codex:
            summary = engine.recover_on_start()
        run_codex.assert_not_called()
        item = [i for i in summary["items"] if i["task_id"] == "p02_rec_0001"][0]
        self.assertEqual(item["category"], "NEEDS_HUMAN")
        self.assertEqual(item["action"], "needs_human")
        files = self.needs_human_files()
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["task_id"], "p02_rec_0001")
        self.assertIn("reason", payload)
        self.assertIn("claim", payload["evidence"])
        # 任务文件保留在 inbox（未被执行、未归档、未删除）
        self.assertTrue((self.root / "exchange" / "p02" / "inbox"
                         / "p02_rec_0001.ready.json").is_file())

    def test_ac09_security_gate_deferred_task_is_not_requeued(self):
        self.drop_p02()
        deferred = sup.deferred_dir(self.root) / "p02__p02_rec_0001.ready.json"
        deferred.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.root / "exchange" / "p02" / "inbox" / "p02_rec_0001.ready.json"),
                    str(deferred))
        engine = self.supervisor()
        summary = engine.recover_on_start()
        self.assertIn("p02_rec_0001", [i["task_id"] for i in summary["deferred_parked"]])
        self.assertFalse((self.root / "exchange" / "p02" / "inbox"
                          / "p02_rec_0001.ready.json").exists(),
                         "门禁阻断的任务不得自动重新入队")
        self.assertTrue(deferred.is_file())

    def test_ac11_double_recovery_is_idempotent(self):
        self.drop_p02()
        self.stale_claim()
        engine = self.supervisor()
        first = engine.recover_on_start()
        second = engine.recover_on_start()
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(second["items"], [], "第二次恢复不应重复处理同一认领")
        inbox = list((self.root / "exchange" / "p02" / "inbox").glob("*.ready.json"))
        self.assertEqual(len(inbox), 1, "不得重复入队")
        self.assertEqual(len(self.needs_human_files()), 0)

    def test_ac12_concurrent_recovery_single_winner(self):
        self.drop_p02()
        self.stale_claim()
        lock = sup.recovery_lock_path(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        # 另一个"恢复进程"持有锁且仍存活
        lock.write_text(json.dumps({"pid": 999999, "at": "2026-10-24T00:00:00+00:00"}),
                        encoding="utf-8")
        claim_before = (sup.claims_dir(self.root) / "p02__p02_rec_0001.json").read_text(
            encoding="utf-8")
        engine = self.supervisor()
        with mock.patch.object(sup, "pid_alive", side_effect=lambda p: p == 999999):
            summary = engine.recover_on_start()
        self.assertEqual(summary["lock"], "busy")
        self.assertTrue(all(item["action"] == "skipped" for item in summary["items"]),
                        summary["items"])
        inbox_file = self.root / "exchange" / "p02" / "inbox" / "p02_rec_0001.ready.json"
        self.assertTrue(inbox_file.is_file(), "未持锁不得移动或删除任务文件")
        self.assertEqual(len(self.needs_human_files()), 0, "未持锁不得写入人工处理记录")
        self.assertEqual((sup.claims_dir(self.root) / "p02__p02_rec_0001.json")
                         .read_text(encoding="utf-8"), claim_before,
                         "未持锁不得改写认领证据")

    def test_ac12_stale_lock_holder_is_taken_over(self):
        self.drop_p02()
        self.stale_claim()
        lock = sup.recovery_lock_path(self.root)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": 999999, "at": "2026-10-23T10:00:00+00:00"}),
                        encoding="utf-8")
        summary = self.supervisor().recover_on_start()
        self.assertNotEqual(summary["lock"], "busy")
        self.assertEqual(len(summary["items"]), 1)

    def test_requeued_claim_can_be_reclaimed(self):
        self.drop_p02()
        self.stale_claim()
        engine = self.supervisor()
        engine.recover_on_start()
        cand = {"kind": "p02", "task_id": "p02_rec_0001",
                "path": str(self.root / "exchange" / "p02" / "inbox" / "p02_rec_0001.ready.json"),
                "mtime": 0}
        self.assertEqual(engine.claim(cand)[0], "acquired")
        claim = json.loads((sup.claims_dir(self.root) / "p02__p02_rec_0001.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(claim["state"], "running")
        self.assertTrue(claim.get("history"))

    def test_needs_human_claim_refuses_execution(self):
        self.drop_p02()
        self.stale_claim()
        engine = self.supervisor()
        engine.recover_on_start()
        claim_path = sup.claims_dir(self.root) / "p02__p02_rec_0001.json"
        claim_path.write_text(json.dumps({"kind": "p02", "task_id": "p02_rec_0001",
                                          "pid": os.getpid(), "state": "needs_human",
                                          "at": "2026-10-24T00:00:00+00:00"}), encoding="utf-8")
        cand = {"kind": "p02", "task_id": "p02_rec_0001",
                "path": str(self.root / "exchange" / "p02" / "inbox" / "p02_rec_0001.ready.json"),
                "mtime": 0}
        state, detail = engine.claim(cand)
        self.assertEqual(state, "needs_human")
        self.assertIn("人工", detail)


class NeedsHumanViewTests(RecoveryFixture):
    """AC-14：只读查看，不能伪造成功。"""

    def test_ac14_recovery_command_is_read_only(self):
        engine = self.supervisor()
        engine.heartbeat()
        target = sup.needs_human_dir(self.root) / "p02__p02_rec_0001.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"kind": "p02", "task_id": "p02_rec_0001", "job_id": "p02_job_nh",
                   "reason": "Builder 可能已执行但缺少完整证据",
                   "evidence": {"claim": "claims/p02__p02_rec_0001.json"},
                   "at": "2026-10-24T00:00:00+00:00"}
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        before = target.read_text(encoding="utf-8")

        code = sup.cmd_recovery(argparse.Namespace(root=str(self.root)))
        self.assertEqual(code, sup.EXIT_OK)
        self.assertEqual(target.read_text(encoding="utf-8"), before, "查看不得改写证据")

        status = sup.build_status(self.root)
        self.assertIn("recovery", status)
        self.assertEqual(status["recovery"]["needs_human"], 1)

        source = (ROOT / "bridge" / "supervisor.py").read_text(encoding="utf-8")
        self.assertNotIn("force_success", source)
        self.assertNotIn("mark_success", source)


if __name__ == "__main__":
    unittest.main()
