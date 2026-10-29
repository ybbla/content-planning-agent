from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.agent import Agent
from src.model import DeterministicMockModel
from src.schemas import Scenario
from src.state import TaskStore
from src.tools import BusinessTools

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def catalog():
    with (ROOT / "data" / "assets.json").open(encoding="utf-8") as handle:
        assets = json.load(handle)
    with (ROOT / "data" / "tool_fees.json").open(encoding="utf-8") as handle:
        fees = json.load(handle)
    return assets, fees


@pytest.fixture
def scenario_dict():
    return {
        "task_id": "test-001",
        "theme": "测试活动",
        "budget": 20,
        "deadline_hours": 8,
        "quality_preference": "high",
    }


@pytest.fixture
def run_agent(tmp_path, catalog, scenario_dict):
    assets, fees = catalog

    def run(model=None, raw=None, max_steps=12, store=None, mode="mock"):
        scenario = Scenario.from_dict(deepcopy(raw or scenario_dict))
        store = store or TaskStore(tmp_path / "state")
        model = model or DeterministicMockModel()
        model_name = getattr(model, "name", "test-model")
        info = {"mode": mode, "name": model_name}

        def model_factory():
            if isinstance(model, Exception):
                raise model
            return model

        agent = Agent(
            scenario,
            model_factory,
            lambda model_info: BusinessTools(scenario, assets, fees, store, model_info),
            store,
            mode,
            model_name,
            max_steps,
        )
        result = agent.run()
        return result, agent, store

    return run
