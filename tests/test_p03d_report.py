import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bridge"))

import p02_bridge as p02  # noqa: E402


def build_evidence(publish=None, sandbox_status="SANDBOX_UNENFORCED"):
    evidence = {
        "job_id": "01TESTJOB",
        "request_id": "p03d_remove_001",
        "status": "P02_READY_FOR_HUMAN",
        "codex_version": "codex-cli 0.163.0",
        "sandbox_mode": "workspace-write",
        "sandbox_status": sandbox_status,
        "execution_mode": "WINDOWS_TEST_MODE",
        "isolation_claim": "NONE",
        "risk_accepted": True,
        "test_isolation": "TEST_ISOLATION_PARTIAL",
        "base_commit": "base000",
        "head_commit": "head000",
        "evidence_status": {"diff": "OK"},
        "routes": [],
        "calls": [],
        "artifacts": {},
        "rounds": [],
    }
    if publish is not None:
        evidence["publish"] = publish
    return evidence


class ReportBoundaryTests(unittest.TestCase):
    def setUp(self):
        # render_report 不依赖实例状态，直接绕过 __init__ 构造。
        self.bridge = p02.P02Bridge.__new__(p02.P02Bridge)
        self.receipt = {"fix_rounds": 0}

    def render(self, publish=None):
        return self.bridge.render_report(build_evidence(publish), self.receipt, None)

    def test_created_pr_report_does_not_claim_never_pushed(self):
        text = self.render({"status": "PUBLISH_PR_CREATED",
                            "branch": "soloship/p03d_remove_001",
                            "pr_url": "https://github.com/o/r/pull/7",
                            "detail": "ok"})
        self.assertIn("已按授权推送分支 `soloship/p03d_remove_001`", text)
        self.assertIn("Draft PR", text)
        self.assertNotIn("未推送远端、未创建 PR", text)
        self.assertIn("未合并、未部署", text)

    def test_existing_pr_is_reported_as_pr(self):
        text = self.render({"status": "PUBLISH_PR_EXISTS",
                            "branch": "soloship/p03d_remove_001",
                            "pr_url": "https://github.com/o/r/pull/7"})
        self.assertIn("创建/复用 Draft PR", text)
        self.assertNotIn("未创建 PR、未合并", text)

    def test_recovered_pr_is_reported_as_pr(self):
        text = self.render({"status": "PUBLISH_PR_RECOVERED",
                            "branch": "soloship/p03d_remove_001",
                            "pr_url": "https://github.com/o/r/pull/7"})
        self.assertIn("创建/复用 Draft PR", text)

    def test_pushed_without_pr_is_stated_explicitly(self):
        text = self.render({"status": "PUBLISH_PUSHED", "branch": "soloship/p03d_remove_001"})
        self.assertIn("未创建 PR", text)
        self.assertNotIn("未推送远端", text)

    def test_failed_publish_is_not_reported_as_success(self):
        text = self.render({"status": "PUBLISH_FAILED_AUTH", "branch": "soloship/x",
                            "detail": "缺少受限凭据环境变量 GH_TOKEN"})
        self.assertIn("发布未完成", text)
        self.assertIn("PUBLISH_FAILED_AUTH", text)
        self.assertNotIn("已按授权推送分支", text)

    def test_no_publish_record_keeps_old_wording(self):
        text = self.render()
        self.assertIn("未推送远端、未创建 PR、未合并、未部署", text)

    def test_sandbox_boundary_line_still_present(self):
        text = self.render()
        self.assertIn("SANDBOX_UNENFORCED", text)
        self.assertIn("未在 OS 层强制", text)


if __name__ == "__main__":
    unittest.main()
