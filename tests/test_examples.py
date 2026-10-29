import json
import shutil

from src.validate_examples import REAL_CASES, ROOT, validate_mock_examples, validate_real_case, validate_resume_pair


def response_metadata(trace):
    return [
        {"step": index, "response_id": f"resp-{index}", "provider_model": "real-test-model", "created": 123}
        for index in range(1, len(trace["steps"]) + 1)
    ]


def test_committed_mock_examples_match_results():
    assert validate_mock_examples() == []


def test_any_committed_real_examples_are_valid():
    for case in REAL_CASES:
        if (ROOT / "examples" / case).exists():
            assert validate_real_case(case) == []


def test_tampered_repeat_evidence_is_rejected(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    path = tmp_path / "examples" / "mock_repeat_second" / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result["duplicate"] = False
    path.write_text(json.dumps(result), encoding="utf-8")
    errors = validate_mock_examples(tmp_path)
    assert any("mock_repeat_second" in error for error in errors)


def test_tampered_over_budget_trace_is_rejected(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    path = tmp_path / "examples" / "mock_over_budget" / "trace.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    trace["steps"][2]["result_summary"]["total_cost"] = 11
    path.write_text(json.dumps(trace), encoding="utf-8")
    result_path = path.with_name("result.json")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["task_plan"]["steps"] = trace["steps"]
    result_path.write_text(json.dumps(result), encoding="utf-8")
    errors = validate_mock_examples(tmp_path)
    assert any("mock_over_budget" in error for error in errors)


def test_real_failure_evidence_requires_preserved_success(tmp_path):
    source = ROOT / "examples" / "mock_tool_failure"
    target = tmp_path / "examples" / "real_tool_failure"
    shutil.copytree(source, target)
    for name in ("result.json", "trace.json"):
        path = target / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["model"]["mode"] = "real"
        payload["model"]["name"] = "real-test-model"
        if name == "trace.json":
            payload["model_exchanges"] = response_metadata(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_real_case("real_tool_failure", tmp_path) == []
    result_path = target / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["tool_results"]["candidates"] = []
    result_path.write_text(json.dumps(result), encoding="utf-8")
    assert validate_real_case("real_tool_failure", tmp_path)


def test_real_gate_rejects_mock_rename_and_inconsistent_input(tmp_path):
    source = ROOT / "examples" / "mock_normal"
    target = tmp_path / "examples" / "real_normal"
    shutil.copytree(source, target)
    for name in ("result.json", "trace.json"):
        path = target / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["model"]["mode"] = "real"
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_real_case("real_normal", tmp_path)

    for name in ("result.json", "trace.json"):
        path = target / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["model"]["name"] = "real-test-model"
        if name == "trace.json":
            payload["model_exchanges"] = response_metadata(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    input_path = target / "input.json"
    input_data = json.loads(input_path.read_text(encoding="utf-8"))
    input_data["task_id"] = "different"
    input_path.write_text(json.dumps(input_data), encoding="utf-8")
    assert validate_real_case("real_normal", tmp_path)


def test_result_steps_must_match_trace(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    path = tmp_path / "examples" / "mock_normal" / "result.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    result["task_plan"]["steps"] = []
    path.write_text(json.dumps(result), encoding="utf-8")
    assert any("mock_normal" in error for error in validate_mock_examples(tmp_path))


def test_completed_evidence_requires_consistent_saved_plan_and_trace_cost(tmp_path):
    for name, mutate in (("saved_plan", None), ("trace_cost", 999)):
        root = tmp_path / name
        shutil.copytree(ROOT / "examples", root / "examples")
        if name == "saved_plan":
            path = root / "examples" / "mock_normal" / "result.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["saved_plan"] = None
        else:
            path = root / "examples" / "mock_normal" / "trace.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["total_cost"] = mutate
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert any("mock_normal" in error for error in validate_mock_examples(root))


def test_no_feasible_evidence_cannot_drop_all_proof_steps(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    for name in ("result.json", "trace.json"):
        path = tmp_path / "examples" / "mock_no_feasible_budget" / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        if name == "result.json":
            payload["task_plan"]["steps"] = []
            payload["step_count"] = 0
            payload["estimates"] = []
            payload["tool_results"] = {"candidates": [], "scripts": []}
        else:
            payload["steps"] = []
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert any("mock_no_feasible_budget" in error for error in validate_mock_examples(tmp_path))


def test_synchronized_step_numbers_and_exact_theme_are_checked(tmp_path):
    for name in ("steps", "theme"):
        root = tmp_path / name
        shutil.copytree(ROOT / "examples", root / "examples")
        if name == "steps":
            for filename in ("result.json", "trace.json"):
                path = root / "examples" / "mock_normal" / filename
                payload = json.loads(path.read_text(encoding="utf-8"))
                steps = payload["steps"] if filename == "trace.json" else payload["task_plan"]["steps"]
                for step in steps:
                    step["step"] = 99
                path.write_text(json.dumps(payload), encoding="utf-8")
        else:
            path = root / "examples" / "mock_normal" / "input.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["theme"] = "夏"
            path.write_text(json.dumps(payload), encoding="utf-8")
        assert any("mock_normal" in error for error in validate_mock_examples(root))


def test_real_metadata_requires_full_fields_and_each_step(tmp_path):
    target = tmp_path / "examples" / "real_normal"
    shutil.copytree(ROOT / "examples" / "mock_normal", target)
    for name in ("result.json", "trace.json"):
        path = target / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["model"]["mode"] = "real"
        payload["model"]["name"] = "real-test-model"
        if name == "trace.json":
            payload["model_exchanges"] = [{"step": 1, "response_id": "resp-only"}]
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_real_case("real_normal", tmp_path)
    trace_path = target / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    trace["model_exchanges"] = [None]
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    assert validate_real_case("real_normal", tmp_path)


def test_real_provider_model_must_match_declared_model(tmp_path):
    target = tmp_path / "examples" / "real_normal"
    shutil.copytree(ROOT / "examples" / "mock_normal", target)
    for name in ("result.json", "trace.json"):
        path = target / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["model"]["mode"] = "real"
        payload["model"]["name"] = "real-test-model"
        if name == "trace.json":
            payload["model_exchanges"] = response_metadata(payload)
            payload["model_exchanges"][0]["provider_model"] = "different"
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_real_case("real_normal", tmp_path)


def test_no_feasible_search_step_must_match_candidates(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    for name in ("result.json", "trace.json"):
        path = tmp_path / "examples" / "mock_no_feasible_budget" / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        steps = payload["steps"] if name == "trace.json" else payload["task_plan"]["steps"]
        steps[0]["result_summary"]["candidates"] = []
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert any("mock_no_feasible_budget" in error for error in validate_mock_examples(tmp_path))


def test_save_step_must_match_final_plan(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    for name in ("result.json", "trace.json"):
        path = tmp_path / "examples" / "mock_normal" / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        steps = payload["steps"] if name == "trace.json" else payload["task_plan"]["steps"]
        next(step for step in steps if step["code"] == "PLAN_SAVED")["result_summary"]["total_cost"] = 999
        path.write_text(json.dumps(payload), encoding="utf-8")
    assert any("mock_normal" in error for error in validate_mock_examples(tmp_path))


def test_theme_must_match_tools_not_only_goal(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    input_path = tmp_path / "examples" / "mock_normal" / "input.json"
    input_data = json.loads(input_path.read_text(encoding="utf-8"))
    input_data["theme"] = "完全不同的主题"
    input_path.write_text(json.dumps(input_data), encoding="utf-8")
    result_path = tmp_path / "examples" / "mock_normal" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["task_plan"]["goal"] = "为主题“完全不同的主题”完成内容活动方案"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    assert any("mock_normal" in error for error in validate_mock_examples(tmp_path))


def test_input_theme_whitespace_uses_scenario_normalization(tmp_path):
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    path = tmp_path / "examples" / "mock_normal" / "input.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["theme"] = "  夏季新品发布  "
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert validate_mock_examples(tmp_path) == []


def test_resume_pairs_link_history_and_tampering_is_rejected(tmp_path):
    assert validate_resume_pair(ROOT, "mock") == []
    assert validate_resume_pair(ROOT, "real") == []
    shutil.copytree(ROOT / "examples", tmp_path / "examples")
    path = tmp_path / "examples" / "real_resume_failure_second" / "trace.json"
    trace = json.loads(path.read_text(encoding="utf-8"))
    trace["steps"][0]["rationale"] = "被篡改的历史"
    path.write_text(json.dumps(trace), encoding="utf-8")
    assert validate_resume_pair(tmp_path, "real")

    trace = json.loads(path.read_text(encoding="utf-8"))
    trace["steps"][0]["rationale"] = json.loads((ROOT / "examples" / "real_resume_failure_second" / "trace.json").read_text(encoding="utf-8"))["steps"][0]["rationale"]
    trace["resumed_from_run_id"] = "run-forged"
    trace["resume_count"] = 99
    trace["new_step_count"] = 99
    path.write_text(json.dumps(trace), encoding="utf-8")
    assert validate_resume_pair(tmp_path, "real")
