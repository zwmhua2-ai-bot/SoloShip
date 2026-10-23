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


def make_task(request_id: str = "p02_test_0001", **overrides) -> dict:
    task = {
        "schema_version": "1.0",
        "request_id": request_id,
        "project_id": "demo-app",
        "title": "P02 测试任务：Feature.remove",
        "goal": "实现 Feature.remove(title)，空白输入返回 False，并补测试",
        "requested_paths": ["src/**"],
        "constraints": ["只改 src/ 与 tests/"],
        "non_goals": ["不改生产仓库"],
        "acceptance": [
            {"id": "AC-01", "description": "remove 删除并返回 True", "verification": "automated"},
            {"id": "AC-03", "description": "空白输入返回 False", "verification": "automated"},
        ],
        "workspace": {"repo": "demo/soloship-demo", "base_ref": "HEAD", "allow_paths": ["src", "tests"]},
        "verify": {
            "test_command": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            "timeout_seconds": 120,
            "require_diff": True,
        },
        "codex": {"sandbox": "workspace-write", "timeout_seconds": 600},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(task.get(key), dict):
            task[key].update(value)
        else:
            task[key] = value
    return task


class P02BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p02test-"))
        self.repo = self.tmp / "demo" / "soloship-demo"
        init_repo(self.repo)
        self.bridge = p02.P02Bridge(
            self.tmp, codex_cmd=[sys.executable, FAKE_CODEX],
            sandbox_status="SANDBOX_UNENFORCED",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def submit(self, task: dict, build: str = "strict", review: str = "auto") -> dict:
        env = {"FAKE_BUILD_MODE": build, "FAKE_REVIEW_MODE": review}
        with mock.patch.dict(os.environ, env):
            self.bridge.inbox.mkdir(parents=True, exist_ok=True)
            target = self.bridge.inbox / f"{task['request_id']}.ready.json"
            target.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
            self.bridge.poll()
        receipt = self.bridge.receipts / f"{task['project_id']}__{task['request_id']}.receipt.json"
        self.assertTrue(receipt.is_file(), "缺少回执")
        return json.loads(receipt.read_text(encoding="utf-8"))

    # 1. 首次开发后审查通过，零轮修复
    def test_zero_fix_rounds(self):
        receipt = self.submit(make_task("p02_ok_0001"))
        self.assertEqual(receipt["status"], p02.READY)
        self.assertEqual(receipt["fix_rounds"], 0)
        self.assertEqual(receipt["review_status"], "REVIEW_APPROVED")
        self.assertTrue(receipt["tests_ok"])
        self.assertIn("src/feature.py", receipt["changed_paths"])
        artifact = receipt["artifacts"]["diff_r0"]
        self.assertEqual(len(artifact["sha256"]), 64)
        report = (self.bridge.reports / "demo-app__p02_ok_0001.report.md").read_text(encoding="utf-8")
        self.assertIn("P02_READY_FOR_HUMAN", report)
        self.assertIn("sha256", report)

    # 2. Reviewer 发现缺陷，修复一次后通过
    def test_one_fix_round(self):
        receipt = self.submit(make_task("p02_fix1"), build="buggy_once")
        self.assertEqual(receipt["status"], p02.READY)
        self.assertEqual(receipt["fix_rounds"], 1)
        with sqlite3.connect(self.bridge.db_path) as db:
            kinds = [r[0] for r in db.execute(
                "SELECT kind FROM p02_rounds WHERE job_id=? ORDER BY id", (receipt["job_id"],))]
        self.assertEqual(kinds, ["IMPLEMENT", "REVIEW", "FIX", "REVIEW"])

    # 3. 连续两轮修复仍失败，系统停止
    def test_two_fix_rounds_then_blocked(self):
        receipt = self.submit(make_task("p02_fix2"), build="buggy_forever", review="changes")
        self.assertEqual(receipt["status"], p02.BLOCKED_LIMIT)
        self.assertEqual(receipt["fix_rounds"], 2)
        with sqlite3.connect(self.bridge.db_path) as db:
            fixes = db.execute("SELECT COUNT(*) FROM p02_rounds WHERE job_id=? AND kind='FIX'",
                               (receipt["job_id"],)).fetchone()[0]
        self.assertEqual(fixes, 2)

    # 4. Reviewer 返回无效 JSON，系统拒绝通过
    def test_invalid_review_json(self):
        receipt = self.submit(make_task("p02_invalid"), review="invalid")
        self.assertEqual(receipt["status"], p02.BLOCKED_REVIEW_INVALID)
        self.assertEqual(receipt["fix_rounds"], 0)
        self.assertIn("INVALID", receipt["evidence_status"]["review"])

    # 5. Reviewer 意外修改代码，系统发现并阻断
    def test_reviewer_tampering_blocked(self):
        receipt = self.submit(make_task("p02_tamper"), review="tamper")
        self.assertEqual(receipt["status"], p02.BLOCKED_TAMPER)

    # 6. 修复越过允许路径，系统拒收
    def test_fix_outside_allowlist(self):
        receipt = self.submit(make_task("p02_escape"), build="outside")
        self.assertEqual(receipt["status"], p02.BLOCKED_PATH)

    # 7. 代码 SHA 改变后，旧审查结论失效（candidate_sha 不一致）
    def test_stale_candidate_sha(self):
        receipt = self.submit(make_task("p02_stale"), review="stale")
        self.assertEqual(receipt["status"], p02.BLOCKED_REVIEW_INVALID)
        self.assertEqual(receipt["fix_rounds"], 0)
        self.assertIn("INVALID", receipt["evidence_status"]["review"])

    # 8. 重复提交不导致并发重复修改
    def test_duplicate_submission(self):
        task = make_task("p02_dup_0001")
        first = self.submit(task)
        second = self.submit(task)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["status"], p02.DUPLICATE)
        with sqlite3.connect(self.bridge.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM p02_jobs").fetchone()[0], 1)

    # 附加：非 demo 仓库直接拒收（沙箱未生效时的硬闸门）
    def test_repo_gate(self):
        other = self.tmp / "demo" / "other"
        init_repo(other)
        task = make_task("p02_repo", workspace={"repo": "demo/other"})
        self.bridge.inbox.mkdir(parents=True, exist_ok=True)
        (self.bridge.inbox / "p02_repo.ready.json").write_text(
            json.dumps(task, ensure_ascii=False), encoding="utf-8")
        self.bridge.poll()
        error = self.bridge.receipts / "p02_repo.ready.json.error.json"
        self.assertTrue(error.is_file())
        self.assertIn("未授权仓库", error.read_text(encoding="utf-8"))

    # 附加：worktree 登记与回收计划（不自动删除）
    def test_worktree_registry(self):
        receipt = self.submit(make_task("p02_registry"))
        plan = self.bridge.cleanup_plan()
        entry = [item for item in plan if item["request_id"] == "p02_registry"][0]
        self.assertEqual(entry["state"], "READY_FOR_CLEANUP")
        self.assertTrue((self.bridge.worktrees / "p02_registry").exists())
        self.assertEqual(receipt["status"], p02.READY)

    # 附加：沙箱未生效必须在回执里显式标记
    def test_sandbox_status_reported(self):
        receipt = self.submit(make_task("p02_sandbox"))
        self.assertEqual(receipt["sandbox_status"], "SANDBOX_UNENFORCED")
        self.assertEqual(receipt["test_isolation"], "TEST_ISOLATION_PARTIAL")
        self.assertIn("UNAVAILABLE_ENFORCEMENT", receipt["evidence_status"]["sandbox"])


if __name__ == "__main__":
    unittest.main()
