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

import security  # noqa: E402


class SecurityGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p02s-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_status_is_unverified(self):
        self.assertEqual(security.load(self.tmp)["status"], security.UNVERIFIED)
        self.assertFalse(security.is_verified(self.tmp))

    def test_blocked_status_refuses_auto_dev(self):
        security.record(self.tmp, security.BLOCKED, "mxc unusable")
        ok, message = security.require_auto_dev(self.tmp)
        self.assertFalse(ok)
        self.assertIn("自动开发被阻断", message)

    def test_corrupt_status_is_unverified(self):
        path = security.status_file(self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken", encoding="utf-8")
        self.assertEqual(security.load(self.tmp)["status"], security.UNVERIFIED)

    def test_verified_allows_auto_dev(self):
        security.record(self.tmp, security.VERIFIED, "measured")
        ok, _ = security.require_auto_dev(self.tmp)
        self.assertTrue(ok)

    def _run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "bridge" / script), "--root", str(self.tmp), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            cwd=str(ROOT), stdin=subprocess.DEVNULL,
        )

    def test_p02_once_is_blocked_without_verification(self):
        proc = self._run("p02_bridge.py", "--once")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("SANDBOX-BLOCKED", proc.stdout)

    def test_p01_once_is_blocked_without_verification(self):
        proc = self._run("p01_bridge.py", "--once")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("SANDBOX-BLOCKED", proc.stdout)

    def test_p02_once_runs_after_verification(self):
        security.record(self.tmp, security.VERIFIED, "measured")
        proc = self._run("p02_bridge.py", "--once")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("本次处理 0 个文件", proc.stdout)

    def test_p00_file_handoff_still_works(self):
        # P00 must stay usable: no security gate on the file-handoff bridge.
        proc = self._run("p00_bridge.py", "--once")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("本次处理 0 个文件", proc.stdout)


    def test_external_repo_requires_authorization(self):
        import p01_bridge

        ext = Path(tempfile.mkdtemp(prefix="ext-repo-"))
        subprocess.run(["git", "-C", str(ext), "init", "-q"], check=True)
        task = {
            "schema_version": "1.0", "request_id": "ext_repo_0001", "project_id": "demo-app",
            "title": "t", "goal": "g", "requested_paths": ["src/**"],
            "constraints": ["c"], "non_goals": ["n"],
            "acceptance": [{"id": "AC-01", "description": "d", "verification": "automated"}],
            "workspace": {"repo": str(ext), "base_ref": "HEAD", "allow_paths": ["src"]},
            "verify": {"test_command": ["py", "-3", "-m", "unittest"], "timeout_seconds": 60},
        }
        errors = p01_bridge.validate_p01_task(task, self.tmp)
        self.assertTrue(any("已授权" in item for item in errors), errors)

        (self.tmp / "config").mkdir(exist_ok=True)
        (self.tmp / "config" / "bridge.json").write_text(json.dumps({
            "windows_test_mode": {"enabled": True, "risk_accepted": True,
                                  "approved_absolute_repos": [str(ext)]}
        }), encoding="utf-8")
        errors2 = p01_bridge.validate_p01_task(task, self.tmp)
        self.assertFalse(any("已授权" in item for item in errors2), errors2)
        self.assertEqual(security.approved_absolute_repos(self.tmp), [str(ext.resolve())])

    def test_repo_status_is_currently_blocked(self):
        self.assertFalse(security.is_verified(ROOT), "本机沙箱未验证，状态文件不应为 VERIFIED")
        data = security.load(ROOT)
        self.assertEqual(data["status"], security.BLOCKED)
        self.assertIn("mxc", json.dumps(data, ensure_ascii=False).lower())


if __name__ == "__main__":
    unittest.main()
