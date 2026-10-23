#!/usr/bin/env python3
"""SS-V01-003：测试命令授权策略（`bridge/test_policy.py`）回归。

覆盖验收 AC-01～AC-07：
  - `python -c` / 未获批 `-m` 模块 / shell 解释器 / 路径穿越 / 不可信目录的解释器 一律拒绝
  - 可信配置中获批的 argv 正常执行
  - 任务 JSON 无法扩大授权集合
  - P01 / P02 / Supervisor 使用同一套判定
  - 拒绝发生在进程启动之前（`subprocess.run` 不被调用）
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

import p01_bridge  # noqa: E402
import p02_bridge  # noqa: E402
import policy_fixture as fx  # noqa: E402
import supervisor as sup  # noqa: E402
import test_policy  # noqa: E402

FAKE_CODEX = str(ROOT / "tests" / "fake_codex.py")
GOOD_CMD = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    (repo / "src").mkdir(exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    git(repo, "add", "-A")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "baseline"], check=True, capture_output=True)


def task_with(cmd, request_id="policy_task_0001") -> dict:
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "project_id": "demo-app",
        "title": "策略测试任务",
        "goal": "验证测试命令授权",
        "requested_paths": ["src/**"],
        "constraints": [],
        "non_goals": [],
        "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
        "workspace": {"repo": "demo/soloship-demo", "base_ref": "HEAD",
                      "allow_paths": ["src", "tests"]},
        "verify": {"test_command": list(cmd), "timeout_seconds": 60, "require_diff": True},
        "codex": {"sandbox": "workspace-write", "timeout_seconds": 60},
    }


class ResolveTests(unittest.TestCase):
    """解析层：只认可信解释器 + 完整 argv 匹配。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="policytest-"))
        fx.write_config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def resolve(self, argv):
        return test_policy.resolve(self.tmp, argv)

    def test_approved_command_allowed(self):
        ok, argv, reason, source = self.resolve(GOOD_CMD)
        self.assertTrue(ok, reason)
        self.assertEqual(argv, GOOD_CMD)
        self.assertEqual(source, "config")

    def test_short_discover_allowed(self):
        ok, _argv, reason, _source = self.resolve(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"])
        self.assertTrue(ok, reason)

    def test_py_launcher_version_flag_allowed(self):
        if not fx.python_launcher_available():
            self.skipTest("本机没有 py 启动器")
        ok, argv, reason, _source = self.resolve(
            ["py", "-3", "-m", "unittest", "discover", "-s", "tests", "-v"])
        self.assertTrue(ok, reason)
        # 匹配时忽略启动器版本参数，但执行时保留原始 argv（交给 py 自己选版本）
        self.assertEqual(argv, ["py", "-3", "-m", "unittest", "discover", "-s", "tests", "-v"])

    def test_python_dash_c_rejected(self):
        ok, _argv, reason, _source = self.resolve([sys.executable, "-c", "print('x')"])
        self.assertFalse(ok)
        self.assertIn("-c", reason)

    def test_unapproved_module_rejected(self):
        ok, _argv, reason, _source = self.resolve([sys.executable, "-m", "http.server"])
        self.assertFalse(ok)
        self.assertIn("未获批准", reason)

    def test_unittest_with_extra_args_rejected(self):
        ok, _argv, reason, _source = self.resolve(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "--failfast"])
        self.assertFalse(ok, reason)

    def test_shell_interpreter_rejected(self):
        ok, _argv, reason, _source = self.resolve(["cmd.exe", "/c", "echo", "hi"])
        self.assertFalse(ok)
        self.assertIn("解释器", reason)

    def test_dotdot_path_rejected(self):
        ok, _argv, reason, _source = self.resolve(
            ["..\\evil\\python.exe", "-m", "unittest", "discover", "-s", "tests"])
        self.assertFalse(ok)
        self.assertIn("..", reason)

    def test_untrusted_directory_interpreter_rejected(self):
        """同名 python.exe 放在不受信任的目录里，不得因「路径存在」放行。"""
        fake_dir = self.tmp / "untrusted-bin"
        fake_dir.mkdir()
        fake = fake_dir / "python.exe"
        fake.write_bytes(b"")  # 只要能解析成路径即可，本用例绝不会执行它
        ok, _argv, reason, _source = self.resolve(
            [str(fake), "-m", "unittest", "discover", "-s", "tests"])
        self.assertFalse(ok)
        self.assertIn("目录不受信任", reason)

    def test_empty_argv_rejected(self):
        ok, _argv, reason, _source = self.resolve([])
        self.assertFalse(ok)
        self.assertIn("非空", reason)


class PolicyConfigTests(unittest.TestCase):
    """配置层：缺失 / 非法 / 空列表一律拒绝，不退回内置白名单。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="policycfg-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assertDenied(self, reason_fragment=""):
        ok, _argv, reason, source = test_policy.resolve(self.tmp, GOOD_CMD)
        self.assertFalse(ok, "配置不完整时必须拒绝")
        if reason_fragment:
            self.assertIn(reason_fragment, reason)
        self.assertIn(source, ("missing", "invalid"))
        return reason

    def test_missing_config_file_denies(self):
        self.assertDenied("test_command_policy")

    def test_missing_section_denies(self):
        fx.write_config(self.tmp, policy=False)
        self.assertDenied("test_command_policy")

    def test_empty_approved_denies(self):
        fx.write_config(self.tmp, policy=fx.test_command_policy(approved=[]))
        self.assertDenied("approved")

    def test_invalid_approved_entry_denies(self):
        fx.write_config(self.tmp, policy={"approved": ["-m unittest"]})
        self.assertDenied()

    def test_unparsable_config_denies(self):
        (self.tmp / "config").mkdir(parents=True, exist_ok=True)
        (self.tmp / "config" / "bridge.json").write_text("{ not json", encoding="utf-8")
        self.assertDenied()


class ValidatorTests(unittest.TestCase):
    """任务契约层：不合规的 test_command 在受理阶段就被拒绝。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="policyval-"))
        init_repo(self.tmp / "demo" / "soloship-demo")
        fx.write_config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_approved_command_passes_validation(self):
        errors = p01_bridge.validate_p01_task(task_with(GOOD_CMD), self.tmp)
        self.assertEqual(errors, [])

    def test_dash_c_command_rejected_by_validator(self):
        errors = p01_bridge.validate_p01_task(
            task_with([sys.executable, "-c", "print(1)"]), self.tmp)
        self.assertTrue(any("test_command" in item for item in errors), errors)

    def test_unapproved_module_rejected_by_validator(self):
        errors = p01_bridge.validate_p01_task(
            task_with([sys.executable, "-m", "pytest"]), self.tmp)
        self.assertTrue(any("test_command" in item for item in errors), errors)

    def test_task_cannot_extend_policy(self):
        """任务 JSON 里塞策略字段 → 未知字段，直接拒绝。"""
        task = task_with(GOOD_CMD)
        task["test_command_policy"] = fx.test_command_policy()
        errors = p01_bridge.validate_p01_task(task, self.tmp)
        self.assertTrue(any("未知字段" in item for item in errors), errors)


class P02ExecutionTests(unittest.TestCase):
    """执行层：不信任传入命令，拒绝发生在进程启动之前。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="policyp02-"))
        init_repo(self.tmp / "demo" / "soloship-demo")
        fx.write_config(self.tmp)
        self.bridge = p02_bridge.P02Bridge(
            self.tmp, codex_cmd=[sys.executable, FAKE_CODEX],
            sandbox_status="SANDBOX_UNENFORCED")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def submit(self, task: dict) -> tuple[str, Path]:
        self.bridge.inbox.mkdir(parents=True, exist_ok=True)
        target = self.bridge.inbox / f"{task['request_id']}.ready.json"
        target.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return self.bridge.process_one(target), target

    def test_run_tests_blocks_without_spawning(self):
        with mock.patch.object(subprocess, "run") as run:
            result = self.bridge.run_tests([sys.executable, "-c", "print(1)"],
                                           self.tmp, 30)
        self.assertTrue(result.get("policy_blocked"), result)
        self.assertIsNone(result["exit_code"])
        run.assert_not_called()

    def test_process_one_rejects_rejected_command_before_codex(self):
        with mock.patch("codex_adapter.run_codex") as run_codex:
            status, _path = self.submit(task_with([sys.executable, "-c", "print(1)"]))
        self.assertEqual(status, p02_bridge.REJECTED)
        run_codex.assert_not_called()
        receipt = self.bridge.receipts / "policy_task_0001.ready.json.error.json"
        self.assertTrue(receipt.is_file(), "拒绝时必须留回执")
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertIn("test_command", payload["reason"])

    def test_execution_stage_does_not_trust_validator(self):
        """即使校验被绕过（模拟配置在校验后收紧），执行阶段仍要拒绝。"""
        with mock.patch.object(p02_bridge, "validate_p01_task", return_value=[]):
            with mock.patch("codex_adapter.run_codex") as run_codex:
                with mock.patch.object(subprocess, "run") as run:
                    status, _path = self.submit(task_with([sys.executable, "-c", "print(1)"]))
        self.assertEqual(status, p02_bridge.BLOCKED_TEST_COMMAND)
        run_codex.assert_not_called()
        # codex 版本探测会起一次子进程；这里只要求"未授权命令本身"没被启动
        launched = [call.args[0] for call in run.call_args_list if isinstance(call.args[0], list)]
        self.assertFalse(any("print(1)" in " ".join(str(p) for p in cmd) for cmd in launched),
                         "未授权命令不得进入子进程")
        receipt = self.bridge.receipts / "demo-app__policy_task_0001.receipt.json"
        self.assertTrue(receipt.is_file())
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(payload["status"], p02_bridge.BLOCKED_TEST_COMMAND)
        self.assertIsNone(payload["tests_ok"])
        report = self.bridge.reports / "demo-app__policy_task_0001.report.json"
        self.assertTrue(report.is_file(), "拒绝原因要进报告，作为审计证据")
        evidence = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(evidence["test_command_policy"]["allowed"])
        self.assertIn("test_command", evidence["test_command_policy"]["reason"])

    def test_approved_command_reaches_test_process(self):
        """获批命令仍要走真实测试进程（用 mock 拦住，不跑真实项目测试）。"""
        fake = subprocess.CompletedProcess(args=["x"], returncode=0, stdout="ok", stderr="")
        with mock.patch.object(subprocess, "run", return_value=fake) as run:
            result = self.bridge.run_tests(GOOD_CMD, self.tmp, 30)
        self.assertNotIn("policy_blocked", result)
        self.assertEqual(result["exit_code"], 0)
        run.assert_called_once()


class P01ExecutionTests(unittest.TestCase):
    """P01 执行层同样复检，并清洗子进程环境。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="policyp01-"))
        init_repo(self.tmp / "repo")
        fx.write_config(self.tmp, mode=fx.windows_test_mode(approved_repos=["repo"]))
        self.bridge = p01_bridge.P01Bridge(self.tmp, codex_cmd=[sys.executable, FAKE_CODEX])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_execution_stage_rechecks_command(self):
        task = task_with([sys.executable, "-c", "print(1)"], request_id="p01_policy_0001")
        task["workspace"] = {"repo": "repo", "base_ref": "HEAD", "allow_paths": ["src", "tests"]}
        with mock.patch.object(p01_bridge, "validate_p01_task", return_value=[]):
            with mock.patch.dict(os.environ, {"FAKE_CODEX_MODE": "edit"}):
                with mock.patch.object(subprocess, "run", wraps=subprocess.run) as spy:
                    self.bridge.inbox.mkdir(parents=True, exist_ok=True)
                    target = self.bridge.inbox / f"{task['request_id']}.ready.json"
                    target.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
                    status = self.bridge.process_one(target)
        self.assertEqual(status, p01_bridge.STATUS_BLOCKED_TEST_COMMAND)
        launched = [call.args[0] for call in spy.call_args_list if isinstance(call.args[0], list)]
        self.assertFalse(any("print(1)" in " ".join(str(p) for p in cmd) for cmd in launched),
                         "未授权命令不得进入子进程")

    def test_validator_rejects_when_policy_missing(self):
        fx.write_config(self.tmp, policy=False,
                        mode=fx.windows_test_mode(approved_repos=["repo"]))
        task = task_with(GOOD_CMD, request_id="p01_policy_0002")
        task["workspace"] = {"repo": "repo", "base_ref": "HEAD", "allow_paths": ["src"]}
        errors = p01_bridge.validate_p01_task(task, self.tmp)
        self.assertTrue(any("test_command" in item for item in errors), errors)


class SupervisorConsistencyTests(unittest.TestCase):
    """Supervisor 与 P01/P02 共用同一判定：未经授权即拒绝，且不调 Codex。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="policysup-"))
        init_repo(self.tmp / "demo" / "soloship-demo")
        fx.write_config(self.tmp)
        (self.tmp / "exchange" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.tmp / "exchange" / "p02" / "inbox").mkdir(parents=True, exist_ok=True)
        (self.tmp / ".control").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_supervisor_rejects_without_calling_codex(self):
        task = task_with([sys.executable, "-c", "print(1)"], request_id="p02_policy_0001")
        target = self.tmp / "exchange" / "p02" / "inbox" / "p02_policy_0001.ready.json"
        target.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        preflight = {"codex_ready": True, "codex": {"found": True}, "login": {"logged_in": True},
                     "sandbox": {"execution_mode": "WINDOWS_TEST_MODE",
                                 "sandbox_status": "SANDBOX_BLOCKED"},
                     "github": {"enabled": False}, "blocking": [], "degraded": [], "ok": True}
        engine = sup.Supervisor(self.tmp, codex_cmd=[sys.executable, FAKE_CODEX],
                                preflight_info=preflight)
        with mock.patch("codex_adapter.run_codex") as run_codex:
            result = engine.tick()
        executed = result.get("executed") or {}
        self.assertEqual(executed.get("status"), p02_bridge.REJECTED, result)
        run_codex.assert_not_called()
        self.assertFalse((self.tmp / ".worktrees" / "p02_policy_0001").exists(),
                         "拒绝时不应创建 worktree")


if __name__ == "__main__":
    unittest.main()
