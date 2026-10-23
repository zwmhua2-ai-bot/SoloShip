#!/usr/bin/env python3
"""SS-V01-005：GitHub Publisher 发布门禁与幂等性回归。

覆盖 AC-01..AC-14。全部使用 Fake gh + 临时裸远端，**不对真实 GitHub 执行任何写操作**。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "tests"))

import github_publisher as gp  # noqa: E402
from p03_fixture import REPO, PublisherFixture, sh  # noqa: E402

PUBLISHER_SOURCE = (ROOT / "bridge" / "github_publisher.py").read_text(encoding="utf-8")
# 只检查代码部分：跳过模块 docstring（里面本来就用"never force-pushes/merges/deploys"描述边界）
_CODE_START = PUBLISHER_SOURCE.index('"""', PUBLISHER_SOURCE.index('"""') + 3) + 3
PUBLISHER_CODE = PUBLISHER_SOURCE[_CODE_START:]


class GateTests(PublisherFixture):
    """发布前门禁：本地证据与远端身份。"""

    def disabled_config(self):
        cfg = json.loads(json.dumps(self.config))
        cfg["github"]["enabled"] = False
        return cfg

    def test_ac01_disabled_rejected(self):
        with self.env():
            result = self.publish(config=self.disabled_config())
        self.assertEqual(result.status, gp.SKIPPED_DISABLED)
        self.assert_no_remote_branch()

    def test_ac02_repo_not_whitelisted_rejected(self):
        with self.env():
            result = self.publish(repo="someone/else")
        self.assertEqual(result.status, gp.BLOCKED_REPO)
        self.assert_no_remote_branch()

    def test_ac05_candidate_vs_review_mismatch_rejected(self):
        """Reviewer 通过、但任务记录的提交与 candidate 不一致 → 拒绝。"""
        with self.env():
            result = self.publish(job=self.job(head_commit="f" * 40))
        self.assertEqual(result.status, gp.BLOCKED_GATE)
        self.assert_no_remote_branch()

    def test_ac05_red_tests_rejected(self):
        with self.env():
            result = self.publish(job=self.job(tests_ok=False))
        self.assertEqual(result.status, gp.BLOCKED_GATE)

    def test_ac05_unapproved_review_rejected(self):
        with self.env():
            result = self.publish(job=self.job(review_status="REVIEW_CHANGES_REQUESTED"))
        self.assertEqual(result.status, gp.BLOCKED_GATE)


class RemoteBaseTests(PublisherFixture):
    """远端 Base SHA：读取失败 / 空值 / 非法 / 冲突一律拒绝。"""

    def test_ac03_unreachable_remote_rejected(self):
        broken = self.tmp / "missing" / "fixture" / "soloship-demo.git"
        sh(["git", "-C", str(self.worktree), "remote", "set-url", "origin", str(broken)])
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_REMOTE)
        self.assertIn("base", result.detail.lower())

    def test_ac03_empty_ls_remote_rejected(self):
        """远端可达但目标分支不存在（ls-remote 返回空）→ 拒绝。"""
        empty = self.tmp / "empty.git"
        sh(["git", "init", "--bare", "-q", str(empty)])
        sh(["git", "-C", str(self.worktree), "remote", "set-url", "origin", str(empty)])
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_REMOTE)
        self.assert_no_remote_branch()

    def test_ac04_base_moved_rejected(self):
        with self.env():
            result = self.publish(expected_base_sha="0" * 40)
        self.assertEqual(result.status, gp.BLOCKED_BASE_MOVED)
        self.assert_no_remote_branch()

    def test_ac14_base_ok_publishes(self):
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.PR_CREATED)


class ExistingPrTests(PublisherFixture):
    """已有 PR 的查询、复用与冲突规则（查询先于任何推送）。"""

    def test_ac06_open_draft_matching_sha_reused_without_push(self):
        self.write_pr()
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.PR_EXISTS)
        self.assertEqual(result.pr_number, 7)
        self.assert_no_remote_branch()

    def test_ac07_closed_pr_not_reused(self):
        self.write_pr(state="CLOSED", isDraft=False)
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assertIn("关闭", result.detail + "已合并")
        self.assert_no_remote_branch()

    def test_ac07_merged_pr_not_reused(self):
        self.write_pr(state="MERGED", isDraft=False)
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assert_no_remote_branch()

    def test_ac08_non_draft_pr_not_reused(self):
        self.write_pr(isDraft=False)
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assertIn("Draft", result.detail)
        self.assert_no_remote_branch()

    def test_ac09_pr_head_sha_mismatch_rejected(self):
        self.write_pr(headRefOid="f" * 40)
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_BRANCH_CONFLICT)
        self.assert_no_remote_branch()

    def test_ac09_pr_head_branch_mismatch_rejected(self):
        self.write_pr(headRefName="soloship/other-task")
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assert_no_remote_branch()

    def test_ac09_pr_base_branch_mismatch_rejected(self):
        self.write_pr(baseRefName="develop")
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)

    def test_ac10_query_failure_blocks_publish(self):
        with self.env(FAKE_GH_MODE="listfail"):
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assert_no_remote_branch()

    def test_ac10_bad_json_blocks_publish(self):
        with self.env(FAKE_GH_MODE="badjson"):
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assert_no_remote_branch()

    def test_ac10_incomplete_record_blocks_publish(self):
        self.write_pr()
        payload = json.loads((self.gh_state / "pr.json").read_text(encoding="utf-8"))
        del payload["headRefName"]
        (self.gh_state / "pr.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assertIn("不完整", result.detail)

    def test_ac10_ambiguous_result_blocks_publish(self):
        first = self.write_pr()
        other = dict(first, number=8, url=f"https://github.com/{REPO}/pull/8")
        (self.gh_state / "pr.json").write_text(json.dumps([first, other]), encoding="utf-8")
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assertIn("歧义", result.detail)

    def test_ac11_second_run_does_not_create_second_pr(self):
        with self.env():
            first = self.publish()
            second = self.publish()
        self.assertEqual(first.status, gp.PR_CREATED)
        self.assertEqual(second.status, gp.PR_EXISTS)
        creates = [line for line in self.gh_log.read_text(encoding="utf-8").splitlines()
                   if '"pr", "create"' in line]
        self.assertEqual(len(creates), 1, "不得重复创建 PR")

    def test_ac12_timeout_after_create_reconciles(self):
        """创建 PR 超时：第一次查询还没有 PR，超时后必须重新读取远端对账。"""
        self.write_pr()
        original_gh = gp.Publisher._gh
        seen = {"lists": 0}

        def flaky_gh(self, *args, **kwargs):
            if args[:2] == ("pr", "list"):
                seen["lists"] += 1
                if seen["lists"] == 1:
                    # 首次查询：远端还没有 PR
                    return subprocess.CompletedProcess(args=list(args), returncode=0,
                                                       stdout="[]", stderr="")
            return original_gh(self, *args, **kwargs)

        original_run = gp.Publisher._run

        def flaky_run(self, cmd, *args, **kwargs):
            if "pr" in cmd and "create" in cmd:
                # 先真的执行一次（Fake gh 会写下 PR 记录），再模拟"返回超时"
                outcome = original_run(self, cmd, *args, **kwargs)
                raise subprocess.TimeoutExpired(cmd, timeout=1)
            return original_run(self, cmd, *args, **kwargs)

        with self.env():
            with mock.patch.object(gp.Publisher, "_gh", flaky_gh):
                with mock.patch.object(gp.Publisher, "_run", flaky_run):
                    result = self.publish()
        self.assertEqual(result.status, gp.PR_RECOVERED)
        self.assertEqual(result.pr_number, 7)
        self.assertGreaterEqual(seen["lists"], 2, "超时后必须重新查询对账")

    def test_ac12_timeout_without_pr_reports_failure(self):
        original_run = gp.Publisher._run

        def flaky_run(self, cmd, *args, **kwargs):
            if "pr" in cmd and "create" in cmd:
                raise subprocess.TimeoutExpired(cmd, timeout=1)
            return original_run(self, cmd, *args, **kwargs)

        with self.env():
            with mock.patch.object(gp.Publisher, "_run", flaky_run):
                result = self.publish()
        self.assertEqual(result.status, gp.FAILED_PR)
        self.assertIn("超时", result.detail)


class PushSafetyTests(PublisherFixture):
    """推送安全与不确定结果处理。"""

    def test_ac13_no_force_push_merge_or_deploy_in_source(self):
        for needle in ("--force", "--force-with-lease", "pr merge", "pr close",
                       "release create", "deploy", "merge_pr"):
            self.assertNotIn(needle, PUBLISHER_CODE,
                             f"Publisher 不得包含 {needle!r}（不自动合并/部署/强推）")

    def test_ac13_branch_conflict_refuses_to_overwrite(self):
        sh(["git", "-C", str(self.repo), "push", "-q", "origin",
            f"{self.base_sha}:refs/heads/{self.branch}"])
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_BRANCH_CONFLICT)
        remote_sha = sh(["git", "-C", str(self.remote), "rev-parse",
                         f"refs/heads/{self.branch}"]).stdout.strip()
        self.assertEqual(remote_sha, self.base_sha, "不得覆盖未知远端提交")

    def test_ac12_push_timeout_reconciles_with_remote(self):
        """推送超时但分支已落地：按远端 SHA 对账后继续，而不是盲目重推。"""
        original_git = gp.Publisher._git
        calls = {"push": 0}

        def flaky_git(self, *args, **kwargs):
            if args and args[0] == "push":
                calls["push"] += 1
                outcome = original_git(self, *args, **kwargs)
                # 推送实际已完成，但调用方看到的是超时：必须靠远端对账确认
                raise subprocess.TimeoutExpired(list(args), timeout=1)
            return original_git(self, *args, **kwargs)

        with self.env():
            with mock.patch.object(gp.Publisher, "_git", flaky_git):
                result = self.publish()
        self.assertEqual(result.status, gp.PR_CREATED)
        self.assertEqual(calls["push"], 1, "对账成功时不得重复推送")

    def test_ac12_push_timeout_without_branch_reports_failure(self):
        original_git = gp.Publisher._git

        def flaky_git(self, *args, **kwargs):
            if args and args[0] == "push":
                raise subprocess.TimeoutExpired(list(args), timeout=1)
            return original_git(self, *args, **kwargs)

        with self.env():
            with mock.patch.object(gp.Publisher, "_git", flaky_git):
                result = self.publish()
        self.assertEqual(result.status, gp.FAILED_PUSH)
        self.assertIn("超时", result.detail)


class PublishFailurePathTests(PublisherFixture):
    """推送失败仍走 FAILED_PUSH（远端可达、仅推送被拒）。"""

    def test_push_failure_reported(self):
        original_git = gp.Publisher._git

        def reject_push(self, *args, **kwargs):
            if args and args[0] == "push":
                return subprocess.CompletedProcess(args=list(args), returncode=1,
                                                   stdout="", stderr="remote rejected")
            return original_git(self, *args, **kwargs)

        with self.env():
            with mock.patch.object(gp.Publisher, "_git", reject_push):
                result = self.publish()
        self.assertEqual(result.status, gp.FAILED_PUSH)


class HeadRepositoryTests(PublisherFixture):
    """SS-V01-005-R1：PR 来源仓库身份校验。"""

    def test_ac_r2_correct_repo_draft_pr_reused(self):
        self.write_pr()
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.PR_EXISTS)
        self.assert_no_remote_branch()

    def test_ac_r1_fork_pr_not_reused(self):
        self.write_pr(headRepositoryOwner={"login": "someone-else"},
                      isCrossRepository=True)
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assertIn("跨", result.detail)
        self.assert_no_remote_branch()

    def test_ac_r1_same_name_other_owner_not_reused(self):
        """同名仓库但 Owner 不同（isCrossRepository 也报 False）→ 仍然拒绝。"""
        self.write_pr(headRepositoryOwner={"login": "someone-else"})
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assertIn("来源", result.detail)
        self.assert_no_remote_branch()

    def test_ac_r3_missing_head_repository_rejected(self):
        self.write_pr()
        payload = json.loads((self.gh_state / "pr.json").read_text(encoding="utf-8"))
        del payload["headRepository"]
        (self.gh_state / "pr.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assertIn("不完整", result.detail)

    def test_ac_r3_missing_head_owner_rejected(self):
        self.write_pr()
        payload = json.loads((self.gh_state / "pr.json").read_text(encoding="utf-8"))
        del payload["headRepositoryOwner"]
        (self.gh_state / "pr.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)

    def test_ac_r3_missing_is_cross_repository_rejected(self):
        self.write_pr()
        payload = json.loads((self.gh_state / "pr.json").read_text(encoding="utf-8"))
        del payload["isCrossRepository"]
        (self.gh_state / "pr.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)

    def test_ac_r3_non_object_head_repository_rejected(self):
        self.write_pr(headRepository="soloship-demo")
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)

    def test_ac_r4_truncated_query_result_rejected(self):
        """命中数达到查询上限：无法确认'未查到'就是'不存在' → 拒绝。"""
        base = self.write_pr()
        original_gh = gp.Publisher._gh

        def capped_gh(self, *args, **kwargs):
            if args[:2] == ("pr", "list"):
                items = [dict(base, number=i, url=f"https://github.com/{REPO}/pull/{i}")
                         for i in range(gp.Publisher.PR_QUERY_LIMIT)]
                return subprocess.CompletedProcess(args=list(args), returncode=0,
                                                   stdout=json.dumps(items), stderr="")
            return original_gh(self, *args, **kwargs)

        with self.env():
            with mock.patch.object(gp.Publisher, "_gh", capped_gh):
                result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_PR_STATE)
        self.assertIn("上限", result.detail)
        self.assert_no_remote_branch()


if __name__ == "__main__":
    unittest.main()
