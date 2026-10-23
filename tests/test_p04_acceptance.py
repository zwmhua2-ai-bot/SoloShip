"""P04-C：AI 验收助手的行为约束。

核心约束：
  * 只依据 exchange/ 下的真实文件，不推测；核对不了的验收项标 unverified；
  * 永不自动合并（auto_merge 恒为 False）；
  * 修复任务草案只在确有失败/阻塞时生成，且不覆盖已存在的任务文件。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
+    def test_list_all_after_remove(self):
+        todos = TodoList()
+        todos.add("a")
+        todos.remove("a")
+        self.assertEqual(todos.list_all(), [])
+    def test_count_after_remove(self):
+        todos = TodoList()
+        todos.add("a")
+        todos.remove("a")
+        self.assertEqual(todos.count(), 0)
+    def test_add_returns_true(self):
+        self.assertIs(TodoList().add("a"), True)
+"""

STDERR_TAIL = """test_add_returns_true (tests.test_todo.TestTodo) ... ok
test_list_all_after_remove (tests.test_todo.TestTodo) ... ok
test_count_after_remove (tests.test_todo.TestTodo) ... ok
test_remove_missing_returns_false (tests.test_todo.TestTodo) ... ok

----------------------------------------------------------------------
Ran 8 tests in 0.001s

OK
"""

ACCEPTANCE = [
    {"id": "AC-01", "description": "remove 命中时返回 True 并删除该项"},
    {"id": "AC-02", "description": "list_all 与 count 同步反映删除"},
    {"id": "AC-03", "description": "空标题不应删除任何内容"},
]


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class AcceptanceBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write_json(self.root / "config" / "bridge.json", {
            "windows_test_mode": {"approved_absolute_repos": [DEV_REPO]}
        })
        self.task_id = "task_dev_001"
        self.patch = self.root / "exchange" / "p02" / "reports" / "artifacts" / f"{self.task_id}.r0.patch"
        self.patch.parent.mkdir(parents=True, exist_ok=True)
        self.patch.write_text(DIFF, encoding="utf-8")
        self.task_file = self.make_task()

    def tearDown(self):
        self._tmp.cleanup()

    def make_task(self) -> dict:
        task = {
            "schema_version": "1.0", "request_id": self.task_id, "project_id": "demo-app",
            "title": "TodoList 增加 remove()", "goal": "实现 remove",
            "requested_paths": ["src/**", "tests/**"],
            "constraints": ["不引入新依赖"],
            "non_goals": ["不支持批量删除"],
            "acceptance": ACCEPTANCE,
            "workspace": {"repo": DEV_REPO, "base_branch": "main"},
            "verify": {"commands": [["py", "-3", "-m", "unittest"]]},
            "codex": {"timeout_s": 600},
        }
        write_json(self.root / "exchange" / "p03" / f"{self.task_id}.task.json", task)
        return task

    def add_p02(self, task_id=None, *, status="P02_READY_FOR_HUMAN", repo=DEV_REPO,
                tests_exit=0, review="REVIEW_APPROVED", blocking=0,
                publish=None, stderr_tail=STDERR_TAIL,
                finished_at="2026-10-23T12:05:00+00:00"):
        task_id = task_id or self.task_id
        job_id = f"p02_{task_id}"
        receipt = {
            "phase": "P02", "status": status, "job_id": job_id, "request_id": task_id,
            "project_id": "demo-app", "base_commit": "a" * 40, "head_commit": "b" * 40,
            "fix_rounds": 1, "review_status": review, "blocking_findings": blocking,
            "tests_ok": tests_exit == 0,
            "created_at": "2026-10-23T12:00:00+00:00", "finished_at": finished_at,
        }
        write_json(self.root / "exchange" / "p02" / "receipts"
                   / f"demo-app__{task_id}.receipt.json", receipt)
        write_json(self.root / "exchange" / "p02" / "reports" / f"demo-app__{task_id}.report.json", {
            "phase": "P02", "status": status, "job_id": job_id, "request_id": task_id,
            "project_id": "demo-app", "title": "TodoList 增加 remove()", "goal": "实现 remove",
            "repo": repo, "sandbox_status": "SANDBOX_UNENFORCED", "isolation_claim": "NONE",
            "tests": {"command": ["py", "-3", "-m", "unittest"], "exit_code": tests_exit,
                      "stderr_tail": stderr_tail, "stdout_tail": ""},
            "reviews": [{"status": review}],
            "calls": [{"actual_model": "UNVERIFIED", "role": "explorer"}],
            "artifacts": {"diff_r0": {"path": str(self.patch)}},
        })
        if publish:
            publish = dict(publish)
            publish.setdefault("job_id", job_id)
            write_json(self.root / ".control" / "p03" / "publish" / f"{job_id}.json", publish)
        return receipt


class TestBuild(AcceptanceBase):
    def test_build_latest_task(self):
        self.add_p02()

        data = acc.build(self.root)

        self.assertTrue(data["ok"])
        self.assertEqual(data["task_id"], self.task_id)
        self.assertEqual(data["state"], "waiting_human_review")
        self.assertEqual(data["changed_files"], ["src/todo.py", "tests/test_todo.py"])
        self.assertEqual(data["tests"]["exit_code"], 0)
        self.assertEqual(data["tests"]["passed"], 8)
        self.assertTrue(data["tests"]["ok"])
        self.assertFalse(data["auto_merge"])
        self.assertEqual(data["review"]["status"], "REVIEW_APPROVED")

    def test_build_by_explicit_id(self):
        self.add_p02("task_other", finished_at="2026-10-23T12:30:00+00:00")
        self.add_p02(self.task_id, finished_at="2026-10-23T12:05:00+00:00")

        latest = acc.build(self.root)
        explicit = acc.build(self.root, "task_dev_001")

        self.assertEqual(latest["task_id"], "task_other")
        self.assertEqual(explicit["task_id"], self.task_id)

    def test_unknown_id_reports_error(self):
        self.add_p02()

        data = acc.build(self.root, "no_such_task")

        self.assertFalse(data["ok"])
        self.assertIn("no_such_task", data["error"])

    def test_no_development_task_reports_error(self):
        write_json(self.root / "exchange" / "receipts" / "handoff__h1.receipt.json", {
            "phase": "P00", "status": "ACCEPTED", "job_id": "p00_h1",
            "request_id": "h1", "project_id": "handoff",
            "finished_at": "2026-10-23T12:00:00+00:00",
        })
        acc.status_index.build(self.root)

        data = acc.build(self.root)

        self.assertFalse(data["ok"])
        self.assertIn("开发任务", data["error"])


class TestAcceptanceReview(AcceptanceBase):
    def test_assertion_seen_in_diff_is_still_not_verified(self):
        self.add_p02()

        acs = {item["id"]: item for item in acc.build(self.root)["acceptance"]}

        self.assertEqual(acs["AC-01"]["status"], "assertion_seen_in_diff")
        self.assertEqual(acs["AC-01"]["missing_tokens"], [])
        self.assertGreater(acs["AC-01"]["assertions_in_diff"], 0)
        # 关键：自动判定不得宣称"已验证"
        self.assertFalse(acs["AC-01"]["verified"])
        self.assertEqual(acs["AC-01"]["verdict"], "needs_human_review")
        self.assertIn("仍需人工判定", acs["AC-01"]["basis"])
        self.assertEqual(acs["AC-02"]["status"], "assertion_seen_in_diff")
        self.assertEqual(set(acs["AC-02"]["matched_tokens"]), {"list_all", "count"})

    def test_summary_never_claims_acceptance_verified(self):
        self.add_p02()

        data = acc.build(self.root)

        self.assertFalse(data["acceptance_verified"])
        self.assertIn("需人工判定", data["acceptance_note"])
        self.assertTrue(all(item["verified"] is False for item in data["acceptance"]))

    def test_partial_match_is_not_reported_as_covered(self):
        self.add_p02(stderr_tail="""test_list_all_after_remove (tests.test_todo.TestTodo) ... ok

----------------------------------------------------------------------
Ran 8 tests in 0.001s

OK
""")

        acs = {item["id"]: item for item in acc.build(self.root)["acceptance"]}
        data = acc.build(self.root)
        risks = " ".join(data["risks"])

        self.assertEqual(acs["AC-02"]["status"], "partially_covered")
        self.assertEqual(acs["AC-02"]["matched_tokens"], ["list_all"])
        self.assertEqual(acs["AC-02"]["missing_tokens"], ["count"])
        self.assertIn("AC-02", risks)
        self.assertIn("部分覆盖", risks)

    def test_unmatched_item_is_unverified(self):
        self.add_p02()

        acs = {item["id"]: item for item in acc.build(self.root)["acceptance"]}

        self.assertEqual(acs["AC-03"]["status"], "unverified")
        self.assertEqual(acs["AC-03"]["matched_tokens"], [])
        self.assertIn("无法据此核对", acs["AC-03"]["basis"])

    def test_failing_tests_cannot_cover_anything(self):
        self.add_p02(tests_exit=1)

        data = acc.build(self.root)

        self.assertTrue(all(item["status"] == "unverified" for item in data["acceptance"]))
        self.assertTrue(any("AC-01" in risk for risk in data["risks"]))

    def test_missing_test_output_marks_all_unverified(self):
        self.add_p02(stderr_tail="")

        data = acc.build(self.root)

        self.assertTrue(all(item["status"] == "unverified" for item in data["acceptance"]))
        self.assertIsNone(data["tests"]["passed"])


class TestRisks(AcceptanceBase):
    def test_sandbox_and_draft_risks(self):
        self.add_p02(publish={"status": "PUBLISH_PR_CREATED",
                              "pr_url": "https://github.com/x/y/pull/4"})

        risks = " ".join(acc.build(self.root)["risks"])

        self.assertIn("SANDBOX_UNENFORCED", risks)
        self.assertIn("Draft", risks)
        self.assertIn("模型", risks)

    def test_missing_review_is_flagged(self):
        self.add_p02(review=None, blocking=0)

        risks = " ".join(acc.build(self.root)["risks"])

        self.assertIn("Review", risks)

    def test_no_pr_is_flagged(self):
        self.add_p02()

        risks = " ".join(acc.build(self.root)["risks"])

        self.assertIn("本地分支", risks)


class TestDraftFix(AcceptanceBase):
    def test_no_draft_when_clean(self):
        self.add_p02()

        self.assertIsNone(acc.build(self.root)["draft_fix"])

    def test_draft_generated_on_blocking_findings(self):
        self.add_p02(review="REVIEW_CHANGES_REQUESTED", blocking=2)

        draft = acc.build(self.root)["draft_fix"]

        self.assertIsNotNone(draft)
        self.assertEqual(draft["request_id"], f"{self.task_id}_fix1")
        self.assertEqual(draft["project_id"], "demo-app")
        self.assertEqual(draft["workspace"], self.task_file["workspace"])
        self.assertEqual(draft["acceptance"], self.task_file["acceptance"])
        self.assertEqual(draft["draft_of"], self.task_id)
        self.assertEqual(draft["generated_by"], "p04-acceptance-assistant")
        self.assertIn("Review 阻塞项 2 条", draft["goal"])

    def test_draft_generated_on_test_failure(self):
        self.add_p02(tests_exit=1)

        draft = acc.build(self.root)["draft_fix"]

        self.assertIn("测试未通过", draft["goal"])

    def test_draft_generated_on_blocked_state(self):
        self.add_p02(status="P02_BLOCKED_AUTH", tests_exit=None, review=None)

        data = acc.build(self.root)

        self.assertEqual(data["state"], "blocked")
        self.assertIsNotNone(data["draft_fix"])

    def test_draft_does_not_overwrite_existing_name(self):
        self.add_p02(status="P02_BLOCKED_AUTH", tests_exit=None, review=None)
        write_json(self.root / "exchange" / "p03" / f"{self.task_id}_fix1.task.json",
                   {"request_id": f"{self.task_id}_fix1", "keep": True})

        draft = acc.build(self.root)["draft_fix"]

        self.assertEqual(draft["request_id"], f"{self.task_id}_fix2")
        kept = json.loads((self.root / "exchange" / "p03" / f"{self.task_id}_fix1.task.json")
                          .read_text(encoding="utf-8"))
        self.assertTrue(kept["keep"])


class TestRenderAndWrite(AcceptanceBase):
    def test_markdown_sections(self):
        self.add_p02()

        text = acc.render_markdown(acc.build(self.root))

        for heading in ("# AI 验收报告", "## 完成的功能", "## 验收标准核对",
                        "## 未验证事项与剩余风险", "## 需要人工体验的检查项",
                        "## 修复任务草案", "## 边界"):
            self.assertIn(heading, text)
        self.assertIn("自动合并：**否**", text)
        self.assertIn("线索级", text)
        self.assertIn("verified=False", text)
        self.assertIn("src/todo.py", text)

    def test_markdown_preserves_possible_test_hints(self):
        # AC-04 类情形：关键词未直接命中，但存在名字相近的用例 → 保留为线索
        write_json(self.root / "exchange" / "p03" / f"{self.task_id}.task.json", {
            **self.task_file,
            "acceptance": [{"id": "AC-04", "description": "add/list_all/count 继续通过"}],
        })
        self.add_p02(stderr_tail="test_add_and_list (tests.test_todo.TodoListTests) ... ok\n"
                                 "test_count_after_remove (tests.test_todo.TodoListTests) ... ok\n"
                                 "\nRan 8 tests in 0.001s\n\nOK\n")

        ac = acc.build(self.root)["acceptance"][0]
        text = acc.render_markdown(acc.build(self.root))

        self.assertEqual(ac["status"], "partially_covered")
        self.assertEqual(ac["missing_tokens"], ["list_all"])
        self.assertIn("test_add_and_list", ac["possible_tests"])
        self.assertIn("仅供人工追踪，不作为证据", text)

    def test_markdown_for_error(self):
        text = acc.render_markdown({"ok": False, "error": "索引里没有真实开发任务记录"})
        self.assertIn("验收失败", text)
        self.assertIn("索引里没有真实开发任务记录", text)

    def test_main_writes_files(self):
        self.add_p02()

        with mock.patch("sys.stdout"):
            code = acc.main(["--root", str(self.root)])

        out = self.root / "exchange" / "p04" / "acceptance"
        self.assertEqual(code, 0)
        self.assertTrue((out / f"{self.task_id}.acceptance.md").is_file())
        payload = json.loads((out / f"{self.task_id}.acceptance.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["task_id"], self.task_id)
        self.assertFalse(payload["auto_merge"])
        self.assertEqual(list((self.root / "exchange" / "p04" / "drafts").glob("*.json")), [])

    def test_main_writes_draft_when_needed(self):
        self.add_p02(status="P02_BLOCKED_AUTH", tests_exit=None, review=None)

        with mock.patch("sys.stdout"):
            code = acc.main(["--root", str(self.root)])

        drafts = list((self.root / "exchange" / "p04" / "drafts").glob("*.task.json"))
        self.assertEqual(code, 0)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].name, f"{self.task_id}_fix1.task.json")

    def test_print_mode_does_not_write(self):
        self.add_p02()

        with mock.patch("sys.stdout"):
            code = acc.main(["--root", str(self.root), "--print"])

        self.assertEqual(code, 0)
        self.assertFalse((self.root / "exchange" / "p04").exists())

    def test_print_mode_exit_code_on_error(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(acc.main(["--root", str(self.root), "--print"]), 1)

    def test_originals_untouched(self):
        self.add_p02()
        before = {
            path: path.read_bytes()
            for path in (self.root / "exchange").rglob("*.json")
            if "p04" not in path.parts
        }

        with mock.patch("sys.stdout"):
            acc.main(["--root", str(self.root)])

        after = {
            path: path.read_bytes()
            for path in (self.root / "exchange").rglob("*.json")
            if "p04" not in path.parts and "status" not in path.parts
        }
        for path, content in after.items():
            self.assertEqual(content, before[path], f"{path} 被改写")


if __name__ == "__main__":
    unittest.main(verbosity=2)
