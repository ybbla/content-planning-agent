from __future__ import annotations

from copy import deepcopy

import pytest

from src.schemas import Scenario
from src.state import TaskStore
from src.tools import BusinessTools


def make_tools(scenario_dict, catalog, tmp_path, raw_override=None):
    assets, fees = catalog
    scenario = Scenario.from_dict(raw_override or scenario_dict)
    store = TaskStore(tmp_path / "tools-state")
    return BusinessTools(scenario, assets, fees, store, {"mode": "mock", "name": "test"}), store


def prepare_estimate(tools, asset="asset-high"):
    assert tools.execute("search_assets", {"theme": "测试活动"}).ok
    assert tools.execute("generate_script", {"theme": "测试活动"}).ok
    return tools.execute("estimate_cost", {"asset_id": asset, "script_id": "script-1"})


def save_args(task_id="test-001", asset="asset-high", estimate="estimate-1"):
    return {"task_id": task_id, "asset_id": asset, "script_id": "script-1", "estimate_id": estimate}


def test_tool_dependencies_and_script_reuse(scenario_dict, catalog, tmp_path):
    tools, _ = make_tools(scenario_dict, catalog, tmp_path)
    assert tools.execute("generate_script", {"theme": "测试活动"}).code == "SEARCH_REQUIRED"
    prepare_estimate(tools)
    repeated = tools.execute("generate_script", {"theme": "测试活动"})
    assert repeated.code == "SCRIPT_REUSED"
    assert repeated.data["script_id"] == "script-1"
    assert len(tools.state.scripts) == 1


def test_stale_estimate_cannot_save_different_candidate(scenario_dict, catalog, tmp_path):
    tools, store = make_tools(scenario_dict, catalog, tmp_path)
    prepare_estimate(tools)
    tools.execute("estimate_cost", {"asset_id": "asset-economy", "script_id": "script-1"})
    rejected = tools.execute("save_plan", save_args(asset="asset-high", estimate="estimate-1"))
    assert rejected.code == "LATEST_ESTIMATE_REQUIRED"
    assert store.get("test-001") is None


def test_over_budget_save_is_recomputed_and_rejected(scenario_dict, catalog, tmp_path):
    raw = scenario_dict | {"budget": 10}
    tools, store = make_tools(scenario_dict, catalog, tmp_path, raw)
    estimate = prepare_estimate(tools)
    assert estimate.ok and not estimate.data["feasible"]
    rejected = tools.execute("save_plan", save_args())
    assert rejected.code == "CONSTRAINT_VIOLATION"
    assert store.get("test-001") is None


def test_deadline_save_is_rejected(scenario_dict, catalog, tmp_path):
    raw = scenario_dict | {"deadline_hours": 7}
    tools, store = make_tools(scenario_dict, catalog, tmp_path, raw)
    prepare_estimate(tools)
    assert tools.execute("save_plan", save_args()).code == "CONSTRAINT_VIOLATION"
    assert store.get("test-001") is None


def test_save_fault_occurs_before_side_effect_and_only_once(scenario_dict, catalog, tmp_path):
    raw = scenario_dict | {"fault": {"tool": "save_plan", "kind": "transient"}}
    tools, store = make_tools(scenario_dict, catalog, tmp_path, raw)
    prepare_estimate(tools)
    failed = tools.execute("save_plan", save_args())
    assert not failed.ok
    assert store.get("test-001") is None
    assert tools.state.saved_plan is None
    assert tools.execute("save_plan", save_args()).ok
    assert store.get("test-001")["total_cost"] == 12


def test_invalid_arguments_do_not_consume_one_time_fault(scenario_dict, catalog, tmp_path):
    raw = scenario_dict | {"fault": {"tool": "search_assets", "kind": "transient"}}
    tools, _ = make_tools(scenario_dict, catalog, tmp_path, raw)
    assert tools.execute("search_assets", {}).code == "INVALID_ARGUMENTS"
    assert tools.execute("search_assets", {"theme": "测试活动"}).code == "TEMPORARY_TOOL_FAILURE"
    assert tools.execute("search_assets", {"theme": "测试活动"}).ok


def test_permanent_save_fault_never_allows_side_effect(scenario_dict, catalog, tmp_path):
    raw = scenario_dict | {"fault": {"tool": "save_plan", "kind": "permanent"}}
    tools, store = make_tools(scenario_dict, catalog, tmp_path, raw)
    prepare_estimate(tools)
    for _ in range(2):
        result = tools.execute("save_plan", save_args())
        assert result.code == "PERMANENT_TOOL_FAILURE"
        assert store.get("test-001") is None
        assert tools.state.saved_plan is None


def test_save_directory_creation_failure_becomes_tool_error(scenario_dict, catalog, tmp_path):
    tools, store = make_tools(scenario_dict, catalog, tmp_path)
    prepare_estimate(tools)
    store.tasks_dir.parent.write_text("父路径是普通文件", encoding="utf-8")
    result = tools.execute("save_plan", save_args())
    assert result.code == "PERSISTENCE_ERROR"
    assert tools.state.saved_plan is None


@pytest.mark.parametrize("action", ["search_assets", "generate_script", "estimate_cost", "save_plan"])
def test_permanent_fault_on_every_tool_has_no_state_change(action, scenario_dict, catalog, tmp_path):
    raw = scenario_dict | {"fault": {"tool": action, "kind": "permanent"}}
    tools, store = make_tools(scenario_dict, catalog, tmp_path, raw)
    arguments = {
        "search_assets": {"theme": "测试活动"},
        "generate_script": {"theme": "测试活动"},
        "estimate_cost": {"asset_id": "asset-high", "script_id": "script-1"},
        "save_plan": save_args(),
    }
    if action != "search_assets":
        tools.execute("search_assets", {"theme": "测试活动"})
    if action in {"estimate_cost", "save_plan"}:
        tools.execute("generate_script", {"theme": "测试活动"})
    if action == "save_plan":
        tools.execute("estimate_cost", {"asset_id": "asset-high", "script_id": "script-1"})
    before = deepcopy(tools.state)
    for _ in range(2):
        assert tools.execute(action, arguments[action]).code == "PERMANENT_TOOL_FAILURE"
        assert tools.state == before
        assert store.get("test-001") is None
    assert list(tmp_path.glob("**/*.tmp")) == []
