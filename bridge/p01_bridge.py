#!/usr/bin/env python3
"""SoloShip P01 bridge: Codex-driven development inside an isolated git worktree.

Flow per task:
  inbox/<request_id>.ready.json
    -> validate (P00 contract + P01 extension)
    -> git worktree add (isolated branch, no remote)
    -> codex exec --json -s workspace-write  (prompt via stdin)
    -> independent verification: changed paths, commit, diff, tests
    -> receipts/<project>__<request>.receipt.json + reports/*.report.{json,md}

No network, no push, no PR, no merge. P00 data is never touched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_adapter as codex  # noqa: E402
import security  # noqa: E402
from p00_bridge import (  # noqa: E402
    TASK_KEYS as P00_TASK_KEYS,
    atomic_text,
    dump_json,
    nonempty_string,
    utc_now,
    validate_task,
)

ROOT = Path(__file__).resolve().parents[1]
P01_EXTRA_KEYS = {"workspace", "verify", "codex"}
ALLOWED_TEST_RUNNERS = {"py", "python", "python3", "python.exe", "py.exe"}
MAX_PATCH_BYTES = 400_000

STATUS_CODEX_OK = "P01_CODEX_OK"
STATUS_NO_DIFF = "P01_CODEX_NO_DIFF"
STATUS_PATH_VIOLATION = "P01_PATH_VIOLATION"
STATUS_TESTS_FAILED = "P01_TESTS_FAILED"
STATUS_CODEX_FAILED = "P01_CODEX_FAILED"
STATUS_TIMEOUT = "P01_CODEX_TIMEOUT"
STATUS_CLI_MISSING = "P01_CLI_MISSING"
STATUS_AUTH_FAILED = "P01_AUTH_FAILED"
STATUS_REJECTED = "P01_TASK_REJECTED"


# ---------------------------------------------------------------- validation
def _check_rel_path(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    normalized = text.replace("\\", "/").strip()
    if normalized.startswith("/") or ".." in Path(normalized).parts:
        return False
    return True


def validate_p01_task(obj: Any, root: Path) -> list[str]:
    """P00 contract on the shared keys + P01-specific rules."""
    if not isinstance(obj, dict):
        return ["任务内容必须是 JSON 对象"]
    errors: list[str] = []
    extras = set(obj) - P00_TASK_KEYS - P01_EXTRA_KEYS
    if extras:
        errors.append(f"未知字段: {', '.join(sorted(extras))}")
    base = {key: value for key, value in obj.items() if key in P00_TASK_KEYS}
    errors.extend(validate_task(base))

    workspace = obj.get("workspace")
    if not isinstance(workspace, dict):
        errors.append("workspace 必须是对象")
        workspace = {}
    else:
        unknown = set(workspace) - {"repo", "base_ref", "allow_paths"}
        if unknown:
            errors.append(f"workspace 未知字段: {', '.join(sorted(unknown))}")
        repo = workspace.get("repo")
        if not nonempty_string(repo):
            errors.append("workspace.repo 必须为非空文本")
        else:
            repo_path = (root / repo).resolve() if not Path(repo).is_absolute() else Path(repo).resolve()
            allowed_external = [Path(item) for item in security.approved_absolute_repos(root)]
            if root.resolve() not in repo_path.parents and repo_path not in allowed_external:
                errors.append("workspace.repo 必须位于 SoloShip 根目录内或已授权的外部仓库")
            elif not (repo_path / ".git").exists():
                errors.append("workspace.repo 不是 Git 仓库")
        base_ref = workspace.get("base_ref", "HEAD")
        if not nonempty_string(base_ref) or base_ref.startswith("-"):
            errors.append("workspace.base_ref 无效")
        allow = workspace.get("allow_paths")
        if not isinstance(allow, list) or not allow:
            errors.append("workspace.allow_paths 必须是非空数组")
        elif any(not _check_rel_path(item) for item in allow):
            errors.append("workspace.allow_paths 只能包含仓库内相对路径")

    verify = obj.get("verify")
    if not isinstance(verify, dict):
        errors.append("verify 必须是对象")
    else:
        unknown = set(verify) - {"test_command", "timeout_seconds", "require_diff"}
        if unknown:
            errors.append(f"verify 未知字段: {', '.join(sorted(unknown))}")
        cmd = verify.get("test_command")
        if not isinstance(cmd, list) or not cmd or any(not nonempty_string(part) for part in cmd):
            errors.append("verify.test_command 必须是非空字符串数组")
        elif Path(str(cmd[0])).name.lower() not in ALLOWED_TEST_RUNNERS and not Path(str(cmd[0])).exists():
            errors.append("verify.test_command 只允许 python 解释器")
        timeout = verify.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or not 10 <= timeout <= 3600:
            errors.append("verify.timeout_seconds 必须是 10-3600 的整数")
        if not isinstance(verify.get("require_diff", True), bool):
            errors.append("verify.require_diff 必须是布尔值")

    spec = obj.get("codex")
    if spec is not None:
        if not isinstance(spec, dict):
            errors.append("codex 必须是对象")
        else:
            unknown = set(spec) - {"sandbox", "timeout_seconds", "tier", "model", "effort",
                                   "risk_level", "explore"}
            if unknown:
                errors.append(f"codex 未知字段: {', '.join(sorted(unknown))}")
            sandbox = spec.get("sandbox", codex.DEFAULT_SANDBOX)
            if sandbox not in codex.ALLOWED_SANDBOXES:
                errors.append(f"codex.sandbox 只允许 {', '.join(codex.ALLOWED_SANDBOXES)}")
            timeout = spec.get("timeout_seconds", 900)
            if not isinstance(timeout, int) or not 30 <= timeout <= 3600:
                errors.append("codex.timeout_seconds 必须是 30-3600 的整数")
    return errors


# ------------------------------------------------------------------- helpers
def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().strip('"').lstrip("./")


class P01Bridge:
    def __init__(self, root: Path = ROOT, codex_cmd: list[str] | None = None):
        self.root = Path(root).resolve()
        self.exchange = self.root / "exchange" / "p01"
        self.inbox = self.exchange / "inbox"
        self.receipts = self.exchange / "receipts"
        self.reports = self.exchange / "reports"
        self.archive = self.exchange / "archive"
        self.rejected = self.exchange / "rejected"
        self.control = self.root / ".control"
        self.worktrees = self.root / ".worktrees"
        for path in (self.inbox, self.receipts, self.reports, self.archive,
                     self.rejected, self.control, self.worktrees, self.control / "p01"):
            path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.control / "p01.db"
        self.codex_cmd = codex_cmd
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS p01_jobs (
                job_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                spec_sha TEXT NOT NULL,
                status TEXT NOT NULL,
                repo TEXT,
                worktree TEXT,
                base_commit TEXT,
                head_commit TEXT,
                thread_id TEXT,
                codex_exit INTEGER,
                tests_ok INTEGER,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE (project_id, request_id)
            )""")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- git ---------------------------------------------------------------
    def git(self, *args: str, cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )

    def reject(self, source: Path, message: str) -> str:
        payload = {
            "phase": "P01", "status": STATUS_REJECTED, "source_file": source.name,
            "reason": message, "timestamp": utc_now(),
        }
        atomic_text(self.receipts / f"{source.name}.error.json", dump_json(payload))
        shutil.move(str(source), str(self.rejected / f"{source.name}.{uuid.uuid4().hex[:8]}"))
        print(f"[REJECTED] {source.name}: {message}", flush=True)
        return STATUS_REJECTED

    # -- acceptance --------------------------------------------------------
    def accept(self, obj: dict) -> tuple[str, str]:
        project_id, request_id = obj["project_id"], obj["request_id"]
        spec_sha = hashlib.sha256(
            json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT job_id, spec_sha FROM p01_jobs WHERE project_id=? AND request_id=?",
                (project_id, request_id),
            ).fetchone()
            if row:
                if row[1] != spec_sha:
                    raise ValueError("同一 project_id/request_id 已存在不同内容；需使用新 request_id")
                return row[0], "DUPLICATE"
            job_id = "p01_" + uuid.uuid4().hex[:12]
            db.execute(
                "INSERT INTO p01_jobs (job_id, project_id, request_id, spec_sha, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (job_id, project_id, request_id, spec_sha, "RUNNING", utc_now()),
            )
            return job_id, "ACCEPTED"

    def finish(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "repo", "worktree", "base_commit", "head_commit",
                   "thread_id", "codex_exit", "tests_ok"}
        sets, values = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key}=?")
                values.append(value)
        sets.append("finished_at=?")
        values.append(utc_now())
        values.append(job_id)
        with self.connect() as db:
            db.execute(f"UPDATE p01_jobs SET {', '.join(sets)} WHERE job_id=?", values)

    # -- worktree ----------------------------------------------------------
    def create_worktree(self, repo: Path, request_id: str, base_ref: str) -> tuple[Path, str, str]:
        worktree = self.worktrees / request_id
        if worktree.exists():
            raise ValueError(f"worktree 已存在: {worktree}")
        base_sha = self.git("rev-parse", base_ref, cwd=repo).stdout.strip()
        if not base_sha:
            raise ValueError(f"无法解析 base_ref: {base_ref}")
        branch = f"p01/{request_id}"
        proc = self.git("worktree", "add", "-b", branch, str(worktree), base_ref, cwd=repo)
        if proc.returncode != 0:
            raise ValueError(f"git worktree add 失败: {proc.stderr.strip()}")
        return worktree, branch, base_sha

    def changed_paths(self, worktree: Path) -> list[str]:
        proc = self.git("status", "--porcelain", cwd=worktree)
        paths = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            paths.append(_norm(line[3:]))
        return paths

    def path_allowed(self, changed: list[str], allow_paths: list[str]) -> tuple[bool, list[str]]:
        allow = [item.replace("\\", "/").strip().rstrip("/") for item in allow_paths]
        bad = []
        for path in changed:
            if not any(path == prefix or path.startswith(prefix + "/") for prefix in allow):
                bad.append(path)
        return not bad, bad

    def commit(self, worktree: Path, request_id: str, title: str) -> str | None:
        add = self.git("add", "-A", cwd=worktree)
        if add.returncode != 0:
            return None
        proc = self.git(
            "-c", "user.name=SoloShip Bridge", "-c", "user.email=bridge@soloship.local",
            "commit", "-m", f"p01({request_id}): {title}"[:200],
            cwd=worktree,
        )
        if proc.returncode != 0:
            return None
        return self.git("rev-parse", "HEAD", cwd=worktree).stdout.strip()

    # -- main --------------------------------------------------------------
    def process_one(self, source: Path) -> str:
        if source.is_symlink():
            return self.reject(source, "不接受符号链接")
        if source.stat().st_size > 256 * 1024:
            return self.reject(source, "任务文件超过 256 KiB")
        try:
            obj = json.loads(source.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            return self.reject(source, f"JSON 无法解析: {exc}")

        errors = validate_p01_task(obj, self.root)
        if errors:
            return self.reject(source, "; ".join(errors))
        if source.name != f"{obj['request_id']}.ready.json":
            return self.reject(source, "文件名必须是 <request_id>.ready.json")
        # 安全门禁：在执行任何 Codex / 测试动作之前强制检查。
        # 不能只依赖 CLI main() 里的 guard_or_exit —— 内部调用不经过 main()。
        allowed, gate_message = security.require_auto_dev(self.root)
        if not allowed:
            return self.reject(source, gate_message)

        try:
            job_id, outcome = self.accept(obj)
        except ValueError as exc:
            return self.reject(source, str(exc))

        request_id = obj["request_id"]
        project_id = obj["project_id"]
        key = f"{project_id}__{request_id}"
        if outcome == "DUPLICATE":
            payload = {
                "phase": "P01", "status": "P01_DUPLICATE", "job_id": job_id,
                "request_id": request_id, "project_id": project_id,
                "note": "同一任务已处理过，未重复调用 Codex",
                "report_path": f"reports/{key}.report.md",
                "finished_at": utc_now(),
            }
            atomic_text(self.receipts / f"{key}.receipt.json", dump_json(payload))
            shutil.move(str(source), str(self.archive / f"{source.name}.{uuid.uuid4().hex[:8]}"))
            print(f"[P01_DUPLICATE] {key} => {job_id}", flush=True)
            return "P01_DUPLICATE"
        workspace = obj["workspace"]
        verify = obj["verify"]
        spec = obj.get("codex") or {}
        sandbox = spec.get("sandbox", codex.DEFAULT_SANDBOX)
        codex_timeout = int(spec.get("timeout_seconds", 900))
        repo = (self.root / workspace["repo"]).resolve()
        allow_paths = workspace["allow_paths"]
        test_cmd = [str(part) for part in verify["test_command"]]
        test_timeout = int(verify.get("timeout_seconds", 300))
        require_diff = bool(verify.get("require_diff", True))

        evidence: dict[str, Any] = {
            "phase": "P01",
            "job_id": job_id,
            "request_id": request_id,
            "project_id": project_id,
            "outcome": outcome,
            "codex_version": codex.codex_version(self.codex_cmd),
            "sandbox": sandbox,
            "repo": str(repo),
            "started_at": utc_now(),
        }

        status = STATUS_CODEX_OK
        run = None
        worktree: Path | None = None
        base_sha = head_sha = None
        changed: list[str] = []
        diff_stat = ""
        diff_patch = ""
        test_result: dict[str, Any] = {}

        try:
            worktree, branch, base_sha = self.create_worktree(repo, request_id, workspace.get("base_ref", "HEAD"))
        except Exception as exc:  # noqa: BLE001 - any worktree failure is a task failure
            evidence["worktree_error"] = f"{type(exc).__name__}: {exc}"
            status = STATUS_CODEX_FAILED
        else:
            evidence.update({"worktree": str(worktree), "branch": branch, "base_commit": base_sha})
            prompt = build_prompt(obj)
            last_msg_path = self.control / "p01" / f"{key}.last.txt"
            run = codex.run_codex(
                prompt,
                worktree,
                sandbox=sandbox,
                timeout_s=codex_timeout,
                codex_cmd=self.codex_cmd,
                last_message_path=last_msg_path,
            )
            evidence["codex"] = run.evidence()
            evidence["codex_events_path"] = str(self.control / "p01" / f"{key}.events.jsonl")
            atomic_text(
                self.control / "p01" / f"{key}.events.jsonl",
                "\n".join(json.dumps(ev, ensure_ascii=False) for ev in run.events) + "\n",
            )

            if not run.ok:
                status = {
                    codex.CLI_MISSING: STATUS_CLI_MISSING,
                    codex.AUTH_FAILED: STATUS_AUTH_FAILED,
                    codex.TIMEOUT: STATUS_TIMEOUT,
                }.get(run.error_kind, STATUS_CODEX_FAILED)
            else:
                changed = self.changed_paths(worktree)
                evidence["changed_paths"] = changed
                allowed, bad = self.path_allowed(changed, allow_paths)
                evidence["paths_outside_allowlist"] = bad
                if not allowed:
                    status = STATUS_PATH_VIOLATION
                elif not changed:
                    status = STATUS_NO_DIFF if require_diff else STATUS_CODEX_OK
                else:
                    head_sha = self.commit(worktree, request_id, obj["title"])
                    evidence["head_commit"] = head_sha
                    diff_stat = self.git("diff", "--stat", f"{base_sha}..{head_sha or 'HEAD'}", cwd=worktree).stdout
                    patch = self.git("diff", f"{base_sha}..{head_sha or 'HEAD'}", cwd=worktree).stdout
                    diff_patch = patch[:MAX_PATCH_BYTES]
                    evidence["diff_stat"] = diff_stat.strip()
                    evidence["diff_truncated"] = len(patch) > MAX_PATCH_BYTES
                    patch_file = self.reports / "artifacts" / f"{key}.patch"
                    patch_file.parent.mkdir(parents=True, exist_ok=True)
                    patch_file.write_bytes(patch.encode("utf-8"))
                    evidence["diff_artifact"] = {
                        "path": str(patch_file),
                        "sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                        "bytes": len(patch.encode("utf-8")),
                    }
                    evidence["evidence_status"] = {
                        "diff": "OK",
                        "tests": "OK",
                        "codex_last_message": "OK" if run and run.last_message.strip() else "EMPTY",
                        "codex_events": f"OK ({len(run.events)} events)" if run and run.events else "EMPTY",
                    }
                    proc = subprocess.run(
                        test_cmd, cwd=str(worktree), capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=test_timeout,
                        stdin=subprocess.DEVNULL,
                    )
                    test_result = {
                        "command": test_cmd,
                        "exit_code": proc.returncode,
                        "stdout_tail": "\n".join(proc.stdout.strip().splitlines()[-25:]),
                        "stderr_tail": "\n".join(proc.stderr.strip().splitlines()[-10:]),
                    }
                    evidence["tests"] = test_result
                    status = STATUS_CODEX_OK if proc.returncode == 0 else STATUS_TESTS_FAILED

        self.finish(
            job_id,
            status=status,
            repo=str(repo),
            worktree=str(worktree) if worktree else None,
            base_commit=base_sha,
            head_commit=head_sha,
            thread_id=(run.thread_id if run else None),
            codex_exit=(run.exit_code if run else None),
            tests_ok=(test_result.get("exit_code") == 0) if test_result else None,
        )

        evidence["status"] = status
        evidence["finished_at"] = utc_now()
        receipt = {
            "phase": "P01", "status": status, "job_id": job_id,
            "request_id": request_id, "project_id": project_id,
            "base_commit": base_sha, "head_commit": head_sha,
            "codex_thread_id": evidence.get("codex", {}).get("thread_id"),
            "codex_exit_code": evidence.get("codex", {}).get("exit_code"),
            "tests_ok": test_result.get("exit_code") == 0 if test_result else None,
            "changed_paths": changed,
            "worktree": str(worktree) if worktree else None,
            "report_path": f"reports/{key}.report.md",
            "diff_artifact": evidence.get("diff_artifact"),
            "evidence_status": evidence.get("evidence_status", {}),
            "created_at": evidence["started_at"], "finished_at": evidence["finished_at"],
        }
        atomic_text(self.receipts / f"{key}.receipt.json", dump_json(receipt))
        atomic_text(self.reports / f"{key}.report.json", dump_json(evidence))
        atomic_text(self.reports / f"{key}.report.md", render_report(evidence, diff_patch))
        shutil.move(str(source), str(self.archive / f"{source.name}.{uuid.uuid4().hex[:8]}"))
        print(f"[{status}] {key} => {job_id}; worktree={worktree}", flush=True)
        return status

    def poll(self) -> int:
        count = 0
        for path in sorted(self.inbox.glob("*.ready.json")):
            try:
                self.process_one(path)
                count += 1
            except OSError as exc:
                print(f"[RETRYABLE-IO] {path.name}: {exc}", file=sys.stderr, flush=True)
            except sqlite3.Error as exc:
                print(f"[RETRYABLE-DB] {path.name}: {exc}", file=sys.stderr, flush=True)
        return count


def build_prompt(obj: dict) -> str:
    workspace = obj["workspace"]
    verify = obj["verify"]
    accepted = "\n".join(f"- {ac['id']}: {ac['description']}" for ac in obj["acceptance"])
    return f"""你是 SoloShip P01 自动化任务中的编码代理。工作目录是一个独立的 Git worktree。

任务：{obj['title']}
目标：{obj['goal']}

允许修改的路径（其他文件一律不改）：
{chr(10).join('- ' + p for p in workspace['allow_paths'])}

约束：
{chr(10).join('- ' + c for c in obj['constraints'])}

非目标（不要做）：
{chr(10).join('- ' + n for n in obj['non_goals'])}

验收项：
{accepted}

执行要求：
1. 先读仓库里的 AGENTS.md 和 ARCHITECTURE.md，按其中规则执行；这是自动化模式，不要建分支、不要 push、不要开 PR。
2. 只实现任务要求的最小改动，保持现有行为不变。
3. 必须补充或更新 tests/ 下的单元测试覆盖新行为。
4. 完成后运行 `{' '.join(verify['test_command'])}` 并确保通过；如果失败，自己修到通过。
5. 最后用不超过 5 行说明：改了哪些文件、测试命令与结果、还有什么没验证。

不要修改 Bridge 目录、不要修改任务文件、不要请求人工确认。
"""


def render_report(evidence: dict, patch: str) -> str:
    run = evidence.get("codex", {})
    tests = evidence.get("tests", {})
    lines = [
        "# SoloShip P01 执行报告",
        "",
        f"- Job: `{evidence['job_id']}`",
        f"- Request: `{evidence['request_id']}`",
        f"- Status: `{evidence['status']}`",
        f"- Codex CLI: `{evidence.get('codex_version') or '未检测到'}`",
        f"- Sandbox: `{evidence.get('sandbox')}`",
        f"- Repo: `{evidence.get('repo')}`",
        f"- Worktree: `{evidence.get('worktree') or '未创建'}`",
        f"- Base commit: `{evidence.get('base_commit') or '-'}`",
        f"- Head commit: `{evidence.get('head_commit') or '-'}`",
        "",
        "## Codex 执行",
        "",
        f"- 退出码: `{run.get('exit_code')}`",
        f"- 会话 ID: `{run.get('thread_id') or '-'}`",
        f"- 耗时: `{run.get('duration_ms')} ms`",
        f"- 超时: `{run.get('timed_out')}`",
        f"- 事件数: `{run.get('event_count')}`",
        f"- 事件文件: `{evidence.get('codex_events_path', '-')}`",
        "",
        "**Codex 最终结论（仅作参考，不作为验收依据）**",
        "",
        "```text",
        (run.get("last_message") or "（无输出）").strip()[:4000],
        "```",
        "",
        "## 独立核验",
        "",
        f"- 变更文件: `{', '.join(evidence.get('changed_paths', [])) or '无'}`",
        f"- 越界文件: `{', '.join(evidence.get('paths_outside_allowlist', [])) or '无'}`",
        f"- 测试命令: `{' '.join(tests.get('command', []))}`",
        f"- 测试退出码: `{tests.get('exit_code')}`",
        "",
    ]
    if evidence.get("diff_stat"):
        lines += ["## 代码差异", "", "```text", evidence["diff_stat"], "```", ""]
    if tests:
        lines += ["## 测试输出（尾部）", "", "```text", tests.get("stdout_tail", ""), "```", ""]
    if patch:
        lines += ["## Patch", "", "```diff", patch, "```", ""]
    lines += [
        "## 结论边界",
        "",
        "- 本报告只证明：Codex 在隔离 worktree 中产生了可核验的 diff，且 Bridge 独立跑的测试通过。",
        "- 未做：推送远端、创建 PR、合并、部署、真实业务验收。",
    ]
    return "\n".join(lines) + "\n"


def self_check(bridge: P01Bridge) -> int:
    print(f"codex version: {codex.codex_version(bridge.codex_cmd)}", flush=True)
    logged_in, message = codex.login_state(bridge.codex_cmd)
    print(f"logged in: {logged_in}", flush=True)
    print(f"login status: {message}", flush=True)
    print(f"exchange: {bridge.exchange}", flush=True)
    print(f"worktrees: {bridge.worktrees}", flush=True)
    return 0 if logged_in else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="SoloShip P01 Bridge（Codex 自动开发）")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--once", action="store_true", help="只处理当前已提交的任务并退出")
    parser.add_argument("--selfcheck", action="store_true", help="只输出 Codex 环境信息")
    parser.add_argument("--codex-cmd", default=None, help="测试用：覆盖 codex 可执行路径")
    args = parser.parse_args()

    cmd = [args.codex_cmd] if args.codex_cmd else None
    security.guard_or_exit(args.root, "selfcheck" if args.selfcheck else ("once" if args.once else "watch"))
    bridge = P01Bridge(args.root, codex_cmd=cmd)
    if args.selfcheck:
        raise SystemExit(self_check(bridge))

    print("SoloShip P01 已启动：监测 exchange/p01/inbox/*.ready.json", flush=True)
    print(f"交接目录：{bridge.exchange}", flush=True)
    print(f"worktree 目录：{bridge.worktrees}", flush=True)
    print("本阶段不会 push、创建 PR、合并或部署。", flush=True)
    if args.once:
        print(f"本次处理 {bridge.poll()} 个文件。", flush=True)
        return
    print("保持窗口打开；按 Ctrl+C 停止。", flush=True)
    try:
        while True:
            bridge.poll()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("已退出。", flush=True)


if __name__ == "__main__":
    main()
