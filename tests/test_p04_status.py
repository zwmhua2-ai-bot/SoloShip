"""P04-A：任务状态索引的行为约束。

重点保证三件事：
  1. latest.json 只认 development，不会被更晚的 P00 交接记录顶掉；
  2. state/pr 字段能区分 开发完成 / 等待人工验收 / PR 已创建 / PR 已合并；
  3. 索引是只读旁路：不改原始报告、不留临时文件、失败不外抛。
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))

import status_index as si  # noqa: E402

DEV_REPO = "D:\\SoloShip\\repos\\soloship-demo"
OTHER_REPO = "D:\\SoloShip\\repos\\other-app"


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def make_root(tmp: str, approved=None) -> Path:
    root = Path(tmp)
    write_json(root / "config" / "bridge.json", {
        "windows_test_mode": {
            "approved_absolute_repos": [DEV_REPO] if approved is None else list(approved),
        }
    })
    return root


def add_p02(root: Path, task_id: str, *, project="demo-app",
            status="P02_READY_FOR_HUMAN", repo=DEV_REPO,
            created_at=None,
            finished_at="2026-10-23T12:05:00+00:00",
            tests_exit=0, review_status="REVIEW_APPROVED", blocking=0,
            publish=None, write_report=True) -> dict:
    if created_at is None and finished_at:
        # 回执语义：created_at 必须早于 finished_at，否则时间排序会失真。
        created_at = (datetime.fromisoformat(finished_at)
                      - timedelta(minutes=5)).isoformat(timespec="seconds")
    job_id = f"p02_{task_id}"
    receipt = {
        "phase": "P02", "status": status, "job_id": job_id,
        "request_id": task_id, "project_id": project,
        "base_commit": "a" * 40, "head_commit": "b" * 40, "fix_rounds": 0,
        "review_status": review_status, "blocking_findings": blocking,
        "tests_ok": tests_exit == 0, "created_at": created_at, "finished_at": finished_at,
    }
    if publish is not None:
        receipt["publish"] = publish
        job_id_publish = dict(publish)
        job_id_publish.setdefault("job_id", job_id)
        write_json(root / ".control" / "p03" / "publish" / f"{job_id}.json", job_id_publish)
    write_json(root / "exchange" / "p02" / "receipts" / f"{project}__{task_id}.receipt.json", receipt)

    if write_report:
        write_json(root / "exchange" / "p02" / "reports" / f"{project}__{task_id}.report.json", {
            "phase": "P02", "status": status, "job_id": job_id, "request_id": task_id,
            "project_id": project, "title": f"标题 {task_id}", "goal": f"目标 {task_id}",
            "repo": repo, "base_commit": "a" * 40, "head_commit": "b" * 40,
            "started_at": created_at, "finished_at": finished_at,
            "tests": {"command": ["py", "-3", "-m", "unittest"], "exit_code": tests_exit,
                      "stderr_tail": "", "stdout_tail": ""},
            "reviews": [{"status": review_status}],
            "calls": [{"actual_model": "UNVERIFIED", "role": "explorer"}],
        })
    return receipt


def add_p00(root: Path, task_id: str, *, project="handoff",
            status="ACCEPTED", finished_at="2026-10-23T23:00:00+00:00") -> dict:
    receipt = {
        "phase": "P00", "status": status, "job_id": f"p00_{task_id}",
        "request_id": task_id, "project_id": project,
        "created_at": "2026-10-23T22:59:00+00:00", "finished_at": finished_at,
    }
    write_json(root / "exchange" / "receipts" / f"{project}__{task_id}.receipt.json", receipt)
    return receipt


def snapshot(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*.json")):
        if "status" in path.parts:
            continue
        result[str(path.relative_to(root))] = path.read_bytes()
    return result


class StatusIndexBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestLatestSelection(StatusIndexBase):
    def test_newer_handoff_does_not_become_latest_development(self):
        add_p02(self.root, "task_dev_001")
        # 交接记录时间明显更晚，但它不是开发任务
        add_p00(self.root, "handoff_001", finished_at="2026-10-23T23:59:00+00:00")

        result = si.build(self.root)

        self.assertTrue(result["ok"])
        self.assertEqual(result["latest_development"], "task_dev_001")
        self.assertEqual(result["latest_any"], "handoff_001")

        latest = si.load_latest(self.root)
        self.assertEqual(latest["task"]["task_id"], "task_dev_001")
        self.assertEqual(latest["task"]["kind"], "development")
        self.assertEqual(latest["latest_any"]["task_id"], "handoff_001")
        self.assertEqual(latest["latest_any"]["kind"], "handoff")
        self.assertTrue(latest["has_development_task"])

    def test_latest_is_null_without_development_task(self):
        add_p00(self.root, "handoff_001")

        si.build(self.root)
        latest = si.load_latest(self.root)

        self.assertIsNone(latest["task"])
        self.assertFalse(latest["has_development_task"])
        self.assertEqual(latest["latest_any"]["task_id"], "handoff_001")

    def test_newest_development_wins(self):
        add_p02(self.root, "task_old", finished_at="2026-10-20T10:00:00+00:00")
        add_p02(self.root, "task_new", finished_at="2026-10-22T10:00:00+00:00")

        si.build(self.root)

        self.assertEqual(si.load_latest(self.root)["task"]["task_id"], "task_new")
        ids = [item["task_id"] for item in si.load_index(self.root)["tasks"]]
        self.assertEqual(ids, ["task_new", "task_old"])

    def test_index_counts(self):
        add_p02(self.root, "task_dev_001")
        add_p02(self.root, "task_demo_001", project="demo", repo=OTHER_REPO)
        add_p00(self.root, "handoff_001")

        si.build(self.root)
        counts = si.load_index(self.root)["counts"]

        self.assertEqual(counts["total"], 3)
        self.assertEqual(counts["by_kind"], {"development": 1, "demo": 1, "handoff": 1})


class TestKindClassification(StatusIndexBase):
    def test_kind_rules(self):
        approved = si.approved_repos(self.root)

        self.assertEqual(si.classify_kind("P00", DEV_REPO, approved), si.KIND_HANDOFF)
        self.assertEqual(si.classify_kind("P02", DEV_REPO, approved), si.KIND_DEVELOPMENT)
        self.assertEqual(si.classify_kind("P02", OTHER_REPO, approved), si.KIND_DEMO)
        self.assertEqual(si.classify_kind("P02", None, approved), si.KIND_UNKNOWN)
        self.assertEqual(si.classify_kind("P02", "   ", approved), si.KIND_UNKNOWN)


class TestStateMapping(StatusIndexBase):
    def test_pr_created_with_number_from_url(self):
        add_p02(self.root, "task_pr", publish={
            "status": "PUBLISH_PR_CREATED",
            "pr_url": "https://github.com/zwmhua2-ai-bot/soloship-demo/pull/7",
            "branch": "soloship/task_pr",
        })

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "pr_created")
        self.assertEqual(task["state_label"], "PR 已创建（Draft，未合并）")
        self.assertTrue(task["human_action_required"])
        self.assertEqual(task["pr"]["state"], "created")
        self.assertEqual(task["pr"]["number"], 7)
        self.assertEqual(task["pr"]["branch"], "soloship/task_pr")

    def test_pr_merged(self):
        add_p02(self.root, "task_merged", publish={
            "status": "PUBLISH_PR_MERGED",
            "pr_url": "https://github.com/zwmhua2-ai-bot/soloship-demo/pull/9",
            "pr_number": 9,
        })

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "pr_merged")
        self.assertEqual(task["pr"]["state"], "merged")
        self.assertFalse(task["human_action_required"])
        self.assertEqual(task["pr"]["number"], 9)

    def test_waiting_human_review_without_publish(self):
        add_p02(self.root, "task_wait")

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "waiting_human_review")
        self.assertEqual(task["pr"]["state"], "none")
        self.assertIsNone(task["pr"]["url"])
        self.assertTrue(task["human_action_required"])

    def test_blocked_auth_is_visible_with_raw_status(self):
        add_p02(self.root, "task_blocked", status="P02_BLOCKED_AUTH",
                tests_exit=None, review_status=None)

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "blocked")
        self.assertEqual(task["status"], "P02_BLOCKED_AUTH")
        self.assertTrue(task["human_action_required"])

    def test_failed_state(self):
        add_p02(self.root, "task_failed", status="P02_REPO_NOT_ALLOWED",
                tests_exit=None, review_status=None)

        si.build(self.root)
        self.assertEqual(si.load_latest(self.root)["task"]["state"], "failed")

    def test_handoff_states_do_not_ask_for_human(self):
        add_p00(self.root, "handoff_ok")
        add_p00(self.root, "handoff_bad", status="REJECTED")

        si.build(self.root)
        by_id = {item["task_id"]: item for item in si.load_index(self.root)["tasks"]}

        self.assertEqual(by_id["handoff_ok"]["state"], "handoff_ok")
        self.assertFalse(by_id["handoff_ok"]["human_action_required"])
        self.assertEqual(by_id["handoff_bad"]["state"], "handoff_failed")

    def test_dev_done_for_p01(self):
        root = self.root
        write_json(root / "exchange" / "p01" / "receipts" / "demo-app__task_p01.receipt.json", {
            "phase": "P01", "status": "P01_CODEX_OK", "job_id": "p01_x",
            "request_id": "task_p01", "project_id": "demo-app",
            "created_at": "2026-10-01T00:00:00+00:00", "finished_at": "2026-10-01T01:00:00+00:00",
        })

        si.build(root)
        task = si.load_index(root)["tasks"][0]

        self.assertEqual(task["state"], "dev_done")
        self.assertFalse(task["human_action_required"])


class TestPublishFallback(StatusIndexBase):
    def test_receipt_publish_used_when_journal_missing(self):
        add_p02(self.root, "task_snap", publish={
            "status": "PUBLISH_PR_CREATED",
            "pr_url": "https://github.com/zwmhua2-ai-bot/soloship-demo/pull/3",
        })
        for path in (self.root / ".control" / "p03" / "publish").glob("*.json"):
            path.unlink()

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "pr_created")
        self.assertEqual(task["pr"]["number"], 3)


class TestLookup(StatusIndexBase):
    def test_find_task_exact_match(self):
        add_p02(self.root, "task_a", finished_at="2026-10-21T10:00:00+00:00")
        add_p02(self.root, "task_b", finished_at="2026-10-22T10:00:00+00:00")
        si.build(self.root)

        self.assertEqual(si.find_task(self.root, "task_a")["task_id"], "task_a")
        self.assertIsNone(si.find_task(self.root, "task"))
        self.assertIsNone(si.find_task(self.root, "task_a_fix1"))

    def test_cli_latest_exit_code(self):
        add_p00(self.root, "handoff_only")
        si.build(self.root)
        with mock.patch("sys.stdout"):
            self.assertEqual(si.main(["latest", "--root", str(self.root)]), 1)

        add_p02(self.root, "task_dev")
        si.build(self.root)
        with mock.patch("sys.stdout"):
            self.assertEqual(si.main(["latest", "--root", str(self.root)]), 0)


class TestSideEffectFree(StatusIndexBase):
    def test_source_files_untouched(self):
        add_p02(self.root, "task_a", publish={
            "status": "PUBLISH_PR_CREATED",
            "pr_url": "https://github.com/zwmhua2-ai-bot/soloship-demo/pull/1",
        })
        add_p00(self.root, "handoff_a")
        before = snapshot(self.root)

        si.build(self.root)

        self.assertEqual(snapshot(self.root), before)

    def test_no_temp_files_left_behind(self):
        add_p02(self.root, "task_a")
        si.build(self.root)

        leftovers = list((self.root / "exchange" / "status").rglob(".write-*"))
        self.assertEqual(leftovers, [])

    def test_project_file_written(self):
        add_p02(self.root, "task_a")
        add_p00(self.root, "handoff_a")

        si.build(self.root)
        data = json.loads((self.root / "exchange" / "status" / "projects" / "demo-app.json")
                          .read_text(encoding="utf-8"))

        self.assertEqual(data["project_id"], "demo-app")
        self.assertEqual(data["latest_development"]["task_id"], "task_a")
        self.assertEqual(data["counts"]["total"], 1)

    def test_refresh_swallows_errors(self):
        add_p02(self.root, "task_a")
        with mock.patch.object(si, "collect_tasks", side_effect=RuntimeError("boom")):
            result = si.refresh(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["error"])

    def test_missing_report_does_not_break_record(self):
        add_p02(self.root, "task_noreport", write_report=False)

        si.build(self.root)
        records = si.load_index(self.root)["tasks"]

        self.assertEqual(len(records), 1)
        task = records[0]
        self.assertEqual(task["task_id"], "task_noreport")
        self.assertIsNone(task["report_path"])
        # 仓库未知时不得冒充开发任务，也不得冒充演示任务
        self.assertEqual(task["kind"], "unknown")
        self.assertIsNone(si.load_latest(self.root)["task"])
        self.assertTrue(task["tests"]["ok"])  # 回执里的 tests_ok 仍可用

    def test_timestamp_falls_back_to_mtime(self):
        add_p02(self.root, "task_no_time", created_at=None, finished_at=None,
                write_report=False)
        target = self.root / "exchange" / "p02" / "receipts" / "demo-app__task_no_time.receipt.json"
        os.utime(target, (1_700_000_000, 1_700_000_000))

        si.build(self.root)
        task = si.load_index(self.root)["tasks"][0]

        self.assertEqual(task["timestamp_source"], "file_mtime")
        self.assertTrue(task["updated_at"].startswith("2023-11-14"))

    def test_acceptance_path_only_when_file_exists(self):
        add_p02(self.root, "task_a")
        si.build(self.root)
        self.assertIsNone(si.load_latest(self.root)["task"]["acceptance_path"])

        target = self.root / "exchange" / "p04" / "acceptance" / "task_a.acceptance.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# 验收\n", encoding="utf-8")

        si.build(self.root)
        self.assertEqual(si.load_latest(self.root)["task"]["acceptance_path"],
                         "exchange/p04/acceptance/task_a.acceptance.md")

    def test_model_verification_marked_unverified(self):
        add_p02(self.root, "task_a")
        si.build(self.root)
        self.assertEqual(si.load_latest(self.root)["task"]["model_verification"], "UNVERIFIED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
