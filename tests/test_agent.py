from __future__ import annotations

import json

import pytest

from src.model import DeterministicMockModel, ModelUnavailable, QueueModel


def action(name, arguments=None):
    return json.dumps({"action": name, "arguments": arguments or {}, "rationale": "测试动作"}, ensure_ascii=False)


def test_normal_completes_with_high_quality_plan(run_agent):
    result, _, _ = run_agent()
    assert result["status"] == "completed"
    assert result["total_cost"] == 12
    assert result["total_hours"] == 8
    assert result["saved_plan"]["asset"]["asset_id"] == "asset-high"
    assert [step["action"] for step in result["task_plan"]["steps"]] == [
        "search_assets", "generate_script", "estimate_cost", "save_plan", "finish"
    ]


def test_over_budget_replans_and_reuses_script(run_agent, scenario_dict):
    raw = scenario_dict | {"budget": 10}
    result, _, _ = run_agent(raw=raw)
    assert result["status"] == "completed"
    assert [item["total_cost"] for item in result["estimates"]] == [12, 8]
    assert result["estimates"][0]["feasible"] is False
    assert result["estimates"][1]["feasible"] is True
    assert result["estimates"][0]["script_id"] == result["estimates"][1]["script_id"]
    assert result["total_cost"] == 8
    assert result["saved_plan"]["asset"]["asset_id"] == "asset-economy"


def test_no_feasible_budget_returns_shortfall_without_save(run_agent, scenario_dict):
    result, _, store = run_agent(raw=scenario_dict | {"budget": 6})
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "no_feasible_plan"
    assert result["reason"]["details"]["deficit"]["cost"] == 2
    assert result["total_cost"] is None
    assert result["saved_plan"] is None
    assert store.get("test-001") is None


def test_no_feasible_deadline_returns_shortfall(run_agent, scenario_dict):
    result, _, _ = run_agent(raw=scenario_dict | {"deadline_hours": 4})
    assert result["status"] == "unfinished"
    assert result["reason"]["details"]["deficit"] == {"cost": 0, "hours": 1}
    assert result["total_hours"] is None


def test_missing_requirements_do_not_call_model(run_agent, scenario_dict):
    model = QueueModel([])
    for field, code in (("budget", "missing_budget"), ("deadline_hours", "missing_deadline")):
        raw = dict(scenario_dict)
        del raw[field]
        result, _, _ = run_agent(model=model, raw=raw)
        assert result["status"] == "unfinished"
        assert result["reason"]["code"] == code
        assert result["step_count"] == 0
    assert model.calls == 0


def test_transient_failure_is_explicitly_retried_and_preserves_search(run_agent, scenario_dict):
    raw = scenario_dict | {"fault": {"tool": "generate_script", "kind": "transient"}}
    result, _, _ = run_agent(raw=raw)
    assert result["status"] == "completed"
    steps = result["task_plan"]["steps"]
    assert steps[1]["succeeded"] is False
    assert steps[1]["code"] == "TEMPORARY_TOOL_FAILURE"
    assert steps[2]["action"] == "generate_script"
    assert steps[2]["succeeded"] is True
    assert len(result["tool_results"]["candidates"]) == 2


def test_permanent_failure_aborts_without_fake_script(run_agent, scenario_dict):
    raw = scenario_dict | {"fault": {"tool": "generate_script", "kind": "permanent"}}
    result, _, store = run_agent(raw=raw)
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "tool_failure"
    assert result["tool_results"]["scripts"] == []
    assert store.get("test-001") is None


def test_duplicate_short_circuits_before_model_and_preserves_original(run_agent, scenario_dict, tmp_path):
    from src.state import TaskStore

    store = TaskStore(tmp_path / "shared")
    first, _, _ = run_agent(store=store)
    second_model = QueueModel([])
    second, second_agent, _ = run_agent(model=second_model, store=store, mode="real")
    assert first["status"] == second["status"] == "completed"
    assert second["duplicate"] is True
    assert second["step_count"] == 0
    assert second_model.calls == 0
    assert second["source_model"]["mode"] == "mock"
    assert second_agent.trace["diagnostics"] == ["duplicate_short_circuit"]
    assert second["total_cost"] == first["total_cost"]


def test_same_task_id_with_different_business_input_conflicts(run_agent, scenario_dict, tmp_path):
    from src.state import TaskStore

    store = TaskStore(tmp_path / "shared")
    first, _, _ = run_agent(store=store)
    conflict, _, _ = run_agent(raw=scenario_dict | {"budget": 99}, store=store)
    assert conflict["status"] == "unfinished"
    assert conflict["reason"]["code"] == "task_id_conflict"
    assert conflict["saved_plan"] is None
    assert store.get("test-001")["total_cost"] == first["total_cost"]


def test_max_steps_has_no_off_by_one_or_fake_save(run_agent):
    model = DeterministicMockModel()
    result, _, store = run_agent(model=model, max_steps=2)
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "max_steps"
    assert result["step_count"] == 2
    assert store.get("test-001") is None


def test_zero_steps_never_calls_model(run_agent):
    model = QueueModel([])
    result, _, _ = run_agent(model=model, max_steps=0)
    assert result["reason"]["code"] == "max_steps"
    assert model.calls == 0


def test_invalid_json_is_observation_and_does_not_leak_raw_output(run_agent):
    secret = "sk-not-to-be-stored"
    model = QueueModel([f"not-json-{secret}", action("abort", {"reason": "model_aborted"})])
    result, agent, _ = run_agent(model=model)
    assert result["step_count"] == 2
    assert result["task_plan"]["steps"][0]["code"] == "INVALID_MODEL_OUTPUT"
    assert secret not in json.dumps(agent.trace, ensure_ascii=False)


def test_unknown_action_and_early_finish_are_rejected(run_agent):
    model = QueueModel([
        action("delete_everything"),
        action("finish"),
        action("abort", {"reason": "model_aborted"}),
    ])
    result, _, _ = run_agent(model=model)
    codes = [step["code"] for step in result["task_plan"]["steps"]]
    assert codes == ["INVALID_MODEL_OUTPUT", "SAVE_REQUIRED", "ABORTED"]


def test_save_on_last_step_still_returns_saved_fact(run_agent):
    model = QueueModel([
        action("search_assets", {"theme": "测试活动"}),
        action("generate_script", {"theme": "测试活动"}),
        action("estimate_cost", {"asset_id": "asset-high", "script_id": "script-1"}),
        action("save_plan", {"task_id": "test-001", "asset_id": "asset-high", "script_id": "script-1", "estimate_id": "estimate-1"}),
    ])
    result, _, _ = run_agent(model=model, max_steps=4)
    assert result["status"] == "completed"
    assert result["total_cost"] == 12
    assert "max_steps_after_save" in result["diagnostics"]


def test_model_failure_and_abort_after_save_do_not_erase_plan(run_agent, tmp_path):
    from src.state import TaskStore

    prefix = [
        action("search_assets", {"theme": "测试活动"}),
        action("generate_script", {"theme": "测试活动"}),
        action("estimate_cost", {"asset_id": "asset-high", "script_id": "script-1"}),
        action("save_plan", {"task_id": "test-001", "asset_id": "asset-high", "script_id": "script-1", "estimate_id": "estimate-1"}),
    ]
    failed, _, _ = run_agent(
        model=QueueModel(prefix + [ModelUnavailable("网络断开")]),
        store=TaskStore(tmp_path / "failed-save-state"),
    )
    assert failed["status"] == "completed"
    assert "model_unavailable_after_save" in failed["diagnostics"]

    aborted, _, _ = run_agent(
        model=QueueModel(prefix + [action("abort", {"reason": "model_aborted"})]),
        store=TaskStore(tmp_path / "aborted-save-state"),
    )
    assert aborted["status"] == "completed"
    assert "abort_after_save" in aborted["diagnostics"]


def test_model_unavailable_never_silently_uses_mock(run_agent):
    result, _, _ = run_agent(model=ModelUnavailable("API 不可用"), mode="real")
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "model_unavailable"
    assert result["step_count"] == 0
    assert result["model"]["mode"] == "real"


def test_abort_rejects_non_string_reason_and_message(run_agent):
    model = QueueModel([
        action("abort", {"reason": []}),
        action("abort", {"reason": "tool_failure", "message": []}),
        action("abort", {"reason": "model_aborted", "message": "主动停止"}),
    ])
    result, _, _ = run_agent(model=model)
    codes = [step["code"] for step in result["task_plan"]["steps"]]
    assert codes == ["INVALID_ARGUMENTS", "INVALID_ARGUMENTS", "ABORTED"]
    assert result["reason"]["message"] == "主动停止"


def test_invalid_abort_after_save_does_not_crash_or_hide_saved_fact(run_agent):
    model = QueueModel([
        action("search_assets", {"theme": "测试活动"}),
        action("generate_script", {"theme": "测试活动"}),
        action("estimate_cost", {"asset_id": "asset-high", "script_id": "script-1"}),
        action("save_plan", {"task_id": "test-001", "asset_id": "asset-high", "script_id": "script-1", "estimate_id": "estimate-1"}),
        action("abort", {"reason": []}),
        action("finish"),
    ])
    result, _, _ = run_agent(model=model)
    assert result["status"] == "completed"
    assert result["task_plan"]["steps"][4]["code"] == "INVALID_ARGUMENTS"
    assert result["saved_plan"]["total_cost"] == 12


def test_premature_no_feasible_abort_is_rejected_then_replans(run_agent):
    model = QueueModel([
        action("search_assets", {"theme": "测试活动"}),
        action("generate_script", {"theme": "测试活动"}),
        action("estimate_cost", {"asset_id": "asset-high", "script_id": "script-1"}),
        action("abort", {"reason": "no_feasible_plan"}),
        action("estimate_cost", {"asset_id": "asset-economy", "script_id": "script-1"}),
        action("save_plan", {"task_id": "test-001", "asset_id": "asset-economy", "script_id": "script-1", "estimate_id": "estimate-2"}),
        action("finish"),
    ])
    result, _, _ = run_agent(model=model, raw={"task_id": "test-001", "theme": "测试活动", "budget": 10, "deadline_hours": 8})
    assert result["status"] == "completed"
    assert result["task_plan"]["steps"][3]["code"] == "PREMATURE_NO_FEASIBLE"
    assert result["total_cost"] == 8


def test_no_feasible_abort_is_rejected_when_feasible_estimate_exists(run_agent):
    model = QueueModel([
        action("search_assets", {"theme": "测试活动"}),
        action("generate_script", {"theme": "测试活动"}),
        action("estimate_cost", {"asset_id": "asset-high", "script_id": "script-1"}),
        action("abort", {"reason": "no_feasible_plan"}),
        action("save_plan", {"task_id": "test-001", "asset_id": "asset-high", "script_id": "script-1", "estimate_id": "estimate-1"}),
        action("finish"),
    ])
    result, _, _ = run_agent(model=model)
    assert result["task_plan"]["steps"][3]["code"] == "PREMATURE_NO_FEASIBLE"
    assert result["status"] == "completed"


def test_zero_steps_precedes_model_factory(run_agent):
    result, _, _ = run_agent(model=ModelUnavailable("不应初始化"), max_steps=0, mode="real")
    assert result["reason"]["code"] == "max_steps"


def test_corrupt_success_record_returns_storage_error(run_agent, tmp_path):
    from src.state import TaskStore

    store = TaskStore(tmp_path / "corrupt-state")
    first, _, _ = run_agent(store=store)
    assert first["status"] == "completed"
    path = tmp_path / "corrupt-state" / "tasks" / "test-001.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["saved_plan"]
    path.write_text(json.dumps(record), encoding="utf-8")
    second, _, _ = run_agent(store=store)
    assert second["status"] == "unfinished"
    assert second["reason"]["code"] == "storage_error"


def test_deficit_is_bound_to_one_candidate_under_cost_time_tradeoff(run_agent, scenario_dict):
    _, agent, _ = run_agent(raw=scenario_dict | {"budget": 6, "deadline_hours": 8})
    agent.tools.state.candidates = {"a": {"asset_id": "a"}, "b": {"asset_id": "b"}}
    agent.tools.state.estimates = {
        "e1": {"estimate_id": "e1", "asset_id": "a", "total_cost": 8, "total_hours": 10, "feasible": False},
        "e2": {"estimate_id": "e2", "asset_id": "b", "total_cost": 12, "total_hours": 4, "feasible": False},
    }
    details = agent._no_feasible_details()
    assert details["deficit_basis"]["asset_id"] == "a"
    assert details["deficit"] == {"cost": 2, "hours": 2}


@pytest.mark.parametrize(
    "override",
    [
        {"theme": "不同主题"},
        {"budget": 99},
        {"deadline_hours": 99},
        {"quality_preference": "economy"},
    ],
)
def test_every_business_fingerprint_change_is_conflict(run_agent, scenario_dict, tmp_path, override):
    from src.state import TaskStore

    store = TaskStore(tmp_path / "fingerprint-state")
    assert run_agent(store=store)[0]["status"] == "completed"
    conflict, _, _ = run_agent(raw=scenario_dict | override, store=store)
    assert conflict["status"] == "unfinished"
    assert conflict["reason"]["code"] == "task_id_conflict"


def test_zero_steps_still_honors_existing_success(run_agent, tmp_path):
    from src.state import TaskStore

    store = TaskStore(tmp_path / "zero-duplicate-state")
    assert run_agent(store=store)[0]["status"] == "completed"
    duplicate, _, _ = run_agent(model=ModelUnavailable("不应初始化"), store=store, max_steps=0)
    assert duplicate["status"] == "completed"
    assert duplicate["duplicate"] is True


def test_empty_or_invalid_utf8_record_is_storage_error(run_agent, tmp_path):
    from src.state import TaskStore

    for name, content in (("empty", b"{}"), ("utf8", b"\xff")):
        store = TaskStore(tmp_path / name)
        path = store.tasks_dir / "test-001.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        model = QueueModel([])
        result, _, _ = run_agent(model=model, store=store)
        assert result["status"] == "unfinished"
        assert result["reason"]["code"] == "storage_error"
        assert model.calls == 0


def test_nested_plan_corruption_is_rejected_even_with_recomputed_checksum(run_agent, tmp_path):
    from src.state import TaskStore, plan_checksum

    store = TaskStore(tmp_path / "nested-corrupt")
    assert run_agent(store=store)[0]["status"] == "completed"
    path = store.tasks_dir / "test-001.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["saved_plan"]["asset"] = {}
    record["plan_checksum"] = plan_checksum(record["saved_plan"])
    path.write_text(json.dumps(record), encoding="utf-8")
    result, _, _ = run_agent(store=store)
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "storage_error"


def test_record_price_components_must_match_total(run_agent, tmp_path):
    from src.state import TaskStore, plan_checksum

    store = TaskStore(tmp_path / "price-mismatch")
    assert run_agent(store=store)[0]["status"] == "completed"
    path = store.tasks_dir / "test-001.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["saved_plan"]["asset"]["cost"] = 999
    record["saved_plan"]["asset"]["hours"] = 999
    record["plan_checksum"] = plan_checksum(record["saved_plan"])
    path.write_text(json.dumps(record), encoding="utf-8")
    result, _, _ = run_agent(store=store)
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "storage_error"


def test_model_surrogate_is_invalid_observation_not_output_crash(run_agent):
    model = QueueModel([
        action("abort", {"reason": "model_aborted", "message": "\ud800"}),
        action("abort", {"reason": "model_aborted", "message": "安全终止"}),
    ])
    result, agent, _ = run_agent(model=model)
    assert result["task_plan"]["steps"][0]["code"] == "INVALID_MODEL_OUTPUT"
    assert result["reason"]["message"] == "安全终止"
    assert agent.trace["steps"][1]["succeeded"] is True


def test_nonfinite_model_argument_is_rejected_even_after_save(run_agent):
    model = QueueModel([
        action("search_assets", {"theme": "测试活动"}),
        action("generate_script", {"theme": "测试活动"}),
        action("estimate_cost", {"asset_id": "asset-high", "script_id": "script-1"}),
        action("save_plan", {"task_id": "test-001", "asset_id": "asset-high", "script_id": "script-1", "estimate_id": "estimate-1"}),
        action("finish", {"x": 1e999}),
        action("finish"),
    ])
    result, _, _ = run_agent(model=model)
    assert result["status"] == "completed"
    assert result["task_plan"]["steps"][4]["code"] == "INVALID_MODEL_OUTPUT"
    assert result["saved_plan"]["total_cost"] == 12


def test_record_with_unserializable_source_model_is_storage_error(run_agent, tmp_path):
    from src.state import TaskStore

    for name, value in (("surrogate", "\ud800"), ("infinite", 1e999)):
        store = TaskStore(tmp_path / name)
        assert run_agent(store=store)[0]["status"] == "completed"
        path = store.tasks_dir / "test-001.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["source_model"]["name"] = value
        path.write_text(json.dumps(record), encoding="utf-8")
        result, _, _ = run_agent(store=store)
        assert result["status"] == "unfinished"
        assert result["reason"]["code"] == "storage_error"


def test_json_integer_digit_limit_is_storage_error(run_agent, tmp_path):
    from src.state import TaskStore

    store = TaskStore(tmp_path / "long-int")
    path = store.tasks_dir / "test-001.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"value":' + "9" * 5000 + "}", encoding="utf-8")
    result, _, _ = run_agent(store=store)
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "storage_error"
