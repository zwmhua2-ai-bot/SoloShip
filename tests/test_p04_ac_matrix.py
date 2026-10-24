#!/usr/bin/env python3
"""SS-V01-AC-001：逐条 AC 验收证据矩阵。

覆盖 AC-01..AC-09（AC-10/AC-11 由既有 P02/P03/P04 用例与全量回归覆盖）。
全部使用隔离临时根，不执行真实 GitHub 写操作。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))

import acceptance as acc  # noqa: E402

DEV_REPO = "D:\\SoloShip\\repos\\soloship-demo"
DIFF = """diff --git a/src/todo.py b/src/todo.py
--- a/src/todo.py
+++ b/src/todo.py
@@ -1,3 +1,8 @@
+def remove(self, title):
+    pass
diff --git a/tests/test_todo.py b/tests/test_todo.py
--- a/tests/test_todo.py
+++ b/tests/test_todo.py
@@ -1,2 +1,20 @@
+    def test_remove_returns_true(self):
+        todos = TodoList()
+        todos.add("a")
+        self.assertIs(todos.remove("a"), True)
+    def test_count_after_remove(self):
+        todos = TodoList()
+        todos.add("a")
+        todos.remove("a")
+        self.assertEqual(todos.count(), 0)
+"""

STDERR_TAIL = """test_remove_returns_true (tests.test_todo.TestTodo) ... ok
test_count_after_remove (tests.test_todo.TestTodo) ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.001s

OK
"""

FAILED_TAIL = """test_remove_returns_true (tests.test_todo.TestTodo) ... FAIL

----------------------------------------------------------------------
Ran 2 tests in 0.001s

FAILED (failures=1)
"""

ACCEPTANCE = [
    {"id": "AC-01", "description": "remove 命中时返回 True 并删除该项",
     "verification": "automated"},
    {"id": "AC-02", "description": "list_all 与 count 同步反映删除",
     "verification": "automated"},
]


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class MatrixBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_json(self.root / "config" / "bridge.json", {
            "windows_test_mode": {"approved_absolute_repos": [DEV_REPO]}
        })
        self.task_id = "task_dev_001"
        self.candidate = "b" * 40
        self.patch = self.root / "exchange" / "p02" / "reports" / "artifacts" / f"{self.task_id}.r0.patch"
        self.patch.parent.mkdir(parents=True, exist_ok=True)
        self.patch.write_text(DIFF, encoding="utf-8")
        self.report_rel = f"exchange/p02/reports/demo-app__{self.task_id}.report.json"
        self.make_task()

    def tearDown(self):
        self._tmp.cleanup()

    def make_task(self, acceptance=None) -> dict:
        task = {
            "schema_version": "1.0", "request_id": self.task_id, "project_id": "demo-app",
            "title": "TodoList 增加 remove()", "goal": "实现 remove",
            "requested_paths": ["src/**", "tests/**"],
            "constraints": ["不引入新依赖"], "non_goals": ["不支持批量删除"],
            "acceptance": acceptance if acceptance is not None else ACCEPTANCE,
            "workspace": {"repo": DEV_REPO, "base_branch": "main"},
            "verify": {"commands": [["py", "-3", "-m", "unittest"]]},
            "codex": {"timeout_s": 600},
        }
        write_json(self.root / "exchange" / "p03" / f"{self.task_id}.task.json", task)
        return task

    def add_p02(self, task_id=None, *, status="P02_READY_FOR_HUMAN", tests_exit=0,
                review="REVIEW_APPROVED", blocking=0, review_sha=None,
                stderr_tail=STDERR_TAIL, head_commit=None, publish=None):
        task_id = task_id or self.task_id
        job_id = f"p02_{task_id}"
        head = head_commit or self.candidate
        receipt = {
            "phase": "P02", "status": status, "job_id": job_id, "request_id": task_id,
            "project_id": "demo-app", "base_commit": "a" * 40, "head_commit": head,
            "fix_rounds": 1, "review_status": review, "blocking_findings": blocking,
            "tests_ok": tests_exit == 0,
            "created_at": "2026-10-23T12:00:00+00:00",
            "finished_at": "2026-10-23T12:05:00+00:00",
        }
        write_json(self.root / "exchange" / "p02" / "receipts"
                   / f"demo-app__{task_id}.receipt.json", receipt)
        review_entry = {"status": review}
        if review_sha is not None:
            review_entry["candidate_sha"] = review_sha
        write_json(self.root / "exchange" / "p02" / "reports" / f"demo-app__{task_id}.report.json", {
            "phase": "P02", "status": status, "job_id": job_id, "request_id": task_id,
            "project_id": "demo-app", "title": "TodoList 增加 remove()", "goal": "实现 remove",
            "repo": DEV_REPO, "sandbox_status": "SANDBOX_UNENFORCED", "isolation_claim": "NONE",
            "tests": {"command": ["py", "-3", "-m", "unittest"], "exit_code": tests_exit,
                      "stderr_tail": stderr_tail, "stdout_tail": ""},
            "reviews": [review_entry],
            "calls": [{"actual_model": "UNVERIFIED", "role": "explorer"}],
            "artifacts": {"diff_r0": {"path": str(self.patch)}},
        })
        if publish:
            publish = dict(publish)
            publish.setdefault("job_id", job_id)
            write_json(self.root / ".control" / "p03" / "publish" / f"{job_id}.json", publish)
        return receipt

    def build(self):
        return acc.build(self.root)


class MatrixContentTests(MatrixBase):
    """AC-01/02/03/05/06/07/08：矩阵字段与保守语义。"""

    def test_ac01_every_ac_has_matrix_record(self):
        self.add_p02()
        data = self.build()
        self.assertEqual(len(data["acceptance"]), len(ACCEPTANCE))
        for ac in data["acceptance"]:
            for key in ("ac_id", "description", "verification", "candidate_sha",
                        "coverage_status", "test_evidence", "review_evidence",
                        "verified", "missing_evidence", "human_action"):
                self.assertIn(key, ac, f"{ac.get('ac_id')} 缺字段 {key}")
        self.assertEqual([ac["ac_id"] for ac in data["acceptance"]],
                         [item["id"] for item in ACCEPTANCE],
                         "AC 必须来自原始任务，不得增删")

    def test_ac02_test_evidence_references_real_run(self):
        self.add_p02()
        data = self.build()
        evidence = data["acceptance"][0]["test_evidence"]
        self.assertTrue(evidence["exit_code"] == 0)
        self.assertIn("test_remove_returns_true", evidence["test_names"])
        self.assertEqual(evidence["report_path"], self.report_rel)
        self.assertTrue(data["candidate_sha"] == self.candidate)

    def test_ac03_failed_tests_never_verify(self):
        self.add_p02(tests_exit=1, stderr_tail=FAILED_TAIL)
        data = self.build()
        for ac in data["acceptance"]:
            self.assertFalse(ac["verified"])
            self.assertEqual(ac["coverage_status"], acc.AC_UNVERIFIED)
            self.assertTrue(any("测试" in item for item in ac["missing_evidence"]))

    def test_ac05_name_only_is_not_coverage(self):
        """测试名命中但 diff 中没有断言 → 只能是线索，不是覆盖。"""
        no_assert = DIFF.replace("self.assertIs(todos.remove(\"a\"), True)", "pass")
        no_assert = no_assert.replace("self.assertEqual(todos.count(), 0)", "pass")
        self.patch.write_text(no_assert, encoding="utf-8")
        self.add_p02()
        data = self.build()
        statuses = {ac["ac_id"]: ac["coverage_status"] for ac in data["acceptance"]}
        self.assertEqual(statuses["AC-01"], acc.AC_NAME_ONLY)
        for ac in data["acceptance"]:
            self.assertFalse(ac["verified"])

    def test_ac06_missing_evidence_is_explicit(self):
        # 有回执但没有报告：测试与审查证据都缺失
        write_json(self.root / "exchange" / "p02" / "receipts"
                   / f"demo-app__{self.task_id}.receipt.json", {
            "phase": "P02", "status": "P02_READY_FOR_HUMAN", "job_id": "p02_x",
            "request_id": self.task_id, "project_id": "demo-app",
            "base_commit": "a" * 40, "head_commit": self.candidate, "fix_rounds": 0,
            "review_status": "REVIEW_APPROVED", "blocking_findings": 0, "tests_ok": True,
            "created_at": "2026-10-23T12:00:00+00:00",
            "finished_at": "2026-10-23T12:05:00+00:00"})
        data = self.build()
        for ac in data["acceptance"]:
            self.assertFalse(ac["verified"])
            self.assertTrue(ac["missing_evidence"], "缺证据必须显式列出")
        self.assertTrue(any(("报告" in risk) or ("未绑定" in risk) or ("缺失" in risk)
                            for risk in data["risks"]), data["risks"])

    def test_ac07_manual_ac_stays_manual(self):
        acceptance = ACCEPTANCE + [
            {"id": "AC-03", "description": "界面操作符合直觉", "verification": "manual"}]
        self.make_task(acceptance)
        self.add_p02()
        data = self.build()
        manual = [ac for ac in data["acceptance"] if ac["ac_id"] == "AC-03"][0]
        self.assertEqual(manual["coverage_status"], acc.AC_MANUAL)
        self.assertFalse(manual["verified"])
        self.assertTrue(manual["human_action"])

    def test_ac08_report_cites_candidate_and_paths(self):
        self.add_p02(review_sha=self.candidate)
        data = self.build()
        self.assertEqual(data["candidate_sha"], self.candidate)
        self.assertEqual(data["report_path"], self.report_rel)
        self.assertEqual(data["evidence_matrix"]["candidate_sha"], self.candidate)
        self.assertTrue(data["evidence_matrix"]["report_path"])


class ReviewBindingTests(MatrixBase):
    """AC-04：Reviewer 必须绑定同一 Candidate SHA，否则不采信。"""

    def test_ac04_review_sha_mismatch_is_not_trusted(self):
        self.add_p02(review_sha="c" * 40)
        data = self.build()
        self.assertFalse(data["review_binding"]["bound"])
        self.assertIn("不一致", data["review_binding"]["reason"])
        self.assertTrue(any("candidate SHA" in risk for risk in data["risks"]))
        for ac in data["acceptance"]:
            self.assertFalse(ac["review_evidence"]["bound"])
            self.assertFalse(ac["verified"])

    def test_ac04_review_without_sha_is_not_trusted(self):
        self.add_p02()
        data = self.build()
        self.assertFalse(data["review_binding"]["bound"])
        self.assertIn("未回报", data["review_binding"]["reason"])

    def test_ac04_bound_review_is_recorded(self):
        self.add_p02(review_sha=self.candidate)
        data = self.build()
        self.assertTrue(data["review_binding"]["bound"])
        for ac in data["acceptance"]:
            self.assertTrue(ac["review_evidence"]["bound"])
            self.assertEqual(ac["review_evidence"]["candidate_sha"], self.candidate)


class IdempotencyTests(MatrixBase):
    """AC-09：重复生成结果一致，且不改写原始运行证据。"""

    def test_ac09_repeated_build_is_stable(self):
        self.add_p02(review_sha=self.candidate)
        first = self.build()
        report_before = (self.root / self.report_rel).read_text(encoding="utf-8")
        second = self.build()
        self.assertEqual(first["acceptance"], second["acceptance"])
        self.assertEqual(first["evidence_matrix"], second["evidence_matrix"])
        self.assertEqual(first["risks"], second["risks"])
        self.assertEqual((self.root / self.report_rel).read_text(encoding="utf-8"),
                         report_before, "不得改写原始运行证据")
        self.assertFalse(first["auto_merge"])
        self.assertFalse(first["acceptance_verified"])


class RenderTests(MatrixBase):
    def test_markdown_contains_matrix_section(self):
        self.add_p02(review_sha=self.candidate)
        text = acc.render_markdown(self.build())
        self.assertIn("证据矩阵", text)
        self.assertIn("AC-01", text)
        self.assertIn(self.candidate[:12], text)


if __name__ == "__main__":
    unittest.main()
