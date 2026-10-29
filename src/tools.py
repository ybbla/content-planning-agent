"""确定性业务工具；模型只能选择动作，不能绕过这里的不变量。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .schemas import Scenario
from .state import StoreError, TaskStore, plan_checksum


@dataclass
class ToolResult:
    ok: bool
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def observation(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "message": self.message, "data": deepcopy(self.data)}


@dataclass
class RunState:
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    scripts: dict[str, dict[str, Any]] = field(default_factory=dict)
    estimates: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_estimate: dict[str, Any] | None = None
    saved_plan: dict[str, Any] | None = None


class BusinessTools:
    def __init__(
        self,
        scenario: Scenario,
        assets: list[dict[str, Any]],
        fees: dict[str, dict[str, int]],
        store: TaskStore,
        model_info: dict[str, str],
    ):
        self.scenario = scenario
        self.assets = {asset["asset_id"]: asset for asset in assets}
        self.fees = fees
        self.store = store
        self.model_info = model_info
        self.state = RunState()
        self.fault = scenario.fault
        self.fault_injected = False
        self._handlers: dict[str, Callable[[dict[str, Any]], ToolResult]] = {
            "search_assets": self._search_assets,
            "generate_script": self._generate_script,
            "estimate_cost": self._estimate_cost,
            "save_plan": self._save_plan,
        }
        self._arguments = {
            "search_assets": {"theme"},
            "generate_script": {"theme"},
            "estimate_cost": {"asset_id", "script_id"},
            "save_plan": {"task_id", "asset_id", "script_id", "estimate_id"},
        }

    def execute(self, action: str, arguments: dict[str, Any]) -> ToolResult:
        if action not in self._handlers:
            return ToolResult(False, "UNKNOWN_ACTION", f"未知动作: {action}")
        if not isinstance(arguments, dict) or set(arguments) != self._arguments[action]:
            return ToolResult(
                False,
                "INVALID_ARGUMENTS",
                f"{action} 参数必须且只能包含: {sorted(self._arguments[action])}",
            )
        if not all(isinstance(value, str) and value for value in arguments.values()):
            return ToolResult(False, "INVALID_ARGUMENTS", "所有工具参数必须是非空字符串")

        fault = self.fault
        if fault and fault["tool"] == action:
            permanent = fault["kind"] == "permanent"
            if permanent or not self.fault_injected:
                self.fault_injected = True
                return ToolResult(
                    False,
                    "PERMANENT_TOOL_FAILURE" if permanent else "TEMPORARY_TOOL_FAILURE",
                    f"注入的{'永久' if permanent else '临时'}故障；工具未执行且无副作用",
                )
        return self._handlers[action](arguments)

    def successful_results(self) -> dict[str, Any]:
        return {
            "candidates": [deepcopy(item) for item in self.state.candidates.values()],
            "scripts": [deepcopy(item) for item in self.state.scripts.values()],
        }

    def _search_assets(self, arguments: dict[str, Any]) -> ToolResult:
        if arguments["theme"] != self.scenario.theme:
            return ToolResult(False, "THEME_MISMATCH", "检索主题必须与任务主题一致")
        self.state.candidates = deepcopy(self.assets)
        return ToolResult(
            True,
            "ASSETS_FOUND",
            "检索到候选素材",
            {"candidates": [deepcopy(item) for item in self.state.candidates.values()]},
        )

    def _generate_script(self, arguments: dict[str, Any]) -> ToolResult:
        if not self.state.candidates:
            return ToolResult(False, "SEARCH_REQUIRED", "请先生成素材检索结果")
        if arguments["theme"] != self.scenario.theme:
            return ToolResult(False, "THEME_MISMATCH", "脚本主题必须与任务主题一致")
        for script in self.state.scripts.values():
            if script["theme"] == self.scenario.theme:
                data = deepcopy(script)
                data["reused"] = True
                return ToolResult(True, "SCRIPT_REUSED", "复用已成功脚本", data)

        script_id = f"script-{len(self.state.scripts) + 1}"
        script = {
            "script_id": script_id,
            "theme": self.scenario.theme,
            "text": f"围绕“{self.scenario.theme}”的三段式活动内容脚本：预热、发布、复盘。",
            "cost": self.fees["generate_script"]["cost"],
            "hours": self.fees["generate_script"]["hours"],
            "reused": False,
        }
        self.state.scripts[script_id] = script
        return ToolResult(True, "SCRIPT_GENERATED", "脚本生成成功", deepcopy(script))

    def _calculate(self, asset_id: str, script_id: str) -> dict[str, Any] | None:
        asset = self.state.candidates.get(asset_id)
        script = self.state.scripts.get(script_id)
        if not asset or not script or script["theme"] != self.scenario.theme:
            return None
        total_cost = asset["cost"] + self.fees["generate_script"]["cost"] + self.fees["save_plan"]["cost"]
        total_hours = asset["hours"] + self.fees["generate_script"]["hours"] + self.fees["save_plan"]["hours"]
        cost_shortfall = max(0, total_cost - self.scenario.budget)
        hours_shortfall = max(0, total_hours - self.scenario.deadline_hours)
        violations = []
        if cost_shortfall > 0:
            violations.append("budget_exceeded")
        if hours_shortfall > 0:
            violations.append("deadline_exceeded")
        return {
            "asset_id": asset_id,
            "script_id": script_id,
            "total_cost": total_cost,
            "total_hours": total_hours,
            "feasible": not violations,
            "violations": violations,
            "cost_shortfall": cost_shortfall,
            "hours_shortfall": hours_shortfall,
        }

    def _estimate_cost(self, arguments: dict[str, Any]) -> ToolResult:
        if arguments["asset_id"] not in self.state.candidates:
            return ToolResult(False, "ASSET_NOT_FOUND", "素材不属于本次成功检索结果")
        if arguments["script_id"] not in self.state.scripts:
            return ToolResult(False, "SCRIPT_NOT_FOUND", "脚本不存在或尚未成功生成")
        quote = self._calculate(arguments["asset_id"], arguments["script_id"])
        if quote is None:
            return ToolResult(False, "DEPENDENCY_MISMATCH", "脚本与任务主题不匹配")
        estimate_id = f"estimate-{len(self.state.estimates) + 1}"
        estimate = {"estimate_id": estimate_id, **quote}
        self.state.estimates[estimate_id] = estimate
        self.state.latest_estimate = estimate
        return ToolResult(
            True,
            "ESTIMATED",
            "估算完成；feasible 表示方案是否满足约束",
            deepcopy(estimate),
        )

    def _save_plan(self, arguments: dict[str, Any]) -> ToolResult:
        if arguments["task_id"] != self.scenario.task_id:
            return ToolResult(False, "TASK_MISMATCH", "保存 task_id 与当前任务不一致")
        estimate = self.state.latest_estimate
        expected = (arguments["asset_id"], arguments["script_id"], arguments["estimate_id"])
        if not estimate or expected != (estimate["asset_id"], estimate["script_id"], estimate["estimate_id"]):
            return ToolResult(False, "LATEST_ESTIMATE_REQUIRED", "保存参数必须匹配当前最新估算")

        quote = self._calculate(arguments["asset_id"], arguments["script_id"])
        if quote is None:
            return ToolResult(False, "DEPENDENCY_MISMATCH", "保存依赖无效")
        if not quote["feasible"]:
            return ToolResult(False, "CONSTRAINT_VIOLATION", "预算或截止时间不允许保存", quote)
        if self.state.saved_plan:
            return ToolResult(False, "ALREADY_SAVED", "本次运行已经保存方案")

        asset = self.state.candidates[arguments["asset_id"]]
        script = self.state.scripts[arguments["script_id"]]
        plan = {
            "task_id": self.scenario.task_id,
            "theme": self.scenario.theme,
            "asset": deepcopy(asset),
            "script": deepcopy(script),
            "save": deepcopy(self.fees["save_plan"]),
            "total_cost": quote["total_cost"],
            "total_hours": quote["total_hours"],
        }
        record = {
            "task_id": self.scenario.task_id,
            "fingerprint": self.scenario.fingerprint(),
            "plan_checksum": plan_checksum(plan),
            "saved_plan": plan,
            "total_cost": quote["total_cost"],
            "total_hours": quote["total_hours"],
            "source_model": deepcopy(self.model_info),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.store.save(record)
        except StoreError as exc:
            return ToolResult(False, "PERSISTENCE_ERROR", f"方案保存失败: {exc}")
        self.state.saved_plan = plan
        return ToolResult(True, "PLAN_SAVED", "方案已原子保存", deepcopy(plan))
