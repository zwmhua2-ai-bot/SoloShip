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

import model_router as mr  # noqa: E402
import p02_bridge as p02  # noqa: E402
import security  # noqa: E402

FAKE_CODEX = str(ROOT / "tests" / "fake_codex.py")
REAL_CONFIG = json.loads((ROOT / "config" / "bridge.json").read_text(encoding="utf-8"))


def task(complexity_paths=("src",), acceptance=1, risk="normal", hard=False, **codex):
    payload = {
        "title": "t",
        "goal": ("g" * 300) if hard else "g",
        "requested_paths": ["src/**", "tests/**", "docs/**"] if hard else ["src/**"],
        "constraints": ["a", "b", "c", "d"] if hard else ["a"],
        "acceptance": [{"id": f"AC-0{i}", "description": "d", "verification": "automated"}
                       for i in range(1, (5 if hard else acceptance) + 1)],
        "workspace": {"repo": "demo/soloship-demo", "allow_paths": list(complexity_paths)},
        "codex": codex,
    }
    return payload


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = mr.Router(REAL_CONFIG)

    def test_role_defaults(self):
        cases = {
            "explorer": ("fast", "gpt-6-luna", "low"),
            "builder": ("balanced", "gpt-6.1-sol", "medium"),
            "reviewer": ("balanced", "gpt-6.1-sol", "high"),
            "fixer_1": ("balanced", "gpt-6.1-sol", "high"),
            "fixer_2": ("deep", "gpt-6-astra", "high"),
        }
        for role, (tier, model, effort) in cases.items():
            d = self.router.decide(role, task=task())
            self.assertEqual((d.tier, d.model, d.effort), (tier, model, effort), role)

    def test_complexity_upgrades_never_downgrades(self):
        simple = task()
        hard = task(hard=True, complexity_paths=("src", "tests"))
        self.assertEqual(self.router.decide("builder", task=simple).tier, "balanced")
        self.assertEqual(self.router.decide("builder", task=hard).tier, "deep")

    def test_fixer_escalation(self):
        self.assertEqual(self.router.decide("fixer", task=task(), fix_round=1).tier, "balanced")
        self.assertEqual(self.router.decide("fixer", task=task(), fix_round=2).tier, "deep")

    def test_high_risk_reviewer_goes_deep(self):
        d = self.router.decide("reviewer", task=task(), risk="high")
        self.assertEqual((d.tier, d.model, d.effort), ("deep", "gpt-6-astra", "high"))

    def test_explicit_model_and_effort(self):
        d = self.router.decide("builder", task=task(model="gpt-6-astra", effort="max"))
        self.assertEqual((d.model, d.effort), ("gpt-6-astra", "max"))
        self.assertEqual(d.source, "task_override")

    def test_explicit_model_not_whitelisted(self):
        with self.assertRaises(mr.RouterError) as ctx:
            self.router.decide("builder", task=task(model="gpt-9-nope"))
        self.assertEqual(ctx.exception.code, mr.MODEL_NOT_ALLOWED)

    def test_unknown_tier(self):
        with self.assertRaises(mr.RouterError) as ctx:
            self.router.decide("builder", task=task(tier="turbo"))
        self.assertEqual(ctx.exception.code, mr.TIER_UNKNOWN)

    def test_unsupported_effort(self):
        with self.assertRaises(mr.RouterError) as ctx:
            self.router.decide("builder", task=task(effort="ludicrous"))
        self.assertIn(ctx.exception.code, (mr.EFFORT_UNSUPPORTED, mr.CONFIG_ERROR))

    def test_config_errors(self):
        broken = json.loads(json.dumps(REAL_CONFIG))
        broken["model_router"]["roles"]["builder"]["tier"] = "nope"
        with self.assertRaises(mr.RouterError):
            mr.Router(broken)

        broken2 = json.loads(json.dumps(REAL_CONFIG))
        broken2["model_router"]["tiers"]["fast"]["model"] = "gpt-9-nope"
        with self.assertRaises(mr.RouterError) as ctx:
            mr.Router(broken2)
        self.assertEqual(ctx.exception.code, "CONFIG_ERROR")

        with self.assertRaises(mr.RouterError):
            mr.Router({})

    def test_cli_args_never_touch_sandbox(self):
        d = self.router.decide("builder", task=task())
        args = self.router.cli_args(d)
        self.assertIn("-m", args)
        self.assertIn("gpt-6.1-sol", args)
        self.assertIn("model_reasoning_effort=\"medium\"", args)
        # 0.163.0 实测：--ignore-user-config 会把环境降为只读，故默认关闭
        self.assertNotIn("--ignore-user-config", args)
        joined = " ".join(args)
        self.assertNotIn("dangerously", joined)
        self.assertNotIn("-s", args)
        self.assertNotIn("workspace-write", joined)


class TelemetryTests(unittest.TestCase):
    def test_ignore_user_config_toggle(self):
        cfg = json.loads(json.dumps(REAL_CONFIG))
        cfg["model_router"]["ignore_user_config"] = True
        router = mr.Router(cfg)
        args = router.cli_args(router.decide("reviewer", task=task()))
        self.assertIn("--ignore-user-config", args)
        self.assertNotIn("-s", args)

    def test_unverified_actual_model(self):
        router = mr.Router(REAL_CONFIG)
        d = router.decide("builder", task=task())

        class Run:
            duration_ms = 1234
            exit_code = 0
            ok = True
            timed_out = False
            thread_id = "th-1"
            events = [{"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}]

        record = router.telemetry(d, Run())
        self.assertEqual(record["requested_model"], "gpt-6.1-sol")
        self.assertEqual(record["actual_model"], "UNVERIFIED")
        self.assertEqual(record["usage"]["output_tokens"], 5)
        self.assertEqual(record["duration_ms"], 1234)


class TestModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p02b-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_enables_test_mode_but_status_stays_blocked(self):
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "bridge.json").write_text(json.dumps(REAL_CONFIG), encoding="utf-8")
        security.record(self.tmp, security.BLOCKED, "sandbox unenforced")
        self.assertEqual(security.execution_mode(self.tmp), "WINDOWS_TEST_MODE")
        ok, message = security.require_auto_dev(self.tmp)
        self.assertTrue(ok)
        self.assertIn("WINDOWS_TEST_MODE", message)
        self.assertEqual(security.load(self.tmp)["status"], security.BLOCKED)
        self.assertFalse(security.is_verified(self.tmp))

    def test_test_mode_requires_risk_acceptance(self):
        cfg = json.loads(json.dumps(REAL_CONFIG))
        cfg["windows_test_mode"]["risk_accepted"] = False
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "bridge.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertIsNone(security.windows_test_mode(self.tmp))
        ok, _ = security.require_auto_dev(self.tmp)
        self.assertFalse(ok)


class BridgeIntegrationTests(unittest.TestCase):
    """End-to-end with fake codex, exercising routing inside the P02 state machine."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p02b-run-"))
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "bridge.json").write_text(json.dumps(REAL_CONFIG), encoding="utf-8")
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
                        "commit", "-q", "-m", "base"], check=True)
        self.bridge = p02.P02Bridge(self.tmp, codex_cmd=[sys.executable, FAKE_CODEX],
                                    sandbox_status="SANDBOX_UNENFORCED")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, request_id="p02b_route_001", **codex):
        base = {
            "schema_version": "1.0", "request_id": request_id, "project_id": "demo-app",
            "title": "路由验证", "goal": "g", "requested_paths": ["src/**"],
            "constraints": ["c"], "non_goals": ["n"],
            "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
            "workspace": {"repo": "demo/soloship-demo", "base_ref": "HEAD", "allow_paths": ["src", "tests"]},
            "verify": {"test_command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       "timeout_seconds": 120},
            "codex": {"sandbox": "workspace-write", "timeout_seconds": 300, **codex},
        }
        return base

    def _run(self, payload, build="strict", review="auto"):
        with mock.patch.dict(os.environ, {"FAKE_BUILD_MODE": build, "FAKE_REVIEW_MODE": review}):
            self.bridge.inbox.mkdir(parents=True, exist_ok=True)
            (self.bridge.inbox / f"{payload['request_id']}.ready.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.bridge.poll()
        receipt_path = self.bridge.receipts / f"demo-app__{payload['request_id']}.receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        report = json.loads((self.bridge.reports /
                             f"demo-app__{payload['request_id']}.report.json").read_text(encoding="utf-8"))
        return receipt, report

    def test_explorer_builder_reviewer_use_different_tiers(self):
        receipt, report = self._run(self._task(explore=True))
        self.assertEqual(receipt["status"], p02.READY)
        roles = {r["role"]: r for r in report["routes"]}
        self.assertEqual(roles["explorer"]["tier"], "fast")
        self.assertEqual(roles["builder"]["tier"], "balanced")
        self.assertEqual(roles["reviewer"]["tier"], "balanced")
        self.assertNotEqual(roles["explorer"]["model"], roles["builder"]["model"])

        calls = {c["role"]: c for c in receipt["calls"]}
        self.assertEqual(calls["explorer"]["requested_model"], "gpt-6-luna")
        self.assertEqual(calls["reviewer"]["requested_effort"], "high")
        self.assertTrue(all(c["actual_model"] == "UNVERIFIED" for c in receipt["calls"]))
        self.assertEqual(receipt["execution_mode"], "WINDOWS_TEST_MODE")
        self.assertTrue(receipt["risk_accepted"])
        self.assertEqual(receipt["isolation_claim"], "NONE")
        self.assertEqual(receipt["sandbox_status"], "SANDBOX_UNENFORCED")

    def test_fix_round_two_escalates_to_deep(self):
        receipt, report = self._run(self._task("p02b_esc_001"), build="buggy_forever", review="changes")
        self.assertEqual(receipt["status"], p02.BLOCKED_LIMIT)
        self.assertEqual(receipt["fix_rounds"], 2)
        fixes = [c for c in receipt["calls"] if c["role"].startswith("fixer")]
        self.assertEqual([f["role"] for f in fixes], ["fixer_1", "fixer_2"])
        self.assertEqual([f["tier"] for f in fixes], ["balanced", "deep"])
        self.assertEqual(fixes[1]["requested_model"], "gpt-6-astra")

    def test_explicit_model_passes_through(self):
        receipt, _ = self._run(self._task("p02b_model_001", model="gpt-6-astra", effort="high"))
        builder = [c for c in receipt["calls"] if c["role"] == "builder"][0]
        self.assertEqual(builder["requested_model"], "gpt-6-astra")
        self.assertEqual(builder["requested_effort"], "high")

    def test_unavailable_model_blocks(self):
        receipt, _ = self._run(self._task("p02b_bad_001", model="gpt-9-nope"))
        self.assertEqual(receipt["status"], p02.BLOCKED_MODEL)

    def test_unregistered_project_rejected_in_test_mode(self):
        # Empty the approved list: the P02 test-mode gate must reject demo-app itself.
        self.bridge.test_mode = dict(self.bridge.test_mode or {}, approved_projects=[])
        payload = self._task("p02b_other_001")
        self.bridge.inbox.mkdir(parents=True, exist_ok=True)
        (self.bridge.inbox / "p02b_other_001.ready.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.bridge.poll()
        error = self.bridge.receipts / "p02b_other_001.ready.json.error.json"
        self.assertTrue(error.is_file())
        self.assertIn("未注册", error.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
