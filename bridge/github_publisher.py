#!/usr/bin/env python3
"""SoloShip P03: GitHub draft-PR publisher.

Publishing is owned by the Bridge, never by the Builder prompt. The publisher
only accepts a candidate that already passed tests and independent review, and
it never force-pushes, never merges, never deploys.

Every step is journaled to `.control/p03/publish/<job_id>.json` so an
interrupted run can resume instead of creating a second PR.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# terminal / intermediate statuses
SKIPPED_DISABLED = "PUBLISH_SKIPPED_DISABLED"
BLOCKED_GATE = "PUBLISH_BLOCKED_GATE"
BLOCKED_REPO = "PUBLISH_BLOCKED_REPO"
BLOCKED_REMOTE = "PUBLISH_BLOCKED_REMOTE"
BLOCKED_BASE_MOVED = "PUBLISH_BLOCKED_BASE_MOVED"
BLOCKED_BRANCH_CONFLICT = "PUBLISH_BLOCKED_BRANCH_CONFLICT"
FAILED_CLI = "PUBLISH_FAILED_GH_CLI"
FAILED_AUTH = "PUBLISH_FAILED_AUTH"
FAILED_PUSH = "PUBLISH_FAILED_PUSH"
FAILED_PR = "PUBLISH_FAILED_PR"
ABORTED = "PUBLISH_ABORTED"
PR_CREATED = "PUBLISH_PR_CREATED"
PR_EXISTS = "PUBLISH_PR_EXISTS"
PR_RECOVERED = "PUBLISH_PR_RECOVERED"
PUSHED = "PUBLISH_PUSHED"

# A Draft PR only exists in these terminal states.
PR_SUCCESS_STATUSES = (PR_CREATED, PR_EXISTS, PR_RECOVERED)

AUTH_MARKERS = ("not logged in", "authentication", "gh auth login", "http 401", "bad credentials")


def pr_number_from_url(url: str | None) -> int | None:
    """pr create 的 stdout 只有 URL；编号必须从 URL 取，不能留空。"""
    match = re.search(r"/pull/(\d+)", str(url or ""))
    return int(match.group(1)) if match else None


@dataclass
class PublishResult:
    status: str
    job_id: str
    branch: str
    candidate_sha: str
    repo: str
    base_branch: str
    pr_url: str | None = None
    pr_number: int | None = None
    stage: str = "init"
    detail: str = ""
    events: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "job_id": self.job_id,
            "branch": self.branch,
            "candidate_sha": self.candidate_sha,
            "repo": self.repo,
            "base_branch": self.base_branch,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "stage": self.stage,
            "detail": self.detail,
            "events": self.events,
        }


class Publisher:
    def __init__(self, root: Path, config: dict, *, gh_cmd: list[str] | None = None,
                 journal_dir: Path | None = None):
        gh = (config or {}).get("github") or {}
        self.cfg = gh
        self.root = Path(root).resolve()
        self.enabled = bool(gh.get("enabled"))
        self.mode = gh.get("mode", "mock")
        self.remote = gh.get("remote", "origin")
        self.allowed_repos = list(gh.get("allowed_repos") or [])
        self.allowed_bases = list(gh.get("allowed_base_branches") or ["main"])
        self.branch_prefix = gh.get("branch_prefix", "soloship/")
        self.draft = bool(gh.get("draft", True))
        self.timeout = int(gh.get("timeout_seconds", 180))
        self.require_env_token = bool(gh.get("require_env_token", False))
        self.token_env = gh.get("token_env", "GH_TOKEN")
        self.gh_cmd = gh_cmd or [gh.get("cli", "gh")]
        self.journal_dir = Path(journal_dir or self.root / ".control" / "p03" / "publish")
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_root(cls, root: Path, **kwargs) -> "Publisher":
        import security

        return cls(root, security.bridge_config(root), **kwargs)

    # ------------------------------------------------------------ plumbing
    def _run(self, cmd: list[str], cwd: Path | None = None, stdin_text: str | None = None):
        return subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=self.timeout,
            input=stdin_text, stdin=None if stdin_text is not None else subprocess.DEVNULL,
        )

    def _credential_args(self) -> list[str]:
        """Pass the PAT through an env var; never place the token itself in argv."""
        if not self.require_env_token:
            return []
        helper = '!f() { echo username=x-access-token; echo "password=$' + self.token_env + '"; }; f'
        return ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]

    def _token_missing(self) -> str | None:
        if not self.require_env_token:
            return None
        if os.environ.get(self.token_env):
            return None
        return (f"缺少受限凭据环境变量 {self.token_env}；"
                "拒绝使用已缓存的账号级 Token 进行发布")

    def _git(self, *args: str, cwd: Path):
        return self._run(["git", *self._credential_args(), "-C", str(cwd), *args])

    def _gh(self, *args: str):
        return self._run([*self.gh_cmd, *args])

    # ------------------------------------------------------------- journal
    def _journal_path(self, job_id: str) -> Path:
        return self.journal_dir / f"{job_id}.json"

    def load_journal(self, job_id: str) -> dict:
        path = self._journal_path(job_id)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _record(self, result: PublishResult, stage: str, **extra) -> None:
        result.stage = stage
        result.events.append({"stage": stage, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                              "status": result.status, **extra})
        payload = self.load_journal(result.job_id)
        payload.update(result.to_dict())
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self._journal_path(result.job_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # -------------------------------------------------------------- checks
    def _gh_ready(self) -> tuple[bool, str]:
        try:
            ver = self._gh("--version")
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"gh 不可用: {type(exc).__name__}"
        if ver.returncode != 0:
            return False, f"gh --version 失败: {ver.stderr.strip()[:200]}"
        auth = self._gh("auth", "status")
        text = f"{auth.stdout}\n{auth.stderr}".lower()
        if auth.returncode != 0 or "logged in" not in text:
            return False, f"gh 未登录: {text.strip()[:200]}"
        return True, ""

    def _remote_matches(self, worktree: Path, repo: str) -> tuple[bool, str]:
        proc = self._git("remote", "get-url", self.remote, cwd=worktree)
        url = proc.stdout.strip()
        if proc.returncode != 0 or not url:
            return False, f"未找到 remote {self.remote}"
        normalized = url.replace(":", "/").replace("\\", "/").rstrip("/")
        normalized = normalized[:-4] if normalized.endswith(".git") else normalized
        if not normalized.endswith(repo):
            return False, f"remote 指向 {url}，与配置仓库 {repo} 不一致"
        return True, url

    def _remote_branch_sha(self, worktree: Path, branch: str) -> str | None:
        proc = self._git("ls-remote", self.remote, f"refs/heads/{branch}", cwd=worktree)
        if proc.returncode != 0:
            return None
        line = proc.stdout.strip().splitlines()
        return line[0].split()[0] if line else None

    # -------------------------------------------------------------- publish
    def publish(self, job: dict, *, worktree: Path, candidate_sha: str, repo: str,
                base_branch: str, expected_base_sha: str | None = None,
                title: str = "", body: str = "", cancelled: bool = False,
                crash_after: str | None = None) -> PublishResult:
        job_id = job.get("job_id", "unknown")
        task_id = job.get("request_id", job_id)
        branch = f"{self.branch_prefix}{task_id}"
        result = PublishResult(status=SKIPPED_DISABLED, job_id=job_id, branch=branch,
                               candidate_sha=candidate_sha, repo=repo, base_branch=base_branch)

        if not self.enabled:
            result.detail = "GitHub 发布未启用（阶段 D 待授权）"
            self._record(result, "disabled")
            return result

        if cancelled:
            result.status = ABORTED
            result.detail = "任务已取消，停止发布"
            self._record(result, "cancelled")
            return result

        # 1) gate: identical candidate for tests + review
        gate_errors = []
        if job.get("status") != "P02_READY_FOR_HUMAN":
            gate_errors.append(f"任务状态不是 READY: {job.get('status')}")
        if job.get("tests_ok") is not True:
            gate_errors.append("测试未通过")
        if job.get("review_status") != "REVIEW_APPROVED":
            gate_errors.append(f"审查未通过: {job.get('review_status')}")
        if job.get("head_commit") != candidate_sha:
            gate_errors.append("candidate SHA 与任务记录不一致")
        if gate_errors:
            result.status = BLOCKED_GATE
            result.detail = "; ".join(gate_errors)
            self._record(result, "gate")
            return result

        # 2) whitelist
        if repo not in self.allowed_repos:
            result.status = BLOCKED_REPO
            result.detail = f"仓库不在白名单: {repo}"
            self._record(result, "whitelist")
            return result
        if base_branch not in self.allowed_bases:
            result.status = BLOCKED_REPO
            result.detail = f"目标分支不在白名单: {base_branch}"
            self._record(result, "whitelist")
            return result

        # 3) credential + CLI
        missing = self._token_missing()
        if missing:
            result.status = FAILED_AUTH
            result.detail = missing
            self._record(result, "credential")
            return result

        ready, why = self._gh_ready()
        if not ready:
            result.status = FAILED_AUTH if "登录" in why else FAILED_CLI
            result.detail = why
            self._record(result, "cli")
            return result

        # 4) remote consistency
        ok, info = self._remote_matches(worktree, repo)
        if not ok:
            result.status = BLOCKED_REMOTE
            result.detail = info
            self._record(result, "remote")
            return result
        self._record(result, "remote_ok", remote=info)

        # 5) base branch must not have moved
        remote_base = self._remote_branch_sha(worktree, base_branch)
        if expected_base_sha and remote_base and remote_base != expected_base_sha:
            result.status = BLOCKED_BASE_MOVED
            result.detail = f"目标分支已变化: remote={remote_base[:12]} expected={expected_base_sha[:12]}"
            self._record(result, "base_check")
            return result
        self._record(result, "base_ok", remote_base=remote_base)

        # 6) resume: already pushed?
        journal = self.load_journal(job_id)
        remote_branch = self._remote_branch_sha(worktree, branch)
        if remote_branch and remote_branch != candidate_sha:
            result.status = BLOCKED_BRANCH_CONFLICT
            result.detail = (f"远端分支 {branch} 已存在且指向 {remote_branch[:12]}，"
                             f"与 candidate {candidate_sha[:12]} 不同；拒绝强制推送")
            self._record(result, "branch_conflict")
            return result

        if not remote_branch:
            result.status = PUSHED
            self._record(result, "pushing")
            if crash_after == "push":
                raise RuntimeError("simulated crash after push")
            push = self._git("push", self.remote, f"HEAD:refs/heads/{branch}", cwd=worktree)
            if push.returncode != 0:
                result.status = FAILED_PUSH
                result.detail = (push.stderr or push.stdout).strip()[-500:]
                self._record(result, "push_failed")
                return result
            remote_branch = self._remote_branch_sha(worktree, branch)
            if remote_branch != candidate_sha:
                result.status = FAILED_PUSH
                result.detail = f"推送后远端 SHA 不匹配: {remote_branch}"
                self._record(result, "push_verify_failed")
                return result
            self._record(result, "pushed", remote_sha=remote_branch)

        # 7) PR: idempotent by head branch
        existing = self._find_pr(repo, branch)
        if existing:
            result.status = PR_RECOVERED if journal.get("stage") in ("pushed", "pr_creating") else PR_EXISTS
            result.pr_url = existing.get("url")
            result.pr_number = existing.get("number")
            result.detail = "已有 PR，复用，不重复创建"
            self._record(result, "pr_exists", pr=existing.get("url"))
            return result

        self._record(result, "pr_creating")
        pr_body = body or self._default_body(job, candidate_sha, branch)
        create = self._run(
            [*self.gh_cmd, "pr", "create", "--repo", repo, "--head", branch,
             "--base", base_branch, "--title", title or f"[SoloShip] {task_id}",
             "--body-file", "-"] + (["--draft"] if self.draft else []),
            stdin_text=pr_body,
        )
        text = f"{create.stdout}\n{create.stderr}".strip()
        if create.returncode != 0:
            lowered = text.lower()
            if any(marker in lowered for marker in AUTH_MARKERS):
                result.status = FAILED_AUTH
            elif "already exists" in lowered:
                found = self._find_pr(repo, branch)
                result.status = PR_RECOVERED if found else FAILED_PR
                result.pr_url = (found or {}).get("url")
                result.pr_number = (found or {}).get("number")
            else:
                result.status = FAILED_PR
            result.detail = text[-500:]
            self._record(result, "pr_failed")
            return result

        if crash_after == "pr_create_before_record":
            raise RuntimeError("simulated crash after PR creation, before journal")

        url = next((line.strip() for line in create.stdout.splitlines()
                    if line.strip().startswith("http")), "")
        result.status = PR_CREATED
        result.pr_url = url or None
        result.pr_number = pr_number_from_url(url)
        if result.pr_number is None:
            # stdout 没给出编号时回查一次，避免发布日志"有 URL 无编号"
            found = self._find_pr(repo, branch)
            if found:
                result.pr_url = result.pr_url or found.get("url")
                result.pr_number = found.get("number")
        result.detail = "Draft PR 已创建，等待人工验收"
        self._record(result, "pr_created", pr=result.pr_url)
        return result

    # ---------------------------------------------------------------- helpers
    def _find_pr(self, repo: str, branch: str) -> dict | None:
        proc = self._gh("pr", "list", "--repo", repo, "--head", branch, "--state", "all",
                        "--json", "number,url,state,isDraft,headRefOid")
        if proc.returncode != 0:
            return None
        try:
            items = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return None
        for item in items:
            if isinstance(item, dict) and item.get("url"):
                return item
        return None

    def _default_body(self, job: dict, candidate_sha: str, branch: str) -> str:
        return f"""## SoloShip 自动交付（Draft）

- Task: `{job.get('request_id')}` / Job `{job.get('job_id')}`
- Candidate SHA: `{candidate_sha}`
- Branch: `{branch}`
- Base SHA: `{job.get('base_commit')}`
- 修改文件: {', '.join(job.get('changed_paths') or []) or '见 diff'}
- 测试: {'通过' if job.get('tests_ok') else '未通过'}（由 Bridge 独立执行）
- 审查: {job.get('review_status')}（独立会话）
- 修复轮次: {job.get('fix_rounds', 0)}

### 安全状态（不因发布而改变）

- Execution mode: `{job.get('execution_mode')}`
- Sandbox status: `{job.get('sandbox_status')}`
- Test isolation: `{job.get('test_isolation')}`
- Isolation claim: `{job.get('isolation_claim', 'NONE')}`

> 本 PR 为 Draft，等待人工验收；未自动合并、未自动部署。
"""
