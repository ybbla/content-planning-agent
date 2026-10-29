"""输入模型与共享数据结构。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BUSINESS_TOOLS = {"search_assets", "generate_script", "estimate_cost", "save_plan"}


class ScenarioError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _number(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioError("invalid_input", f"{field} 必须是数字")
    if isinstance(value, float) and not math.isfinite(value):
        raise ScenarioError("invalid_input", f"{field} 必须是大于等于 0 的有限数字")
    if value < 0:
        raise ScenarioError("invalid_input", f"{field} 必须是大于等于 0 的有限数字")
    return value


@dataclass(frozen=True)
class Scenario:
    task_id: str
    theme: str
    budget: int | float | None
    deadline_hours: int | float | None
    quality_preference: str = "high"
    fault: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Scenario":
        if not isinstance(raw, dict):
            raise ScenarioError("invalid_input", "场景输入必须是 JSON 对象")
        allowed = {"task_id", "theme", "budget", "deadline_hours", "quality_preference", "fault"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ScenarioError("invalid_input", "输入包含未知字段", {"fields": unknown})

        task_id = raw.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            raise ScenarioError("invalid_input", "task_id 须匹配 [A-Za-z0-9_-]{1,64}")
        theme = raw.get("theme")
        if not isinstance(theme, str) or not theme.strip():
            raise ScenarioError("invalid_input", "theme 必须是非空字符串")
        try:
            theme.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ScenarioError("invalid_input", "theme 包含无效 Unicode 字符") from exc
        quality = raw.get("quality_preference", "high")
        if not isinstance(quality, str) or quality not in {"high", "economy"}:
            raise ScenarioError("invalid_input", "quality_preference 只能是 high 或 economy")

        budget = raw.get("budget")
        if budget is not None:
            budget = _number(budget, "budget")
        deadline = raw.get("deadline_hours")
        if deadline is not None:
            deadline = _number(deadline, "deadline_hours")

        fault = raw.get("fault")
        if fault is not None:
            if not isinstance(fault, dict) or set(fault) != {"tool", "kind"}:
                raise ScenarioError("invalid_input", "fault 必须且只能包含 tool、kind")
            if not isinstance(fault["tool"], str) or fault["tool"] not in BUSINESS_TOOLS:
                raise ScenarioError("invalid_input", "fault.tool 不是业务工具")
            if not isinstance(fault["kind"], str) or fault["kind"] not in {"transient", "permanent"}:
                raise ScenarioError("invalid_input", "fault.kind 只能是 transient 或 permanent")
            fault = {"tool": fault["tool"], "kind": fault["kind"]}

        return cls(task_id, theme.strip(), budget, deadline, quality, fault)

    def missing_reason(self) -> tuple[str, str] | None:
        if self.budget is None:
            return "missing_budget", "缺少预算 budget"
        if self.deadline_hours is None:
            return "missing_deadline", "缺少截止时间 deadline_hours"
        return None

    def public_dict(self) -> dict[str, Any]:
        """提供给模型的业务输入，不暴露测试故障配置。"""
        return {
            "task_id": self.task_id,
            "theme": self.theme,
            "budget": self.budget,
            "deadline_hours": self.deadline_hours,
            "quality_preference": self.quality_preference,
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(self.public_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
