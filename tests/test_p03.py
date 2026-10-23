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

import github_publisher as gp  # noqa: E402
import p02_bridge as p02  # noqa: E402

FAKE_GH = str(ROOT / "tests" / "fake_gh.py")
REPO = "fixture/soloship-demo"


def sh(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check,
                          encoding="utf-8", errors="replace")


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p03-"))
        self.remote = self.tmp / "fixture" / "soloship-demo.git"
        self.remote.parent.mkdir(parents=True, exist_ok=True)
        sh(["git", "init", "--bare", "-q", str(self.remote)])
        self.repo = self.tmp / "demo" / "soloship-demo"
        self.repo.mkdir(parents=True)
        sh(["git", "-C", str(self.repo), "init", "-q", "-b", "main"])
        sh(["git", "-C", str(self.repo), "config", "user.name", "t"])
        sh(["git", "-C", str(self.repo), "config", "user.email", "t@t"])
        (self.repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        sh(["git", "-C", str(self.repo), "add", "-A"])
        sh(["git", "-C", str(self.repo), "commit", "-q", "-m", "base"])
        self.base_sha = sh(["git", "-C", str(self.repo), "rev-parse", "HEAD"]).stdout.strip()
        sh(["git", "-C", str(self.repo), "remote", "add", "origin", str(self.remote)])
        sh(["git", "-C", str(self.repo), "push", "-q", "origin", "main"])

        # candidate worktree on a task branch
        self.worktree = self.tmp / "wt"
        sh(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "task-branch", str(self.worktree)])
        (self.worktree / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        sh(["git", "-C", str(self.worktree), "add", "-A"])
        sh(["git", "-C", str(self.worktree), "commit", "-q", "-m", "feature"])
        self.candidate = sh(["git", "-C", str(self.worktree), "rev-parse", "HEAD"]).stdout.strip()

        self.gh_state = self.tmp / "gh-state"
        self.gh_log = self.tmp / "gh.log"
        self.config = {
            "github": {
                "enabled": True, "mode": "mock", "remote": "origin",
                "allowed_repos": [REPO], "allowed_base_branches": ["main"],
                "branch_prefix": "soloship/", "draft": True,
                "target_repo": REPO, "target_branch": "main",
            }
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def publisher(self, config=None, gh_cmd=None):
        return gp.Publisher(self.tmp, config or self.config,
                            gh_cmd=gh_cmd or [sys.executable, FAKE_GH],
                            journal_dir=self.tmp / ".control" / "p03" / "publish")

    def job(self, **overrides):
        payload = {
            "job_id": "p03_job_0001", "request_id": "p03_task_0001", "status": "P02_READY_FOR_HUMAN",
            "head_commit": self.candidate, "base_commit": self.base_sha,
            "tests_ok": True, "review_status": "REVIEW_APPROVED", "fix_rounds": 0,
            "changed_paths": ["app.py"], "execution_mode": "WINDOWS_TEST_MODE",
            "sandbox_status": "SANDBOX_UNENFORCED", "test_isolation": "TEST_ISOLATION_PARTIAL",
            "isolation_claim": "NONE", "title": "P03 test",
        }
        payload.update(overrides)
        return payload

    def env(self, **extra):
        base = {"FAKE_GH_STATE": str(self.gh_state), "FAKE_GH_LOG": str(self.gh_log)}
        base.update(extra)
        return mock.patch.dict(os.environ, base)

    def publish(self, **kwargs):
        kwargs.setdefault("worktree", self.worktree)
        kwargs.setdefault("candidate_sha", self.candidate)
        kwargs.setdefault("repo", REPO)
        kwargs.setdefault("base_branch", "main")
        kwargs.setdefault("expected_base_sha", self.base_sha)
        job = kwargs.pop("job", self.job())
        return self.publisher(kwargs.pop("config", None), kwargs.pop("gh_cmd", None)).publish(job, **kwargs)

    # -- happy path --------------------------------------------------------
    def test_draft_pr_created(self):
        with self.env():
            result = self.publish()
        self.assertEqual(result.status, gp.PR_CREATED)
        self.assertEqual(result.branch, "soloship/p03_task_0001")
        self.assertTrue(result.pr_url.endswith("/pull/7"))
        remote_sha = sh(["git", "-C", str(self.remote), "rev-parse", "refs/heads/soloship/p03_task_0001"]).stdout.strip()
        self.assertEqual(remote_sha, self.candidate)

    def test_pr_number_recorded_on_create(self):
        # P05-A：gh pr create 的 stdout 只有 URL，编号也必须落进发布日志
        with self.env():
            result = self.publish()

        self.assertEqual(result.pr_number, 7)
        journal = json.loads((self.tmp / ".control" / "p03" / "publish"
                              / "p03_job_0001.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["pr_number"], 7)
        self.assertEqual(journal["pr_url"], result.pr_url)

    def test_pr_number_from_url_helper(self):
        self.assertEqual(gp.pr_number_from_url("https://github.com/o/r/pull/42"), 42)
        self.assertEqual(gp.pr_number_from_url("https://github.com/o/r/pull/42/"), 42)
        self.assertIsNone(gp.pr_number_from_url("https://github.com/o/r"))
        self.assertIsNone(gp.pr_number_from_url(None))

    def test_disabled_skips(self):
        cfg = json.loads(json.dumps(self.config))
        cfg["github"]["enabled"] = False
        with self.env():
            result = self.publish(config=cfg)
        self.assertEqual(result.status, gp.SKIPPED_DISABLED)

    # -- failure matrix ----------------------------------------------------
    def test_gh_missing(self):
        result = self.publish(gh_cmd=["definitely-not-gh"])
        self.assertEqual(result.status, gp.FAILED_CLI)

    def test_gh_not_logged_in(self):
        with self.env(FAKE_GH_MODE="noauth"):
            result = self.publish()
        self.assertEqual(result.status, gp.FAILED_AUTH)

    def test_repo_not_whitelisted(self):
        result = self.publish(repo="someone/other-repo")
        self.assertEqual(result.status, gp.BLOCKED_REPO)

    def test_base_branch_not_whitelisted(self):
        result = self.publish(base_branch="develop")
        self.assertEqual(result.status, gp.BLOCKED_REPO)

    def test_remote_mismatch(self):
        sh(["git", "-C", str(self.worktree), "remote", "set-url", "origin", str(self.tmp / "other" / "different.git")])
        result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_REMOTE)

    def test_base_branch_moved(self):
        result = self.publish(expected_base_sha="0" * 40)
        self.assertEqual(result.status, gp.BLOCKED_BASE_MOVED)

    def test_push_failure(self):
        broken = self.tmp / "missing" / "fixture" / "soloship-demo.git"
        sh(["git", "-C", str(self.worktree), "remote", "set-url", "origin", str(broken)])
        result = self.publish()
        self.assertEqual(result.status, gp.FAILED_PUSH)

    def test_pr_create_failure(self):
        with self.env(FAKE_GH_MODE="create_fail"):
            result = self.publish()
        self.assertEqual(result.status, gp.FAILED_PR)

    # -- gate --------------------------------------------------------------
    def test_gate_blocks_red_tests(self):
        result = self.publish(job=self.job(tests_ok=False))
        self.assertEqual(result.status, gp.BLOCKED_GATE)

    def test_gate_blocks_unapproved_review(self):
        result = self.publish(job=self.job(review_status="REVIEW_CHANGES_REQUESTED"))
        self.assertEqual(result.status, gp.BLOCKED_GATE)

    def test_gate_blocks_sha_mismatch(self):
        result = self.publish(job=self.job(head_commit="f" * 40))
        self.assertEqual(result.status, gp.BLOCKED_GATE)

    def test_cancelled_stops_publish(self):
        result = self.publish(cancelled=True)
        self.assertEqual(result.status, gp.ABORTED)
        proc = sh(["git", "-C", str(self.remote), "show-ref", "--heads"], check=False)
        self.assertNotIn("soloship/", proc.stdout)


    def test_requires_env_token_when_configured(self):
        cfg = json.loads(json.dumps(self.config))
        cfg["github"]["require_env_token"] = True
        cfg["github"]["token_env"] = "GH_TOKEN"
        clean = {"PATH": os.environ.get("PATH", ""), "FAKE_GH_STATE": str(self.gh_state)}
        with mock.patch.dict(os.environ, clean, clear=True):
            result = self.publish(config=cfg)
        self.assertEqual(result.status, gp.FAILED_AUTH)
        self.assertIn("GH_TOKEN", result.detail)

    def test_credential_helper_does_not_embed_token(self):
        cfg = json.loads(json.dumps(self.config))
        cfg["github"]["require_env_token"] = True
        cfg["github"]["token_env"] = "GH_TOKEN"
        publisher = self.publisher(cfg)
        with mock.patch.dict(os.environ, {"GH_TOKEN": "sekret-token-value"}):
            args = publisher._credential_args()
        joined = " ".join(args)
        self.assertNotIn("sekret-token-value", joined)
        self.assertIn("GH_TOKEN", joined)
        self.assertIn("credential.helper=", joined)

    # -- idempotency / recovery -------------------------------------------
    def test_second_run_reuses_existing_pr(self):
        with self.env():
            first = self.publish()
            second = self.publish()
        self.assertEqual(first.status, gp.PR_CREATED)
        self.assertEqual(second.status, gp.PR_EXISTS)
        self.assertEqual(first.pr_url, second.pr_url)

    def test_resume_after_crash_following_push(self):
        with self.env():
            with self.assertRaises(RuntimeError):
                self.publish(crash_after="push")
            journal = self.publisher().load_journal("p03_job_0001")
            self.assertEqual(journal["stage"], "pushing")
            result = self.publish()
        self.assertEqual(result.status, gp.PR_CREATED)

    def test_recover_when_pr_created_but_not_recorded(self):
        with self.env():
            with self.assertRaises(RuntimeError):
                self.publish(crash_after="pr_create_before_record")
            result = self.publish()
        self.assertIn(result.status, (gp.PR_EXISTS, gp.PR_RECOVERED))
        self.assertTrue(result.pr_url)

    def test_branch_conflict_refuses_force_push(self):
        # Remote branch already carries a different commit.
        sh(["git", "-C", str(self.repo), "push", "-q", "origin",
            f"{self.base_sha}:refs/heads/soloship/p03_task_0001"])
        result = self.publish()
        self.assertEqual(result.status, gp.BLOCKED_BRANCH_CONFLICT)
        remote_sha = sh(["git", "-C", str(self.remote), "rev-parse",
                         "refs/heads/soloship/p03_task_0001"]).stdout.strip()
        self.assertEqual(remote_sha, self.base_sha)


class BridgePublishIntegrationTests(unittest.TestCase):
    """P02 READY must record a publish decision without breaking the job."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p03-int-"))
        cfg = json.loads((ROOT / "config" / "bridge.json").read_text(encoding="utf-8"))
        cfg["github"]["enabled"] = False
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "bridge.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.repo = self.tmp / "demo" / "soloship-demo"
        self.repo.mkdir(parents=True)
        sh(["git", "-C", str(self.repo), "init", "-q"])
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / "tests" / "test_app.py").write_text(
            "import unittest\n\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n", encoding="utf-8")
        sh(["git", "-C", str(self.repo), "add", "-A"])
        sh(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-q", "-m", "base"])
        self.bridge = p02.P02Bridge(self.tmp, codex_cmd=[sys.executable, str(ROOT / "tests" / "fake_codex.py")],
                                    sandbox_status="SANDBOX_UNENFORCED")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ready_job_records_publish_gate(self):
        task = {
            "schema_version": "1.0", "request_id": "p03_int_0001", "project_id": "demo-app",
            "title": "publish gate", "goal": "g", "requested_paths": ["src/**"],
            "constraints": ["c"], "non_goals": ["n"],
            "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
            "workspace": {"repo": "demo/soloship-demo", "base_ref": "HEAD", "allow_paths": ["src", "tests"]},
            "verify": {"test_command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       "timeout_seconds": 120},
            "codex": {"sandbox": "workspace-write", "timeout_seconds": 300},
        }
        with mock.patch.dict(os.environ, {"FAKE_BUILD_MODE": "strict", "FAKE_REVIEW_MODE": "auto"}):
            self.bridge.inbox.mkdir(parents=True, exist_ok=True)
            (self.bridge.inbox / "p03_int_0001.ready.json").write_text(
                json.dumps(task, ensure_ascii=False), encoding="utf-8")
            self.bridge.poll()
        receipt = json.loads((self.bridge.receipts / "demo-app__p03_int_0001.receipt.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "P02_READY_FOR_HUMAN")
        self.assertEqual(receipt["publish"]["status"], "PUBLISH_SKIPPED_DISABLED")
        self.assertEqual(receipt["sandbox_status"], "SANDBOX_UNENFORCED")


if __name__ == "__main__":
    unittest.main()
