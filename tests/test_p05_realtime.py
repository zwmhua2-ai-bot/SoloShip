"""P05-A：GitHub 实时状态查询的边界。

三条硬要求：
  1. 只读——不出现任何远端写操作；
  2. 查询失败不得伪造实时状态，历史执行记录必须原样保留并标明"不可确认"；
  3. 较旧的观测不得覆盖较新的状态。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))

import github_status as gs  # noqa: E402
import status_index as si  # noqa: E402
from tests.test_p04_status import add_p02, make_root, write_json  # noqa: E402

FAKE_GH = ROOT / "tests" / "fake_gh.py"
PR_URL = "https://github.com/zwmhua2-ai-bot/soloship-demo/pull/1"


def gh_cmd() -> list[str]:
    return [sys.executable, str(FAKE_GH)]


def fake_env(**extra):
    base = {"PYTHONIOENCODING": "utf-8"}
    base.update(extra)
    return mock.patch.dict(os.environ, base)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def add_task(self, task_id="task_pr_001", **kwargs):
        kwargs.setdefault("publish", {"status": "PUBLISH_PR_CREATED", "pr_url": PR_URL,
                                      "branch": f"soloship/{task_id}"})
        add_p02(self.root, task_id, **kwargs)
        si.build(self.root)
        return task_id

    def realtime(self, task_id="task_pr_001") -> dict:
        return gs.read_realtime(self.root, task_id)


class TestProbeParsing(unittest.TestCase):
    def test_parse_pr_ref(self):
        self.assertEqual(gs.parse_pr_ref(PR_URL),
                         {"repo": "zwmhua2-ai-bot/soloship-demo", "number": 1, "url": PR_URL})
        self.assertEqual(gs.parse_pr_ref(None, "o/r", "9")["number"], 9)
        self.assertIsNone(gs.parse_pr_ref(None, None, None))
        self.assertIsNone(gs.parse_pr_ref("https://example.com/x"))

    def test_classify(self):
        self.assertEqual(gs.classify_pr({"state": "open", "draft": True}), gs.STATE_DRAFT)
        self.assertEqual(gs.classify_pr({"state": "open", "draft": False}), gs.STATE_OPEN)
        self.assertEqual(gs.classify_pr({"state": "closed", "merged_at": None}), gs.STATE_CLOSED)
        self.assertEqual(gs.classify_pr({"state": "closed", "merged_at": "2026-10-23T13:30:00Z"}),
                         gs.STATE_MERGED)
        self.assertEqual(gs.classify_pr({"state": "closed", "merged": True}), gs.STATE_MERGED)
        self.assertEqual(gs.classify_pr({}), gs.STATE_UNKNOWN)


class TestReadOnly(unittest.TestCase):
    def test_allows_only_get_endpoints(self):
        gs.assert_readonly(["api", "repos/o/r/pulls/1"])
        gs.assert_readonly(["pr", "view", "1"])
        gs.assert_readonly(["pr", "list"])

    def test_rejects_writes(self):
        for argv in (["pr", "merge", "1"], ["pr", "close", "1"], ["pr", "create"],
                     ["api", "-X", "POST", "repos/o/r/pulls"],
                     ["api", "--method=DELETE", "repos/o/r"],
                     ["api", "repos/o/r/pulls/1", "-f", "state=closed"],
                     ["repo", "edit"]):
            with self.subTest(argv=argv):
                with self.assertRaises(gs.ReadOnlyViolation):
                    gs.assert_readonly(argv)

    def test_no_mutating_subcommand_in_module(self):
        # 行为层面确认：真实调用 gh 时 argv 只有 "api <GET 路径>"，没有写参数
        captured = {}

        def fake_run(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"state": "open", "draft": True}), stderr="")

        with mock.patch.object(gs.subprocess, "run", side_effect=fake_run):
            gs.probe(PR_URL, gh_cmd=gh_cmd())

        argv = captured["cmd"][len(gh_cmd()):]
        self.assertEqual(argv, ["api", "repos/zwmhua2-ai-bot/soloship-demo/pulls/1"])
        for token in argv:
            self.assertNotIn(token, gs.FORBIDDEN_FLAGS)


class TestProbeAgainstFakeGh(unittest.TestCase):
    def probe(self, mode="ok", pr_state=None, **kwargs):
        extra = {"FAKE_GH_MODE": mode}
        if pr_state:
            extra["FAKE_GH_PR_STATE"] = pr_state
        with fake_env(**extra):
            return gs.probe(PR_URL, gh_cmd=gh_cmd(), **kwargs)

    def test_draft(self):
        result = self.probe(pr_state="draft")

        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], gs.STATE_DRAFT)
        self.assertEqual(result["source"]["endpoint"],
                         "repos/zwmhua2-ai-bot/soloship-demo/pulls/1")
        self.assertEqual(result["source"]["via"], "gh api")
        self.assertTrue(result["checked_at"].endswith("+00:00"))
        self.assertTrue(result["evidence"]["draft"])

    def test_open(self):
        self.assertEqual(self.probe(pr_state="open")["state"], gs.STATE_OPEN)

    def test_closed(self):
        self.assertEqual(self.probe(pr_state="closed")["state"], gs.STATE_CLOSED)

    def test_merged(self):
        result = self.probe(pr_state="merged")
        self.assertEqual(result["state"], gs.STATE_MERGED)
        self.assertIsNotNone(result["evidence"]["merged_at"])

    def test_auth_failure_is_not_faked(self):
        result = self.probe(mode="noauth")

        self.assertFalse(result["ok"])
        self.assertIsNone(result["state"])
        self.assertEqual(result["reason"], "AUTH")

    def test_http_404(self):
        self.assertEqual(self.probe(mode="apierror")["reason"], "NOT_FOUND")

    def test_network_failure(self):
        self.assertEqual(self.probe(mode="netdown")["reason"], "NETWORK")

    def test_bad_payload(self):
        self.assertEqual(self.probe(mode="badjson")["reason"], "BAD_PAYLOAD")

    def test_gh_missing(self):
        with fake_env():
            result = gs.probe(PR_URL, gh_cmd=["definitely-not-a-real-gh"])
        self.assertEqual(result["reason"], "GH_MISSING")

    def test_timeout(self):
        def slow(*_a, **_k):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=1)

        with mock.patch.object(gs.subprocess, "run", side_effect=slow):
            result = gs.probe(PR_URL, gh_cmd=gh_cmd())
        self.assertEqual(result["reason"], "TIMEOUT")

    def test_missing_reference(self):
        result = gs.probe(None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "NO_PR_REFERENCE")
        self.assertIsNone(result["state"])


def ok_probe(state="draft"):
    def _probe(pr_url=None, repo=None, number=None, checked_at=None):
        return {"ok": True, "state": state, "reason": None, "error": "",
                "checked_at": checked_at,
                "pr": gs.parse_pr_ref(pr_url, repo, number),
                "source": {"kind": "github", "via": "gh api",
                           "endpoint": gs.endpoint_for(gs.parse_pr_ref(pr_url, repo, number))},
                "evidence": {"draft": state == "draft"}}
    return _probe


def fail_probe(reason="NETWORK", error="boom"):
    def _probe(pr_url=None, repo=None, number=None, checked_at=None):
        return {"ok": False, "state": None, "reason": reason, "error": error,
                "checked_at": checked_at,
                "pr": gs.parse_pr_ref(pr_url, repo, number),
                "source": {"kind": "github", "via": "gh api", "endpoint": None}}
    return _probe


def probe_side_effect(state="draft"):
    """模拟 probe 的调用形态（positional pr_url/repo/number + checked_at 关键字）。"""
    def _side_effect(*args, **kwargs):
        positional = list(args) + [None, None, None]
        return ok_probe(state)(positional[0], positional[1], positional[2],
                               checked_at=kwargs.get("checked_at"))
    return _side_effect


class TestRefreshWrites(Base):
    def test_success_record(self):
        self.add_task()

        result = gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("draft"))

        self.assertTrue(result["confirmed"])
        record = self.realtime()
        self.assertEqual(record["schema"], gs.SCHEMA)
        self.assertEqual(record["state"], "draft")
        self.assertTrue(record["confirmed"])
        self.assertEqual(record["pr"]["number"], 1)
        self.assertEqual(len(record["history"]), 1)
        self.assertTrue(record["checked_at"].endswith("+00:00"))
        self.assertEqual(record["source"]["endpoint"],
                         "repos/zwmhua2-ai-bot/soloship-demo/pulls/1")

    def test_atomic_write_leaves_no_temp(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe())

        leftovers = list((self.root / "exchange" / "status" / "realtime").glob(".write-*"))
        self.assertEqual(leftovers, [])

    def test_failure_keeps_last_success_and_marks_unconfirmed(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("draft"),
                        checked_at="2026-10-23T13:00:00+00:00")
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL,
                        probe_fn=fail_probe("AUTH", "HTTP 401"),
                        checked_at="2026-10-23T13:10:00+00:00")

        record = self.realtime()
        self.assertFalse(record["confirmed"])
        self.assertEqual(record["state"], "draft")          # 保留上一次成功观测
        self.assertEqual(record["last_success"]["checked_at"], "2026-10-23T13:00:00+00:00")
        self.assertEqual(record["last_attempt"]["reason"], "AUTH")
        self.assertEqual(len(record["history"]), 2)

    def test_stale_observation_cannot_overwrite_newer_state(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("merged"),
                        checked_at="2026-10-23T14:00:00+00:00")

        result = gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL,
                                 probe_fn=ok_probe("draft"),
                                 checked_at="2026-10-23T13:00:00+00:00")

        record = self.realtime()
        self.assertTrue(result["skipped_stale"])
        self.assertEqual(record["state"], "merged")
        self.assertEqual(record["checked_at"], "2026-10-23T14:00:00+00:00")
        self.assertEqual(len(record["history"]), 1)

    def test_two_queries_in_same_second_are_not_treated_as_stale(self):
        # 回归：checked_at 曾只到秒且用 >= 判旧，导致同一秒内的第二次查询被丢弃
        self.add_task()

        first = gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("draft"))
        second = gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("merged"))

        self.assertFalse(second["skipped_stale"])
        self.assertNotEqual(first["checked_at"], second["checked_at"])
        self.assertEqual(self.realtime()["state"], "merged")

    def test_reports_are_not_modified(self):
        self.add_task()
        targets = [
            self.root / "exchange" / "p02" / "reports" / "demo-app__task_pr_001.report.json",
            self.root / "exchange" / "p02" / "receipts" / "demo-app__task_pr_001.receipt.json",
        ]
        before = {path: path.read_bytes() for path in targets}

        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("merged"))
        si.build(self.root)

        self.assertEqual({path: path.read_bytes() for path in targets}, before)


class TestIndexIntegration(Base):
    def test_realtime_merged_upgrades_state(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("merged"))

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "pr_merged")
        self.assertEqual(task["state_basis"], "github_realtime")
        self.assertTrue(task["realtime"]["confirmed"])
        self.assertFalse(task["human_action_required"])
        # 历史执行记录仍保留原始结论
        self.assertEqual(task["pr"]["state"], "created")
        self.assertEqual(task["pr"]["status"], "PUBLISH_PR_CREATED")

    def test_realtime_draft_keeps_pr_created(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("draft"))

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "pr_created")
        self.assertEqual(task["state_basis"], "github_realtime")
        self.assertEqual(task["realtime"]["state"], "draft")

    def test_realtime_closed(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("closed"))

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "pr_closed")
        self.assertEqual(task["state_label"], "PR 已关闭（未合并）")
        self.assertTrue(task["human_action_required"])

    def test_failed_query_keeps_history_and_flags_unconfirmed(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=fail_probe())

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "pr_created")
        self.assertEqual(task["state_basis"], "publish_log")
        self.assertFalse(task["realtime"]["confirmed"])
        self.assertTrue(task["realtime"]["available"])
        self.assertIn("不可确认", task["realtime"]["note"])
        self.assertEqual(task["realtime"]["reason"], "NETWORK")

    def test_transient_failure_does_not_downgrade_last_known_state(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("merged"),
                        checked_at="2026-10-23T14:00:00+00:00")
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=fail_probe("AUTH"),
                        checked_at="2026-10-23T14:05:00+00:00")

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertEqual(task["state"], "pr_merged")
        self.assertEqual(task["state_basis"], "github_realtime_stale")
        self.assertFalse(task["realtime"]["confirmed"])
        self.assertEqual(task["realtime"]["state"], "merged")
        self.assertIn("最后一次成功观测", task["realtime"]["note"])

    def test_not_queried_is_not_reported_as_unchanged(self):
        self.add_task()

        si.build(self.root)
        task = si.load_latest(self.root)["task"]

        self.assertFalse(task["realtime"]["available"])
        self.assertIn("尚未查询", task["realtime"]["note"])
        self.assertEqual(task["state_basis"], "publish_log")

    def test_pr_number_source_traceable(self):
        self.add_task("task_a")
        si.build(self.root)
        derived = si.find_task(self.root, "task_a")
        self.assertEqual(derived["pr"]["number_source"], "derived_from_url")

        add_p02(self.root, "task_b", publish={"status": "PUBLISH_PR_CREATED",
                                             "pr_url": "https://github.com/o/r/pull/5",
                                             "pr_number": 5, "branch": "soloship/task_b"})
        si.build(self.root)
        explicit = si.find_task(self.root, "task_b")
        self.assertEqual(explicit["pr"]["number"], 5)
        self.assertEqual(explicit["pr"]["number_source"], "publish")


class TestAfterTaskHook(Base):
    """任务结束后刷新：必须只是旁路，不能影响任务结论。"""

    def bridge(self):
        import p02_bridge
        return p02_bridge.P02Bridge(self.root)

    def test_hook_writes_record_for_published_task(self):
        bridge = self.bridge()
        bridge.realtime_probe = probe_side_effect("draft")

        result = bridge._refresh_realtime("task_pr_001", {"pr_url": PR_URL,
                                                          "status": "PUBLISH_PR_CREATED"})

        self.assertTrue(result["confirmed"])
        self.assertEqual(self.realtime()["state"], "draft")

    def test_hook_skips_without_pr(self):
        bridge = self.bridge()
        self.assertEqual(bridge._refresh_realtime("task_x", {})["skipped"], "NO_PR")
        self.assertIsNone(self.realtime("task_x"))

    def test_hook_respects_config_toggle(self):
        write_json(self.root / "config" / "bridge.json", {
            "windows_test_mode": {"approved_absolute_repos": []},
            "realtime": {"after_task": False},
        })
        bridge = self.bridge()
        bridge.realtime_probe = probe_side_effect("draft")

        result = bridge._refresh_realtime("task_pr_001", {"pr_url": PR_URL})

        self.assertEqual(result["skipped"], "DISABLED")
        self.assertIsNone(self.realtime())

    def test_hook_never_raises(self):
        bridge = self.bridge()

        def boom(*_a, **_k):
            raise RuntimeError("probe exploded")

        bridge.realtime_probe = boom
        result = bridge._refresh_realtime("task_pr_001", {"pr_url": PR_URL})

        self.assertEqual(result["skipped"], "ERROR")
        self.assertIn("probe exploded", result["error"])


class TestCli(Base):
    def test_refresh_all_with_fake_gh(self):
        self.add_task()

        with fake_env(FAKE_GH_PR_STATE="merged"):
            with mock.patch.object(gs, "probe", side_effect=probe_side_effect("merged")):
                with mock.patch("sys.stdout"):
                    code = gs.main(["refresh", "--all", "--root", str(self.root)])

        self.assertEqual(code, 0)
        record = self.realtime()
        self.assertTrue(record["confirmed"])
        self.assertEqual(record["state"], "merged")

    def test_refresh_single_task(self):
        self.add_task()

        with mock.patch.object(gs, "probe", side_effect=probe_side_effect("draft")):
            with mock.patch("sys.stdout"):
                code = gs.main(["refresh", "--task-id", "task_pr_001", "--root", str(self.root)])

        self.assertEqual(code, 0)
        self.assertEqual(self.realtime()["state"], "draft")

    def test_refresh_skips_task_without_pr(self):
        add_p02(self.root, "task_no_pr", publish=None)
        si.build(self.root)

        with mock.patch("sys.stdout"):
            code = gs.main(["refresh", "--task-id", "task_no_pr", "--root", str(self.root)])

        self.assertEqual(code, 0)
        self.assertIsNone(self.realtime("task_no_pr"))

    def test_show_missing_record(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(gs.main(["show", "--task-id", "nope", "--root", str(self.root)]), 1)

    def test_show_existing_record(self):
        self.add_task()
        gs.refresh_task(self.root, "task_pr_001", pr_url=PR_URL, probe_fn=ok_probe("open"))

        with mock.patch("sys.stdout"):
            self.assertEqual(gs.main(["show", "--task-id", "task_pr_001", "--root", str(self.root)]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
