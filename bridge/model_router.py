#!/usr/bin/env python3
"""SoloShip model router.

Maps agent roles (explorer / builder / reviewer / fixer) to a model tier and a
reasoning effort, then to concrete `codex exec` arguments. Rules are data-driven
from config/bridge.json and are owned by the Bridge, not by task prompts.

Hard rules:
  - only whitelisted models may be requested;
  - an unavailable or non-whitelisted model fails loudly (no silent fallback);
  - model escalation never touches sandbox or approval flags;
  - the second fix round is Deep and the fix budget is still owned by the bridge.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TIER_ORDER = ["fast", "balanced", "deep"]

MODEL_EFFORTS = {
    "gpt-6-luna": ["low", "medium", "high", "xhigh", "max"],
    "gpt-6.1-sol": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-6-astra": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "gpt-6-sol": ["low", "medium", "high", "xhigh", "max", "ultra"],
}

CONFIG_ERROR = "CONFIG_ERROR"
MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
TIER_UNKNOWN = "TIER_UNKNOWN"
EFFORT_UNSUPPORTED = "EFFORT_UNSUPPORTED"
ROLE_UNKNOWN = "ROLE_UNKNOWN"


DEFAULT_CONFIG = {
    "model_router": {
        "tiers": {
            "fast": {"model": "gpt-6-luna", "effort": "low"},
            "balanced": {"model": "gpt-6.1-sol", "effort": "medium"},
            "deep": {"model": "gpt-6-astra", "effort": "high"},
        },
        "roles": {
            "explorer": {"tier": "fast", "effort": "low"},
            "builder": {"tier": "balanced", "effort": "medium"},
            "reviewer": {"tier": "balanced", "effort": "high"},
            "fixer_1": {"tier": "balanced", "effort": "high"},
            "fixer_2": {"tier": "deep", "effort": "high"},
        },
        "high_risk_reviewer": {"tier": "deep", "effort": "high"},
        "whitelist": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"],
        "supported_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
        "complexity": {"low_max": 2, "high_min": 6,
                       "delta": {"low": -1, "medium": 0, "high": 1}},
        "ignore_user_config": True,
    }
}


class RouterError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass
class RouteDecision:
    role: str
    tier: str
    model: str
    effort: str
    reason: str
    complexity: str = "medium"
    risk: str = "normal"
    source: str = "role_default"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "tier": self.tier,
            "model": self.model,
            "effort": self.effort,
            "reason": self.reason,
            "complexity": self.complexity,
            "risk": self.risk,
            "source": self.source,
            "extra": self.extra,
        }


def complexity_score(task: dict | None) -> int:
    """Deterministic, prompt-independent complexity estimate."""
    if not isinstance(task, dict):
        return 3
    score = 0
    workspace = task.get("workspace") or {}
    allow_paths = workspace.get("allow_paths") or []
    acceptance = task.get("acceptance") or []
    constraints = task.get("constraints") or []
    goal = task.get("goal") or ""
    requested = task.get("requested_paths") or []
    if len(allow_paths) > 1:
        score += 1
    if len(acceptance) >= 4:
        score += 2
    elif len(acceptance) >= 2:
        score += 1
    if len(constraints) >= 4:
        score += 1
    if len(goal) > 250:
        score += 1
    if len(requested) > 2:
        score += 1
    return score


def complexity_level(score: int, cfg: dict) -> str:
    low_max = int(cfg.get("low_max", 2))
    high_min = int(cfg.get("high_min", 6))
    if score <= low_max:
        return "low"
    if score >= high_min:
        return "high"
    return "medium"


class Router:
    def __init__(self, config: dict):
        router_cfg = config.get("model_router") if isinstance(config, dict) else None
        if not isinstance(router_cfg, dict):
            raise RouterError(CONFIG_ERROR, "缺少 model_router 配置")
        self.cfg = router_cfg
        self.tiers: dict[str, dict] = self.cfg.get("tiers") or {}
        self.roles: dict[str, dict] = self.cfg.get("roles") or {}
        self.whitelist: list[str] = list(self.cfg.get("whitelist") or [])
        self.efforts: list[str] = list(self.cfg.get("supported_efforts") or [])
        self.complexity_cfg = self.cfg.get("complexity") or {}
        self.high_risk_reviewer = self.cfg.get("high_risk_reviewer") or {"tier": "deep", "effort": "high"}
        self.ignore_user_config = bool(self.cfg.get("ignore_user_config", True))
        self._validate_config()

    @classmethod
    def from_root(cls, root: Path) -> "Router":
        import security

        config = security.bridge_config(root)
        if not config.get("model_router"):
            config = DEFAULT_CONFIG
        return cls(config)

    # ------------------------------------------------------------ validation
    def _validate_config(self) -> None:
        if not self.tiers:
            raise RouterError(CONFIG_ERROR, "tiers 为空")
        for name in TIER_ORDER:
            if name not in self.tiers:
                raise RouterError(CONFIG_ERROR, f"缺少档位: {name}")
        if not self.whitelist:
            raise RouterError(CONFIG_ERROR, "whitelist 为空")
        for name, spec in self.tiers.items():
            model = spec.get("model")
            effort = spec.get("effort")
            if model not in self.whitelist:
                raise RouterError(CONFIG_ERROR, f"档位 {name} 的模型不在白名单: {model}")
            if model not in MODEL_EFFORTS:
                raise RouterError(MODEL_UNAVAILABLE, f"模型未在本机目录中: {model}")
            if effort not in self.efforts or effort not in MODEL_EFFORTS[model]:
                raise RouterError(EFFORT_UNSUPPORTED, f"档位 {name} 的推理强度不受支持: {effort}")
        for role, spec in self.roles.items():
            if spec.get("tier") not in self.tiers:
                raise RouterError(CONFIG_ERROR, f"角色 {role} 引用了未知档位: {spec.get('tier')}")
            effort = spec.get("effort")
            if effort not in self.efforts:
                raise RouterError(EFFORT_UNSUPPORTED, f"角色 {role} 的推理强度不支持: {effort}")
        if self.high_risk_reviewer.get("tier") not in self.tiers:
            raise RouterError(CONFIG_ERROR, "high_risk_reviewer 档位无效")

    # --------------------------------------------------------------- routing
    def _resolve(self, tier: str, effort: str, role: str, reason: str, *, complexity: str,
                 risk: str, source: str, extra: dict | None = None) -> RouteDecision:
        if tier not in self.tiers:
            raise RouterError(TIER_UNKNOWN, f"未知档位: {tier}")
        model = self.tiers[tier]["model"]
        if model not in self.whitelist:
            raise RouterError(MODEL_NOT_ALLOWED, f"模型不在白名单: {model}")
        if model not in MODEL_EFFORTS:
            raise RouterError(MODEL_UNAVAILABLE, f"模型不可用: {model}")
        if effort not in MODEL_EFFORTS[model]:
            raise RouterError(EFFORT_UNSUPPORTED, f"{model} 不支持推理强度 {effort}")
        return RouteDecision(role=role, tier=tier, model=model, effort=effort, reason=reason,
                             complexity=complexity, risk=risk, source=source, extra=extra or {})

    def decide(self, role: str, *, task: dict | None = None, fix_round: int = 0,
               override: dict | None = None, complexity: str | None = None,
               risk: str | None = None) -> RouteDecision:
        if role == "fixer":
            role = "fixer_2" if fix_round >= 2 else "fixer_1"
        if role not in self.roles:
            raise RouterError(ROLE_UNKNOWN, f"未知角色: {role}")

        task = task if isinstance(task, dict) else {}
        risk = (risk or (task.get("codex") or {}).get("risk_level") or "normal").lower()
        if risk not in ("normal", "high"):
            raise RouterError(CONFIG_ERROR, f"risk_level 非法: {risk}")
        score = complexity_score(task)
        level = complexity or complexity_level(score, self.complexity_cfg)

        override = override if override is not None else {
            key: (task.get("codex") or {})[key]
            for key in ("tier", "model", "effort")
            if isinstance(task.get("codex"), dict) and key in task["codex"]
        }
        if not isinstance(override, dict):
            raise RouterError(CONFIG_ERROR, "override 必须是对象")
        unknown = set(override) - {"model", "tier", "effort"}
        if unknown:
            raise RouterError(CONFIG_ERROR, f"override 含未知字段: {', '.join(sorted(unknown))}")

        # 1) explicit model / effort from the task contract (Bridge-validated)
        explicit_model = override.get("model")
        explicit_tier = override.get("tier")
        explicit_effort = override.get("effort")

        if explicit_model is not None:
            if explicit_model not in self.whitelist:
                raise RouterError(MODEL_NOT_ALLOWED, f"任务显式指定的模型不在白名单: {explicit_model}")
            if explicit_model not in MODEL_EFFORTS:
                raise RouterError(MODEL_UNAVAILABLE, f"任务显式指定的模型不可用: {explicit_model}")
            tier = next((name for name, spec in self.tiers.items() if spec["model"] == explicit_model), None)
            if tier is None:
                raise RouterError(MODEL_NOT_ALLOWED, f"模型未绑定到任何档位: {explicit_model}")
            effort = explicit_effort or self.tiers[tier]["effort"]
            return self._resolve(tier, effort, role, "任务显式指定模型", complexity=level,
                                 risk=risk, source="task_override")

        if explicit_tier is not None and explicit_tier not in self.tiers:
            raise RouterError(TIER_UNKNOWN, f"任务显式档位无效: {explicit_tier}")

        # 2) forced upgrades that prompts cannot bypass
        if role == "fixer_2":
            spec = self.roles["fixer_2"]
            return self._resolve(spec["tier"], spec.get("effort", "high"), role,
                                 "第二次修复强制升级 Deep", complexity=level, risk=risk,
                                 source="policy_escalation")
        if role == "fixer_1":
            spec = self.roles["fixer_1"]
            return self._resolve(spec["tier"], spec.get("effort", "high"), role,
                                 "首次修复固定 Balanced", complexity=level, risk=risk,
                                 source="policy_escalation")
        if role == "reviewer" and risk == "high":
            spec = self.high_risk_reviewer
            return self._resolve(spec["tier"], spec.get("effort", "high"), role,
                                 "高风险任务使用 Deep Reviewer", complexity=level, risk=risk,
                                 source="policy_high_risk")

        # 3) explicit tier (still clamped by role lock for fixer_2 / high-risk reviewer)
        if explicit_tier is not None:
            spec = self.tiers[explicit_tier]
            effort = explicit_effort or spec["effort"]
            return self._resolve(explicit_tier, effort, role, "任务显式指定档位", complexity=level,
                                 risk=risk, source="task_override")

        # 4) role default, adjusted upward by task complexity (never downgraded)
        base_tier = self.roles[role]["tier"]
        base_effort = self.roles[role].get("effort", self.tiers[base_tier]["effort"])
        delta = max(0, int(self.complexity_cfg.get("delta", {}).get(level, 0)))
        index = min(len(TIER_ORDER) - 1, TIER_ORDER.index(base_tier) + delta)
        tier = TIER_ORDER[index]
        effort = base_effort if tier == base_tier else self.tiers[tier]["effort"]
        if explicit_effort:
            effort = explicit_effort
        reason = f"角色默认 {base_tier}，复杂度 {level}（score={score}）调整到 {tier}"
        return self._resolve(tier, effort, role, reason, complexity=level, risk=risk,
                             source="complexity_adjusted" if tier != base_tier else "role_default",
                             extra={"complexity_score": score})

    # ------------------------------------------------------------ CLI wiring
    def cli_args(self, decision: RouteDecision) -> list[str]:
        """Only model/effort switches; never sandbox or approval flags."""
        args = ["-m", decision.model, "-c", f'model_reasoning_effort="{decision.effort}"']
        if self.ignore_user_config:
            args.append("--ignore-user-config")
        return args

    def telemetry(self, decision: RouteDecision, run: Any, role_label: str | None = None) -> dict:
        usage = getattr(run, "usage", None)
        events = getattr(run, "events", []) or []
        for event in events:
            if isinstance(event, dict) and event.get("type") == "turn.completed":
                usage = event.get("usage")
        return {
            "role": role_label or decision.role,
            "tier": decision.tier,
            "requested_model": decision.model,
            "requested_effort": decision.effort,
            "actual_model": "UNVERIFIED",
            "actual_model_note": "codex exec 的 JSONL 事件不返回模型标识，无法从运行记录确认实际模型",
            "route_source": decision.source,
            "route_reason": decision.reason,
            "complexity": decision.complexity,
            "risk": decision.risk,
            "duration_ms": getattr(run, "duration_ms", None),
            "exit_code": getattr(run, "exit_code", None),
            "ok": getattr(run, "ok", None),
            "timed_out": getattr(run, "timed_out", None),
            "thread_id": getattr(run, "thread_id", None),
            "usage": usage,
        }
