"""单动作 ReAct 循环与状态一致性控制。"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import asdict
from typing import Any, Callable
from uuid import uuid4

from .model import ModelUnavailable
from .schemas import Scenario
from .state import StoreError, TaskStore, checkpoint_checksum, plan_checksum
from .tools import BusinessTools, RunState, ToolResult

ACTIONS = {"search_assets", "generate_script", "estimate_cost", "save_plan", "finish", "abort"}
ABORT_REASONS = {"no_feasible_plan", "tool_failure", "model_aborted"}
RESUMABLE_REASONS = {"max_steps", "model_unavailable", "tool_failure"}


def early_failure(task_id: str, mode: str, model_name: str, code: str, message: str) -> dict[str, Any]:
    reason = {"code": code, "message": message, "details": {}}
    return {
        "task_id": task_id,
        "run_id": f"run-{uuid4().hex}",
        "status": "unfinished",
        "reason": reason,
        "duplicate": False,
        "resumed": False,
        "resume_count": 0,
        "resumed_from_run_id": None,
        "new_step_count": 0,
        "model": {"mode": mode, "name": model_name},
        "source_model": None,
        "task_plan": {"goal": "完成内容活动方案", "constraints": {}, "steps": []},
        "tool_results": {"candidates": [], "scripts": []},
        "estimates": [],
        "saved_plan": None,
        "total_cost": None,
        "total_hours": None,
        "deficit": None,
        "step_count": 0,
        "diagnostics": [],
    }


class Agent:
    def __init__(
        self,
        scenario: Scenario,
        model_factory: Callable[[], Any],
        tools_factory: Callable[[dict[str, str]], BusinessTools],
        store: TaskStore,
        mode: str,
        model_name: str,
        max_steps: int = 12,
    ):
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 0:
            raise ValueError("max_steps 必须是大于等于 0 的整数")
        self.scenario = scenario
        self.model_factory = model_factory
        self.tools_factory = tools_factory
        self.store = store
        self.mode = mode
        self.model_name = model_name
        self.max_steps = max_steps
        self.run_id = f"run-{uuid4().hex}"
        self.model_info = {"mode": mode, "name": model_name}
        if mode == "real":
            self.model_info["requested_name"] = model_name
        self.steps: list[dict[str, Any]] = []
        self.last_observation: dict[str, Any] | None = None
        self.model_exchanges: list[dict[str, Any]] = []
        self.diagnostics: list[str] = []
        self.resumed = False
        self.resume_count = 0
        self.resumed_from_run_id: str | None = None
        self.new_step_count = 0
        self.checkpoint_error: str | None = None
        self.tools: BusinessTools | None = None
        self.trace: dict[str, Any] = {}

    def _base_result(self) -> dict[str, Any]:
        return early_failure(self.scenario.task_id, self.mode, self.model_name, "internal", "未初始化") | {
            "run_id": self.run_id,
            "model": deepcopy(self.model_info),
            "reason": None,
            "task_plan": {
                "goal": f"为主题“{self.scenario.theme}”完成内容活动方案",
                "constraints": {
                    "budget": self.scenario.budget,
                    "deadline_hours": self.scenario.deadline_hours,
                    "quality_preference": self.scenario.quality_preference,
                },
                "steps": self.steps,
            },
        }

    def _checkpoint_data(self) -> dict[str, Any]:
        return {
            "version": 1,
            "task_id": self.scenario.task_id,
            "fingerprint": self.scenario.fingerprint(),
            "mode": self.mode,
            "model_info": deepcopy(self.model_info),
            "run_id": self.run_id,
            "resume_count": self.resume_count,
            "run_state": asdict(self.tools.state) if self.tools else asdict(RunState()),
            "steps": deepcopy(self.steps),
            "model_exchanges": deepcopy(self.model_exchanges),
            "last_observation": deepcopy(self.last_observation),
            "diagnostics": deepcopy(self.diagnostics),
            "fault": deepcopy(self.tools.fault if self.tools else self.scenario.fault),
            "fault_injected": self.tools.fault_injected if self.tools else False,
        }

    def _persist_checkpoint(self) -> None:
        if self.tools:
            checkpoint = self._checkpoint_data()
            checkpoint["checksum"] = checkpoint_checksum(checkpoint)
            self.store.save_checkpoint(checkpoint)

    def _checkpoint_error(self, checkpoint: dict[str, Any]) -> str | None:
        try:
            json.dumps(checkpoint, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (UnicodeError, ValueError, TypeError):
            return "续跑检查点包含无法安全序列化的内容"
        required = {"version", "task_id", "fingerprint", "mode", "model_info", "run_id", "resume_count", "run_state", "steps", "model_exchanges", "last_observation", "diagnostics", "fault", "fault_injected", "checksum"}
        if not required.issubset(checkpoint) or checkpoint["version"] != 1:
            return "续跑检查点版本或字段无效"
        if not isinstance(checkpoint["checksum"], str) or checkpoint["checksum"] != checkpoint_checksum(checkpoint):
            return "续跑检查点校验和不匹配"
        if checkpoint["task_id"] != self.scenario.task_id or not isinstance(checkpoint["fingerprint"], str):
            return "续跑检查点任务标识无效"
        model_info = checkpoint["model_info"]
        if not isinstance(checkpoint["mode"], str) or checkpoint["mode"] not in {"real", "mock"} or not isinstance(model_info, dict) or not isinstance(model_info.get("name"), str):
            return "续跑检查点模型信息无效"
        if checkpoint["mode"] == "real" and not isinstance(model_info.get("requested_name"), str):
            return "真实续跑检查点缺少请求模型名"
        if not isinstance(checkpoint["run_id"], str) or isinstance(checkpoint["resume_count"], bool) or not isinstance(checkpoint["resume_count"], int) or checkpoint["resume_count"] < 0:
            return "续跑检查点运行标识无效"
        state = checkpoint["run_state"]
        if not isinstance(state, dict) or set(state) != {"candidates", "scripts", "estimates", "latest_estimate", "saved_plan"}:
            return "续跑检查点运行态结构无效"
        if any(not isinstance(state[field], dict) for field in ("candidates", "scripts", "estimates")):
            return "续跑检查点工具结果结构无效"
        if state["saved_plan"] is not None:
            return "没有成功记录时，检查点不能包含已保存方案"
        candidates, scripts, estimates = state["candidates"], state["scripts"], state["estimates"]
        if any(key != value.get("asset_id") or value.get("theme", self.scenario.theme) != self.scenario.theme for key, value in candidates.items() if isinstance(value, dict)) or any(not isinstance(value, dict) for value in candidates.values()):
            return "续跑检查点候选结构无效"
        if list(scripts) != [f"script-{index}" for index in range(1, len(scripts) + 1)]:
            return "续跑检查点脚本 ID 不连续"
        if any(key != value.get("script_id") or value.get("theme") != self.scenario.theme for key, value in scripts.items() if isinstance(value, dict)) or any(not isinstance(value, dict) for value in scripts.values()):
            return "续跑检查点脚本内容无效"
        if list(estimates) != [f"estimate-{index}" for index in range(1, len(estimates) + 1)]:
            return "续跑检查点估算 ID 不连续"
        if any(key != value.get("estimate_id") or value.get("asset_id") not in candidates or value.get("script_id") not in scripts for key, value in estimates.items() if isinstance(value, dict)) or any(not isinstance(value, dict) for value in estimates.values()):
            return "续跑检查点估算依赖无效"
        expected_latest = list(estimates.values())[-1] if estimates else None
        if state["latest_estimate"] != expected_latest:
            return "续跑检查点最新估算不一致"
        steps, exchanges = checkpoint["steps"], checkpoint["model_exchanges"]
        if not isinstance(steps, list) or not isinstance(exchanges, list):
            return "续跑检查点步骤结构无效"
        required_step = {"step", "action", "arguments", "rationale", "succeeded", "code", "result_summary", "error"}
        if any(not isinstance(step, dict) or not required_step.issubset(step) for step in steps) or [step["step"] for step in steps] != list(range(1, len(steps) + 1)):
            return "续跑检查点步骤编号或字段无效"
        if any(not isinstance(step["arguments"], dict) or not isinstance(step["rationale"], str) or not isinstance(step["succeeded"], bool) or not isinstance(step["result_summary"], dict) for step in steps):
            return "续跑检查点步骤内容无效"
        if checkpoint["mode"] == "real":
            if len(exchanges) != len(steps) or any(not isinstance(item, dict) for item in exchanges):
                return "续跑检查点 API 元数据与步骤不一致"
            if [item.get("step") for item in exchanges] != list(range(1, len(steps) + 1)):
                return "续跑检查点 API 元数据步骤不连续"
            if any(not isinstance(item.get("response_id"), str) or not item["response_id"] or not isinstance(item.get("provider_model"), str) or isinstance(item.get("created"), bool) or not isinstance(item.get("created"), int) for item in exchanges):
                return "续跑检查点 API 元数据内容无效"
            if len({item["response_id"] for item in exchanges}) != len(exchanges):
                return "续跑检查点 API 响应 ID 重复"
        elif exchanges:
            return "Mock 续跑检查点不应包含 API 元数据"
        last = checkpoint["last_observation"]
        if not steps:
            if last is not None:
                return "零步骤检查点不应包含最后观察"
        elif not isinstance(last, dict) or last.get("action") != steps[-1]["action"] or last.get("arguments") != steps[-1]["arguments"] or last.get("ok") != steps[-1]["succeeded"] or last.get("code") != steps[-1]["code"] or last.get("data") != steps[-1]["result_summary"]:
            return "续跑检查点最后观察与最后步骤不一致"
        fault = checkpoint["fault"]
        if fault is not None and (not isinstance(fault, dict) or set(fault) != {"tool", "kind"} or fault["tool"] not in {"search_assets", "generate_script", "estimate_cost", "save_plan"} or fault["kind"] not in {"transient", "permanent"}):
            return "续跑检查点故障配置无效"
        if not isinstance(checkpoint["diagnostics"], list) or not all(isinstance(item, str) for item in checkpoint["diagnostics"]) or not isinstance(checkpoint["fault_injected"], bool):
            return "续跑检查点诊断或故障状态无效"
        return None

    def _restore_checkpoint(self, checkpoint: dict[str, Any], preserve_model: bool = False) -> None:
        self.resumed = True
        self.resume_count = checkpoint["resume_count"] + 1
        self.resumed_from_run_id = checkpoint["run_id"]
        if not preserve_model:
            self.model_info = deepcopy(checkpoint["model_info"])
            self.tools.model_info = deepcopy(self.model_info)
        self.steps = deepcopy(checkpoint["steps"])
        self.model_exchanges = deepcopy(checkpoint["model_exchanges"])
        self.last_observation = deepcopy(checkpoint["last_observation"])
        self.diagnostics = deepcopy(checkpoint["diagnostics"]) + ["resumed"]
        self.tools.state = RunState(**deepcopy(checkpoint["run_state"]))
        self.tools.fault = deepcopy(checkpoint["fault"])
        self.tools.fault_injected = checkpoint["fault_injected"]

    def _finalize(
        self,
        status: str,
        reason: dict[str, Any] | None = None,
        saved_record: dict[str, Any] | None = None,
        duplicate: bool = False,
    ) -> dict[str, Any]:
        if status == "unfinished" and self.tools and reason and reason["code"] in RESUMABLE_REASONS:
            try:
                self._persist_checkpoint()
                self.checkpoint_error = None
            except StoreError as exc:
                self.checkpoint_error = str(exc)
        elif status == "unfinished" and self.tools and reason and reason["code"] in {"no_feasible_plan", "model_aborted"}:
            try:
                self.store.delete_checkpoint(self.scenario.task_id)
            except StoreError as exc:
                self.diagnostics.append(f"non_resumable_cleanup_failed: {exc}")
        if self.checkpoint_error and status == "unfinished":
            reason = self._reason("storage_error", f"续跑检查点保存失败: {self.checkpoint_error}")
        if status == "completed":
            try:
                self.store.delete_checkpoint(self.scenario.task_id)
            except StoreError as exc:
                self.diagnostics.append(f"checkpoint_cleanup_failed: {exc}")
        if self.checkpoint_error and status == "completed":
            self.diagnostics.append("checkpoint_write_failed_after_save")
        result = self._base_result()
        state = self.tools.state if self.tools else None
        saved_plan = saved_record["saved_plan"] if saved_record else (deepcopy(state.saved_plan) if state else None)
        total_cost = saved_record["total_cost"] if saved_record else (saved_plan["total_cost"] if saved_plan else None)
        total_hours = saved_record["total_hours"] if saved_record else (saved_plan["total_hours"] if saved_plan else None)
        source_model = deepcopy(saved_record.get("source_model")) if saved_record else (deepcopy(self.model_info) if saved_plan else None)
        result.update(
            {
                "status": status,
                "reason": reason,
                "duplicate": duplicate,
                "resumed": self.resumed,
                "resume_count": self.resume_count,
                "resumed_from_run_id": self.resumed_from_run_id,
                "new_step_count": self.new_step_count,
                "source_model": source_model,
                "tool_results": self.tools.successful_results() if self.tools else {"candidates": [], "scripts": []},
                "estimates": deepcopy(list(state.estimates.values())) if state else [],
                "saved_plan": saved_plan,
                "total_cost": total_cost,
                "total_hours": total_hours,
                "deficit": reason.get("details", {}).get("deficit") if reason else None,
                "step_count": len(self.steps),
                "diagnostics": list(self.diagnostics),
            }
        )
        self.trace = {
            "task_id": self.scenario.task_id,
            "run_id": self.run_id,
            "model": deepcopy(self.model_info),
            "model_exchanges": deepcopy(self.model_exchanges),
            "source_model": deepcopy(result["source_model"]),
            "duplicate": duplicate,
            "resumed": self.resumed,
            "resume_count": self.resume_count,
            "resumed_from_run_id": self.resumed_from_run_id,
            "new_step_count": self.new_step_count,
            "steps": deepcopy(self.steps),
            "final_status": status,
            "reason": deepcopy(reason),
            "diagnostics": list(self.diagnostics),
            "total_cost": total_cost,
            "total_hours": total_hours,
        }
        return result

    @staticmethod
    def _reason(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"code": code, "message": message, "details": details or {}}

    def _existing_record_error(self, record: dict[str, Any]) -> str | None:
        try:
            json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (UnicodeError, ValueError, TypeError):
            return "成功任务记录包含无法安全序列化的内容"
        required = {"task_id", "fingerprint", "plan_checksum", "saved_plan", "total_cost", "total_hours", "source_model", "created_at"}
        if not required.issubset(record):
            return "成功任务记录缺少必要字段"
        plan = record["saved_plan"]
        if not isinstance(plan, dict) or not {"task_id", "theme", "asset", "script", "total_cost", "total_hours"}.issubset(plan):
            return "成功任务记录中的方案结构无效"
        if record["task_id"] != self.scenario.task_id or plan["task_id"] != self.scenario.task_id:
            return "成功任务记录的任务 ID 不匹配"
        if not isinstance(plan["theme"], str) or not plan["theme"].strip() or not isinstance(plan["asset"], dict) or not isinstance(plan["script"], dict) or not isinstance(plan.get("save"), dict):
            return "成功任务记录中的方案内容无效"
        asset = plan["asset"]
        script = plan["script"]
        if not {"asset_id", "name", "quality", "cost", "hours"}.issubset(asset) or not {"script_id", "theme", "text", "cost", "hours"}.issubset(script) or not {"cost", "hours"}.issubset(plan["save"]):
            return "成功任务记录中的素材或脚本结构无效"
        if any(not isinstance(item.get(field), str) or not item[field] for item, field in ((asset, "asset_id"), (asset, "name"), (asset, "quality"), (script, "script_id"), (script, "theme"), (script, "text"))):
            return "成功任务记录中的素材或脚本文本无效"
        if script["theme"] != plan["theme"]:
            return "成功任务记录中的脚本主题不一致"
        for container in (record, plan):
            for field in ("total_cost", "total_hours"):
                value = container[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)) or value < 0:
                    return "成功任务记录中的费用或耗时无效"
        for container in (asset, script, plan["save"]):
            for field in ("cost", "hours"):
                value = container[field]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)) or value < 0:
                    return "成功任务记录中的素材或脚本费用耗时无效"
        try:
            if not isinstance(record["plan_checksum"], str) or record["plan_checksum"] != plan_checksum(plan):
                return "成功任务记录的方案校验和不匹配"
        except (UnicodeError, ValueError, TypeError):
            return "成功任务记录无法计算方案校验和"
        if plan["total_cost"] != record["total_cost"] or plan["total_hours"] != record["total_hours"]:
            return "成功任务记录与方案的费用或耗时不一致"
        if plan["total_cost"] != asset["cost"] + script["cost"] + plan["save"]["cost"] or plan["total_hours"] != asset["hours"] + script["hours"] + plan["save"]["hours"]:
            return "成功任务记录的报价明细与合计不一致"
        source = record["source_model"]
        if not isinstance(source, dict) or source.get("mode") not in {"real", "mock"} or not isinstance(source.get("name"), str):
            return "成功任务记录中的模型信息无效"
        if not isinstance(record["fingerprint"], str) or not isinstance(record["created_at"], str):
            return "成功任务记录中的指纹或时间无效"
        return None

    def _record(
        self,
        action: str,
        arguments: dict[str, Any],
        rationale: str,
        result: ToolResult,
    ) -> None:
        step = {
            "step": len(self.steps) + 1,
            "action": action,
            "arguments": deepcopy(arguments),
            "rationale": rationale[:500],
            "succeeded": result.ok,
            "code": result.code,
            "result_summary": deepcopy(result.data),
            "error": None if result.ok else {"code": result.code, "message": result.message[:500]},
        }
        self.steps.append(step)
        self.new_step_count += 1
        self.last_observation = {"action": action, "arguments": deepcopy(arguments), **result.observation()}
        try:
            self._persist_checkpoint()
        except StoreError as exc:
            self.checkpoint_error = str(exc)

    def _context(self) -> dict[str, Any]:
        state = self.tools.state
        return {
            "task": self.scenario.public_dict(),
            "state": {
                "candidates": deepcopy(list(state.candidates.values())),
                "scripts": deepcopy(list(state.scripts.values())),
                "estimates": deepcopy(list(state.estimates.values())),
                "latest_estimate": deepcopy(state.latest_estimate),
                "saved_plan": deepcopy(state.saved_plan),
            },
            "last_observation": deepcopy(self.last_observation),
        }

    def _parse(self, raw: str) -> tuple[str, dict[str, Any], str]:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("模型输出不是合法 JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {"action", "arguments", "rationale"}:
            raise ValueError("模型输出必须且只能包含 action、arguments、rationale")
        try:
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("模型输出包含无效 Unicode 或非有限数字") from exc
        action = payload["action"]
        arguments = payload["arguments"]
        rationale = payload["rationale"]
        if not isinstance(action, str) or action not in ACTIONS:
            raise ValueError("未知 action")
        if not isinstance(arguments, dict) or not isinstance(rationale, str):
            raise ValueError("arguments 必须是对象，rationale 必须是字符串")
        return action, arguments, rationale

    def _no_feasible_details(self) -> dict[str, Any] | None:
        """只有全部候选均有最新估算且均不可行时，才允许声明全局无方案。"""
        state = self.tools.state
        if not state.candidates or any(item["feasible"] for item in state.estimates.values()):
            return None
        latest_by_asset: dict[str, dict[str, Any]] = {}
        for estimate in state.estimates.values():
            latest_by_asset[estimate["asset_id"]] = estimate
        if set(latest_by_asset) != set(state.candidates):
            return None

        shortfalls = []
        for asset_id, estimate in latest_by_asset.items():
            shortfalls.append(
                {
                    "asset_id": asset_id,
                    "total_cost": estimate["total_cost"],
                    "total_hours": estimate["total_hours"],
                    "deficit": {
                        "cost": max(0, estimate["total_cost"] - self.scenario.budget),
                        "hours": max(0, estimate["total_hours"] - self.scenario.deadline_hours),
                    },
                }
            )
        # 顶层缺口始终属于同一个最低成本候选，不拼接不同方案的费用与耗时。
        basis = min(shortfalls, key=lambda item: (item["total_cost"], item["total_hours"], item["asset_id"]))
        return {
            "budget": self.scenario.budget,
            "deadline_hours": self.scenario.deadline_hours,
            "deficit_basis": deepcopy(basis),
            "deficit": deepcopy(basis["deficit"]),
            "candidate_shortfalls": shortfalls,
        }

    def _abort_reason(self, code: str, message: str) -> dict[str, Any]:
        details: dict[str, Any] = {}
        if code == "no_feasible_plan":
            details = self._no_feasible_details() or {}
        elif code == "tool_failure" and self.last_observation:
            details = {"last_error": deepcopy(self.last_observation)}
        return self._reason(code, message, details)

    def run(self) -> dict[str, Any]:
        missing = self.scenario.missing_reason()
        if missing:
            result = self._finalize("unfinished", self._reason(*missing))
            return result

        try:
            existing = self.store.get(self.scenario.task_id)
        except StoreError as exc:
            return self._finalize("unfinished", self._reason("storage_error", str(exc)))
        if existing is not None:
            record_error = self._existing_record_error(existing)
            if record_error:
                return self._finalize("unfinished", self._reason("storage_error", record_error))
            if existing.get("fingerprint") != self.scenario.fingerprint():
                return self._finalize(
                    "unfinished",
                    self._reason("task_id_conflict", "task_id 已用于不同业务输入；旧方案未被修改"),
                )
            if existing["saved_plan"]["theme"] != self.scenario.theme:
                return self._finalize("unfinished", self._reason("storage_error", "成功记录指纹匹配但方案主题不一致"))
            self.diagnostics.append("duplicate_short_circuit")
            return self._finalize("completed", saved_record=existing, duplicate=True)

        try:
            checkpoint = self.store.get_checkpoint(self.scenario.task_id)
        except StoreError as exc:
            return self._finalize("unfinished", self._reason("storage_error", str(exc)))
        if checkpoint is not None:
            checkpoint_error = self._checkpoint_error(checkpoint)
            if checkpoint_error:
                return self._finalize("unfinished", self._reason("storage_error", checkpoint_error))
            if checkpoint["fingerprint"] != self.scenario.fingerprint():
                return self._finalize("unfinished", self._reason("task_id_conflict", "task_id 的续跑检查点属于不同业务输入"))
            if checkpoint["mode"] != self.mode:
                return self._finalize("unfinished", self._reason("resume_mode_conflict", "续跑必须沿用检查点的 real/mock 模式"))
            requested_name = checkpoint["model_info"].get("requested_name", checkpoint["model_info"].get("name"))
            recover_unconfigured = self.mode == "real" and requested_name == "unconfigured" and not checkpoint["steps"]
            if requested_name != self.model_name and not recover_unconfigured:
                return self._finalize("unfinished", self._reason("resume_model_conflict", "续跑必须沿用检查点的模型配置"))
            if checkpoint["fault"] != self.scenario.fault:
                return self._finalize("unfinished", self._reason("checkpoint_conflict", "续跑必须沿用检查点的故障配置"))
            self.tools = self.tools_factory(self.model_info)
            self._restore_checkpoint(checkpoint, preserve_model=recover_unconfigured)
        else:
            self.tools = self.tools_factory(self.model_info)

        if self.max_steps == 0:
            return self._finalize("unfinished", self._reason("max_steps", "已达到模型动作步数上限"))

        try:
            model = self.model_factory()
        except ModelUnavailable as exc:
            return self._finalize("unfinished", self._reason("model_unavailable", str(exc)))

        while True:
            if self.checkpoint_error:
                if self.tools.state.saved_plan:
                    return self._finalize("completed")
                return self._finalize("unfinished", self._reason("storage_error", self.checkpoint_error))
            if self.new_step_count >= self.max_steps:
                if self.tools.state.saved_plan:
                    self.diagnostics.append("max_steps_after_save")
                    return self._finalize("completed")
                return self._finalize("unfinished", self._reason("max_steps", "已达到模型动作步数上限"))

            try:
                raw = model.decide(self._context())
                metadata = getattr(model, "last_response", None)
                if metadata:
                    self.model_exchanges.append({"step": len(self.steps) + 1, **deepcopy(metadata)})
                    if self.mode == "real" and isinstance(metadata.get("provider_model"), str) and metadata["provider_model"]:
                        self.model_info["name"] = metadata["provider_model"]
            except ModelUnavailable as exc:
                if self.tools.state.saved_plan:
                    self.diagnostics.append("model_unavailable_after_save")
                    return self._finalize("completed")
                return self._finalize("unfinished", self._reason("model_unavailable", str(exc)))

            try:
                action, arguments, rationale = self._parse(raw)
            except ValueError as exc:
                self._record("model_output", {}, "", ToolResult(False, "INVALID_MODEL_OUTPUT", str(exc)))
                continue

            if action == "finish":
                if arguments:
                    self._record(action, arguments, rationale, ToolResult(False, "INVALID_ARGUMENTS", "finish 不接受参数"))
                elif not self.tools.state.saved_plan:
                    self._record(action, arguments, rationale, ToolResult(False, "SAVE_REQUIRED", "保存成功后才能结束"))
                else:
                    self._record(action, arguments, rationale, ToolResult(True, "FINISHED", "模型确认结束"))
                    return self._finalize("completed")
                continue

            if action == "abort":
                reason = arguments.get("reason")
                message = arguments.get("message")
                invalid = (
                    set(arguments) - {"reason", "message"}
                    or not isinstance(reason, str)
                    or reason not in ABORT_REASONS
                    or ("message" in arguments and (not isinstance(message, str) or not message.strip()))
                )
                if invalid:
                    self._record(action, arguments, rationale, ToolResult(False, "INVALID_ARGUMENTS", "abort 参数类型或原因无效"))
                    continue
                if self.tools.state.saved_plan:
                    self._record(action, arguments, rationale, ToolResult(False, "ABORT_AFTER_SAVE", "方案已保存，忽略终止请求"))
                    self.diagnostics.append("abort_after_save")
                    return self._finalize("completed")
                if reason == "no_feasible_plan" and self._no_feasible_details() is None:
                    self._record(
                        action,
                        arguments,
                        rationale,
                        ToolResult(False, "PREMATURE_NO_FEASIBLE", "尚有未估算候选或已有可行方案，不能声明无可行方案"),
                    )
                    continue
                message = message or "模型决定终止"
                self._record(action, arguments, rationale, ToolResult(True, "ABORTED", message))
                return self._finalize("unfinished", self._abort_reason(reason, message))

            result = self.tools.execute(action, arguments)
            self._record(action, arguments, rationale, result)
