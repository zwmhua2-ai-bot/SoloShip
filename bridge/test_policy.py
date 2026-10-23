#!/usr/bin/env python3
"""SS-V01-003: trusted test-command policy.

任务 JSON 的 `verify.test_command` 只是**请求**，不是授权。真正允许执行什么，
由 Bridge 管理的 `config/bridge.json` → `test_command_policy.approved` 决定。

判定规则（顺序即拒绝优先级）：

1. 配置必须存在、可读、结构合法且 `approved` 非空 —— 否则**全部拒绝**（fail closed），
   不回退到任何内置白名单。
2. `argv[0]` 必须是受信任的 Python 解释器或 `py` 启动器：
   - 名称必须是 `python` / `python.exe` / `python3` / `python3.exe` / `py` / `py.exe`；
   - 必须能解析成真实路径，且解析后（跟随符号链接/联接）的**父目录**在受信任目录集合内。
     「某个绝对路径存在」本身不构成授权。
   - **PATH 只用于定位候选文件，不构成信任来源**：`shutil.which()` 命中的目录不会被加入
     可信集合，因此把同名 `python.exe` 放到 PATH 前面的用户目录里不会获得授权。
3. `py` 启动器允许紧随一个 `-3` / `-3.x` 版本选择参数，其余参数一律按原样匹配。
4. 去掉解释器后的 argv **必须与 `approved` 中的某一条完整相等**（严格匹配，不支持前缀/通配）。
5. `-c` 内联代码永远拒绝；未出现在 `approved` 里的 `-m` 模块单独给出更明确的拒绝原因。
6. 放行时返回的 `argv[0]` 是**已验证的规范化绝对路径**（其余参数原样保留），
   调用方必须直接执行该返回值，避免"校验一个路径、执行另一个路径"。

本模块只做纯判定，不启动任何进程；执行入口（P01/P02）必须在使用返回值前再次调用，
不得直接相信任务 JSON 里的原始命令。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

POLICY_KEY = "test_command_policy"
SCHEMA = "soloship.test_command_policy/v1"

PYTHON_NAMES = {"python", "python.exe", "python3", "python3.exe"}
LAUNCHER_NAMES = {"py", "py.exe"}
INTERPRETER_NAMES = PYTHON_NAMES | LAUNCHER_NAMES
_LAUNCHER_VERSION = re.compile(r"^-3(?:\.\d+)?$")

SOURCE_CONFIG = "config"
SOURCE_MISSING = "missing"
SOURCE_INVALID = "invalid"


@dataclass(frozen=True)
class Policy:
    approved: tuple[tuple[str, ...], ...] = ()
    source: str = SOURCE_MISSING
    reason: str = ""
    trusted_dirs: frozenset[Path] = field(default_factory=frozenset)


def config_path(root: Path) -> Path:
    return Path(root).resolve() / "config" / "bridge.json"


def _system_root() -> Path | None:
    """Windows 系统目录（`py.exe` 的固定安装位置）。非 Windows 返回 None。"""
    if sys.platform != "win32":
        return None
    for key in ("SystemRoot", "WINDIR"):
        value = os.environ.get(key)
        if not value:
            continue
        try:
            return Path(value).resolve()
        except (OSError, RuntimeError):
            return None
    return None


def _trusted_dirs(root: Path, extra: Iterable[Any] = ()) -> frozenset[Path]:
    """受信任的解释器目录。

    **不从 PATH 推导**（SS-V01-003-R1）：若把 `shutil.which()` 命中的目录纳入可信集合，
    用户可写目录一旦排在 PATH 前面，同名 `python.exe` 就会自动取得授权。
    PATH 只用于**定位**候选文件，是否可信仍由下面这个固定集合决定：

    - 当前解释器所在目录（`sys.executable`）
    - 解释器安装根（`sys.base_prefix` / `sys.prefix`）
    - Windows 系统目录（`py.exe` 的固定位置，非普通用户可写）
    - 配置 `test_command_policy.trusted_interpreter_dirs`（唯一的人工扩展入口）
    """
    dirs: set[Path] = set()

    def add(value: Any) -> None:
        try:
            dirs.add(Path(str(value)).resolve())
        except (OSError, RuntimeError):
            return

    add(Path(sys.executable).parent)
    for base in (getattr(sys, "base_prefix", None), getattr(sys, "prefix", None)):
        if base:
            add(base)
    system_root = _system_root()
    if system_root is not None:
        add(system_root)
    for item in extra or ():
        add(item)
    return frozenset(dirs)


def _normalize_entries(raw: Any) -> tuple[list[tuple[str, ...]], str]:
    """把配置里的 approved 规范成 argv 尾部列表；返回 (entries, problem)。"""
    if not isinstance(raw, list) or not raw:
        return [], "test_command_policy.approved 必须是非空数组"
    entries: list[tuple[str, ...]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            argv = item.get("argv")
        elif isinstance(item, (list, tuple)):
            argv = list(item)
        else:
            return [], f"test_command_policy.approved[{index}] 结构非法"
        if not isinstance(argv, (list, tuple)) or not argv:
            return [], f"test_command_policy.approved[{index}].argv 必须是非空数组"
        parts: list[str] = []
        for part in argv:
            if not isinstance(part, str) or not part or "\x00" in part:
                return [], f"test_command_policy.approved[{index}] 含非法参数"
            parts.append(part)
        # 允许配置作者把解释器也写进去：去掉后再匹配，避免"看起来像能跑却永远匹配不上"。
        if Path(parts[0].replace("\\", "/")).name.lower() in INTERPRETER_NAMES:
            parts = parts[1:]
            if not parts:
                return [], f"test_command_policy.approved[{index}] 只有解释器，没有参数"
        entries.append(tuple(parts))
    return entries, ""


def load_policy(root: Path) -> Policy:
    """读取并校验策略。任何缺陷都返回空 approved（= 拒绝执行）。"""
    path = config_path(root)
    if not path.is_file():
        return Policy(source=SOURCE_MISSING,
                      reason=f"缺少配置文件 {path}（{POLICY_KEY} 不可读），测试命令未获授权",
                      trusted_dirs=_trusted_dirs(root))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, UnicodeError) as exc:
        return Policy(source=SOURCE_INVALID,
                      reason=f"配置不可读: {type(exc).__name__}，测试命令未获授权",
                      trusted_dirs=_trusted_dirs(root))
    if not isinstance(data, dict):
        return Policy(source=SOURCE_INVALID, reason="配置根节点不是对象，测试命令未获授权",
                      trusted_dirs=_trusted_dirs(root))
    section = data.get(POLICY_KEY)
    if not isinstance(section, dict):
        return Policy(source=SOURCE_MISSING,
                      reason=f"配置缺少 {POLICY_KEY} 段，测试命令未获授权",
                      trusted_dirs=_trusted_dirs(root))
    entries, problem = _normalize_entries(section.get("approved"))
    dirs = _trusted_dirs(root, section.get("trusted_interpreter_dirs"))
    if problem:
        return Policy(source=SOURCE_INVALID, reason=problem, trusted_dirs=dirs)
    return Policy(approved=tuple(entries), source=SOURCE_CONFIG, reason="", trusted_dirs=dirs)


def _reject(reason: str, source: str, argv: list[str]) -> tuple[bool, list[str], str, str]:
    return False, list(argv), reason, source


def _basename(raw: str) -> str:
    return Path(raw.replace("\\", "/")).name.lower()


def _has_parent_ref(raw: str) -> bool:
    return ".." in Path(raw.replace("\\", "/")).parts


def _resolve_interpreter(raw: str, policy: Policy) -> tuple[Path | None, str]:
    name = _basename(raw)
    if name not in INTERPRETER_NAMES:
        return None, (f"test_command 只允许受信任的 Python 解释器"
                      f"（{'/'.join(sorted(INTERPRETER_NAMES))}），实际为 {raw!r}")
    if _has_parent_ref(raw):
        return None, f"test_command 的解释器路径含 '..'，拒绝: {raw!r}"
    has_sep = "/" in raw or "\\" in raw
    candidate = Path(raw) if has_sep else (shutil.which(raw) and Path(shutil.which(raw)))
    if candidate is None:
        return None, f"test_command 的解释器无法解析: {raw!r}"
    if has_sep and not candidate.is_absolute():
        return None, f"test_command 的解释器必须是绝对路径或可解析的命令名: {raw!r}"
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"test_command 的解释器路径无法解析: {raw!r}（{type(exc).__name__}）"
    if not resolved.is_file():
        return None, f"test_command 的解释器不存在: {resolved}"
    if resolved.parent not in policy.trusted_dirs:
        return None, (f"test_command 的解释器所在目录不受信任: {resolved.parent}"
                      "（仅允许当前解释器目录/已配置的可信目录）")
    return resolved, ""


def resolve(root: Path, argv: Any) -> tuple[bool, list[str], str, str]:
    """判定一条测试命令。返回 (是否允许, 规范化后的 argv, 拒绝原因, 策略来源)。"""
    policy = load_policy(root)
    raw_argv = list(argv) if isinstance(argv, (list, tuple)) else []
    if not policy.approved:
        return _reject(policy.reason or "测试命令未获授权", policy.source, raw_argv)

    if not isinstance(argv, (list, tuple)) or not argv:
        return _reject("test_command 必须是非空数组", policy.source, raw_argv)
    for part in argv:
        if not isinstance(part, str) or not part or "\x00" in part:
            return _reject("test_command 只能包含非空字符串", policy.source, raw_argv)

    raw_interpreter = str(argv[0])
    resolved, problem = _resolve_interpreter(raw_interpreter, policy)
    if problem:
        return _reject(problem, policy.source, raw_argv)

    raw_tail = [str(part) for part in argv[1:]]
    tail = raw_tail
    if _basename(raw_interpreter) in LAUNCHER_NAMES and tail and _LAUNCHER_VERSION.match(tail[0]):
        tail = tail[1:]

    if tail and tail[0] == "-c":
        return _reject("test_command 禁止 -c 内联代码（可执行任意代码）", policy.source, raw_argv)

    allowed_modules = {entry[1] for entry in policy.approved if len(entry) >= 2 and entry[0] == "-m"}
    if tail and tail[0] == "-m":
        module = tail[1] if len(tail) >= 2 else ""
        if module and module not in allowed_modules:
            return _reject(f"test_command 使用了未获批准的模块: {module}", policy.source, raw_argv)

    if tuple(tail) not in policy.approved:
        wanted = "；".join(" ".join(entry) for entry in policy.approved)
        return _reject(
            f"test_command 未获批准（实际参数: {' '.join(tail) or '<空>'}；"
            f"允许的命令: {wanted}）", policy.source, raw_argv)

    return True, [str(resolved), *raw_tail], "", policy.source
