"""可复制运行的 CLI 入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .agent import Agent, early_failure
from .model import DeterministicMockModel, OpenAIModel
from .schemas import Scenario, ScenarioError, TASK_ID_RE
from .state import TaskStore, sanitize_json, write_json
from .tools import BusinessTools

ROOT = Path(__file__).resolve().parent.parent


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须大于等于 0")
    return parsed


def _trace_from_early(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": result["task_id"],
        "run_id": result["run_id"],
        "model": result["model"],
        "model_exchanges": [],
        "source_model": None,
        "duplicate": False,
        "steps": [],
        "final_status": result["status"],
        "reason": result["reason"],
        "diagnostics": [],
        "total_cost": None,
        "total_hours": None,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="内容任务规划 ReAct Agent")
    parser.add_argument("--scenario", required=True, type=Path, help="UTF-8 JSON 场景文件")
    parser.add_argument("--mode", choices=("real", "mock"), default="real")
    parser.add_argument("--max-steps", type=nonnegative_int, default=12)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data" / "runtime")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(ROOT / ".env")
    mode = args.mode
    model_name = os.getenv("OPENAI_MODEL", "unconfigured") if mode == "real" else DeterministicMockModel.name
    model_name = model_name.encode("utf-8", "replace").decode("utf-8")
    raw: Any = {}
    parsed_ok = False
    try:
        with args.scenario.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        scenario = Scenario.from_dict(raw)
    except (OSError, UnicodeError, ValueError, ScenarioError) as exc:
        code = exc.code if isinstance(exc, ScenarioError) else "invalid_input"
        task_id = raw.get("task_id", "unknown") if isinstance(raw, dict) else "unknown"
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            task_id = "unknown"
        result = early_failure(task_id, mode, model_name, code, str(exc))
        trace = _trace_from_early(result)
    else:
        parsed_ok = True
        with (ROOT / "data" / "assets.json").open("r", encoding="utf-8") as handle:
            assets = json.load(handle)
        with (ROOT / "data" / "tool_fees.json").open("r", encoding="utf-8") as handle:
            fees = json.load(handle)
        store = TaskStore(args.state_dir)
        model_factory = OpenAIModel.from_env if mode == "real" else DeterministicMockModel
        tools_factory = lambda model_info: BusinessTools(scenario, assets, fees, store, model_info)
        agent = Agent(scenario, model_factory, tools_factory, store, mode, model_name, args.max_steps)
        result = agent.run()
        trace = agent.trace

    result["trace_path"] = "trace.json"
    input_artifact = raw if parsed_ok else sanitize_json(raw)
    write_json(args.output_dir / "input.json", input_artifact if isinstance(input_artifact, dict) else {"raw_input": input_artifact}, ensure_ascii=not parsed_ok)
    write_json(args.output_dir / "result.json", result)
    write_json(args.output_dir / "trace.json", trace)
    summary = {
        "task_id": result["task_id"],
        "status": result["status"],
        "reason": result["reason"],
        "duplicate": result["duplicate"],
        "resumed": result["resumed"],
        "new_steps": result["new_step_count"],
        "total_cost": result["total_cost"],
        "total_hours": result["total_hours"],
        "steps": result["step_count"],
        "output_dir": str(args.output_dir),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())
