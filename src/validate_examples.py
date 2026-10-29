"""校验提交的运行证据；real 证据不会被 Mock 替代。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REAL_CASES = ("real_normal", "real_over_budget", "real_tool_failure", "real_resume_failure_first", "real_resume_failure_second")


def _load(directory: Path, name: str) -> Any:
    with (directory / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _common(result: dict[str, Any], trace: dict[str, Any], mode: str, input_data: dict[str, Any]) -> None:
    _require(result["task_id"] == trace["task_id"] == input_data["task_id"], "输入、结果与 Trace 的 task_id 不一致")
    _require(result["run_id"] == trace["run_id"], "结果与 Trace 的 run_id 不一致")
    _require(result["model"]["mode"] == trace["model"]["mode"] == mode, "模型模式不正确")
    _require(result["model"]["name"] == trace["model"]["name"], "结果与 Trace 的模型名称不一致")
    for field in ("resumed", "resume_count", "resumed_from_run_id", "new_step_count"):
        _require(result.get(field) == trace.get(field), f"结果与 Trace 的 {field} 不一致")
    _require(result["status"] == trace["final_status"], "结果与 Trace 的最终状态不一致")
    steps = trace["steps"]
    _require(result["step_count"] == len(steps), "步骤数不一致")
    _require(result["task_plan"]["steps"] == steps, "result.task_plan.steps 与 Trace 不一致")
    _require([step.get("step") for step in steps] == list(range(1, len(steps) + 1)), "步骤编号必须从 1 连续递增")
    _require(result["total_cost"] == trace["total_cost"] and result["total_hours"] == trace["total_hours"], "结果与 Trace 的费用或耗时不一致")
    if result["status"] == "completed":
        _require(isinstance(result["saved_plan"], dict), "完成状态缺少已保存方案")
        _require(result["total_cost"] == result["saved_plan"]["total_cost"], "结果与保存方案费用不一致")
        _require(result["total_hours"] == result["saved_plan"]["total_hours"], "结果与保存方案耗时不一致")
    else:
        _require(result["saved_plan"] is None, "未完成状态却包含保存方案")
    constraints = result["task_plan"]["constraints"]
    for field in ("budget", "deadline_hours"):
        _require(constraints.get(field) == input_data.get(field), f"输入与结果的 {field} 不一致")
    _require(constraints.get("quality_preference", "high") == input_data.get("quality_preference", "high"), "质量偏好不一致")
    theme = input_data.get("theme", "").strip()
    _require(result["task_plan"]["goal"] == f"为主题“{theme}”完成内容活动方案", "输入主题与任务目标不一致")
    for step in steps:
        if step["action"] in {"search_assets", "generate_script"}:
            _require(step["arguments"].get("theme") == theme, "工具主题与输入不一致")
    _require(all(script.get("theme") == theme for script in result["tool_results"]["scripts"]), "成功脚本主题与输入不一致")
    if isinstance(result["saved_plan"], dict):
        _require(result["saved_plan"]["theme"] == theme and result["saved_plan"]["script"]["theme"] == theme, "保存方案主题与输入不一致")
        if not result["duplicate"]:
            _require(any(step["succeeded"] and step["code"] == "PLAN_SAVED" and step["result_summary"] == result["saved_plan"] for step in steps), "保存步骤与最终方案不一致")
    exchanges = trace.get("model_exchanges", [])
    if mode == "mock":
        _require(result["model"]["name"] == "deterministic-mock-v1", "Mock 证据模型名称不正确")
        _require(exchanges == [], "Mock 证据不应包含 API 响应元数据")
    else:
        _require(result["model"]["name"] not in {"deterministic-mock-v1", "queue-model", "unconfigured"}, "real 证据使用 Mock 名称")
        _require(isinstance(exchanges, list) and len(exchanges) == len(steps), "每个模型步骤都必须有 API 响应元数据")
        _require(all(isinstance(item, dict) for item in exchanges), "API 响应元数据必须是对象")
        _require([item["step"] for item in exchanges] == list(range(1, len(steps) + 1)), "API 响应未逐步骤覆盖")
        for exchange in exchanges:
            _require(isinstance(exchange.get("response_id"), str) and exchange["response_id"], "API 响应缺少 response_id")
            _require(isinstance(exchange.get("provider_model"), str) and exchange["provider_model"], "API 响应缺少 provider_model")
            _require(exchange["provider_model"] == result["model"]["name"], "API provider_model 与声明模型不一致")
            created = exchange.get("created")
            _require(isinstance(created, int) and not isinstance(created, bool) and created >= 0, "API 响应 created 无效")
        _require(len({item["response_id"] for item in exchanges}) == len(exchanges), "API response_id 不应重复")


def _estimate_index(steps: list[dict[str, Any]], cost: int, feasible: bool, start: int = 0) -> int:
    return next(
        index
        for index, step in enumerate(steps)
        if index >= start
        and step["succeeded"]
        and step["action"] == "estimate_cost"
        and step["result_summary"].get("total_cost") == cost
        and step["result_summary"].get("feasible") is feasible
    )


def _validate_over_budget(result: dict[str, Any], steps: list[dict[str, Any]]) -> None:
    _require(result["status"] == "completed" and result["total_cost"] == 8, "超预算用例最终应为 8")
    high = _estimate_index(steps, 12, False)
    economy = _estimate_index(steps, 8, True, high + 1)
    _require(
        any(index > economy and step["succeeded"] and step["code"] == "PLAN_SAVED" for index, step in enumerate(steps)),
        "替代估算后没有成功保存",
    )
    _require(steps[high]["result_summary"]["script_id"] == steps[economy]["result_summary"]["script_id"], "换路未复用脚本")


def _validate_no_feasible(result: dict[str, Any], steps: list[dict[str, Any]], expected: dict[str, int]) -> None:
    candidates = result["tool_results"]["candidates"]
    estimates = result["estimates"]
    _require(bool(candidates) and bool(estimates), "无方案结论缺少检索或估算")
    _require(any(step["succeeded"] and step["action"] == "search_assets" and step["result_summary"].get("candidates") == candidates for step in steps), "成功检索步骤与候选集合不一致")
    _require({item["asset_id"] for item in candidates} == {item["asset_id"] for item in estimates}, "未估算全部候选")
    _require(all(not item["feasible"] for item in estimates), "存在可行估算却声明无方案")
    for estimate in estimates:
        _require(
            any(step["succeeded"] and step["action"] == "estimate_cost" and step["result_summary"] == estimate for step in steps),
            "结果估算缺少对应成功步骤",
        )
    shortfalls = [
        {
            "asset_id": item["asset_id"],
            "total_cost": item["total_cost"],
            "total_hours": item["total_hours"],
            "deficit": {
                "cost": max(0, item["total_cost"] - result["task_plan"]["constraints"]["budget"]),
                "hours": max(0, item["total_hours"] - result["task_plan"]["constraints"]["deadline_hours"]),
            },
        }
        for item in estimates
    ]
    basis = min(shortfalls, key=lambda item: (item["total_cost"], item["total_hours"], item["asset_id"]))
    details = result["reason"]["details"]
    _require(details["candidate_shortfalls"] == shortfalls, "候选缺口与实际估算不一致")
    _require(details["deficit_basis"] == basis and details["deficit"] == basis["deficit"], "缺口未绑定正确候选")
    _require(details["deficit"] == expected, "缺口不符合场景预期")


def _validate_failure(result: dict[str, Any], steps: list[dict[str, Any]], require_recovery: bool) -> None:
    failed = next(
        index
        for index, step in enumerate(steps)
        if not step["succeeded"] and step["code"] in {"TEMPORARY_TOOL_FAILURE", "PERMANENT_TOOL_FAILURE"}
    )
    prior_success = [step for step in steps[:failed] if step["succeeded"]]
    _require(bool(prior_success), "故障前没有可保留的成功步骤")
    _require(failed + 1 < len(steps), "故障后没有模型后续动作")
    following = steps[failed + 1]
    _require(bool(following.get("rationale")), "故障后的动作缺少简要决策依据")
    _require(
        following["action"] in {steps[failed]["action"], "abort", "search_assets", "generate_script", "estimate_cost", "save_plan"},
        "故障后的动作不是显式恢复、换路或终止",
    )
    if any(step["action"] == "search_assets" and step["succeeded"] for step in prior_success):
        _require(bool(result["tool_results"]["candidates"]), "成功检索结果未保留")
    if any(step["action"] == "generate_script" and step["succeeded"] for step in prior_success):
        _require(bool(result["tool_results"]["scripts"]), "成功脚本结果未保留")
    if require_recovery:
        _require(
            any(index > failed and step["succeeded"] and step["action"] == steps[failed]["action"] for index, step in enumerate(steps)),
            "Mock 临时故障未显式恢复",
        )
    if result["status"] == "completed":
        _require(result["saved_plan"] is not None, "完成状态缺少已保存方案")
        _require(any(step["succeeded"] and step["code"] == "PLAN_SAVED" for step in steps), "完成状态没有保存成功步骤")
    else:
        _require(result["saved_plan"] is None, "未完成状态却包含保存方案")


def validate_resume_pair(root: Path, mode: str) -> list[str]:
    prefix = f"{mode}_resume_failure"
    try:
        first_result = _load(root / "examples" / f"{prefix}_first", "result.json")
        first_trace = _load(root / "examples" / f"{prefix}_first", "trace.json")
        second_result = _load(root / "examples" / f"{prefix}_second", "result.json")
        second_trace = _load(root / "examples" / f"{prefix}_second", "trace.json")
        first_input = _load(root / "examples" / f"{prefix}_first", "input.json")
        second_input = _load(root / "examples" / f"{prefix}_second", "input.json")
        _require(first_input == second_input, "续跑两段输入不一致")
        _require(first_result["run_id"] != second_result["run_id"], "两次运行应使用不同 run_id")
        _require(second_result["resumed"] is True and second_trace["resumed"] is True, "第二次运行未标记续跑")
        _require(second_result["resumed_from_run_id"] == first_result["run_id"] == second_trace["resumed_from_run_id"] == first_trace["run_id"], "续跑来源 run_id 不匹配")
        _require(second_result["resume_count"] == second_trace["resume_count"] == 1, "续跑次数不正确")
        for field in ("resumed", "resume_count", "resumed_from_run_id", "new_step_count"):
            _require(second_result[field] == second_trace[field], f"第二段结果与 Trace 的 {field} 不一致")
        _require(second_trace["steps"][:2] == first_trace["steps"], "续跑丢失或修改了历史步骤")
        _require(second_trace["model_exchanges"][:2] == first_trace["model_exchanges"], "续跑丢失或修改了历史 API 元数据")
        _require(second_result["new_step_count"] == second_trace["new_step_count"] == len(second_trace["steps"]) - len(first_trace["steps"]), "本次新增步数不正确")
    except (OSError, KeyError, TypeError, ValueError, AttributeError, json.JSONDecodeError) as exc:
        return [f"{prefix}_pair: {type(exc).__name__}: {exc}"]
    return []


def validate_mock_examples(root: Path = ROOT) -> list[str]:
    expected = {
        "mock_normal": ("completed", 12),
        "mock_over_budget": ("completed", 8),
        "mock_no_feasible_budget": ("unfinished", None),
        "mock_missing_budget": ("unfinished", None),
        "mock_missing_deadline": ("unfinished", None),
        "mock_tool_failure": ("completed", 12),
        "mock_time_insufficient": ("unfinished", None),
        "mock_repeat_first": ("completed", 12),
        "mock_repeat_second": ("completed", 12),
        "mock_max_steps": ("unfinished", None),
        "mock_resume_failure_first": ("unfinished", None),
        "mock_resume_failure_second": ("completed", 12),
    }
    errors = []
    saved_repeats = []
    for case, (status, cost) in expected.items():
        directory = root / "examples" / case
        try:
            result = _load(directory, "result.json")
            trace = _load(directory, "trace.json")
            input_data = _load(directory, "input.json")
            _require(isinstance(input_data, dict), "输入不是对象")
            _common(result, trace, "mock", input_data)
            _require(result["status"] == status and result["total_cost"] == cost, "状态或费用不符合预期")
            steps = trace["steps"]
            if case == "mock_over_budget":
                _validate_over_budget(result, steps)
            elif case == "mock_tool_failure":
                _validate_failure(result, steps, require_recovery=True)
            elif case == "mock_missing_budget":
                _require(result["reason"]["code"] == "missing_budget" and not steps, "缺预算证据不正确")
            elif case == "mock_missing_deadline":
                _require(result["reason"]["code"] == "missing_deadline" and not steps, "缺截止时间证据不正确")
            elif case == "mock_no_feasible_budget":
                _validate_no_feasible(result, steps, {"cost": 2, "hours": 0})
            elif case == "mock_time_insufficient":
                _validate_no_feasible(result, steps, {"cost": 0, "hours": 1})
            elif case == "mock_max_steps":
                _require(result["reason"]["code"] == "max_steps" and len(steps) == 2, "步数上限证据不正确")
            elif case == "mock_resume_failure_first":
                _require(result["reason"]["code"] == "max_steps" and result["resumed"] is False, "续跑第一段状态不正确")
                _require(len(steps) == 2 and steps[-1]["code"] == "TEMPORARY_TOOL_FAILURE", "续跑第一段未在故障后停止")
            elif case == "mock_resume_failure_second":
                _require(result["resumed"] is True and result["new_step_count"] == 4, "续跑第二段状态不正确")
                _validate_failure(result, steps, require_recovery=True)
            elif case.startswith("mock_repeat_"):
                duplicate = case == "mock_repeat_second"
                _require(result["duplicate"] is duplicate, "重复标记不正确")
                _require((len(steps) == 0) is duplicate, "重复执行应零模型动作")
                if duplicate:
                    _require("duplicate_short_circuit" in result["diagnostics"], "缺少重复短路诊断")
                saved_repeats.append(result["saved_plan"])
        except (OSError, KeyError, TypeError, ValueError, AttributeError, StopIteration, json.JSONDecodeError) as exc:
            errors.append(f"{case}: {type(exc).__name__}: {exc}")
    if len(saved_repeats) == 2 and saved_repeats[0] != saved_repeats[1]:
        errors.append("repeat: 重复执行改变了原保存方案")
    errors.extend(validate_resume_pair(root, "mock"))
    return errors


def validate_real_case(case: str, root: Path = ROOT) -> list[str]:
    directory = root / "examples" / case
    errors: list[str] = []
    try:
        result = _load(directory, "result.json")
        trace = _load(directory, "trace.json")
        input_data = _load(directory, "input.json")
        _require(isinstance(input_data, dict), "输入不是对象")
        _common(result, trace, "real", input_data)
        steps = trace["steps"]
        if case == "real_normal":
            _require(result["status"] == "completed", "真实正常用例未完成")
            _require(result["total_cost"] in {8, 12}, "真实正常用例费用无效")
            _require(any(step["succeeded"] and step["code"] == "PLAN_SAVED" for step in steps), "没有保存成功")
        elif case == "real_over_budget":
            _validate_over_budget(result, steps)
        elif case == "real_tool_failure":
            _validate_failure(result, steps, require_recovery=False)
        elif case == "real_resume_failure_first":
            _require(result["status"] == "unfinished" and result["reason"]["code"] == "max_steps", "真实续跑第一段未按上限停止")
            _require(result["resumed"] is False and len(steps) == 2, "真实续跑第一段步骤不正确")
            _require(steps[-1]["code"] == "TEMPORARY_TOOL_FAILURE" and not steps[-1]["succeeded"], "真实续跑第一段未记录工具失败")
        elif case == "real_resume_failure_second":
            _require(result["status"] == "completed" and result["resumed"] is True, "真实续跑第二段未完成恢复")
            _require(result["new_step_count"] == 4 and len(steps) == 6, "真实续跑新增或累计步骤不正确")
            _validate_failure(result, steps, require_recovery=True)
        else:
            raise ValueError(f"未知真实用例: {case}")
    except (OSError, KeyError, TypeError, ValueError, AttributeError, StopIteration, json.JSONDecodeError) as exc:
        errors.append(f"{case}: {type(exc).__name__}: {exc}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 examples 中的输入、结果和 Trace")
    parser.add_argument("--require-real", action="store_true", help="全部真实模型与续跑证据必须存在且通过")
    args = parser.parse_args()
    errors = validate_mock_examples()
    for case in REAL_CASES:
        directory = ROOT / "examples" / case
        if directory.exists() or args.require_real:
            errors.extend(validate_real_case(case))
    if (ROOT / "examples" / "real_resume_failure_first").exists() or args.require_real:
        errors.extend(validate_resume_pair(ROOT, "real"))
    if errors:
        print("\n".join(errors))
        return 1
    print("Mock 证据通过" + ("；全部 real 与续跑证据通过" if args.require_real else "；存在的 real 证据通过"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
