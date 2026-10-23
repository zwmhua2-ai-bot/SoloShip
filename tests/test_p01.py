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

import codex_adapter as codex  # noqa: E402
import p01_bridge as bridge_mod  # noqa: E402

FAKE_CODEX = str(ROOT / "tests" / "fake_codex.py")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "src" / "todo.py").write_text(
        '"""demo"""\n\n\nclass TodoList:\n    def __init__(self):\n        self._items = []\n',
        encoding="utf-8",
    )
    (repo / "tests" / "test_todo.py").write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "-m", "baseline"],
        check=True, capture_output=True,
    )


def make_task(request_id: str = "p01_test_0001", **overrides) -> dict:
    task = {
        "schema_version": "1.0",
        "request_id": request_id,
        "project_id": "demo-app",
        "title": "P01 测试任务",
        "goal": "验证 Bridge 能驱动 Codex 并独立核验结果",
        "requested_paths": ["src/**"],
        "constraints": ["只改 src/ 与 tests/"],
        "non_goals": ["不改生产仓库"],
        "acceptance": [
            {"id": "AC-01", "description": "产生可核验 diff", "verification": "automated"},
        ],
        "workspace": {"repo": "repo", "base_ref": "HEAD", "allow_paths": ["src", "tests"]},
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


class P01BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p01test-"))
        self.repo = self.tmp / "repo"
        init_repo(self.repo)
        # 显式授权测试环境：统一安全门禁要求 WINDOWS_TEST_MODE 必须
        # enabled + risk_accepted 才放行。这里只为让用例运行在已授权模式下，
        # 不修改生产配置，也不等于任何形式的自动豁免。
        (self.tmp / "config").mkdir(parents=True, exist_ok=True)
        (self.tmp / "config" / "bridge.json").write_text(json.dumps({
            "windows_test_mode": {
                "enabled": True, "risk_accepted": True,
                "approved_projects": ["demo-app"],
                "approved_repos": ["repo"],
                "approved_absolute_repos": [],
                "isolation_claim": "NONE",
            }
        }), encoding="utf-8")
        self.bridge = bridge_mod.P01Bridge(self.tmp, codex_cmd=[sys.executable, FAKE_CODEX])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def submit(self, task: dict) -> dict:
        self.bridge.inbox.mkdir(parents=True, exist_ok=True)
        target = self.bridge.inbox / f"{task['request_id']}.ready.json"
        target.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        self.bridge.poll()
        receipt = self.bridge.receipts / f"{task['project_id']}__{task['request_id']}.receipt.json"
        self.assertTrue(receipt.is_file(), f"缺少回执: {receipt}")
        return json.loads(receipt.read_text(encoding="utf-8"))

    def run_with(self, mode: str, task: dict) -> dict:
        with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": mode}):
            return self.submit(task)

    # -- happy path --------------------------------------------------------
    def test_real_diff_and_tests_pass(self):
        receipt = self.run_with("edit", make_task())
        self.assertEqual(receipt["status"], "P01_CODEX_OK")
        self.assertTrue(receipt["tests_ok"])
        self.assertIn("src/greeting.py", receipt["changed_paths"])
        self.assertTrue(receipt["head_commit"])
        self.assertTrue(receipt["codex_thread_id"])

        report = self.bridge.reports / "demo-app__p01_test_0001.report.md"
        text = report.read_text(encoding="utf-8")
        self.assertIn("P01_CODEX_OK", text)
        self.assertIn("src/greeting.py", text)
        self.assertIn("+def greet", text)  # real patch, not model prose

        with sqlite3.connect(self.bridge.db_path) as db:
            row = db.execute(
                "SELECT status, tests_ok, thread_id FROM p01_jobs WHERE request_id='p01_test_0001'"
            ).fetchone()
        self.assertEqual(row[0], "P01_CODEX_OK")
        self.assertEqual(row[1], 1)
        self.assertTrue(row[2])

    def test_duplicate_is_idempotent(self):
        task = make_task("p01_test_dup")
        first = self.run_with("edit", task)
        second = self.run_with("edit", task)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["status"], "P01_DUPLICATE")
        with sqlite3.connect(self.bridge.db_path) as db:
            count = db.execute("SELECT COUNT(*) FROM p01_jobs").fetchone()[0]
        self.assertEqual(count, 1)

    # -- failure matrix ----------------------------------------------------
    def test_cli_missing(self):
        self.bridge.codex_cmd = ["definitely-not-a-codex-binary"]
        receipt = self.submit(make_task("p01_test_cli"))
        self.assertEqual(receipt["status"], "P01_CLI_MISSING")

    def test_auth_failed(self):
        receipt = self.run_with("auth", make_task("p01_test_auth"))
        self.assertEqual(receipt["status"], "P01_AUTH_FAILED")

    def test_nonzero_exit(self):
        receipt = self.run_with("fail", make_task("p01_test_fail"))
        self.assertEqual(receipt["status"], "P01_CODEX_FAILED")
        self.assertEqual(receipt["codex_exit_code"], 3)

    def test_timeout(self):
        run = codex.run_codex(
            "ping",
            self.tmp / "repo",
            timeout_s=2,
            codex_cmd=[sys.executable, FAKE_CODEX],
            extra_env={"FAKE_CODEX_MODE": "timeout"},
        )
        self.assertTrue(run.timed_out)
        self.assertEqual(run.error_kind, codex.TIMEOUT)
        self.assertFalse(run.ok)

    def test_tests_failed(self):
        receipt = self.run_with("breaktest", make_task("p01_test_red"))
        self.assertEqual(receipt["status"], "P01_TESTS_FAILED")
        self.assertFalse(receipt["tests_ok"])

    def test_no_diff(self):
        receipt = self.run_with("nochange", make_task("p01_test_nodiff"))
        self.assertEqual(receipt["status"], "P01_CODEX_NO_DIFF")
        self.assertEqual(receipt["changed_paths"], [])

    def test_path_violation(self):
        receipt = self.run_with("outside", make_task("p01_test_escape"))
        self.assertEqual(receipt["status"], "P01_PATH_VIOLATION")
        self.assertIn("README.md", receipt["changed_paths"])

    # -- task contract -----------------------------------------------------
    def test_dangerous_sandbox_rejected(self):
        task = make_task("p01_test_danger", codex={"sandbox": "danger-full-access"})
        self.bridge.inbox.mkdir(parents=True, exist_ok=True)
        (self.bridge.inbox / f"{task['request_id']}.ready.json").write_text(
            json.dumps(task, ensure_ascii=False), encoding="utf-8"
        )
        self.bridge.poll()
        reject = self.bridge.receipts / "p01_test_danger.ready.json.error.json"
        self.assertTrue(reject.is_file())
        self.assertIn("sandbox", reject.read_text(encoding="utf-8"))

    def test_repo_outside_root_rejected(self):
        task = make_task("p01_test_escape_repo", workspace={"repo": "C:/Windows"})
        errors = bridge_mod.validate_p01_task(task, self.tmp)
        self.assertTrue(any("根目录内" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
