#!/usr/bin/env python3
"""SoloShip P02-S: execution-isolation gate.

Auto-development (P01/P02 builders, reviewers, test executors) stays blocked
until a *measured* sandbox verification is recorded. Nothing here can grant
itself VERIFIED: the status file is written only by an explicit verification
run whose measurements live next to it.

Measured on this host, 2026-10-23 (codex-cli 0.163.0):
  - `-c windows.sandbox=mxc sandbox ...` -> exit 1, no output (MXC unusable)
  - `codex sandbox -P :workspace -- ...` -> network blocked, filesystem NOT
    confined (read + write outside the workspace both succeeded)
=> status stays SANDBOX_BLOCKED.
"""
from __future__ import annotations

import json
from pathlib import Path

VERIFIED = "SANDBOX_VERIFIED"
BLOCKED = "SANDBOX_BLOCKED"
UNVERIFIED = "SANDBOX_UNVERIFIED"

ALLOWED_WITHOUT_GATE = {"selfcheck", "probe", "cleanup"}


def status_file(root: Path) -> Path:
    return Path(root).resolve() / ".control" / "security" / "sandbox_status.json"


def config_file(root: Path) -> Path:
    return Path(root).resolve() / "config" / "bridge.json"


def bridge_config(root: Path) -> dict:
    path = config_file(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def windows_test_mode(root: Path) -> dict | None:
    """User-approved native-Windows test mode. Never reports itself as isolation."""
    mode = bridge_config(root).get("windows_test_mode")
    if not isinstance(mode, dict) or not mode.get("enabled") or not mode.get("risk_accepted"):
        return None
    return mode


def execution_mode(root: Path) -> str:
    if is_verified(root):
        return "SANDBOX_VERIFIED"
    if windows_test_mode(root):
        return "WINDOWS_TEST_MODE"
    return "BLOCKED"


def approved_absolute_repos(root: Path) -> list[str]:
    """Repos outside the SoloShip root that the operator explicitly authorized."""
    mode = bridge_config(root).get("windows_test_mode") or {}
    raw = mode.get("approved_absolute_repos") or []
    resolved = []
    for item in raw:
        try:
            resolved.append(str(Path(item).resolve()))
        except (OSError, RuntimeError):
            continue
    return resolved


def load(root: Path) -> dict:
    path = status_file(root)
    if not path.is_file():
        return {"status": UNVERIFIED, "reason": "尚未执行隔离验证"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return {"status": UNVERIFIED, "reason": f"状态文件不可读: {type(exc).__name__}"}
    if not isinstance(data, dict) or "status" not in data:
        return {"status": UNVERIFIED, "reason": "状态文件缺少 status 字段"}
    return data


def is_verified(root: Path) -> bool:
    return load(root).get("status") == VERIFIED


def require_auto_dev(root: Path) -> tuple[bool, str]:
    data = load(root)
    if data.get("status") == VERIFIED:
        return True, "隔离已验证"
    mode = windows_test_mode(root)
    if mode:
        return True, (
            "WINDOWS_TEST_MODE 已启用：允许在已注册的演示项目上自动开发。"
            "这不是沙箱隔离，路径检查仅为事后检测。"
        )
    reason = data.get("reason") or data.get("status")
    return False, (
        f"自动开发被阻断：沙箱状态 {data.get('status')}（{reason}）。"
        "仅 P00 文件交接、自检与诊断命令可用。"
    )


def record(root: Path, status: str, reason: str, **fields) -> Path:
    if status not in (VERIFIED, BLOCKED, UNVERIFIED):
        raise ValueError(f"非法状态: {status}")
    path = status_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "reason": reason, **fields}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def guard_or_exit(root: Path, mode: str) -> None:
    """mode: watch / once / selfcheck / probe / cleanup."""
    if mode in ALLOWED_WITHOUT_GATE:
        return
    ok, message = require_auto_dev(root)
    if not ok:
        print(f"[SANDBOX-BLOCKED] {message}", flush=True)
        raise SystemExit(3)
