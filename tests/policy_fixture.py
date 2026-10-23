#!/usr/bin/env python3
"""共享测试夹具：为临时根写入最小 bridge.json（含测试命令授权策略）。

SS-V01-003 起，`verify.test_command` 必须命中 `test_command_policy.approved`
才允许执行；配置缺失或非法一律拒绝。测试用的临时根不会自动继承仓库配置，
因此这里提供统一入口，避免每个用例各写一份字典。

注意：这是**测试夹具**，不是运行时兜底。生产配置里没有策略段时，Bridge 会拒绝
执行测试，不会回退到这里。
"""
from __future__ import annotations

import json
from pathlib import Path

APPROVED_DISCOVER = ["-m", "unittest", "discover", "-s", "tests"]
APPROVED_DISCOVER_VERBOSE = ["-m", "unittest", "discover", "-s", "tests", "-v"]


def test_command_policy(approved: list[list[str]] | None = None) -> dict:
    entries = approved if approved is not None else [APPROVED_DISCOVER, APPROVED_DISCOVER_VERBOSE]
    return {
        "schema": "soloship.test_command_policy/v1",
        "approved": [{"argv": list(item)} for item in entries],
    }


def windows_test_mode(approved_projects: list[str] | None = None,
                      approved_repos: list[str] | None = None,
                      approved_absolute_repos: list[str] | None = None) -> dict:
    return {
        "enabled": True,
        "risk_accepted": True,
        "approved_projects": approved_projects if approved_projects is not None else ["demo-app"],
        "approved_repos": approved_repos if approved_repos is not None else ["demo/soloship-demo"],
        "approved_absolute_repos": approved_absolute_repos or [],
        "isolation_claim": "NONE",
    }


def bridge_config(*, mode: dict | None = None, policy: dict | None | bool = True,
                  extra: dict | None = None) -> dict:
    """构造一份完整配置；policy=False 表示刻意不写策略段。"""
    config: dict = {"windows_test_mode": mode if mode is not None else windows_test_mode()}
    if policy is True:
        config["test_command_policy"] = test_command_policy()
    elif isinstance(policy, dict):
        config["test_command_policy"] = policy
    if extra:
        config.update(extra)
    return config


def write_config(root: Path, *, mode: dict | None = None, policy: dict | None | bool = True,
                 extra: dict | None = None) -> Path:
    target = Path(root) / "config"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "bridge.json"
    path.write_text(json.dumps(bridge_config(mode=mode, policy=policy, extra=extra),
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def ensure_policy(root: Path, approved: list[list[str]] | None = None) -> Path:
    """给已写好的 bridge.json 补上策略段（保持其余键不变）。

    供既有夹具一行接入，避免把每个 setUp 的字典都重写一遍。
    """
    path = Path(root) / "config" / "bridge.json"
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
        if not isinstance(data, dict):
            data = {}
    data.setdefault("test_command_policy", test_command_policy(approved))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def python_launcher_available() -> bool:
    """py 启动器是否可解析（Linux/CI 上可能没有，用于跳过对应用例）。"""
    import shutil

    return shutil.which("py") is not None


__all__ = [
    "APPROVED_DISCOVER",
    "APPROVED_DISCOVER_VERBOSE",
    "test_command_policy",
    "windows_test_mode",
    "bridge_config",
    "write_config",
    "ensure_policy",
    "python_launcher_available",
]
