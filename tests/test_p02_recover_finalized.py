"""SS-V01-002-R1：已完成任务在 Journal 清理后，报告丢失能否恢复。

先进测试后改代码：本文件第一步用于证明缺陷是否存在。
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
        "title": "R1 恢复测试：Feature.remove",
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


class FinalizedRecoveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="r1-"))
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
        env = mock.patch.dict(os.environ, {"FAKE_BUILD_MODE": "strict",
                                           "FAKE_REVIEW_MODE": "approve"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def bridge(self) -> p02.P02Bridge:
        return p02.P02Bridge(self.root, codex_cmd=[sys.executable, FAKE_CODEX],
                             sandbox_status="SANDBOX_UNENFORCED")

    def run_normal(self, bridge: p02.P02Bridge, request_id: str):
        task = dev_task(request_id)
        source = bridge.inbox / f"{request_id}.ready.json"
        source.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        status = bridge.process_one(source)
        self.assertEqual(status, p02.READY)
        with sqlite3.connect(bridge.db_path) as db:
            row = db.execute("SELECT job_id, status, finished_at FROM p02_jobs"
                             " WHERE request_id=?", (request_id,)).fetchone()
        self.assertEqual(row[1], p02.READY)
        self.assertIsNotNone(row[2], "正常完成后必须有 finished_at")
        return task, row[0]

    @staticmethod
    def key(task: dict) -> str:
        return f"{task['project_id']}__{task['request_id']}"


class FinalizedRecoveryTests(FinalizedRecoveryBase):
    def test_r1_report_recoverable_after_journal_cleanup(self):
        """AC-R1：正常完成 → 终态 → Journal 已清理 → 删除 report.json → 重启恢复。"""
        bridge = self.bridge()
        task, job_id = self.run_normal(bridge, "r1_normal_0001")
        key = self.key(task)

        # 1) Journal 已按正常流程清理
        self.assertEqual([p.name for p in bridge.finalizing_dir.glob("*.json")], [],
                         "正常完成后 FINALIZING Journal 应已清理")

        # 2) 删除 report.json，模拟交付证据丢失
        report_json = bridge.reports / f"{key}.report.json"
        before = report_json.read_bytes()
        report_json.unlink()

        # 3) 模拟进程重启：全新 Bridge 实例
        fresh = self.bridge()
        summary = fresh.recover_finalizing()

        # 4) AC-R1/R2：报告必须能从可信持久化证据恢复，且内容一致
        self.assertTrue(report_json.is_file(), "report.json 应可从可信证据恢复")
        self.assertEqual(report_json.read_bytes(), before, "恢复内容必须与原报告逐字节一致")
        self.assertEqual(summary["needs_human"], [], "存在可信证据时不应进入人工处理")

    def test_r1_report_md_also_recoverable(self):
        bridge = self.bridge()
        task, job_id = self.run_normal(bridge, "r1_md_0001")
        key = self.key(task)
        report_md = bridge.reports / f"{key}.report.md"
        before = report_md.read_bytes()
        report_md.unlink()

        fresh = self.bridge()
        fresh.recover_finalizing()

        self.assertTrue(report_md.is_file(), "report.md 也应可恢复")
        self.assertEqual(report_md.read_bytes(), before, "markdown 报告内容必须一致")

    def test_r3_repeat_recovery_is_idempotent(self):
        bridge = self.bridge()
        task, job_id = self.run_normal(bridge, "r1_idem_0001")
        key = self.key(task)
        report_json = bridge.reports / f"{key}.report.json"
        report_json.unlink()

        marker = self.tmp / "codex_called.txt"
        with mock.patch.dict(os.environ, {"FAKE_ENV_DUMP": str(marker)}):
            fresh = self.bridge()
            fresh.recover_finalizing()
            first = report_json.read_bytes()
            summary = fresh.recover_finalizing()
            second = report_json.read_bytes()

        self.assertEqual(first, second, "重复恢复不得改变内容")
        self.assertFalse(marker.exists(), "恢复不得调用 Codex")
        self.assertEqual(summary["recovered"], [])
        self.assertEqual(summary["needs_human"], [])
        self.assertEqual(sorted(p.name for p in fresh.reports.glob(f"{key}*")),
                         sorted([f"{key}.report.json", f"{key}.report.md"]))

    def test_r4_no_manifest_goes_needs_human(self):
        """AC-R4：历史任务没有足够证据时，明确人工处理，不伪造报告。"""
        bridge = self.bridge()
        task, job_id = self.run_normal(bridge, "r1_hist_0001")
        key = self.key(task)
        # 模拟历史任务：删除全部可重放证据 + 报告
        for p in list(bridge.reports.glob(f"{key}*")) + list(bridge.finalizing_dir.glob("*")):
            p.unlink()
        for dirname in ("finalized",):
            for p in (bridge.state_dir / dirname).glob(f"{job_id}*"):
                p.unlink()

        fresh = self.bridge()
        summary = fresh.recover_finalizing()

        self.assertFalse((bridge.reports / f"{key}.report.md").exists(), "不得伪造报告")
        reasons = " ".join(item.get("reason", "") for item in summary["missing_reports"])
        self.assertIn("REPORT_MISSING_NO_EVIDENCE", reasons)
        self.assertTrue(list(fresh.state_dir.glob("finalizing/*.needs_human.json")),
                        "应留下人工处理标记")


if __name__ == "__main__":
    unittest.main(verbosity=2)
