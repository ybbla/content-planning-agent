from __future__ import annotations

import pytest

from src.model import DeterministicMockModel, ModelUnavailable, OpenAIModel


def test_real_model_requires_explicit_configuration(monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ModelUnavailable, match="OPENAI_API_KEY"):
        OpenAIModel.from_env()


def test_api_key_is_redacted_from_model_errors():
    model = OpenAIModel.__new__(OpenAIModel)
    model._api_key = "sk-secret"
    message = model._safe_error(RuntimeError("request failed with sk-secret"))
    assert "sk-secret" not in message
    assert "[redacted]" in message


def test_mock_selects_candidates_by_quality_without_hardcoded_ids():
    context = {
        "task": {"task_id": "t", "theme": "主题", "quality_preference": "economy"},
        "state": {
            "candidates": [
                {"asset_id": "custom-high", "quality": "high"},
                {"asset_id": "custom-economy", "quality": "economy"},
            ],
            "scripts": [{"script_id": "custom-script"}],
            "estimates": [],
            "latest_estimate": None,
            "saved_plan": None,
        },
        "last_observation": None,
    }
    import json

    decision = json.loads(DeterministicMockModel().decide(context))
    assert decision["arguments"]["asset_id"] == "custom-economy"

    context["task"]["quality_preference"] = "high"
    context["state"]["candidates"] = [{"asset_id": "only-economy", "quality": "economy"}]
    fallback = json.loads(DeterministicMockModel().decide(context))
    assert fallback["arguments"]["asset_id"] == "only-economy"


def test_mock_replans_with_custom_ids_and_same_script():
    context = {
        "task": {"task_id": "t", "theme": "主题", "quality_preference": "high"},
        "state": {
            "candidates": [{"asset_id": "a", "quality": "high"}, {"asset_id": "b", "quality": "economy"}],
            "scripts": [{"script_id": "s"}],
            "estimates": [{"asset_id": "a", "script_id": "s", "feasible": False}],
            "latest_estimate": {"asset_id": "a", "script_id": "s", "feasible": False},
            "saved_plan": None,
        },
        "last_observation": None,
    }
    import json

    decision = json.loads(DeterministicMockModel().decide(context))
    assert decision["action"] == "estimate_cost"
    assert decision["arguments"] == {"asset_id": "b", "script_id": "s"}
    context["state"]["estimates"].append({"asset_id": "b", "script_id": "s", "feasible": False})
    context["state"]["latest_estimate"] = context["state"]["estimates"][-1]
    assert json.loads(DeterministicMockModel().decide(context))["action"] == "abort"


def test_real_model_records_safe_response_metadata():
    from types import SimpleNamespace

    response = SimpleNamespace(
        id="resp-123",
        model="provider-model",
        created=123,
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"action":"finish","arguments":{},"rationale":"ok"}'))],
    )
    completions = SimpleNamespace(create=lambda **kwargs: response)
    model = OpenAIModel.__new__(OpenAIModel)
    model.name = "provider-model"
    model.api_style = "chat"
    model._api_key = "secret"
    model.last_response = None
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    model.decide({})
    assert model.last_response == {"response_id": "resp-123", "provider_model": "provider-model", "created": 123}


def test_model_error_and_metadata_surrogates_are_sanitized():
    from types import SimpleNamespace

    model = OpenAIModel.__new__(OpenAIModel)
    model.api_style = "chat"
    model._api_key = "secret"
    message = model._safe_error(RuntimeError("bad \ud800"))
    message.encode("utf-8")
    response = SimpleNamespace(
        id="\ud800", model="provider", created=123,
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
    )
    model.name = "provider"
    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))
    model.decide({})
    model.last_response["response_id"].encode("utf-8")


def test_responses_api_uses_output_text_and_created_at():
    from types import SimpleNamespace

    calls = []
    response = SimpleNamespace(
        output_text='{"action":"finish","arguments":{},"rationale":"ok"}',
        id="resp-responses", model="provider-model", created_at=123.0,
    )
    model = OpenAIModel.__new__(OpenAIModel)
    model.name = "provider-model"
    model.api_style = "responses"
    model._api_key = "secret"
    model.last_response = {"stale": True}
    model.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or response))
    raw = model.decide({"task": {}})
    assert raw == response.output_text
    assert calls[0]["model"] == "provider-model"
    assert calls[0]["max_output_tokens"] == 500
    assert model.last_response == {"response_id": "resp-responses", "provider_model": "provider-model", "created": 123}


def test_invalid_api_style_is_rejected():
    with pytest.raises(ModelUnavailable, match="OPENAI_API_STYLE"):
        OpenAIModel("key", "https://example.test/v1", "model", "invalid")
