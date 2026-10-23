#!/usr/bin/env python3
"""SoloShip: credential isolation helpers.

Only the GitHub publisher may see the release token. Every other child process
(Codex builder/explorer/reviewer/fixer, test executor, git probes) gets a scrubbed
environment. Tokens are matched by name, never inspected or logged.
"""
from __future__ import annotations

import os

SECRET_ENV_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_PAT",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_CLIENT_SECRET",
    "NPM_TOKEN",
    "DOCKER_PASSWORD",
    "HF_TOKEN",
)

SECRET_ENV_PREFIXES = ("GITHUB_", "GH_", "CODEX_", "OPENAI_")
SECRET_ENV_SUFFIXES = ("_TOKEN", "_SECRET", "_API_KEY", "_PASSWORD")

# 路径型变量：名字会被前缀规则命中，但不携带任何凭据值，只决定工具去哪里找登录态。
# 默认一样剥离（普通测试执行器不该拿到）；只有明确需要认证的 Codex 执行环境
# 才通过 scrub_secrets(..., keep_paths=...) 显式保留。
PATH_ENV_NAMES = ("CODEX_HOME",)


def is_secret_name(name: str) -> bool:
    """是否凭据名。路径型变量（CODEX_HOME）不算凭据。"""
    upper = name.upper()
    if upper in PATH_ENV_NAMES:
        return False
    if upper in SECRET_ENV_NAMES:
        return True
    if any(upper.startswith(prefix) for prefix in SECRET_ENV_PREFIXES):
        return True
    if any(upper.endswith(suffix) for suffix in SECRET_ENV_SUFFIXES):
        return True
    return any(needle in upper for needle in ("SECRET", "TOKEN", "CREDENTIAL", "API_KEY", "APIKEY", "PASSWD"))


def should_strip(name: str, keep_paths: tuple[str, ...] = ()) -> bool:
    """该变量是否应在子进程环境中被移除。"""
    upper = name.upper()
    if upper in PATH_ENV_NAMES:
        allowed = {item.upper() for item in keep_paths}
        return upper not in allowed
    return is_secret_name(name)


def scrub_secrets(env: dict[str, str] | None = None,
                  keep_paths: tuple[str, ...] = ()) -> tuple[dict[str, str], list[str]]:
    """Return a copy of env without credential variables, plus the removed names.

    keep_paths 只能保留 PATH_ENV_NAMES 里登记的路径型变量，传凭据名会被忽略。
    """
    source = dict(os.environ if env is None else env)
    allowed = {item.upper() for item in keep_paths if item.upper() in PATH_ENV_NAMES}
    removed = [key for key in source if should_strip(key, tuple(allowed))]
    for key in removed:
        source.pop(key, None)
    return source, sorted(removed)


def has_secret(env: dict[str, str]) -> list[str]:
    return sorted(key for key in env if is_secret_name(key))
