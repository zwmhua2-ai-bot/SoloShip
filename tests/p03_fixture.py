#!/usr/bin/env python3
"""P03 发布测试的共享夹具（SS-V01-005 引入）。

只提供**测试环境**：临时裸远端 + 本地仓库 + 任务 worktree + Fake gh。
所有断言仍写在各自的测试类里，这里不放任何验收逻辑。

注意：Fake gh 永不联网；本夹具也不对真实 GitHub 执行任何写操作。
"""
from __future__ import annotations

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

FAKE_GH = str(ROOT / "tests" / "fake_gh.py")
REPO = "fixture/soloship-demo"


def sh(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check,
                          encoding="utf-8", errors="replace")


class PublisherFixture(unittest.TestCase):
    """临时远端 + worktree + Fake gh；不触碰真实 GitHub。"""

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
        base = {"FAKE_GH_STATE": str(self.gh_state), "FAKE_GH_LOG": str(self.gh_log),
                "FAKE_GH_CANDIDATE": self.candidate}
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

    # -- helpers for PR fixtures ------------------------------------------
    @property
    def branch(self) -> str:
        return "soloship/p03_task_0001"

    def write_pr(self, **overrides) -> dict:
        """直接在 Fake gh 的状态目录里放一条 PR 记录（模拟远端已有 PR）。"""
        owner, _, name = REPO.partition("/")
        pr = {
            "number": 7, "url": f"https://github.com/{REPO}/pull/7",
            "state": "OPEN", "isDraft": True, "headRefOid": self.candidate,
            "headRefName": self.branch, "baseRefName": "main",
            "headRepository": {"name": name},
            "headRepositoryOwner": {"login": owner},
            "isCrossRepository": False,
        }
        pr.update(overrides)
        self.gh_state.mkdir(parents=True, exist_ok=True)
        (self.gh_state / "pr.json").write_text(json.dumps(pr), encoding="utf-8")
        return pr

    def assert_no_remote_branch(self):
        """被拒绝/未推送时，远端不得出现任务分支。"""
        refs = sh(["git", "-C", str(self.remote), "show-ref", "--heads"], check=False).stdout
        self.assertNotIn("soloship/", refs, "未推送或被拒时不得产生远端分支")
