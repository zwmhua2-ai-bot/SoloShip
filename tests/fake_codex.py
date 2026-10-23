#!/usr/bin/env python3
"""Fake `codex` used by tests/test_p01.py and tests/test_p02.py.

Mimics the adapter's argv shape (`exec -C <dir> -s <sandbox> --json -o <file> -`)
and picks behaviour from env vars:

  FAKE_CODEX_MODE   legacy P01 modes: edit / nochange / fail / auth / timeout /
                    outside / breaktest
  FAKE_BUILD_MODE   P02 builder: strict / buggy_once / buggy_forever / outside / nochange
  FAKE_REVIEW_MODE  P02 reviewer: auto / approve / changes / invalid / tamper / stale /
                    shortsha / suffixsha   （后两个用于 SS-V01-004 的 SHA 严格绑定回归）

Reviewer mode is selected by the prompt itself (contains 独立代码审查员), so the
same binary can play both roles.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

STRICT_MARKER = "STRICT_OK"


def parse(argv: list[str]) -> tuple[Path, Path | None]:
    cwd = Path.cwd()
    last = None
    for index, arg in enumerate(argv):
        if arg in ("-C", "--cd") and index + 1 < len(argv):
            cwd = Path(argv[index + 1])
        if arg in ("-o", "--output-last-message") and index + 1 < len(argv):
            last = Path(argv[index + 1])
    return cwd, last


def emit(thread_id: str, message: str) -> None:
    events = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"item_type": "agent_message", "text": message}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    for event in events:
        print(json.dumps(event, ensure_ascii=False), flush=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def head_sha(cwd: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or "0" * 40
    except (OSError, subprocess.SubprocessError):
        return "0" * 40


def feature_source(strict: bool) -> str:
    marker = f"    # {STRICT_MARKER}\n" if strict else ""
    trim = ("        if not title.strip():\n            return False\n" if strict
            else "        if not title:\n            return False\n")
    return (
        '"""feature module produced by fake codex."""\n\n\n'
        "class Feature:\n"
        '    """Tiny list with remove()."""\n\n'
        + marker +
        "    def __init__(self):\n        self._items = []\n\n"
        "    def add(self, title: str) -> None:\n"
        "        self._items.append(title.strip())\n\n"
        "    def remove(self, title: str) -> bool:\n"
        + trim +
        "        if title in self._items:\n"
        "            self._items.remove(title)\n            return True\n"
        "        return False\n\n"
        "    def list_all(self):\n        return list(self._items)\n\n"
        f"# revision: {time.time_ns()}\n"
    )


def feature_tests(strict: bool) -> str:
    blank_case = '"   "' if strict else '""'
    return (
        "import sys\nfrom pathlib import Path\nimport unittest\n\n"
        'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))\n'
        "from feature import Feature\n\n\n"
        "class FeatureTests(unittest.TestCase):\n"
        "    def test_remove(self):\n"
        "        f = Feature()\n        f.add('A')\n        f.add('B')\n"
        "        self.assertTrue(f.remove('A'))\n        self.assertEqual(f.list_all(), ['B'])\n\n"
        "    def test_remove_missing(self):\n"
        "        self.assertFalse(Feature().remove('X'))\n\n"
        "    def test_blank(self):\n"
        f"        self.assertFalse(Feature().remove({blank_case}))\n\n\n"
        'if __name__ == "__main__":\n    unittest.main()\n'
    )


def do_review(cwd: Path, last: Path | None, thread_id: str) -> int:
    mode = os.environ.get("FAKE_REVIEW_MODE", "auto")
    sha = head_sha(cwd)
    feature = cwd / "src" / "feature.py"
    strict = feature.is_file() and STRICT_MARKER in feature.read_text(encoding="utf-8", errors="replace")

    if mode == "tamper":
        write(cwd / "src" / "tampered.py", "# reviewer must not write\n")
    if mode == "invalid":
        text = "{not valid json,,"
    else:
        approve = (mode in ("approve", "shortsha", "suffixsha", "tamper")
                   or (mode == "auto" and strict))
        if mode == "changes":
            approve = False
        findings = [] if approve else [{
            "severity": "BLOCKER",
            "file": "src/feature.py",
            "line": 14,
            "description": "remove() 未处理仅含空白的输入，与验收标准 AC-03 不符",
            "evidence": "diff 中 `if not title:` 只判断空字符串，未 strip",
            "suggested_fix": "改为 `if not title.strip(): return False`",
        }]
        # SS-V01-004：shortsha / suffixsha 用来证明"自报 PASS 但 SHA 不是完整一致"必须被拒。
        payload_sha = sha
        if mode == "stale":
            payload_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        elif mode == "shortsha":
            payload_sha = sha[:7]
        elif mode == "suffixsha":
            payload_sha = sha + "0"
        text = json.dumps({
            "status": "APPROVED" if approve else "CHANGES_REQUESTED",
            "candidate_sha": payload_sha,
            "findings": findings,
            "summary": "实现满足任务要求" if approve else "存在 1 个阻塞问题",
        }, ensure_ascii=False)

    if last is not None:
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(text, encoding="utf-8")
    emit(thread_id, text[:200])
    return 0


def do_build(cwd: Path, prompt: str, last: Path | None, thread_id: str) -> int:
    mode = os.environ.get("FAKE_BUILD_MODE", "strict")
    is_fix = "你正在修复" in prompt

    if mode == "nochange":
        emit(thread_id, "无需修改。")
        return 0
    if mode == "outside" and is_fix:
        write(cwd / "README.md", "# out of scope\n")
        emit(thread_id, "改了 README。")
        return 0

    strict = mode == "strict" or (mode == "buggy_once" and is_fix)
    write(cwd / "src" / "feature.py", feature_source(strict))
    write(cwd / "tests" / "test_feature.py", feature_tests(strict))
    emit(thread_id, f"写入 src/feature.py 与 tests/test_feature.py（strict={strict}）")
    if last is not None:
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text("fake build done\n", encoding="utf-8")
    return 0


def dump_env_if_requested() -> None:
    path = os.environ.get("FAKE_ENV_DUMP")
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(sorted(os.environ)), encoding="utf-8")


def main() -> int:
    argv = sys.argv[1:]
    dump_env_if_requested()

    if argv[:1] == ["--version"]:
        print("codex-cli 0.0.0-fake")
        return 0
    if argv[:2] == ["login", "status"]:
        # 设 FAKE_CODEX_REQUIRE_CODEX_HOME=1 可验证「后台子进程确实拿到了认证环境」
        if os.environ.get("FAKE_CODEX_REQUIRE_CODEX_HOME") == "1" and not os.environ.get("CODEX_HOME"):
            sys.stderr.write("Not logged in: CODEX_HOME 缺失，找不到 auth.json\n")
            return 1
        print("Logged in using ChatGPT")
        return 0
    if not argv or argv[0] != "exec":
        sys.stderr.write(f"fake codex: unexpected argv {argv}\n")
        return 2

    prompt = sys.stdin.read()
    cwd, last = parse(argv)
    thread_id = f"fake-{os.getpid()}"

    if "独立代码审查员" in prompt:
        return do_review(cwd, last, thread_id)
    if os.environ.get("FAKE_BUILD_MODE"):
        return do_build(cwd, prompt, last, thread_id)

    mode = os.environ.get("FAKE_CODEX_MODE", "edit")
    if mode == "timeout":
        time.sleep(300)
        return 0
    if mode == "auth":
        print(json.dumps({"type": "error", "message": "not logged in"}), flush=True)
        sys.stderr.write("ERROR: not logged in. Please run `codex login`.\n")
        return 1
    if mode == "fail":
        print(json.dumps({"type": "thread.started", "thread_id": thread_id}), flush=True)
        sys.stderr.write("simulated codex failure: model unavailable\n")
        return 3
    if mode == "edit":
        write(cwd / "src" / "greeting.py", 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n')
        write(
            cwd / "tests" / "test_greeting.py",
            "import sys\nfrom pathlib import Path\nimport unittest\n\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / \"src\"))\n"
            "from greeting import greet\n\n\n"
            "class GreetTests(unittest.TestCase):\n"
            "    def test_greet(self):\n"
            "        self.assertEqual(greet(\"Solo\"), \"Hello, Solo!\")\n\n\n"
            "if __name__ == \"__main__\":\n    unittest.main()\n",
        )
        emit(thread_id, "已新增 src/greeting.py 与 tests/test_greeting.py，测试通过。")
    elif mode == "nochange":
        emit(thread_id, "无需修改，代码已满足要求。")
    elif mode == "outside":
        write(cwd / "README.md", "# hacked\n")
        emit(thread_id, "改了 README。")
    elif mode == "breaktest":
        write(
            cwd / "tests" / "test_broken.py",
            "import unittest\n\n\nclass Broken(unittest.TestCase):\n"
            "    def test_fails(self):\n        self.assertEqual(1, 2)\n",
        )
        emit(thread_id, "加了一个会失败的测试。")
    else:
        sys.stderr.write(f"unknown FAKE_CODEX_MODE: {mode}\n")
        return 2

    if last is not None:
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text("fake codex done\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
