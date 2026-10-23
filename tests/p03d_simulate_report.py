#!/usr/bin/env python3
"""P03-D 模拟用「假 Bridge」：只写一份报告，不调用 Codex、不访问网络、不调用 gh。

供 p03d_phase2_publish.ps1 -Simulate 使用，用来在真实配置文件副本上验证
退出码、判定逻辑与 finally 恢复行为。

场景：
  success       报告 = READY + PUBLISH_PR_CREATED，进程退出 0
  pr-exists     报告 = READY + PUBLISH_PR_EXISTS，进程退出 0
  pr-recovered  报告 = READY + PUBLISH_PR_RECOVERED，进程退出 0
  pushed-no-pr  报告 = READY + PUBLISH_PUSHED（无 PR 链接），进程退出 0
  auth-fail     报告 = READY + PUBLISH_FAILED_AUTH，进程退出 0
  bridge-error  报告 = READY + PUBLISH_PR_CREATED，但进程退出 3（模拟 Bridge 崩溃）

退出码：0 正常写入；3 模拟 Bridge 失败。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HEAD = "fccc42fd74d3c53aefd95d3ceb674f53342f8949"
BRANCH = "soloship/p03d_remove_001"
PR_URL = "https://github.com/zwmhua2-ai-bot/soloship-demo/pull/1"

SCENARIOS = {
    "success": ("PUBLISH_PR_CREATED", PR_URL, 0, "Draft PR 已创建，等待人工验收"),
    "pr-exists": ("PUBLISH_PR_EXISTS", PR_URL, 0, "已有 PR，复用，不重复创建"),
    "pr-recovered": ("PUBLISH_PR_RECOVERED", PR_URL, 0, "崩溃后恢复，复用已有 PR"),
    "pushed-no-pr": ("PUBLISH_PUSHED", None, 0, "仅推送，未创建 PR"),
    "auth-fail": ("PUBLISH_FAILED_AUTH", None, 0, "缺少受限凭据环境变量 GH_TOKEN"),
    "bridge-error": ("PUBLISH_PR_CREATED", PR_URL, 3, "模拟 Bridge 进程崩溃"),
}


def build_report(scenario: str, request_id: str) -> dict:
    status, pr_url, _, detail = SCENARIOS[scenario]
    return {
        "phase": "P02",
        "job_id": "01SIMULATEDJOB",
        "request_id": request_id,
        "project_id": "demo-app",
        "title": "TodoList 增加 remove() 方法",
        "status": "P02_READY_FOR_HUMAN",
        "base_commit": HEAD,
        "head_commit": HEAD,
        "sandbox_status": "SANDBOX_UNENFORCED",
        "execution_mode": "WINDOWS_TEST_MODE",
        "isolation_claim": "NONE",
        "test_isolation": "TEST_ISOLATION_PARTIAL",
        "fix_rounds": 0,
        "evidence_status": {"diff": "OK", "tests": "OK", "review": "OK"},
        "publish": {
            "status": status,
            "job_id": "01SIMULATEDJOB",
            "branch": BRANCH,
            "candidate_sha": HEAD,
            "repo": "zwmhua2-ai-bot/soloship-demo",
            "base_branch": "main",
            "pr_url": pr_url,
            "pr_number": 1 if pr_url else None,
            "stage": "simulated",
            "detail": detail,
        },
        "simulated": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="P03-D 模拟假 Bridge")
    parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    parser.add_argument("--report", required=True)
    parser.add_argument("--request-id", default="p03d_remove_001")
    args = parser.parse_args()

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_report(args.scenario, args.request_id)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    print(f"[SIM] scenario={args.scenario} report={report_path}", flush=True)
    return SCENARIOS[args.scenario][2]


if __name__ == "__main__":
    sys.exit(main())
