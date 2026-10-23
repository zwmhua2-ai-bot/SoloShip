"""SS-V01-002：P02 最终化一致性与报告恢复的故障注入测试。

覆盖 AC-01..AC-11。全部使用临时目录与 Fake Codex，不执行真实开发任务，
不触碰真实仓库与凭据。
"""
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

import github_publisher  # noqa: E402
import p02_bridge as p02  # noqa: E402

FAKE_CODEX = str(ROOT / "tests" / "fake_codex.py")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "baseline"], check=True, capture_output=True)


def dev_task(request_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "project_id": "demo-app",
        "title": "最终化测试：Feature.remove",
        "goal": "实现 Feature.remove(title)，空白输入返回 False，并补测试",
        "requested_paths": ["src/**"],
        "constraints": ["只改 src/ 与 tests/"],
        "non_goals": ["不改生产仓库"],
        "acceptance": [
            {"id": "AC-01", "description": "remove 删除并返回 True", "verification": "automated"},
        ],
        "workspace": {"repo": "demo/soloship-demo", "base_ref": "HEAD",
                      "allow_paths": ["src", "tests"]},
        "verify": {
            "test_command": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            "timeout_seconds": 120,
            "require_diff": True,
        },
        "codex": {"sandbox": "workspace-write", "timeout_seconds": 600},
    }


class FinalizeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="finalize-"))
        self.root = self.tmp
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "bridge.json").write_text(json.dumps({
            "windows_test_mode": {
                "enabled": True, "risk_accepted": True,
                "approved_projects": ["demo-app"],
                "approved_repos": ["demo/soloship-demo"],
                "approved_absolute_repos": [],
                "isolation_claim": "NONE",
            }
        }), encoding="utf-8")
        init_repo(self.root / "demo" / "soloship-demo")
        self._bridges = []
        # 让 Fake Codex 走「写入 feature.py + 审查通过」的确定性路径
        env = mock.patch.dict(os.environ, {"FAKE_BUILD_MODE": "strict",
                                           "FAKE_REVIEW_MODE": "approve"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def bridge(self) -> p02.P02Bridge:
        bridge = p02.P02Bridge(self.root, codex_cmd=[sys.executable, FAKE_CODEX],
                               sandbox_status="SANDBOX_UNENFORCED")
        self._bridges.append(bridge)
        return bridge

    def drop(self, bridge: p02.P02Bridge, task: dict) -> Path:
        target = bridge.inbox / f"{task['request_id']}.ready.json"
        target.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return target

    @staticmethod
    def key(task: dict) -> str:
        return f"{task['project_id']}__{task['request_id']}"

    def row(self, bridge: p02.P02Bridge, request_id: str):
        with sqlite3.connect(bridge.db_path) as db:
            return db.execute(
                "SELECT job_id, status, finished_at FROM p02_jobs WHERE request_id=?",
                (request_id,)).fetchone()

    def set_status(self, bridge: p02.P02Bridge, job_id: str, status: str,
                   finished: bool = False) -> None:
        with sqlite3.connect(bridge.db_path) as db:
            db.execute("UPDATE p02_jobs SET status=?, finished_at=? WHERE job_id=?",
                       (status, p02.utc_now() if finished else None, job_id))

    def crash_mid_finalize(self, request_id: str):
        """在交付物写入阶段崩溃：Journal 已写、DB=FINALIZING、无报告。"""
        bridge = self.bridge()
        bridge._materialize = mock.Mock(side_effect=OSError("注入：交付物写入中断"))
        task = dev_task(request_id)
        status = bridge.process_one(self.drop(bridge, task))

        self.assertEqual(status, p02.FINALIZE_ERROR)
        job_id, row_status, finished = self.row(bridge, request_id)
        self.assertEqual(row_status, p02.FINALIZING)
        self.assertIsNone(finished)
        journal = bridge._journal_path(job_id)
        self.assertTrue(journal.is_file(), "崩溃后必须保留 Journal")
        return bridge, task, job_id, journal

    def crash_after_artifacts(self, request_id: str):
        """交付物写完、归档前崩溃：报告齐全、源文件仍在 inbox、DB=FINALIZING。"""
        bridge = self.bridge()
        bridge._archive_source = mock.Mock(side_effect=OSError("注入：归档中断"))
        task = dev_task(request_id)
        status = bridge.process_one(self.drop(bridge, task))

        self.assertEqual(status, p02.FINALIZE_ERROR)
        job_id, row_status, finished = self.row(bridge, request_id)
        self.assertEqual(row_status, p02.FINALIZING)
        self.assertIsNone(finished)
        self.assertTrue((bridge.reports / f"{self.key(task)}.report.md").is_file())
        self.assertTrue((bridge.inbox / f"{task['request_id']}.ready.json").is_file())
        return bridge, task, job_id


class FinalizeTests(FinalizeBase):
    # AC-01 ---------------------------------------------------------------
    def test_ac01_normal_run_produces_complete_evidence(self):
        bridge = self.bridge()
        task = dev_task("fin_normal_0001")
        status = bridge.process_one(self.drop(bridge, task))

        self.assertIn(status, p02.TERMINAL_STATUSES, "正常任务应给出终态结论")
        key = self.key(task)
        for name in (f"{key}.receipt.json", f"{key}.report.json", f"{key}.report.md"):
            self.assertTrue((bridge.receipts / name).is_file() or (bridge.reports / name).is_file(),
                            f"缺少交付物 {name}")
        job_id, row_status, finished = self.row(bridge, task["request_id"])
        self.assertEqual(row_status, status)
        self.assertIsNotNone(finished, "正常完成后必须写 finished_at")
        self.assertFalse(bridge._journal_path(job_id).exists(), "正常完成后 Journal 应清理")
        self.assertEqual([p.name for p in bridge.finalizing_dir.glob("*.json")], [])

    # AC-02 ---------------------------------------------------------------
    def test_ac02_resume_after_crash_in_finalize(self):
        bridge, task, job_id, journal = self.crash_mid_finalize("fin_crash_0001")

        fresh = self.bridge()
        summary = fresh.recover_finalizing()

        self.assertEqual(len(summary["recovered"]), 1)
        self.assertEqual(summary["needs_human"], [])
        key = self.key(task)
        self.assertTrue((fresh.receipts / f"{key}.receipt.json").is_file())
        self.assertTrue((fresh.reports / f"{key}.report.json").is_file())
        self.assertTrue((fresh.reports / f"{key}.report.md").is_file())
        _, row_status, finished = self.row(fresh, task["request_id"])
        self.assertEqual(row_status, p02.READY)
        self.assertIsNotNone(finished)
        self.assertFalse(journal.exists(), "恢复完成后 Journal 必须清理")

    # AC-03 ---------------------------------------------------------------
    def test_ac03_db_done_but_archive_pending(self):
        bridge, task, job_id = self.crash_after_artifacts("fin_archive_0001")
        # 模拟「DB 已完成、归档未完成」：把状态推进为终态，源文件仍在 inbox
        self.set_status(bridge, job_id, p02.READY, finished=True)

        fresh = self.bridge()
        summary = fresh.recover_finalizing()

        self.assertEqual(len(summary["recovered"]), 1)
        self.assertFalse((fresh.inbox / f"{task['request_id']}.ready.json").exists(),
                         "恢复后源文件应已归档")
        self.assertTrue(list(fresh.archive.iterdir()), "归档目录应有文件")
        self.assertEqual(self.row(fresh, task["request_id"])[1], p02.READY)
        self.assertEqual([p.name for p in fresh.finalizing_dir.glob("*.json")], [])

    # AC-04 ---------------------------------------------------------------
    def test_ac04_regenerate_missing_report_from_journal(self):
        bridge, task, job_id = self.crash_after_artifacts("fin_regen_0001")
        report = bridge.reports / f"{self.key(task)}.report.md"
        expected = report.read_text(encoding="utf-8")
        report.unlink()

        fresh = self.bridge()
        summary = fresh.recover_finalizing()

        self.assertEqual(len(summary["recovered"]), 1)
        self.assertTrue(report.is_file(), "缺失报告应据 Journal 重新生成")
        self.assertEqual(report.read_text(encoding="utf-8"), expected, "重生成内容必须与 Journal 一致")

    # AC-05 ---------------------------------------------------------------
    def test_ac05_recovery_does_not_call_codex(self):
        bridge, task, job_id, journal = self.crash_mid_finalize("fin_nocodex_0001")
        marker = self.tmp / "codex_called.txt"

        with mock.patch.dict(os.environ, {"FAKE_ENV_DUMP": str(marker)}):
            fresh = self.bridge()
            summary = fresh.recover_finalizing()

        self.assertEqual(len(summary["recovered"]), 1)
        self.assertFalse(marker.exists(), "恢复流程不得调用 Codex")

    # AC-06 ---------------------------------------------------------------
    def test_ac06_recovery_does_not_publish_pr(self):
        bridge, task, job_id, journal = self.crash_mid_finalize("fin_nopr_0001")
        original = json.loads(journal.read_text(encoding="utf-8"))["evidence"].get("publish")
        with mock.patch.object(github_publisher.Publisher, "publish",
                               side_effect=AssertionError("恢复流程不得发布 PR")):
            fresh = self.bridge()
            summary = fresh.recover_finalizing()

        self.assertEqual(len(summary["recovered"]), 1)
        pub = json.loads((fresh.reports / f"{self.key(task)}.report.json")
                         .read_text(encoding="utf-8")).get("publish")
        self.assertEqual(pub, original, "发布结果必须与崩溃前一致，不得新增发布")
        self.assertIsNone((pub or {}).get("pr_url"))
        self.assertIsNone((pub or {}).get("pr_number"))

    # AC-07 ---------------------------------------------------------------
    def test_ac07_repeated_recovery_is_idempotent(self):
        bridge, task, job_id, journal = self.crash_mid_finalize("fin_idem_0001")
        fresh = self.bridge()
        first = fresh.recover_finalizing()

        key = self.key(task)
        snapshot = {p.name: p.read_bytes() for p in fresh.reports.glob(f"{key}*")}
        second = fresh.recover_finalizing()
        after = {p.name: p.read_bytes() for p in fresh.reports.glob(f"{key}*")}

        self.assertEqual(len(first["recovered"]), 1)
        self.assertEqual(second["scanned"], 0, "第二次扫描不应再有 Journal")
        self.assertEqual(second["recovered"], [])
        self.assertEqual(second["needs_human"], [])
        self.assertEqual(snapshot, after, "重复恢复不得改变交付物")
        self.assertFalse((fresh.receipts / f"{key}.duplicate.json").exists())

    # AC-08 ---------------------------------------------------------------
    def test_ac08_missing_evidence_goes_needs_human(self):
        bridge, task, job_id, journal = self.crash_mid_finalize("fin_human_0001")
        data = json.loads(journal.read_text(encoding="utf-8"))
        data.pop("payload_sha256", None)
        data.pop("evidence", None)          # 证据缺失，但 Journal 校验和重算以绕过完整性检查
        data["payload_sha256"] = p02.sha256_text(
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        journal.write_text(p02.dump_json(data), encoding="utf-8")

        fresh = self.bridge()
        summary = fresh.recover_finalizing()

        self.assertEqual(summary["recovered"], [])
        self.assertEqual(len(summary["needs_human"]), 1)
        self.assertIn("MISSING_EVIDENCE", summary["needs_human"][0]["reason"])
        self.assertEqual(self.row(fresh, task["request_id"])[1], p02.NEEDS_HUMAN)
        self.assertTrue((fresh.finalizing_dir / f"{job_id}.needs_human.json").is_file(),
                        "必须留下人工处理标记")
        self.assertTrue(journal.is_file(), "证据不足时不得删除 Journal")

    # AC-09 ---------------------------------------------------------------
    def test_ac09_lock_conflict_maps_to_busy(self):
        bridge = self.bridge()
        task = dev_task("fin_busy_0001")
        source = self.drop(bridge, task)
        (bridge.locks / f"{task['request_id']}.lock").write_text("999", encoding="utf-8")

        self.assertEqual(bridge.process_one(source), p02.BUSY)

    def test_ac09_runtime_error_is_not_reported_as_busy(self):
        bridge = self.bridge()
        source = self.drop(bridge, dev_task("fin_runtime_0001"))
        with mock.patch.object(bridge, "_run_job", side_effect=RuntimeError("模拟执行期异常")):
            with self.assertRaises(RuntimeError):
                bridge.process_one(source)

    # AC-10 ---------------------------------------------------------------
    def test_ac10_journal_without_db_finalizing(self):
        bridge, task, job_id, journal = self.crash_mid_finalize("fin_nodbfin_0001")
        # 模拟「Journal 已写入、DB 尚未进入 FINALIZING」：回退成 ACCEPTED
        self.set_status(bridge, job_id, "ACCEPTED", finished=False)

        fresh = self.bridge()
        summary = fresh.recover_finalizing()

        self.assertEqual(len(summary["recovered"]), 1)
        self.assertEqual(summary["recovered"][0]["from"], "ACCEPTED")
        _, row_status, finished = self.row(fresh, task["request_id"])
        self.assertEqual(row_status, p02.READY)
        self.assertIsNotNone(finished)
        self.assertTrue((fresh.reports / f"{self.key(task)}.report.md").is_file())

    # AC-11 ---------------------------------------------------------------
    def test_ac11_conflicting_report_is_not_overwritten(self):
        bridge, task, job_id = self.crash_after_artifacts("fin_conflict_0001")
        report = bridge.reports / f"{self.key(task)}.report.md"
        conflict = "# 人工改写的报告，不应被恢复流程覆盖\n"
        report.write_text(conflict, encoding="utf-8")

        fresh = self.bridge()
        summary = fresh.recover_finalizing()

        self.assertEqual(summary["recovered"], [])
        self.assertEqual(len(summary["needs_human"]), 1)
        self.assertIn("ARTIFACT_CONFLICT", summary["needs_human"][0]["reason"])
        self.assertEqual(report.read_text(encoding="utf-8"), conflict, "冲突文件不得被覆盖")
        self.assertEqual(self.row(fresh, task["request_id"])[1], p02.NEEDS_HUMAN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
