#!/usr/bin/env python3
"""Direct local test without ChatGPT Work; it cannot verify Work permissions."""
from pathlib import Path
import json
import os
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
request_id = "p00_local_" + uuid.uuid4().hex[:12]
obj = {
    "schema_version": "1.0",
    "request_id": request_id,
    "project_id": "demo-app",
    "title": "P00 本地 Bridge 连通性测试",
    "goal": "仅验证任务回执与报告写入，不修改代码",
    "requested_paths": ["src/**"],
    "constraints": ["不得修改任何业务代码"],
    "non_goals": ["真实调用 Codex", "创建 GitHub PR"],
    "acceptance": [
        {"id": "AC-01", "description": "Bridge 生成回执", "verification": "automated"},
        {"id": "AC-02", "description": "Bridge 生成模拟报告", "verification": "automated"},
    ],
}
inbox = ROOT / "exchange" / "inbox"
inbox.mkdir(parents=True, exist_ok=True)
pending = inbox / f"{request_id}.pending.json"
ready = inbox / f"{request_id}.ready.json"
with open(pending, "w", encoding="utf-8", newline="\n") as f:
    json.dump(obj, f, ensure_ascii=False, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
os.replace(pending, ready)
print(f"本地任务已提交：{ready}")
print("注意：这只是程序内自测，不证明 ChatGPT Work 能写入文件。")
