from __future__ import annotations

import json

from src.model import DeterministicMockModel, ModelUnavailable, QueueModel
from src.state import StoreError, TaskStore, checkpoint_checksum


def failed_scenario(scenario_dict):
    return scenario_dict | {"fault": {"tool": "generate_script", "kind": "transient"}}


def test_resume_after_tool_failure_preserves_history_and_completes(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "resume")
    first, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store, max_steps=2)
    assert first["status"] == "unfinished"
    assert first["reason"]["code"] == "max_steps"
    assert first["new_step_count"] == 2
    assert store.get_checkpoint("test-001")["steps"][1]["code"] == "TEMPORARY_TOOL_FAILURE"

    second, _, _ = run_agent(model=DeterministicMockModel(), raw=failed_scenario(scenario_dict), store=store)
    assert second["status"] == "completed"
    assert second["resumed"] is True
    assert second["resume_count"] == 1
    assert second["resumed_from_run_id"] == first["run_id"]
    assert second["step_count"] == 6
    assert second["new_step_count"] == 4
    actions = [step["action"] for step in second["task_plan"]["steps"]]
    assert actions.count("search_assets") == 1
    assert actions.count("generate_script") == 2
    assert second["task_plan"]["steps"][:2] == first["task_plan"]["steps"]
    assert store.get_checkpoint("test-001") is None

    third, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store)
    assert third["duplicate"] is True
    assert third["step_count"] == 0


def test_each_invocation_gets_independent_step_budget(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "budget")
    first, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store, max_steps=2)
    second, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store, max_steps=2)
    assert second["status"] == "unfinished"
    assert second["reason"]["code"] == "max_steps"
    assert second["step_count"] == 4
    assert second["new_step_count"] == 2
    third, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store)
    assert third["status"] == "completed"
    assert third["resume_count"] == 2
    assert third["new_step_count"] == 2
    assert third["step_count"] == 6


def test_resume_rejects_business_fingerprint_conflict(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "conflict")
    first, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store, max_steps=2)
    conflict, _, _ = run_agent(raw=failed_scenario(scenario_dict) | {"budget": 99}, store=store)
    assert conflict["status"] == "unfinished"
    assert conflict["reason"]["code"] == "task_id_conflict"
    assert store.get_checkpoint("test-001")["run_id"] == first["run_id"]


def test_resume_rejects_mode_and_model_changes(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "mode")
    first, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store, max_steps=2)
    conflict, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store, mode="real")
    assert conflict["reason"]["code"] == "resume_mode_conflict"
    assert store.get_checkpoint("test-001")["run_id"] == first["run_id"]

    real_store = TaskStore(tmp_path / "model")
    real_mock = DeterministicMockModel()
    responses = iter([
        {"response_id": "resp-1", "provider_model": real_mock.name, "created": 1},
        {"response_id": "resp-2", "provider_model": real_mock.name, "created": 2},
    ])
    original_decide = real_mock.decide
    def decide(context):
        real_mock.last_response = next(responses)
        return original_decide(context)
    real_mock.decide = decide
    run_agent(model=real_mock, raw=failed_scenario(scenario_dict), store=real_store, mode="real", max_steps=2)
    other = DeterministicMockModel()
    other.name = "other-model"
    changed, _, _ = run_agent(model=other, raw=failed_scenario(scenario_dict), store=real_store, mode="real")
    assert changed["reason"]["code"] == "resume_model_conflict"


def test_resume_after_invalid_output_and_model_unavailable(run_agent, scenario_dict, tmp_path):
    invalid_store = TaskStore(tmp_path / "invalid")
    model = QueueModel(["not-json"])
    model.name = "deterministic-mock-v1"
    first, _, _ = run_agent(model=model, store=invalid_store, max_steps=1)
    assert first["task_plan"]["steps"][0]["code"] == "INVALID_MODEL_OUTPUT"
    resumed, _, _ = run_agent(store=invalid_store)
    assert resumed["status"] == "completed"
    assert resumed["task_plan"]["steps"][0] == first["task_plan"]["steps"][0]
    assert resumed["new_step_count"] == 5

    unavailable_store = TaskStore(tmp_path / "unavailable")
    error = ModelUnavailable("网络失败")
    error.name = "deterministic-mock-v1"
    stopped, _, _ = run_agent(model=error, store=unavailable_store)
    assert stopped["reason"]["code"] == "model_unavailable"
    recovered, _, _ = run_agent(store=unavailable_store)
    assert recovered["status"] == "completed"
    assert recovered["resumed"] is True
    assert recovered["new_step_count"] == 5


def test_permanent_failure_remains_permanent_across_resume(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "permanent")
    raw = scenario_dict | {"fault": {"tool": "generate_script", "kind": "permanent"}}
    run_agent(raw=raw, store=store, max_steps=2)
    resumed, _, _ = run_agent(raw=raw, store=store)
    assert resumed["status"] == "unfinished"
    assert resumed["reason"]["code"] == "tool_failure"
    assert resumed["task_plan"]["steps"][1]["code"] == "PERMANENT_TOOL_FAILURE"
    assert resumed["tool_results"]["scripts"] == []


def test_corrupt_checkpoint_is_not_overwritten(run_agent, tmp_path):
    store = TaskStore(tmp_path / "corrupt")
    path = store.checkpoints_dir / "test-001.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    result, _, _ = run_agent(store=store)
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "storage_error"
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_checkpoint_write_failure_is_explicit_and_preserves_success(run_agent, tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "write-error")
    monkeypatch.setattr(store, "save_checkpoint", lambda checkpoint: (_ for _ in ()).throw(StoreError("磁盘不可写")))
    result, _, _ = run_agent(store=store)
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "storage_error"
    assert len(result["tool_results"]["candidates"]) == 2


def test_checkpoint_checksum_and_fake_saved_plan_are_rejected(run_agent, scenario_dict, tmp_path):
    for name, recompute in (("checksum", False), ("fake-save", True)):
        store = TaskStore(tmp_path / name)
        run_agent(raw=failed_scenario(scenario_dict), store=store, max_steps=2)
        path = store.checkpoints_dir / "test-001.json"
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        if recompute:
            checkpoint["run_state"]["saved_plan"] = {"task_id": "test-001", "total_cost": 0}
            checkpoint["checksum"] = checkpoint_checksum(checkpoint)
        else:
            checkpoint["steps"][0]["rationale"] = "篡改"
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
        model = QueueModel([])
        model.name = "deterministic-mock-v1"
        result, _, _ = run_agent(model=model, raw=failed_scenario(scenario_dict), store=store)
        assert result["status"] == "unfinished"
        assert result["reason"]["code"] == "storage_error"
        assert model.calls == 0


def test_checkpoint_nested_state_and_fault_conflict_are_rejected(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "nested")
    run_agent(raw=failed_scenario(scenario_dict), store=store, max_steps=4)
    path = store.checkpoints_dir / "test-001.json"
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    checkpoint["run_state"]["latest_estimate"] = None
    checkpoint["checksum"] = checkpoint_checksum(checkpoint)
    path.write_text(json.dumps(checkpoint), encoding="utf-8")
    result, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store)
    assert result["reason"]["code"] == "storage_error"

    fault_store = TaskStore(tmp_path / "fault-conflict")
    first, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=fault_store, max_steps=2)
    changed, _, _ = run_agent(raw=scenario_dict, store=fault_store)
    assert changed["reason"]["code"] == "checkpoint_conflict"
    assert fault_store.get_checkpoint("test-001")["run_id"] == first["run_id"]


def test_no_feasible_and_model_abort_are_not_resumable(run_agent, scenario_dict, tmp_path):
    no_feasible_store = TaskStore(tmp_path / "no-feasible")
    result, _, _ = run_agent(raw=scenario_dict | {"budget": 6}, store=no_feasible_store)
    assert result["reason"]["code"] == "no_feasible_plan"
    assert no_feasible_store.get_checkpoint("test-001") is None

    abort_store = TaskStore(tmp_path / "abort")
    model = QueueModel([json.dumps({"action": "abort", "arguments": {"reason": "model_aborted"}, "rationale": "主动停止"})])
    result, _, _ = run_agent(model=model, store=abort_store)
    assert result["reason"]["code"] == "model_aborted"
    assert abort_store.get_checkpoint("test-001") is None


def test_checkpoint_mode_type_corruption_is_storage_error(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "mode-type")
    run_agent(raw=failed_scenario(scenario_dict), store=store, max_steps=2)
    path = store.checkpoints_dir / "test-001.json"
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    checkpoint["mode"] = []
    checkpoint["checksum"] = checkpoint_checksum(checkpoint)
    path.write_text(json.dumps(checkpoint), encoding="utf-8")
    result, _, _ = run_agent(raw=failed_scenario(scenario_dict), store=store)
    assert result["status"] == "unfinished"
    assert result["reason"]["code"] == "storage_error"


def test_unconfigured_empty_checkpoint_can_recover_after_configuration(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "reconfigure")
    error = ModelUnavailable("缺少配置")
    error.name = "unconfigured"
    first, _, _ = run_agent(model=error, store=store, mode="real")
    assert first["reason"]["code"] == "model_unavailable"
    configured = DeterministicMockModel()
    configured.name = "configured-model"
    second, _, _ = run_agent(model=configured, store=store, mode="real")
    assert second["status"] == "completed"
    assert second["resumed"] is True
    assert second["model"]["name"] == "configured-model"


def test_real_model_alias_stays_consistent_across_resume(run_agent, scenario_dict, tmp_path):
    store = TaskStore(tmp_path / "alias")

    def configured_model():
        model = DeterministicMockModel()
        model.name = "requested-alias"
        counter = iter(range(1, 20))
        original = model.decide
        def decide(context):
            model.last_response = {"response_id": f"resp-{next(counter)}", "provider_model": "provider-version", "created": 1}
            return original(context)
        model.decide = decide
        return model

    run_agent(model=configured_model(), raw=failed_scenario(scenario_dict), store=store, mode="real", max_steps=2)
    result, agent, _ = run_agent(model=configured_model(), raw=failed_scenario(scenario_dict), store=store, mode="real")
    assert result["status"] == "completed"
    assert result["model"]["name"] == agent.trace["model"]["name"] == "provider-version"
    assert result["model"]["requested_name"] == "requested-alias"
