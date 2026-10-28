from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from pullbeacon.adapters.llm import LiteLLMClient, LLMRequest, parse_json_object
from pullbeacon.config import ModelConfig
from pullbeacon.credentials import Secret
from pullbeacon.domain.errors import ConfigError, CredentialError, LLMError
from pullbeacon.prompts import REVIEW_SCHEMA

KEY = "sk-ant-api03-" + "k" * 40
MODEL = ModelConfig(provider="anthropic", name="anthropic/test-model")
REQUEST = LLMRequest(
    purpose="review", system="sys", user="user", max_output_tokens=100, json_schema=REVIEW_SCHEMA
)


def ok_response(text: str = '{"summary": "", "findings": []}') -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


class Recorder:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client(recorder: Recorder, model: ModelConfig = MODEL) -> LiteLLMClient:
    return LiteLLMClient(model, api_key=Secret(KEY), sleep=lambda _s: None, completion=recorder)


def test_missing_api_key_is_a_clear_error_without_calls() -> None:
    with pytest.raises(CredentialError, match="ANTHROPIC_API_KEY"):
        LiteLLMClient(MODEL, completion=Recorder())


def test_placeholder_model_is_refused() -> None:
    placeholder = ModelConfig(provider="openai", name="openai/REPLACE-WITH-MODEL-ID")
    with pytest.raises(ConfigError, match="placeholder"):
        LiteLLMClient(placeholder, api_key=Secret(KEY), completion=Recorder())


def test_request_has_no_tools_and_bounded_options() -> None:
    recorder = Recorder(ok_response())
    response = client(recorder).complete(REQUEST)
    (call,) = recorder.calls
    assert not {"tools", "functions", "tool_choice"} & set(call)
    assert call["num_retries"] == 0
    assert call["max_tokens"] == 100
    assert call["api_key"] == KEY
    assert [m["role"] for m in call["messages"]] == ["system", "user"]
    assert response.usage.input_tokens == 10
    assert response.usage.llm_calls == 1


@pytest.mark.parametrize(
    "error",
    [
        litellm.RateLimitError("slow", llm_provider="anthropic", model="m"),
        litellm.Timeout("t", model="m", llm_provider="anthropic"),
        litellm.InternalServerError("boom", llm_provider="anthropic", model="m"),
    ],
)
def test_transient_errors_retry_at_most_twice(error: Exception) -> None:
    recorder = Recorder(error, error, ok_response())
    assert client(recorder).complete(REQUEST).text
    assert len(recorder.calls) == 3
    failing = Recorder(error, error, error)
    with pytest.raises(LLMError, match="after retries"):
        client(failing).complete(REQUEST)
    assert len(failing.calls) == 3


def test_auth_error_is_not_retried_and_hides_details() -> None:
    error = litellm.AuthenticationError(f"bad key {KEY}", llm_provider="anthropic", model="m")
    recorder = Recorder(error)
    with pytest.raises(LLMError) as info:
        client(recorder).complete(REQUEST)
    assert len(recorder.calls) == 1
    assert KEY not in str(info.value)


def test_bad_request_is_not_retried() -> None:
    recorder = Recorder(litellm.BadRequestError("bad temperature", model="m", llm_provider="x"))
    with pytest.raises(LLMError, match="invalid parameters"):
        client(recorder).complete(REQUEST)
    assert len(recorder.calls) == 1


def test_empty_response_is_an_error() -> None:
    with pytest.raises(LLMError, match="empty"):
        client(Recorder(ok_response("  "))).complete(REQUEST)


def test_unknown_model_has_unknown_cost() -> None:
    llm = client(Recorder(ok_response()))
    assert llm.estimate_max_cost(REQUEST) is None
    assert llm.complete(REQUEST).usage.cost_source == "unknown"


def test_known_model_cost_estimate_from_local_cost_map() -> None:
    model = ModelConfig(provider="openai", name="openai/gpt-4o")
    estimate = LiteLLMClient(model, api_key=Secret(KEY), completion=Recorder()).estimate_max_cost(
        REQUEST
    )
    assert estimate is not None
    assert estimate > 0


def test_parse_json_object() -> None:
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError, match="not an object"):
        parse_json_object("[1, 2]")
