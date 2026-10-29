"""真实模型与显式离线决策替身。"""

from __future__ import annotations

import json
import os
from typing import Any


class ModelUnavailable(RuntimeError):
    pass


def _safe_text(value: Any) -> str:
    return str(value).encode("utf-8", "replace").decode("utf-8")


SYSTEM_PROMPT = """你是内容任务规划 Agent。每轮只选择一个动作，且只输出一个 JSON 对象：
{"action":"工具名或finish/abort","arguments":{...},"rationale":"一句简短决策依据"}
不要输出 Markdown、解释文字或内部思维过程。

业务工具与参数：
- search_assets: {"theme":"..."}，先检索候选素材。
- generate_script: {"theme":"..."}，依赖成功检索；脚本可跨素材复用。
- estimate_cost: {"asset_id":"...","script_id":"..."}，得到费用、耗时和 feasible。
- save_plan: {"task_id":"...","asset_id":"...","script_id":"...","estimate_id":"..."}，参数必须匹配最新估算。
- finish: {}，仅在 PLAN_SAVED 后使用。
- abort: {"reason":"no_feasible_plan|tool_failure|model_aborted","message":"简短原因"}。

观察工具结果后再决定下一步。超预算或超时间时，可复用脚本并估算其他候选；只有全部候选均已估算且均不可行时，才能以 no_feasible_plan abort。
临时工具失败可由你显式决定重试，永久失败应换路或终止。不得把失败结果当作成功。
quality_preference=high 时，在尚无成本观察的情况下通常先评估高质量候选；若不可行，再根据观察调整。
"""


class OpenAIModel:
    def __init__(self, api_key: str, base_url: str, model: str, api_style: str = "chat"):
        if api_style not in {"chat", "responses"}:
            raise ModelUnavailable("OPENAI_API_STYLE 只能是 chat 或 responses")
        self.api_style = api_style
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelUnavailable("未安装 openai，请先安装 requirements.txt") from exc
        self.name = _safe_text(model)
        self.last_response: dict[str, Any] | None = None
        self._api_key = api_key
        try:
            self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0, max_retries=0)
        except Exception as exc:
            raise ModelUnavailable(self._safe_error(exc)) from exc

    @classmethod
    def from_env(cls) -> "OpenAIModel":
        values = {name: os.getenv(name) for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ModelUnavailable(f"真实模型配置缺失: {', '.join(missing)}")
        return cls(values["OPENAI_API_KEY"], values["OPENAI_BASE_URL"], values["OPENAI_MODEL"], os.getenv("OPENAI_API_STYLE", "chat"))

    def _safe_error(self, exc: Exception) -> str:
        message = _safe_text(exc).replace(self._api_key, "[redacted]")
        return f"{type(exc).__name__}: {message[:300]}"

    def decide(self, context: dict[str, Any]) -> str:
        self.last_response = None
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        try:
            if self.api_style == "responses":
                response = self.client.responses.create(
                    model=self.name,
                    input=messages,
                    max_output_tokens=500,
                )
                raw = response.output_text or ""
                created = getattr(response, "created_at", None)
            else:
                response = self.client.chat.completions.create(
                    model=self.name,
                    messages=messages,
                    temperature=0,
                    max_tokens=350,
                )
                raw = response.choices[0].message.content or ""
                created = getattr(response, "created", None)
            if isinstance(created, float) and created.is_integer():
                created = int(created)
            if isinstance(created, bool) or not isinstance(created, int) or created < 0:
                created = None
            self.last_response = {
                "response_id": _safe_text(response.id) if isinstance(getattr(response, "id", None), str) else None,
                "provider_model": _safe_text(response.model) if isinstance(getattr(response, "model", None), str) else None,
                "created": created,
            }
            return raw
        except Exception as exc:
            raise ModelUnavailable(self._safe_error(exc)) from exc


class DeterministicMockModel:
    name = "deterministic-mock-v1"

    @staticmethod
    def _action(action: str, arguments: dict[str, Any], rationale: str) -> str:
        return json.dumps({"action": action, "arguments": arguments, "rationale": rationale}, ensure_ascii=False)

    def decide(self, context: dict[str, Any]) -> str:
        state = context["state"]
        task = context["task"]
        last = context.get("last_observation")
        if last and not last.get("ok"):
            if last.get("code") == "TEMPORARY_TOOL_FAILURE":
                return self._action(last["action"], last["arguments"], "临时故障后由 Mock 显式重试一次")
            if last.get("code") == "PERMANENT_TOOL_FAILURE":
                return self._action("abort", {"reason": "tool_failure", "message": "永久工具故障"}, "永久故障后停止")

        if state.get("saved_plan"):
            return self._action("finish", {}, "方案已经保存，结束任务")
        if not state["candidates"]:
            return self._action("search_assets", {"theme": task["theme"]}, "先获取候选素材")
        if not state["scripts"]:
            return self._action("generate_script", {"theme": task["theme"]}, "生成可跨素材复用的脚本")

        estimates = state["estimates"]
        if not estimates:
            quality = task["quality_preference"]
            preferred = next(
                (candidate for candidate in state["candidates"] if candidate.get("quality") == quality),
                state["candidates"][0],
            )
            return self._action(
                "estimate_cost",
                {"asset_id": preferred["asset_id"], "script_id": state["scripts"][0]["script_id"]},
                "先估算偏好候选",
            )

        latest = state["latest_estimate"]
        if latest["feasible"]:
            return self._action(
                "save_plan",
                {
                    "task_id": task["task_id"],
                    "asset_id": latest["asset_id"],
                    "script_id": latest["script_id"],
                    "estimate_id": latest["estimate_id"],
                },
                "最新估算满足预算和截止时间，保存方案",
            )

        tried = {estimate["asset_id"] for estimate in estimates}
        for candidate in state["candidates"]:
            if candidate["asset_id"] not in tried:
                return self._action(
                    "estimate_cost",
                    {"asset_id": candidate["asset_id"], "script_id": latest["script_id"]},
                    "当前方案不可行，复用脚本并估算替代候选",
                )
        return self._action(
            "abort",
            {"reason": "no_feasible_plan", "message": "所有候选均不满足约束"},
            "全部候选已估算且均不可行",
        )


class QueueModel:
    """测试协议与异常用的队列模型，不进入生产决策路径。"""

    name = "queue-model"

    def __init__(self, responses: list[str | Exception]):
        self.responses = list(responses)
        self.calls = 0

    def decide(self, context: dict[str, Any]) -> str:
        self.calls += 1
        if not self.responses:
            return json.dumps({"action": "abort", "arguments": {"reason": "model_aborted"}, "rationale": "队列为空"})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
