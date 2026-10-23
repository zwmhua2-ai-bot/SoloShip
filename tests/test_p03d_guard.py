import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))

import p03d_guard as guard  # noqa: E402

BASE_CONFIG = {
    "windows_test_mode": {
        "enabled": True,
        "approved_projects": ["demo-app"],
        "approved_repos": ["demo/soloship-demo"],
        "approved_absolute_repos": ["D:\\SoloShip\\repos\\soloship-demo"],
        "risk_accepted": True,
        "isolation_claim": "NONE",
    },
    "model_router": {
        "tiers": {"fast": {"model": "m1", "effort": "low"}},
        "whitelist": ["m1"],
        "supported_efforts": ["low", "medium"],
    },
    "github": {
        "enabled": False,
        "mode": "live",
        "allowed_repos": ["zwmhua2-ai-bot/soloship-demo"],
        "allowed_base_branches": ["main"],
        "branch_prefix": "soloship/",
        "draft": True,
        "allow_merge": False,
        "allow_force_push": False,
        "require_env_token": True,
        "token_env": "GH_TOKEN",
    },
}


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class ToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p03d-toggle-"))
        self.root = self.tmp / "root"
        self.config = self.root / "config" / "bridge.json"
        write_json(self.config, BASE_CONFIG)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_enable_changes_only_github_enabled(self):
        before = read_json(self.config)
        result = guard.toggle(self.root, True)
        self.assertFalse(result.get("error"), result)
        self.assertTrue(result["changed"])
        self.assertEqual(result["diff"], [["github", "enabled"]])

        after = read_json(self.config)
        self.assertTrue(after["github"]["enabled"])
        self.assertEqual(guard.deep_diff(before, after), [("github", "enabled")])
        # 其余字段逐一保持
        self.assertEqual(before["windows_test_mode"], after["windows_test_mode"])
        self.assertEqual(before["model_router"], after["model_router"])
        for key, value in before["github"].items():
            if key != "enabled":
                self.assertEqual(value, after["github"][key], key)

    def test_single_element_arrays_stay_arrays(self):
        guard.toggle(self.root, True)
        after = read_json(self.config)
        self.assertIsInstance(after["github"]["allowed_repos"], list)
        self.assertEqual(after["github"]["allowed_repos"], ["zwmhua2-ai-bot/soloship-demo"])
        self.assertIsInstance(after["windows_test_mode"]["approved_projects"], list)
        self.assertIsInstance(after["model_router"]["whitelist"], list)

    def test_written_file_has_no_bom_and_is_utf8(self):
        guard.toggle(self.root, True)
        raw = self.config.read_bytes()
        self.assertNotEqual(raw[:3], b"\xef\xbb\xbf")
        raw.decode("utf-8")  # 必须能按纯 utf-8 解码

    def test_second_toggle_is_noop(self):
        guard.toggle(self.root, True)
        backups = sorted(self.config.parent.glob("bridge.json.bak-*"))
        self.assertEqual(len(backups), 1)
        again = guard.toggle(self.root, True)
        self.assertFalse(again["changed"])
        self.assertTrue(again["after"])
        self.assertEqual(len(sorted(self.config.parent.glob("bridge.json.bak-*"))), 1)

    def test_toggle_back_restores_original_document(self):
        original = read_json(self.config)
        guard.toggle(self.root, True)
        guard.toggle(self.root, False)
        self.assertEqual(guard.deep_diff(original, read_json(self.config)), [])

    def test_missing_github_block_is_refused(self):
        broken = json.loads(json.dumps(BASE_CONFIG))
        broken.pop("github")
        write_json(self.config, broken)
        result = guard.toggle(self.root, True)
        self.assertIn("error", result)
        self.assertEqual(read_json(self.config), broken)

    def test_missing_config_is_refused(self):
        self.config.unlink()
        result = guard.toggle(self.root, True)
        self.assertIn("error", result)

    def test_crlf_and_no_trailing_newline_roundtrip_is_byte_identical(self):
        text = json.dumps(BASE_CONFIG, ensure_ascii=False, indent=2).replace("\n", "\r\n")
        raw = text.encode("utf-8")
        self.config.write_bytes(raw)
        self.assertFalse(raw.endswith(b"\n"))

        guard.toggle(self.root, True)
        self.assertEqual(self.config.read_bytes(), raw.replace(b'"enabled": false', b'"enabled": true'))
        guard.toggle(self.root, False)
        self.assertEqual(self.config.read_bytes(), raw)

    def test_lf_with_trailing_newline_is_preserved(self):
        raw = (json.dumps(BASE_CONFIG, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.config.write_bytes(raw)
        guard.toggle(self.root, True)
        guard.toggle(self.root, False)
        self.assertEqual(self.config.read_bytes(), raw)


class SharedConstantTests(unittest.TestCase):
    def test_status_tuple_matches_publisher(self):
        import github_publisher as gp  # noqa: PLC0415 - 仅用于一致性核对

        self.assertEqual(tuple(gp.PR_SUCCESS_STATUSES), tuple(guard.PR_SUCCESS_STATUSES))

    def test_guard_has_no_runtime_dependency_on_publisher(self):
        source = Path(guard.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from github_publisher import", source)
        self.assertNotIn("import github_publisher", source)


class PrecheckInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p03d-inbox-"))
        self.root = self.tmp / "root"
        self.inbox = self.root / "exchange" / "p02" / "inbox"
        self.inbox.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_inbox_passes(self):
        result = guard.precheck_inbox(self.root, ["p03d_remove_001"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["foreign"], [])

    def test_foreign_ready_file_is_rejected(self):
        (self.inbox / "other_task_001.ready.json").write_text("{}", encoding="utf-8")
        result = guard.precheck_inbox(self.root, ["p03d_remove_001"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["foreign"], ["other_task_001.ready.json"])
        self.assertIn("other_task_001", result["error"])

    def test_own_file_is_allowed(self):
        (self.inbox / "p03d_remove_001.ready.json").write_text("{}", encoding="utf-8")
        result = guard.precheck_inbox(self.root, ["p03d_remove_001"])
        self.assertTrue(result["ok"])

    def test_missing_inbox_is_error(self):
        self.inbox.rmdir()
        result = guard.precheck_inbox(self.root, ["p03d_remove_001"])
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])


class CheckResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p03d-check-"))
        self.report = self.tmp / "report.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, status="P02_READY_FOR_HUMAN", publish=None, request_id="p03d_remove_001",
               head="abc123"):
        evidence = {"status": status, "request_id": request_id, "head_commit": head}
        if publish is not None:
            evidence["publish"] = publish
        write_json(self.report, evidence)

    @staticmethod
    def _publish(status, pr_url="https://github.com/o/r/pull/1", branch="soloship/p03d_remove_001",
                 candidate="abc123"):
        return {"status": status, "pr_url": pr_url, "branch": branch,
                "candidate_sha": candidate, "detail": "x"}

    def test_pr_created_passes(self):
        self._write(publish=self._publish("PUBLISH_PR_CREATED"))
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertTrue(result["ok"], result["reasons"])

    def test_pr_exists_and_recovered_also_pass(self):
        for status in ("PUBLISH_PR_EXISTS", "PUBLISH_PR_RECOVERED"):
            self._write(publish=self._publish(status))
            result = guard.check_result(self.report, "p03d_remove_001")
            self.assertTrue(result["ok"], (status, result["reasons"]))

    def test_pushed_without_pr_fails(self):
        self._write(publish=self._publish("PUBLISH_PUSHED", pr_url=None))
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertFalse(result["ok"])
        self.assertTrue(any("PUBLISH_PUSHED" in item for item in result["reasons"]))

    def test_missing_pr_url_fails(self):
        self._write(publish=self._publish("PUBLISH_PR_CREATED", pr_url="  "))
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertFalse(result["ok"])
        self.assertTrue(any("PR 链接" in item for item in result["reasons"]))

    def test_task_not_ready_fails(self):
        self._write(status="P02_BLOCKED_REVIEW", publish=self._publish("PUBLISH_PR_CREATED"))
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertFalse(result["ok"])
        self.assertTrue(any("未通过闸门" in item for item in result["reasons"]))

    def test_candidate_sha_mismatch_fails(self):
        self._write(publish=self._publish("PUBLISH_PR_CREATED", candidate="deadbeef"))
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertFalse(result["ok"])
        self.assertTrue(any("candidate SHA" in item for item in result["reasons"]))

    def test_wrong_branch_prefix_fails(self):
        self._write(publish=self._publish("PUBLISH_PR_CREATED", branch="feature/x"))
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertFalse(result["ok"])
        self.assertTrue(any("分支不在授权前缀" in item for item in result["reasons"]))

    def test_wrong_request_id_fails(self):
        self._write(publish=self._publish("PUBLISH_PR_CREATED"), request_id="other")
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertFalse(result["ok"])
        self.assertTrue(any("任务不匹配" in item for item in result["reasons"]))

    def test_missing_publish_record_fails(self):
        self._write()
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertFalse(result["ok"])
        self.assertTrue(any("没有发布记录" in item for item in result["reasons"]))

    def test_missing_report_fails(self):
        result = guard.check_result(self.report, "p03d_remove_001")
        self.assertFalse(result["ok"])
        self.assertTrue(any("报告不存在" in item for item in result["reasons"]))


class TaskFileTests(unittest.TestCase):
    """参数化任务：按 request_id 找文件，并校验文件内声明一致。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p03d-task-"))
        self.root = self.tmp / "root"
        write_json(self.root / "config" / "bridge.json", BASE_CONFIG)
        self.tasks = self.root / "exchange" / "p03"
        self.tasks.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, request_id="p03d_remove_002", project_id="demo-app",
              repo="D:\\SoloShip\\repos\\soloship-demo", base_ref="main"):
        return {
            "schema_version": "1.0",
            "request_id": request_id,
            "project_id": project_id,
            "title": "t",
            "goal": "g",
            "requested_paths": ["src/**"],
            "constraints": [],
            "non_goals": [],
            "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
            "workspace": {"repo": repo, "base_ref": base_ref, "allow_paths": ["src"]},
            "verify": {"test_command": ["py", "-3", "-m", "unittest"], "timeout_seconds": 60},
            "codex": {"sandbox": "workspace-write", "timeout_seconds": 60},
        }

    def _write(self, obj, name="p03d_remove_002.task.json"):
        write_json(self.tasks / name, obj)

    def test_matching_task_passes(self):
        self._write(self._task())
        result = guard.validate_task_file(self.root, "p03d_remove_002")
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["task_request_id"], "p03d_remove_002")

    def test_request_id_mismatch_is_rejected(self):
        self._write(self._task(request_id="p03d_remove_001"))
        result = guard.validate_task_file(self.root, "p03d_remove_002")
        self.assertFalse(result["ok"])
        self.assertTrue(any("不一致" in item for item in result["errors"]))

    def test_missing_task_file_is_error(self):
        result = guard.validate_task_file(self.root, "p03d_remove_003")
        self.assertFalse(result["ok"])
        self.assertTrue(any("不存在" in item for item in result["errors"]))

    def test_unapproved_project_is_rejected(self):
        self._write(self._task(project_id="other-app"))
        result = guard.validate_task_file(self.root, "p03d_remove_002")
        self.assertFalse(result["ok"])
        self.assertTrue(any("approved_projects" in item for item in result["errors"]))

    def test_unapproved_repo_is_rejected(self):
        self._write(self._task(repo="D:\\Elsewhere\\repo"))
        result = guard.validate_task_file(self.root, "p03d_remove_002")
        self.assertFalse(result["ok"])
        self.assertTrue(any("approved_absolute_repos" in item for item in result["errors"]))

    def test_relative_repo_is_rejected(self):
        self._write(self._task(repo="demo/soloship-demo"))
        result = guard.validate_task_file(self.root, "p03d_remove_002")
        self.assertFalse(result["ok"])
        self.assertTrue(any("绝对路径" in item for item in result["errors"]))

    def test_base_ref_outside_whitelist_is_rejected(self):
        self._write(self._task(base_ref="feature/x"))
        result = guard.validate_task_file(self.root, "p03d_remove_002")
        self.assertFalse(result["ok"])
        self.assertTrue(any("allowed_base_branches" in item for item in result["errors"]))

    def test_cli_exit_codes(self):
        self._write(self._task())
        out = self.tmp / "out.json"
        self.assertEqual(guard.main(["validate-task", "--root", str(self.root),
                                     "--request-id", "p03d_remove_002", "--out", str(out)]), 0)
        self.assertEqual(guard.main(["validate-task", "--root", str(self.root),
                                     "--request-id", "p03d_remove_002"]), 0)
        self.assertEqual(guard.main(["validate-task", "--root", str(self.root),
                                     "--request-id", "p03d_remove_001"]), 2)


class DuplicateTaskTests(unittest.TestCase):
    """重复任务判定依赖 spec_sha：换 request_id 必须产生不同 spec_sha。"""

    def _task(self, request_id):
        return {
            "schema_version": "1.0",
            "request_id": request_id,
            "project_id": "demo-app",
            "title": "TodoList 增加 remove() 方法",
            "goal": "g",
            "requested_paths": ["src/**"],
            "constraints": [],
            "non_goals": [],
            "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
        }

    def test_same_request_id_same_spec_sha(self):
        import p00_bridge  # noqa: PLC0415

        self.assertEqual(p00_bridge.canonical_sha(self._task("p03d_remove_002")),
                         p00_bridge.canonical_sha(self._task("p03d_remove_002")))

    def test_new_request_id_changes_spec_sha(self):
        import p00_bridge  # noqa: PLC0415

        self.assertNotEqual(p00_bridge.canonical_sha(self._task("p03d_remove_001")),
                            p00_bridge.canonical_sha(self._task("p03d_remove_002")))

    def test_new_request_id_is_not_treated_as_duplicate(self):
        """Bridge 的重复判定按 spec_sha，新 id 不会被判成已处理过的任务。"""
        import p00_bridge  # noqa: PLC0415

        seen = {p00_bridge.canonical_sha(self._task("p03d_remove_001"))}
        fresh = p00_bridge.canonical_sha(self._task("p03d_remove_002"))
        self.assertNotIn(fresh, seen)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p03d-cmp-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_identical_documents_compare_equal(self):
        left, right = self.tmp / "a.json", self.tmp / "b.json"
        write_json(left, BASE_CONFIG)
        write_json(right, BASE_CONFIG)
        self.assertTrue(guard.compare_files(left, right)["identical"])

    def test_changed_flag_is_detected(self):
        left, right = self.tmp / "a.json", self.tmp / "b.json"
        write_json(left, BASE_CONFIG)
        changed = json.loads(json.dumps(BASE_CONFIG))
        changed["github"]["enabled"] = True
        write_json(right, changed)
        result = guard.compare_files(left, right)
        self.assertFalse(result["identical"])
        self.assertEqual(result["diff"], [["github", "enabled"]])


class CliExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p03d-cli-"))
        self.root = self.tmp / "root"
        self.config = self.root / "config" / "bridge.json"
        write_json(self.config, BASE_CONFIG)
        self.out = self.tmp / "out.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_toggle_ok_exits_zero(self):
        code = guard.main(["toggle", "--root", str(self.root), "--value", "true",
                           "--out", str(self.out)])
        self.assertEqual(code, 0)
        self.assertEqual(read_json(self.out)["after"], True)

    def test_check_failure_exits_one(self):
        report = self.tmp / "report.json"
        write_json(report, {"status": "P02_READY_FOR_HUMAN", "request_id": "p03d_remove_001",
                            "head_commit": "abc", "publish": {"status": "PUBLISH_FAILED_AUTH"}})
        code = guard.main(["check", "--report", str(report), "--request-id", "p03d_remove_001",
                           "--out", str(self.out)])
        self.assertEqual(code, 1)

    def test_precheck_foreign_exits_one(self):
        inbox = self.root / "exchange" / "p02" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "other.ready.json").write_text("{}", encoding="utf-8")
        code = guard.main(["precheck-inbox", "--root", str(self.root), "--allow", "p03d_remove_001"])
        self.assertEqual(code, 1)

    def test_compare_difference_exits_one(self):
        left, right = self.tmp / "a.json", self.tmp / "b.json"
        write_json(left, BASE_CONFIG)
        changed = json.loads(json.dumps(BASE_CONFIG))
        changed["github"]["draft"] = False
        write_json(right, changed)
        code = guard.main(["compare", "--left", str(left), "--right", str(right)])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
