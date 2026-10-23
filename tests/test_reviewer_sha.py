#!/usr/bin/env python3
"""SS-V01-004：Reviewer 的 candidate_sha 必须与真实提交严格绑定。

覆盖 AC-01..AC-09：
  - 只接受完整对象 ID，且必须与 Bridge 提供的 head_sha 逐字符一致
  - 前缀（7/12 位）、完整+附加字符、另一个合法 SHA、空值/缺字段、非十六进制 一律拒绝
  - Reviewer 自报 PASS 但 SHA 不符时，不得进入 READY
  - 审查期间 HEAD 变化时，原结论不得被接受
"""
import json
import os
import shutil
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

import codex_adapter  # noqa: E402
import p02_bridge as p02  # noqa: E402
import policy_fixture as fx  # noqa: E402
import reviewer  # noqa: E402

FAKE_CODEX = str(ROOT / "tests" / "fake_codex.py")

HEAD40 = "0123456789abcdef0123456789abcdef01234567"
OTHER40 = "fedcba9876543210fedcba9876543210fedcba98"
HEAD64 = "0123456789abcdef" * 4
FINDING = {
    "severity": "BLOCKER", "file": "src/feature.py", "line": 1,
    "description": "d", "evidence": "e", "suggested_fix": "f",
}


def payload(sha=HEAD40, status="APPROVED", findings=None, summary="ok", **extra):
    data = {"status": status, "candidate_sha": sha, "findings": findings or [],
            "summary": summary}
    data.update(extra)
    return data


def task_obj():
    return {
        "schema_version": "1.0", "request_id": "rev_0001", "project_id": "demo-app",
        "title": "审查测试", "goal": "验证 SHA 绑定",
        "requested_paths": ["src/**"], "constraints": [], "non_goals": [],
        "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
    }


def fake_run(text: str) -> codex_adapter.CodexRun:
    return codex_adapter.CodexRun(
        ok=True, error_kind="", exit_code=0, timed_out=False, thread_id="thread-1",
        last_message=text, stdout=text, duration_ms=5,
    )


class PayloadShaTests(unittest.TestCase):
    """AC-01..AC-07：`_validate_payload` 的对象 ID 规则。"""

    def check(self, data, head=HEAD40):
        return reviewer._validate_payload(data, head)

    def test_ac01_full_sha_accepted(self):
        self.assertEqual(self.check(payload()), (True, ""))

    def test_ac01_sha256_length_accepted(self):
        """仓库若改用 SHA-256（64 位），按实际长度判断，不写死 40。"""
        self.assertEqual(self.check(payload(sha=HEAD64), HEAD64), (True, ""))

    def test_ac02_seven_char_prefix_rejected(self):
        ok, reason = self.check(payload(sha=HEAD40[:7]))
        self.assertFalse(ok)
        self.assertIn("candidate_sha", reason)

    def test_ac03_twelve_char_prefix_rejected(self):
        ok, _reason = self.check(payload(sha=HEAD40[:12]))
        self.assertFalse(ok)

    def test_ac04_full_sha_with_extra_chars_rejected(self):
        ok, _reason = self.check(payload(sha=HEAD40 + "0"))
        self.assertFalse(ok)

    def test_ac04_full_sha_with_text_suffix_rejected(self):
        ok, _reason = self.check(payload(sha=HEAD40 + "-patched"))
        self.assertFalse(ok)

    def test_ac05_other_full_sha_rejected(self):
        ok, reason = self.check(payload(sha=OTHER40))
        self.assertFalse(ok)
        self.assertIn("不一致", reason)

    def test_ac06_empty_sha_rejected(self):
        self.assertFalse(self.check(payload(sha=""))[0])

    def test_ac06_missing_field_rejected(self):
        data = payload()
        del data["candidate_sha"]
        ok, reason = self.check(data)
        self.assertFalse(ok)
        self.assertIn("缺少字段", reason)

    def test_ac06_none_sha_rejected(self):
        self.assertFalse(self.check(payload(sha=None))[0])

    def test_ac07_non_hex_rejected(self):
        ok, reason = self.check(payload(sha="g" * 40))
        self.assertFalse(ok)
        self.assertIn("非法字符", reason)

    def test_ac07_non_hex_within_length_rejected(self):
        crafted = HEAD40[:39] + "z"
        self.assertFalse(self.check(payload(sha=crafted))[0])

    def test_case_insensitive_hex_accepted(self):
        """大小写不是安全问题：同一个对象 ID 的大小写差异应被接受。"""
        self.assertEqual(self.check(payload(sha=HEAD40.upper())), (True, ""))

    def test_invalid_head_sha_rejected(self):
        """Bridge 自带的 head_sha 非法时不得放行（视为内部错误）。"""
        ok, reason = self.check(payload(sha="abc"), "abc")
        self.assertFalse(ok)
        self.assertIn("head_sha", reason)


class ReviewFlowTests(unittest.TestCase):
    """AC-08 / AC-10：审查流程对 SHA 不符的处理。"""

    def run_review(self, text, head=HEAD40):
        with mock.patch.object(codex_adapter, "run_codex", return_value=fake_run(text)):
            return reviewer.review(Path(tempfile.mkdtemp(prefix="rev-wt-")), task_obj(),
                                   "base" * 10, head, "diff", {"exit_code": 0}, ["src/feature.py"])

    def test_ac08_approved_with_short_sha_is_invalid(self):
        result = self.run_review(json.dumps(payload(sha=HEAD40[:7])))
        self.assertEqual(result.status, "REVIEW_INVALID")
        self.assertFalse(result.ok)

    def test_ac08_approved_with_other_sha_is_invalid(self):
        result = self.run_review(json.dumps(payload(sha=OTHER40)))
        self.assertEqual(result.status, "REVIEW_INVALID")
        self.assertFalse(result.ok)

    def test_ac10_approved_with_exact_sha_passes(self):
        result = self.run_review(json.dumps(payload()))
        self.assertEqual(result.status, "REVIEW_APPROVED")
        self.assertTrue(result.ok)
        self.assertEqual(result.candidate_sha, HEAD40)

    def test_ac10_changes_requested_with_exact_sha(self):
        result = self.run_review(json.dumps(payload(status="CHANGES_REQUESTED",
                                                    findings=[FINDING])))
        self.assertEqual(result.status, "REVIEW_CHANGES_REQUESTED")
        self.assertFalse(result.ok)


class P02EndToEndTests(unittest.TestCase):
    """AC-08：Reviewer 自报 PASS 但 SHA 错误时，任务不得进入 READY。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rev-p02-"))
        self.repo = self.tmp / "demo" / "soloship-demo"
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "tests" / "test_app.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "-m", "baseline"], check=True, capture_output=True)
        fx.write_config(self.tmp)
        self.bridge = p02.P02Bridge(self.tmp, codex_cmd=[sys.executable, FAKE_CODEX],
                                    sandbox_status="SANDBOX_UNENFORCED")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def submit(self, review_mode: str) -> dict:
        task = {
            "schema_version": "1.0", "request_id": "rev_p02_0001", "project_id": "demo-app",
            "title": "SHA 绑定端到端", "goal": "实现并审查",
            "requested_paths": ["src/**"], "constraints": [], "non_goals": [],
            "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
            "workspace": {"repo": "demo/soloship-demo", "base_ref": "HEAD",
                          "allow_paths": ["src", "tests"]},
            "verify": {"test_command": [sys.executable, "-m", "unittest", "discover",
                                        "-s", "tests", "-v"], "timeout_seconds": 120},
            "codex": {"sandbox": "workspace-write", "timeout_seconds": 120},
        }
        self.bridge.inbox.mkdir(parents=True, exist_ok=True)
        target = self.bridge.inbox / "rev_p02_0001.ready.json"
        target.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_BUILD_MODE": "strict",
                                          "FAKE_REVIEW_MODE": review_mode}):
            self.bridge.poll()
        receipt = self.bridge.receipts / "demo-app__rev_p02_0001.receipt.json"
        return json.loads(receipt.read_text(encoding="utf-8"))

    def test_ac08_short_sha_does_not_reach_ready(self):
        receipt = self.submit("shortsha")
        self.assertEqual(receipt["status"], p02.BLOCKED_REVIEW_INVALID)
        self.assertNotEqual(receipt["status"], p02.READY)

    def test_ac08_suffix_sha_does_not_reach_ready(self):
        receipt = self.submit("suffixsha")
        self.assertEqual(receipt["status"], p02.BLOCKED_REVIEW_INVALID)

    def test_ac10_normal_review_still_ready(self):
        receipt = self.submit("approve")
        self.assertEqual(receipt["status"], p02.READY)


class HeadMovedDuringReviewTests(unittest.TestCase):
    """AC-09：审查期间 HEAD 变化 → 原审查结论无效。"""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="rev-head-"))
        self.git("init", "-q")
        (self.repo / "a.txt").write_text("1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args: str):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True, check=True)

    def test_ac09_head_change_detected(self):
        self.assertEqual(reviewer.worktree_unchanged(self.repo, self.head), (True, ""))
        (self.repo / "b.txt").write_text("2\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b")
        ok, reason = reviewer.worktree_unchanged(self.repo, self.head)
        self.assertFalse(ok)
        self.assertIn("HEAD 变化", reason)

    def test_ac09_dirty_tree_detected(self):
        (self.repo / "a.txt").write_text("changed\n", encoding="utf-8")
        ok, reason = reviewer.worktree_unchanged(self.repo, self.head)
        self.assertFalse(ok)
        self.assertIn("工作区被修改", reason)


if __name__ == "__main__":
    unittest.main()
