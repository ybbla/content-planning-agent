from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def invoke(args):
    return subprocess.run([sys.executable, "-m", "src.main", *args], cwd=ROOT, text=True, capture_output=True)


def test_cli_normal_writes_input_result_and_trace(tmp_path):
    output = tmp_path / "out"
    completed = invoke([
        "--scenario", "data/scenarios/normal.json", "--mode", "mock",
        "--state-dir", str(tmp_path / "state"), "--output-dir", str(output),
    ])
    assert completed.returncode == 0, completed.stderr
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    trace = json.loads((output / "trace.json").read_text(encoding="utf-8"))
    assert result["model"]["mode"] == trace["model"]["mode"] == "mock"
    assert result["total_cost"] == 12
    assert result["trace_path"] == "trace.json"
    assert (output / "input.json").exists()
    assert len(trace["steps"]) == result["step_count"]


def test_cli_business_unfinished_uses_exit_code_two(tmp_path):
    completed = invoke([
        "--scenario", "data/scenarios/no_feasible_budget.json", "--mode", "mock",
        "--state-dir", str(tmp_path / "state"), "--output-dir", str(tmp_path / "out"),
    ])
    assert completed.returncode == 2
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["reason"]["details"]["deficit"]["cost"] == 2


def test_cli_malformed_input_is_reproducible_unfinished(tmp_path):
    scenario = tmp_path / "bad.json"
    scenario.write_text("{bad", encoding="utf-8")
    completed = invoke([
        "--scenario", str(scenario), "--mode", "mock", "--output-dir", str(tmp_path / "out")
    ])
    assert completed.returncode == 2
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["reason"]["code"] == "invalid_input"
    assert result["step_count"] == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"quality_preference": []},
        {"fault": {"tool": [], "kind": "transient"}},
        {"fault": {"tool": "search_assets", "kind": {}}},
    ],
)
def test_cli_wrong_enum_types_return_structured_invalid_input(tmp_path, patch):
    raw = {"task_id": "bad-types", "theme": "测试", "budget": 10, "deadline_hours": 8} | patch
    scenario = tmp_path / "bad-types.json"
    scenario.write_text(json.dumps(raw), encoding="utf-8")
    completed = invoke([
        "--scenario", str(scenario), "--mode", "mock", "--output-dir", str(tmp_path / "out")
    ])
    assert completed.returncode == 2
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["reason"]["code"] == "invalid_input"
    assert result["step_count"] == 0


@pytest.mark.parametrize("field", ["budget", "deadline_hours"])
def test_cli_very_large_integer_is_handled_without_overflow(tmp_path, field):
    raw = {"task_id": "huge-number", "theme": "测试", "budget": 20, "deadline_hours": 8, field: 10**400}
    scenario = tmp_path / "huge.json"
    scenario.write_text(json.dumps(raw), encoding="utf-8")
    completed = invoke([
        "--scenario", str(scenario), "--mode", "mock", "--state-dir", str(tmp_path / "state"),
        "--output-dir", str(tmp_path / "out"),
    ])
    assert completed.returncode == 0, completed.stderr
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "completed"


def test_cli_surrogate_theme_still_writes_structured_error(tmp_path):
    raw = {"task_id": "surrogate", "theme": "\ud800", "budget": 10, "deadline_hours": 8}
    scenario = tmp_path / "surrogate.json"
    scenario.write_text(json.dumps(raw), encoding="utf-8")
    completed = invoke([
        "--scenario", str(scenario), "--mode", "mock", "--output-dir", str(tmp_path / "out")
    ])
    assert completed.returncode == 2, completed.stderr
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["reason"]["code"] == "invalid_input"
    assert (tmp_path / "out" / "input.json").exists()


def test_cli_invalid_utf8_input_returns_structured_error(tmp_path):
    scenario = tmp_path / "invalid-utf8.json"
    scenario.write_bytes(b"\xff")
    completed = invoke([
        "--scenario", str(scenario), "--mode", "mock", "--output-dir", str(tmp_path / "out")
    ])
    assert completed.returncode == 2
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["reason"]["code"] == "invalid_input"


def test_cli_json_integer_digit_limit_returns_invalid_input(tmp_path):
    scenario = tmp_path / "long-int.json"
    scenario.write_text('{"task_id":"long-int","theme":"测试","budget":' + "9" * 5000 + ',"deadline_hours":8}', encoding="utf-8")
    completed = invoke([
        "--scenario", str(scenario), "--mode", "mock", "--output-dir", str(tmp_path / "out")
    ])
    assert completed.returncode == 2
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["reason"]["code"] == "invalid_input"


@pytest.mark.parametrize("number", ["NaN", "1e999"])
def test_cli_nonfinite_input_is_safe_and_structured(tmp_path, number):
    scenario = tmp_path / "nonfinite.json"
    scenario.write_text('{"task_id":"nonfinite","theme":"测试","budget":' + number + ',"deadline_hours":8}', encoding="utf-8")
    completed = invoke([
        "--scenario", str(scenario), "--mode", "mock", "--output-dir", str(tmp_path / "out")
    ])
    assert completed.returncode == 2
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["reason"]["code"] == "invalid_input"
    input_data = json.loads((tmp_path / "out" / "input.json").read_text(encoding="utf-8"))
    assert "invalid_number" in input_data["budget"]


def test_cli_failure_resume_across_two_processes(tmp_path):
    state = tmp_path / "resume-state"
    first = invoke([
        "--scenario", "data/scenarios/resume_failure.json", "--mode", "mock", "--max-steps", "2",
        "--state-dir", str(state), "--output-dir", str(tmp_path / "first"),
    ])
    assert first.returncode == 2
    second = invoke([
        "--scenario", "data/scenarios/resume_failure.json", "--mode", "mock",
        "--state-dir", str(state), "--output-dir", str(tmp_path / "second"),
    ])
    assert second.returncode == 0, second.stderr
    result = json.loads((tmp_path / "second" / "result.json").read_text(encoding="utf-8"))
    assert result["resumed"] is True
    assert result["step_count"] == 6
    assert result["new_step_count"] == 4
    assert not (state / "checkpoints" / "resume-failure-001.json").exists()
