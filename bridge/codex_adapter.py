#!/usr/bin/env python3
"""SoloShip P01: Codex CLI adapter.

Runs `codex exec` non-interactively inside a workspace, collects JSONL events,
exit code, session (thread) id and the final message. Never uses
--dangerously-bypass-approvals-and-sandbox and never enables network access.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from env_guard import PATH_ENV_NAMES, scrub_secrets
from pathlib import Path

DEFAULT_SANDBOX = "workspace-write"
ALLOWED_SANDBOXES = ("read-only", "workspace-write")

AUTH_MARKERS = (
    "not logged in",
    "please log in",
    "please run `codex login`",
    "unauthorized",
    "401",
    "invalid api key",
    "authentication failed",
)

# error_kind values
CLI_MISSING = "cli_missing"
AUTH_FAILED = "auth_failed"
TIMEOUT = "timeout"
NONZERO_EXIT = "nonzero_exit"
SPAWN_ERROR = "spawn_error"
OK = "ok"


@dataclass
class CodexRun:
    ok: bool
    error_kind: str
    exit_code: int | None
    timed_out: bool
    thread_id: str | None
    events: list[dict] = field(default_factory=list)
    last_message: str = ""
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    command: list[str] = field(default_factory=list)
    detail: str = ""
    model: str | None = None
    effort: str | None = None

    def evidence(self) -> dict:
        return {
            "error_kind": self.error_kind,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "thread_id": self.thread_id,
            "duration_ms": self.duration_ms,
            "command": self.command,
            "model": self.model,
            "effort": self.effort,
            "last_message": self.last_message.strip(),
            "stderr_tail": "\n".join(self.stderr.strip().splitlines()[-8:]),
            "event_count": len(self.events),
        }


def resolve_codex(explicit: list[str] | None = None) -> list[str] | None:
    """Return the argv prefix used to launch Codex, or None when unavailable."""
    if explicit:
        return list(explicit)
    exe = shutil.which("codex")
    if not exe:
        return None
    return [exe]


def codex_version(codex_cmd: list[str] | None = None, timeout_s: int = 60) -> str | None:
    cmd = resolve_codex(codex_cmd)
    if cmd is None:
        return None
    try:
        proc = subprocess.run(
            cmd + ["--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.lower().startswith("codex-cli"):
            return line
    return (proc.stdout.strip().splitlines() or [None])[0]


def child_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Codex 子进程唯一的环境来源：先合并，再清洗，路径型变量保留。

    run_codex 与 login_state 必须共用它，否则预检环境和执行环境不一致，
    会得到「预检已登录、实际 401」的假阴性。
    """
    env = {**os.environ, **(extra_env or {})}
    # CODEX_HOME 是登录态所在路径，只有 Codex 认证环境保留；
    # 测试执行器走 p02_support.sanitized_env，默认仍然剥离。
    env, _removed = scrub_secrets(env, keep_paths=PATH_ENV_NAMES)
    env.setdefault("PYTHONUTF8", "1")
    return env


def login_state(codex_cmd: list[str] | None = None, timeout_s: int = 60,
                env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Return (logged_in, raw_message). Read-only check, no files touched."""
    cmd = resolve_codex(codex_cmd)
    if cmd is None:
        return False, "codex executable not found on PATH"
    child = child_env() if env is None else env
    try:
        proc = subprocess.run(
            cmd + ["login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            env=child,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    text = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    return proc.returncode == 0 and "logged in" in text.lower(), text


def _parse_events(raw: str) -> tuple[list[dict], str | None]:
    events: list[dict] = []
    thread_id: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        events.append(obj)
        if obj.get("type") == "thread.started" and isinstance(obj.get("thread_id"), str):
            thread_id = obj["thread_id"]
    return events, thread_id


def _classify(exit_code: int, stderr: str, last_message: str, events: list[dict]) -> tuple[bool, str, str]:
    haystack = f"{stderr}\n{last_message}".lower()
    failed_turn = any(ev.get("type") in ("turn.failed", "error") for ev in events)
    if exit_code == 0 and not failed_turn:
        return True, OK, ""
    if any(marker in haystack for marker in AUTH_MARKERS):
        return False, AUTH_FAILED, "Codex CLI 未登录或认证失效"
    if failed_turn and exit_code == 0:
        return False, NONZERO_EXIT, "Codex 报告 turn.failed"
    return False, NONZERO_EXIT, f"codex exec 退出码 {exit_code}"


def run_codex(
    prompt: str,
    workspace: str | Path,
    *,
    sandbox: str = DEFAULT_SANDBOX,
    timeout_s: int = 900,
    codex_cmd: list[str] | None = None,
    last_message_path: str | Path | None = None,
    extra_env: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
    model: str | None = None,
    effort: str | None = None,
    ignore_user_config: bool = False,
) -> CodexRun:
    """Run one `codex exec` turn and collect verifiable evidence."""
    started = time.monotonic()
    cmd_prefix = resolve_codex(codex_cmd)
    if cmd_prefix is None:
        return CodexRun(
            ok=False,
            error_kind=CLI_MISSING,
            exit_code=None,
            timed_out=False,
            thread_id=None,
            detail="未找到 codex 可执行文件，跳过执行",
        )
    if sandbox not in ALLOWED_SANDBOXES:
        return CodexRun(
            ok=False,
            error_kind=SPAWN_ERROR,
            exit_code=None,
            timed_out=False,
            thread_id=None,
            detail=f"拒绝使用沙箱模式: {sandbox}（只允许 {', '.join(ALLOWED_SANDBOXES)}）",
        )

    workdir = Path(workspace).resolve()
    if not workdir.is_dir():
        return CodexRun(
            ok=False,
            error_kind=SPAWN_ERROR,
            exit_code=None,
            timed_out=False,
            thread_id=None,
            detail=f"工作区不存在: {workdir}",
        )

    last_path = Path(last_message_path).resolve() if last_message_path else None
    cmd = cmd_prefix + [
        "exec",
        "-C", str(workdir),
        "-s", sandbox,
        "--json",
    ]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    if ignore_user_config:
        cmd.append("--ignore-user-config")
    if last_path is not None:
        last_path.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-o", str(last_path)]
    if extra_args:
        cmd += list(extra_args)
    cmd.append("-")  # prompt comes from stdin, avoids shell quoting entirely

    # Codex must never see GitHub/Codex credentials: they belong to the publisher only.
    # Scrub runs AFTER extra_env is merged so injected values cannot bypass it.
    # CODEX_HOME is a path, not a credential; scrubbing it breaks Codex login.
    env = child_env(extra_env)

    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        return CodexRun(
            ok=False,
            error_kind=CLI_MISSING if isinstance(exc, FileNotFoundError) else SPAWN_ERROR,
            exit_code=None,
            timed_out=False,
            thread_id=None,
            command=cmd,
            model=model,
            effort=effort,
            duration_ms=int((time.monotonic() - started) * 1000),
            detail=f"启动失败: {type(exc).__name__}: {exc}",
        )

    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        stdout, stderr = proc.communicate()
    duration_ms = int((time.monotonic() - started) * 1000)

    stdout = stdout or ""
    stderr = stderr or ""
    events, thread_id = _parse_events(stdout)

    last_message = ""
    if last_path is not None and last_path.is_file():
        last_message = last_path.read_text(encoding="utf-8", errors="replace")

    if timed_out:
        return CodexRun(
            ok=False,
            error_kind=TIMEOUT,
            exit_code=proc.returncode,
            timed_out=True,
            thread_id=thread_id,
            events=events,
            last_message=last_message,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            command=cmd,
            model=model,
            effort=effort,
            detail=f"执行超过 {timeout_s}s，已终止",
        )

    ok, kind, detail = _classify(proc.returncode, stderr, last_message, events)
    return CodexRun(
        ok=ok,
        error_kind=kind,
        exit_code=proc.returncode,
        timed_out=False,
        thread_id=thread_id,
        events=events,
        last_message=last_message,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        command=cmd,
        model=model,
        effort=effort,
        detail=detail,
    )
