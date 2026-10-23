"""统一自动开发安全门禁的回归（任务卡 SS-V01-001）。

覆盖：
  AC-01 执行模式 BLOCKED 时 P01 拒绝执行
  AC-02 执行模式 BLOCKED 时 P02 拒绝执行
  AC-03 Supervisor 即使检测到 Codex 可用，也不能绕过门禁
  AC-04 拒绝后任务文件仍可恢复，且不得被标记成功
  AC-05 P00 文件交接在 BLOCKED 下照常工作
  AC-06 显式授权的 WINDOWS_TEST_MODE 仍可执行，且项目白名单继续生效
  AC-07 本文件即新增回归

约束：全部使用临时目录与 Fake Codex，不触碰真实仓库与凭据。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))
sys.path.insert(0, str(ROOT / "tests"))

import policy_fixture as fx  # noqa: E402

import p00_bridge  # noqa: E402
import p01_bridge  # noqa: E402
import p02_bridge  # noqa: E402
import security  # noqa: E402
import supervisor as sup  # noqa: E402

FAKE_CODEX = str(ROOT / "tests" / "fake_codex.py")
GATE_MARK = "自动开发被阻断"

PREFLIGHT_OK = {
    "codex": {"found": True, "cmd": [sys.executable, FAKE_CODEX], "version": "fake"},
    "codex_home": {"present": True},
    "login": {"logged_in": True},
    "env": {"kept_for_codex": ["CODEX_HOME"], "gh_token_in_codex_child": False},
    "github": {"enabled": False, "publish_gate": "ok"},
    "sandbox": {"execution_mode": "WINDOWS_TEST_MODE", "sandbox_status": "SANDBOX_BLOCKED"},
    "blocking": [], "degraded": [], "ok": True, "codex_ready": True,
}


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


def dev_task(request_id: str = "gate_dev_0001", **overrides) -> dict:
    task = {
        "schema_version": "1.0",
        "request_id": request_id,
        "project_id": "demo-app",
        "title": "门禁测试：Feature.remove",
        "goal": "实现 Feature.remove(title)，空白输入返回 False，并补测试",
        "requested_paths": ["src/**"],
        "constraints": ["只改 src/ 与 tests/"],
        "non_goals": ["不改生产仓库"],
        "acceptance": [
            {"id": "AC-01", "description": "remove 删除并返回 True", "verification": "automated"},
        ],
        "workspace": {"repo": "demo/soloship-demo", "base_ref": "HEAD",
                      "allow_paths": ["src", "tests"]},
        "verify": {
            "test_command": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            "timeout_seconds": 120,
            "require_diff": True,
        },
        "codex": {"sandbox": "workspace-write", "timeout_seconds": 600},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(task.get(key), dict):
            task[key].update(value)
        else:
            task[key] = value
    return task


def p00_task(request_id: str = "gate_handoff_0001") -> dict:
    return {
        "schema_version": "1.0", "request_id": request_id, "project_id": "demo-app",
        "title": "门禁测试：P00 交接", "goal": "验证 BLOCKED 下 P00 仍可交接",
        "requested_paths": ["src/**"], "constraints": ["仅交接"], "non_goals": ["不开发"],
        "acceptance": [{"id": "AC-01", "description": "生成回执", "verification": "automated"}],
    }


class GateBase(unittest.TestCase):
    authorized = False

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gate-"))
        self.root = self.tmp
        if self.authorized:
            self.write_config(enabled=True, risk_accepted=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, *, enabled: bool, risk_accepted: bool,
                     projects=("demo-app",), repos=("demo/soloship-demo",)) -> None:
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "bridge.json").write_text(json.dumps({
            "windows_test_mode": {
                "enabled": enabled, "risk_accepted": risk_accepted,
                "approved_projects": list(projects),
                "approved_repos": list(repos),
                "approved_absolute_repos": [],
                "isolation_claim": "NONE",
            },
            # SS-V01-003：命令授权与执行门禁相互独立；缺策略会让校验先失败，
            # 掩盖本文件要验证的门禁行为，因此这里显式登记。
            "test_command_policy": fx.test_command_policy(),
        }), encoding="utf-8")

    def submit(self, directory: Path, task: dict) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{task['request_id']}.ready.json"
        target.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        return target

    def assert_not_executed(self, request_id: str) -> None:
        """没有任何执行痕迹：没建 worktree、没有报告。"""
        self.assertFalse((self.root / ".worktrees" / request_id).exists(),
                         "未授权任务不得创建工作树")


class BlockedGateTests(GateBase):
    """BLOCKED：没有沙箱验证、也没有显式授权。"""

    def setUp(self):
        super().setUp()
        # SS-V01-003：即使处于 BLOCKED，也要写入合法的测试命令策略段。
        # 否则校验阶段会先因"策略缺失"拒绝，掩盖本类用例要验证的门禁拒绝路径。
        self.write_config(enabled=False, risk_accepted=False)
        security.record(self.root, security.BLOCKED, "测试：沙箱不可用")
        self.assertEqual(security.execution_mode(self.root), "BLOCKED")

    def test_ac01_p01_rejects_when_blocked(self):
        init_repo(self.root / "demo" / "soloship-demo")
        bridge = p01_bridge.P01Bridge(self.root, codex_cmd=[sys.executable, FAKE_CODEX])
        source = self.submit(bridge.inbox, dev_task("gate_p01_0001"))
        status = bridge.process_one(source)

        self.assertEqual(status, "P01_TASK_REJECTED")
        self.assertIn(GATE_MARK, json.loads(
            (bridge.receipts / f"{source.name}.error.json").read_text(encoding="utf-8"))["reason"])
        self.assertTrue(list(bridge.rejected.iterdir()), "任务文件必须保留在 rejected/")
        self.assertFalse(list(bridge.reports.iterdir()), "拒绝时不得产出执行报告")
        self.assert_not_executed("gate_p01_0001")

    def test_ac02_p02_rejects_when_blocked(self):
        init_repo(self.root / "demo" / "soloship-demo")
        bridge = p02_bridge.P02Bridge(self.root, codex_cmd=[sys.executable, FAKE_CODEX],
                                      sandbox_status="SANDBOX_UNENFORCED")
        source = self.submit(bridge.inbox, dev_task("gate_p02_0001"))
        status = bridge.process_one(source)

        self.assertEqual(status, "P02_TASK_REJECTED")
        reason = json.loads((bridge.receipts / f"{source.name}.error.json")
                            .read_text(encoding="utf-8"))["reason"]
        self.assertIn(GATE_MARK, reason)
        self.assert_not_executed("gate_p02_0001")

    def test_ac04_rejected_task_is_recoverable_and_not_marked_done(self):
        init_repo(self.root / "demo" / "soloship-demo")
        bridge = p02_bridge.P02Bridge(self.root, codex_cmd=[sys.executable, FAKE_CODEX],
                                      sandbox_status="SANDBOX_UNENFORCED")
        source = self.submit(bridge.inbox, dev_task("gate_p02_0002"))
        original = source.read_text(encoding="utf-8")
        bridge.process_one(source)

        kept = list(bridge.rejected.iterdir())
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding="utf-8"), original, "原文必须完整保留")
        self.assertFalse(source.exists(), "被拒文件不应留在 inbox 反复扫描")
        with bridge.connect() as db:
            rows = db.execute("SELECT status FROM p02_jobs WHERE request_id=?",
                              ("gate_p02_0002",)).fetchall()
        self.assertEqual(rows, [], "未授权任务不得建 job 记录（更不得记成功）")

    def test_ac05_p00_still_works_when_blocked(self):
        bridge = p00_bridge.SoloShipP00(self.root)
        source = self.submit(bridge.inbox, p00_task("gate_p00_0001"))
        status = bridge.process_one(source)

        self.assertEqual(status.lower(), "accepted")
        self.assertTrue(list(bridge.receipts.glob("*.receipt.json")))


class SupervisorBypassTests(GateBase):
    """AC-03：Supervisor 直调 process_one 的路径必须同样被拦。"""

    def setUp(self):
        super().setUp()
        security.record(self.root, security.BLOCKED, "测试：沙箱不可用")

    def _supervisor(self) -> sup.Supervisor:
        supervisor = sup.Supervisor(self.root, codex_cmd=[sys.executable, FAKE_CODEX],
                                    poll_interval=0.05, preflight_info=PREFLIGHT_OK)
        self.addCleanup(supervisor.close_log)
        return supervisor

    def test_ac03_supervisor_defers_instead_of_executing(self):
        supervisor = self._supervisor()
        self.assertTrue(supervisor.codex_ready, "前提：Codex 被判定为可用，仍不得执行")
        inbox = self.root / "exchange" / "p02" / "inbox"
        self.submit(inbox, dev_task("gate_sup_0001"))

        summary = supervisor.tick()
        executed = summary.get("executed") or {}

        self.assertEqual(executed.get("status"), "DEFERRED")
        self.assertIn("SECURITY_GATE_BLOCKED", executed.get("reason", ""))
        self.assert_not_executed("gate_sup_0001")
        self.assertEqual(supervisor.counts["processed"], 0, "不得计入已处理")
        self.assertEqual(supervisor.counts["deferred"], 1)
        self.assertTrue(list((self.root / ".control" / "supervisor" / "deferred").iterdir()),
                        "任务文件必须保留在 deferred/ 供后续恢复")
        self.assertFalse(list((self.root / ".control" / "supervisor" / "claims").iterdir()),
                         "未授权任务不应产生认领记录")

    def test_ac05_supervisor_still_handles_p00_when_blocked(self):
        supervisor = self._supervisor()
        inbox = self.root / "exchange" / "inbox"
        self.submit(inbox, p00_task("gate_sup_p00_0001"))

        summary = supervisor.tick()
        executed = summary.get("executed") or {}

        self.assertEqual(executed.get("kind"), "p00")
        self.assertEqual(str(executed.get("status")).lower(), "accepted")


class AuthorizedModeTests(GateBase):
    """AC-06：显式授权的 WINDOWS_TEST_MODE 保持原功能，且白名单继续生效。"""

    authorized = True

    def test_ac06_authorized_mode_executes(self):
        init_repo(self.root / "demo" / "soloship-demo")
        bridge = p02_bridge.P02Bridge(self.root, codex_cmd=[sys.executable, FAKE_CODEX],
                                      sandbox_status="SANDBOX_UNENFORCED")
        self.assertEqual(bridge.execution_mode, "WINDOWS_TEST_MODE")
        source = self.submit(bridge.inbox, dev_task("gate_auth_0001"))

        status = bridge.process_one(source)
        self.assertNotEqual(status, "P02_TASK_REJECTED", "已授权模式不得被门禁拦下")
        self.assertTrue(list(bridge.reports.iterdir()), "已授权模式应产出执行报告")

    def test_ac06_project_whitelist_still_applies(self):
        self.write_config(enabled=True, risk_accepted=True, projects=["other-app"])
        init_repo(self.root / "demo" / "soloship-demo")
        bridge = p02_bridge.P02Bridge(self.root, codex_cmd=[sys.executable, FAKE_CODEX],
                                      sandbox_status="SANDBOX_UNENFORCED")
        source = self.submit(bridge.inbox, dev_task("gate_auth_0002"))

        status = bridge.process_one(source)
        self.assertEqual(status, "P02_TASK_REJECTED")
        reason = json.loads((bridge.receipts / f"{source.name}.error.json")
                            .read_text(encoding="utf-8"))["reason"]
        self.assertIn("未注册该项目", reason)

    def test_ac06_does_not_auto_enable_without_risk_accept(self):
        self.write_config(enabled=True, risk_accepted=False)
        self.assertIsNone(security.windows_test_mode(self.root))
        self.assertEqual(security.execution_mode(self.root), "BLOCKED")
        ok, message = security.require_auto_dev(self.root)
        self.assertFalse(ok)
        self.assertIn(GATE_MARK, message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
