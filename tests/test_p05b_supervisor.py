"""P05-B：Supervisor 的单实例、调度与凭据边界。

覆盖任务 D 的验收矩阵：
  首次启动 / 重复启动被拒 / 停止后重启 / 自动接收 / 失败不影响队列 /
  重启不重跑 / 未收尾任务不重跑 / 后台子进程能拿到 Codex 认证 /
  未授权项目仍被拒 / 历史记录完整 / 不落盘 PAT。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))

import supervisor as sup  # noqa: E402

SUPERVISOR = ROOT / "bridge" / "supervisor.py"
FAKE_CODEX = ROOT / "tests" / "fake_codex.py"
FAKE_CODEX_CMD = [sys.executable, str(FAKE_CODEX)]

PREFLIGHT_OK = {
    "codex": {"found": True, "cmd": FAKE_CODEX_CMD, "version": "codex-cli 0.0.0-fake"},
    "codex_home": {"present": True},
    "login": {"logged_in": True},
    "env": {"kept_for_codex": ["CODEX_HOME"], "gh_token_in_codex_child": False},
    "github": {"enabled": False, "publish_gate": "ok"},
    "sandbox": {"execution_mode": "WINDOWS_TEST_MODE", "sandbox_status": "SANDBOX_BLOCKED"},
    "blocking": [], "degraded": [], "ok": True, "codex_ready": True,
}
PREFLIGHT_NO_CODEX = {**PREFLIGHT_OK,
                      "codex": {"found": False, "cmd": None, "version": None},
                      "login": {"logged_in": False},
                      "codex_ready": False,
                      "degraded": ["CODEX_CLI_MISSING"]}


def p00_task(req="p00_sup_0001", project="demo-app"):
    return {
        "schema_version": "1.0", "request_id": req, "project_id": project,
        "title": "P05-B supervisor test", "goal": "check pickup",
        "requested_paths": ["src/**"], "constraints": [], "non_goals": [],
        "acceptance": [{"id": "AC-01", "description": "accepted", "verification": "automated"}],
    }


def make_root(tmp: str, *, git_repo: bool = True) -> Path:
    root = Path(tmp)
    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "config" / "bridge.json", root / "config" / "bridge.json")
    (root / "exchange" / "inbox").mkdir(parents=True, exist_ok=True)
    (root / "exchange" / "p02" / "inbox").mkdir(parents=True, exist_ok=True)
    (root / ".control").mkdir(parents=True, exist_ok=True)
    if git_repo:
        repo = root / "repos" / "demo"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                       capture_output=True)
    return root


def drop_p00(root: Path, obj=None) -> Path:
    obj = obj or p00_task()
    path = root / "exchange" / "inbox" / f"{obj['request_id']}.ready.json"
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


def drop_p02(root: Path, req="p02_sup_0001", project="demo-app") -> Path:
    obj = {
        "schema_version": "1.0", "request_id": req, "project_id": project,
        "title": "P05-B p02", "goal": "check dispatch",
        "requested_paths": ["src/**"], "constraints": [], "non_goals": [],
        "acceptance": [{"id": "AC-01", "description": "accepted"}],
        "workspace": {"repo": "repos/demo", "base_ref": "main"},
        "verify": {"commands": [["py", "-3", "-c", "print(1)"]]},
        "codex": {"timeout_s": 60},
    }
    path = root / "exchange" / "p02" / "inbox" / f"{req}.ready.json"
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


def wait_for(predicate, timeout=15.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class Base(unittest.TestCase):
    git_repo = True

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self._tmp.name, git_repo=self.git_repo)
        self.procs = []
        self._supervisors = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        for supervisor in self._supervisors:
            supervisor.close_log()
        self._tmp.cleanup()

    def supervisor(self, **kwargs):
        kwargs.setdefault("preflight_info", PREFLIGHT_OK)
        kwargs.setdefault("poll_interval", 0.2)
        instance = sup.Supervisor(self.root, **kwargs)
        self._supervisors.append(instance)
        return instance

    def spawn_run(self, poll=0.2, max_cycles=None, codex_cmd=None):
        cmd = [sys.executable, str(SUPERVISOR), "_run", "--root", str(self.root),
               "--poll", str(poll)]
        if max_cycles:
            cmd += ["--max-cycles", str(max_cycles)]
        if codex_cmd:
            cmd += ["--codex", json.dumps(codex_cmd)]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=env)
        self.procs.append(proc)
        return proc

    def state(self) -> dict:
        return json.loads((sup.state_path(self.root)).read_text(encoding="utf-8")) \
            if sup.state_path(self.root).is_file() else {}


class TestSingleInstance(Base):
    def test_second_acquire_in_same_process_fails(self):
        first = sup.SingleInstanceLock(self.root)
        second = sup.SingleInstanceLock(self.root)

        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
            self.assertTrue(sup.lock_is_held(self.root))
        finally:
            first.release()

        self.assertFalse(sup.lock_is_held(self.root))
        third = sup.SingleInstanceLock(self.root)
        self.assertTrue(third.acquire())
        third.release()

    def test_cross_process_lock_rejects_start(self):
        holder = subprocess.Popen(
            [sys.executable, str(SUPERVISOR), "_hold-lock", "--root", str(self.root),
             "--seconds", "8"],
            stdout=subprocess.PIPE, text=True, encoding="utf-8")
        self.procs.append(holder)
        line = holder.stdout.readline()
        self.assertIn('"ok": true', line)

        result = subprocess.run(
            [sys.executable, str(SUPERVISOR), "start", "--root", str(self.root)],
            capture_output=True, text=True, encoding="utf-8")
        payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, sup.EXIT_ALREADY_RUNNING)
        self.assertEqual(payload["reason"], "ALREADY_RUNNING")

    def test_pid_file_alone_does_not_block(self):
        # 只有 state.json 里的旧 PID，没有锁 → 不得误判为运行中
        _ = self.supervisor().heartbeat()
        self.assertFalse(sup.lock_is_held(self.root))
        status = sup.build_status(self.root)
        self.assertFalse(status["running"])


class TestPidProbe(Base):
    """回归：旧实现用 os.kill(pid, 0)，在 Windows 上可能被实现为 TerminateProcess。"""

    def test_pid_alive_does_not_kill_target(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.procs.append(proc)
        time.sleep(0.6)

        self.assertTrue(sup.pid_alive(proc.pid))
        self.assertIsNone(proc.poll(), "pid_alive 绝不能终止目标进程")

        proc.kill()
        proc.wait(timeout=10)
        self.assertFalse(sup.pid_alive(proc.pid))

    def test_pid_alive_handles_unknown_and_empty(self):
        self.assertFalse(sup.pid_alive(999999))
        self.assertFalse(sup.pid_alive(None))
        self.assertFalse(sup.pid_alive(0))


class TestLifecycle(Base):
    def test_first_start_records_pid_time_and_heartbeat(self):
        proc = self.spawn_run(max_cycles=1)
        proc.wait(timeout=40)
        state = self.state()

        self.assertEqual(state["state"], "stopped")
        self.assertEqual(state["pid"], proc.pid)
        self.assertTrue(state["started_at"])
        self.assertTrue(state["last_heartbeat"])
        self.assertEqual(state["schema"], sup.SCHEMA)
        self.assertTrue((sup.logs_dir(self.root)).is_dir())
        self.assertTrue(any(sup.logs_dir(self.root).glob("supervisor-*.log")))

    def test_duplicate_start_rejected_then_stop_then_start_again(self):
        first = self.spawn_run(poll=0.2)
        self.assertTrue(wait_for(lambda: self.state().get("state") == "running"))
        pid_first = self.state()["pid"]

        dup = subprocess.run(
            [sys.executable, str(SUPERVISOR), "start", "--root", str(self.root)],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(dup.returncode, sup.EXIT_ALREADY_RUNNING)

        stop = subprocess.run(
            [sys.executable, str(SUPERVISOR), "stop", "--root", str(self.root), "--timeout", "20"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(stop.returncode, sup.EXIT_OK, stop.stdout)
        self.assertTrue(json.loads(stop.stdout)["graceful"])
        first.wait(timeout=20)
        self.assertEqual(self.state()["state"], "stopped")
        self.assertFalse(sup.lock_is_held(self.root))

        second = self.spawn_run(poll=0.2)
        self.assertTrue(wait_for(lambda: (self.state().get("state") == "running"
                                          and self.state().get("pid") == second.pid)))
        self.assertNotEqual(self.state()["pid"], pid_first)
        subprocess.run([sys.executable, str(SUPERVISOR), "stop", "--root", str(self.root),
                        "--timeout", "20"], capture_output=True, text=True)
        second.wait(timeout=20)

    def test_stop_status_exit_code_when_not_running(self):
        result = subprocess.run(
            [sys.executable, str(SUPERVISOR), "status", "--root", str(self.root), "--json"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, sup.EXIT_NOT_RUNNING)
        self.assertFalse(json.loads(result.stdout)["running"])

    def test_graceful_stop_keeps_unprocessed_task_in_inbox(self):
        proc = self.spawn_run(poll=60)  # 首轮空跑后长睡，等待期可被打断
        self.assertTrue(wait_for(lambda: self.state().get("state") == "running"))
        ready = drop_p00(self.root)

        stop = subprocess.run(
            [sys.executable, str(SUPERVISOR), "stop", "--root", str(self.root), "--timeout", "15"],
            capture_output=True, text=True, encoding="utf-8")
        proc.wait(timeout=20)

        self.assertEqual(stop.returncode, sup.EXIT_OK, stop.stdout)
        self.assertTrue(ready.is_file(), "未处理的任务文件必须保留在 inbox，不能丢")
        self.assertEqual(self.state()["state"], "stopped")

        rerun = self.spawn_run(max_cycles=2)
        rerun.wait(timeout=40)
        self.assertFalse(ready.exists())
        self.assertTrue(list((self.root / "exchange" / "receipts").glob("*.receipt.json")))


class TestDispatch(Base):
    def test_new_task_is_received_automatically(self):
        proc = self.spawn_run(poll=0.2)
        self.assertTrue(wait_for(lambda: self.state().get("state") == "running"))
        drop_p00(self.root)

        receipts = self.root / "exchange" / "receipts"
        self.assertTrue(wait_for(lambda: list(receipts.glob("*.receipt.json"))))
        subprocess.run([sys.executable, str(SUPERVISOR), "stop", "--root", str(self.root),
                        "--timeout", "20"], capture_output=True, text=True)
        proc.wait(timeout=20)

        receipt = json.loads(next(receipts.glob("*.receipt.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "ACCEPTED")
        self.assertGreaterEqual(self.state()["counts"]["processed"], 1)

    def test_p00_and_p02_are_both_scheduled(self):
        calls = []

        class FakeP02:
            def process_one(self, path):
                calls.append(("p02", Path(path).name))
                Path(path).rename(self_root / "exchange" / "p02" / "archive"
                                  / Path(path).name)
                return "P02_READY_FOR_HUMAN"

        self_root = self.root
        (self.root / "exchange" / "p02" / "archive").mkdir(parents=True, exist_ok=True)
        supervisor = self.supervisor(p02_factory=lambda: FakeP02())
        drop_p00(self.root)
        drop_p02(self.root)

        first = supervisor.tick()["executed"]
        second = supervisor.tick()["executed"]

        self.assertEqual(first["kind"], "p00")
        self.assertEqual(second["kind"], "p02")
        self.assertEqual(calls, [("p02", "p02_sup_0001.ready.json")])

    def test_one_task_at_a_time(self):
        running = {"count": 0, "max": 0}

        class FakeP02:
            def process_one(self, path):
                running["count"] += 1
                running["max"] = max(running["max"], running["count"])
                time.sleep(0.05)
                running["count"] -= 1
                Path(path).unlink()
                return "P02_READY_FOR_HUMAN"

        supervisor = self.supervisor(p02_factory=lambda: FakeP02())
        for idx in range(3):
            drop_p02(self.root, req=f"p02_sup_000{idx}")
        for _ in range(3):
            supervisor.tick()

        self.assertEqual(running["max"], 1)

    def test_single_concurrency_seam_is_one(self):
        supervisor = self.supervisor()
        self.assertEqual(supervisor.MAX_PARALLEL, 1)
        candidates = [{"kind": "p00", "task_id": f"t{i}", "path": "x", "mtime": i}
                      for i in range(3)]
        self.assertEqual(len(supervisor.select_batch(candidates)), 1)
        self.assertEqual(supervisor.select_batch(candidates)[0]["task_id"], "t0")

    def test_failed_task_does_not_stop_supervisor(self):
        state = {"n": 0}

        class FlakyP00:
            def process_one(self, path):
                state["n"] += 1
                Path(path).unlink()  # 模拟已被消费后才崩
                if state["n"] == 1:
                    raise RuntimeError("boom")
                return "ACCEPTED"

        supervisor = self.supervisor(p00_factory=lambda: FlakyP00())
        drop_p00(self.root, p00_task("p00_sup_0001"))
        first = supervisor.tick()["executed"]
        drop_p00(self.root, p00_task("p00_sup_0002"))
        second = supervisor.tick()["executed"]

        self.assertEqual(first["status"], "SUPERVISOR_ERROR")
        self.assertEqual(second["status"], "ACCEPTED")
        self.assertEqual(supervisor.counts["failed"], 1)
        self.assertEqual(supervisor.counts["processed"], 1)

    def test_unauthorized_project_still_rejected(self):
        supervisor = self.supervisor()
        drop_p02(self.root, req="p02_sup_bad01", project="other-app")

        result = supervisor.tick()["executed"]

        self.assertIn(result["status"], ("REJECTED", "P02_TASK_REJECTED"))
        rejected = list((self.root / "exchange" / "p02" / "rejected").glob("*"))
        self.assertTrue(rejected, "未授权项目必须落到 rejected")
        self.assertIsNone(supervisor.current_task)

    def test_codex_unavailable_defers_dev_task_but_keeps_p00(self):
        supervisor = self.supervisor(preflight_info=PREFLIGHT_NO_CODEX)
        drop_p02(self.root, req="p02_sup_nocx01")
        drop_p00(self.root)

        first = supervisor.tick()["executed"]
        second = supervisor.tick()["executed"]

        self.assertEqual(first["kind"], "p00")
        self.assertEqual(second["status"], "DEFERRED")
        self.assertIn("CODEX_UNAVAILABLE", second["reason"])
        self.assertTrue(list((sup.deferred_dir(self.root)).glob("*")))


class TestRestartSafety(Base):
    def test_completed_task_is_not_reexecuted(self):
        supervisor = self.supervisor()
        drop_p00(self.root)
        supervisor.tick()
        first_job = json.loads(next((self.root / "exchange" / "receipts")
                                    .glob("*.receipt.json")).read_text(encoding="utf-8"))["job_id"]

        drop_p00(self.root)  # 同样的 request_id 再次到达
        result = supervisor.tick()["executed"]

        self.assertEqual(result["status"], "DEFERRED")
        self.assertIn("ALREADY_PROCESSED", result["reason"])
        receipt = json.loads(next((self.root / "exchange" / "receipts")
                                  .glob("*.receipt.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["job_id"], first_job)
        self.assertEqual(len(list((self.root / "exchange" / "archive").iterdir())), 1)
        self.assertEqual(len(list(sup.deferred_dir(self.root).glob("*"))), 1)

    def test_unfinished_old_task_is_deferred_not_rerun(self):
        import sqlite3

        db = self.root / ".control" / "p02" / "p02.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE p02_jobs (
            job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
            spec_sha TEXT NOT NULL, status TEXT NOT NULL, repo TEXT, worktree TEXT,
            base_commit TEXT, head_commit TEXT, fix_rounds INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, finished_at TEXT, UNIQUE (project_id, request_id))""")
        conn.execute("INSERT INTO p02_jobs (job_id, project_id, request_id, spec_sha, status,"
                     " created_at) VALUES ('p02_old','demo-app','p02_sup_old01','x','ACCEPTED',"
                     " '2026-10-23T10:00:00+00:00')")
        conn.commit()
        conn.close()

        supervisor = self.supervisor()
        found = supervisor.detect_unfinished()
        drop_p02(self.root, req="p02_sup_old01")
        result = supervisor.tick()

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["action"], "deferred_until_p05c")
        self.assertIsNone(result["executed"], "未收尾任务不得自动重跑")
        self.assertTrue(sup.recovery_path(self.root).is_file())
        self.assertTrue(list(sup.deferred_dir(self.root).glob("*")) is not None)

    def test_interrupted_claim_is_deferred(self):
        supervisor = self.supervisor()
        drop_p00(self.root, p00_task("p00_sup_claim1"))
        claim = sup.claims_dir(self.root) / "p00__p00_sup_claim1.json"
        claim.parent.mkdir(parents=True, exist_ok=True)
        claim.write_text(json.dumps({"kind": "p00", "task_id": "p00_sup_claim1",
                                     "pid": 999999, "state": "running",
                                     "at": "2026-10-23T10:00:00+00:00"}), encoding="utf-8")

        result = supervisor.tick()["executed"]

        self.assertEqual(result["status"], "DEFERRED")
        self.assertIn("UNFINISHED_CLAIM", result["reason"])

    def test_claim_blocks_second_worker(self):
        supervisor = self.supervisor()
        cand = {"kind": "p00", "task_id": "p00_sup_0001", "path": "x", "mtime": 0}
        self.assertEqual(supervisor.claim(cand)[0], "acquired")
        with mock.patch.object(sup, "pid_alive", return_value=True):
            self.assertEqual(supervisor.claim(cand)[0], "busy")
        supervisor.close_claim(cand, "done")
        self.assertEqual(supervisor.claim(cand)[0], "duplicate")  # 已处理过，不再执行

    def test_history_files_are_not_modified(self):
        supervisor = self.supervisor()
        drop_p00(self.root)
        supervisor.tick()
        before = {p: p.read_bytes() for p in (self.root / "exchange").rglob("*.json")}

        supervisor.tick()
        sup.build_status(self.root)
        after = {p: p.read_bytes() for p in (self.root / "exchange").rglob("*.json")
                 if "status" not in p.parts}

        for path, content in after.items():
            if path in before:
                self.assertEqual(content, before[path], f"{path} 被改写")


class TestPreflight(Base):
    def test_doctor_reports_scrubbed_env_and_real_sandbox(self):
        env = {"CODEX_HOME": str(self.root / "codex-home"),
               "GH_TOKEN": "ghp_should_not_leak_into_codex_child"}
        with mock.patch.dict(os.environ, env):
            info = sup.preflight(self.root, FAKE_CODEX_CMD)

        self.assertTrue(info["codex"]["found"])
        self.assertTrue(info["login"]["logged_in"])
        self.assertIn("CODEX_HOME", info["env"]["kept_for_codex"])
        self.assertFalse(info["env"]["gh_token_in_codex_child"],
                         "GitHub Token 不得进入 Codex 子进程")
        self.assertEqual(info["sandbox"]["execution_mode"], "WINDOWS_TEST_MODE")
        self.assertTrue(info["github"]["require_env_token"])

    def test_background_child_sees_codex_home_but_not_gh_token(self):
        codex_home = self.root / "codex-home"
        codex_home.mkdir(exist_ok=True)
        (codex_home / "auth.json").write_text("{}", encoding="utf-8")
        env = {**os.environ, "CODEX_HOME": str(codex_home),
               "FAKE_CODEX_REQUIRE_CODEX_HOME": "1", "PYTHONIOENCODING": "utf-8",
               "GH_TOKEN": "ghp_should_not_leak"}

        result = subprocess.run(
            [sys.executable, str(SUPERVISOR), "doctor", "--root", str(self.root),
             "--codex", json.dumps(FAKE_CODEX_CMD)],
            capture_output=True, text=True, encoding="utf-8", env=env)
        info = json.loads(result.stdout)

        self.assertTrue(info["login"]["logged_in"], info["login"])
        self.assertIn("CODEX_HOME", info["env"]["kept_for_codex"])
        self.assertFalse(info["env"]["gh_token_in_codex_child"])
        self.assertTrue(info["codex_home"]["present"])

    def test_doctor_without_codex_home_reports_not_logged_in(self):
        env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
        env.update({"FAKE_CODEX_REQUIRE_CODEX_HOME": "1", "PYTHONIOENCODING": "utf-8"})

        result = subprocess.run(
            [sys.executable, str(SUPERVISOR), "doctor", "--root", str(self.root),
             "--codex", json.dumps(FAKE_CODEX_CMD)],
            capture_output=True, text=True, encoding="utf-8", env=env)
        info = json.loads(result.stdout)

        self.assertFalse(info["login"]["logged_in"])
        self.assertIn("CODEX_NOT_LOGGED_IN", " ".join(info["degraded"]))

    def test_publish_gate_blocks_without_token(self):
        supervisor = self.supervisor()
        info = sup.preflight(self.root, FAKE_CODEX_CMD)
        self.assertFalse(info["github"]["token_present"] if not os.environ.get("GH_TOKEN") else True)
        self.assertEqual(info["github"]["publish_gate"], "blocked_missing_token")
        # Supervisor 绝不自行注入 Token
        self.assertFalse(any("GH_TOKEN" in str(v) for v in vars(supervisor).values()
                             if isinstance(v, str)))


class TestNoSecretOnDisk(Base):
    def test_no_token_written_by_lifecycle(self):
        secret = "ghp_p05b_must_never_be_written"
        env = {**os.environ, "GH_TOKEN": secret}
        supervisor = self.supervisor()
        drop_p00(self.root)
        with mock.patch.dict(os.environ, env):
            supervisor.tick()

        hits = []
        for path in self.root.rglob("*"):
            if path.is_file() and path.stat().st_size < 2_000_000:
                try:
                    if secret in path.read_text(encoding="utf-8", errors="ignore"):
                        hits.append(str(path))
                except OSError:
                    continue
        self.assertEqual(hits, [], "PAT 不得落盘到任何文件")


class TestStdioEncoding(Base):
    """回归：任务计划环境下 stdout 被重定向时，日志必须是 UTF-8（P05-B 实测过 UTF-8/GBK 混排）。"""

    def test_redirected_stream_is_reconfigured_to_utf8(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp936", errors="strict")
        stream.write("探测")  # 先写一次，确认原编码确实是 cp936
        stream.flush()
        self.assertEqual(buffer.getvalue(), "探测".encode("cp936"))

        with mock.patch.object(sup.sys, "stdout", stream), \
                mock.patch.object(sup.sys, "stderr", stream):
            changed = sup.force_utf8_stdio()

        self.assertEqual(stream.encoding.lower().replace("-", ""), "utf8")
        self.assertIn("stdout", changed)
        stream.write("启动")
        stream.flush()
        self.assertTrue(buffer.getvalue().endswith("启动".encode("utf-8")))

    def test_tty_stream_is_not_touched(self):
        calls = []

        class Tty:
            encoding = "cp936"

            def isatty(self):
                return True

            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        with mock.patch.object(sup.sys, "stdout", Tty()), \
                mock.patch.object(sup.sys, "stderr", Tty()):
            changed = sup.force_utf8_stdio()

        self.assertEqual(changed, [])
        self.assertEqual(calls, [], "交互式控制台保持本地代码页，不得改写")

    def test_background_run_writes_utf8_boot_log(self):
        boot = self.root / ".control" / "supervisor" / "logs" / "redirected.log"
        boot.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(SUPERVISOR), "_run", "--root", str(self.root),
               "--poll", "0.1", "--max-cycles", "1", "--codex", json.dumps(FAKE_CODEX_CMD)]
        # 故意剥掉 UTF-8 相关的环境变量：修复必须由进程自身完成，不依赖父进程环境
        env = {k: v for k, v in os.environ.items()
               if k not in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO")}
        with open(boot, "wb") as handle:
            proc = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT,
                                    env=env, cwd=str(ROOT))
        try:
            self.assertEqual(proc.wait(timeout=90), 0)
        finally:
            if proc.poll() is None:
                proc.kill()

        raw = boot.read_bytes()
        self.assertTrue(raw)
        text = raw.decode("utf-8")  # 严格解码：失败即说明仍是 GBK 字节
        self.assertIn("Supervisor", text)


class TestStateWriteRace(Base):
    """回归：`start` 轮询 state.json 时，守护进程的原子替换会撞 WinError 5。

    Python 的 open() 不带 FILE_SHARE_DELETE，目标文件被读打开期间 os.replace
    必定被拒。P05-B 重启时正是这样让守护进程在第一次心跳就退出（任务计划报 0）。
    """

    def _hold_then_release(self, path: Path, seconds: float):
        handle = open(path, "r", encoding="utf-8")
        released = []

        def release():
            time.sleep(seconds)
            handle.close()
            released.append(True)

        thread = threading.Thread(target=release, daemon=True)
        thread.start()
        return thread, released

    def test_write_survives_concurrent_reader(self):
        path = sup.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        thread, released = self._hold_then_release(path, 0.15)
        try:
            sup._write_json(path, {"pid": os.getpid(), "state": "running"})
        finally:
            thread.join(timeout=5)

        self.assertTrue(released)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["state"], "running")

    def test_single_attempt_conflicts(self):
        if os.name != "nt":
            self.skipTest("Windows 独占语义才复现 FILE_SHARE_DELETE 冲突")
        path = sup.state_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        thread, _ = self._hold_then_release(path, 0.30)
        try:
            with mock.patch.object(sup, "WRITE_ATTEMPTS", 1):
                with self.assertRaises(PermissionError):
                    sup._write_json(path, {"state": "running"})
        finally:
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
