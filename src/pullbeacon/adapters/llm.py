"""Provider-neutral LLM access.

The model never receives tools: no shell, browser, file system, network or secrets. It gets a
system prompt plus untrusted data and must answer with JSON, which is validated afterwards.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pullbeacon.config import ModelConfig
from pullbeacon.credentials import Secret, provider_api_key
from pullbeacon.domain.errors import ConfigError, LLMError
from pullbeacon.domain.models import Usage
from pullbeacon.logging_utils import get_logger, log_event

Purpose = Literal["review", "explain", "apply"]
MAX_TRANSIENT_RETRIES = 2
CHARS_PER_TOKEN_ESTIMATE = 3.5

log = get_logger("llm")


@dataclass(frozen=True)
class LLMRequest:
    purpose: Purpose
    system: str
    user: str
    max_output_tokens: int
    json_schema: Mapping[str, Any] | None = None
    schema_name: str = "response"

    @property
    def size_chars(self) -> int:
        return len(self.system) + len(self.user)


@dataclass(frozen=True)
class LLMResponse:
    text: str
    usage: Usage
    model: str


class LLMClient(Protocol):
    provider: str
    model: str
    simulated: bool

    def complete(self, request: LLMRequest) -> LLMResponse: ...

    def estimate_max_cost(self, request: LLMRequest) -> float | None:
        """Upper-bound estimate (full output budget) in USD, or ``None`` if unknown."""
        ...


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN_ESTIMATE) + 1


# --------------------------------------------------------------------------- fake


@dataclass
class FakeLLMClient:
    """Deterministic stand-in. ``responses`` maps a purpose to queued raw texts (or a callable)."""

    responses: dict[str, list[str]] = field(default_factory=dict)
    responder: Callable[[LLMRequest], str] | None = None
    estimated_cost: float | None = 0.0
    provider: str = "mock"
    model: str = "mock/deterministic"
    simulated: bool = True
    requests: list[LLMRequest] = field(default_factory=list)
    errors: list[Exception] = field(default_factory=list)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        if self.responder is not None:
            text = self.responder(request)
        else:
            queue = self.responses.get(request.purpose)
            if not queue:
                raise LLMError(f"mock LLM has no queued response for '{request.purpose}'")
            text = queue.pop(0) if len(queue) > 1 else queue[0]
        usage = Usage(
            llm_calls=1,
            input_tokens=estimate_tokens(request.system + request.user),
            output_tokens=estimate_tokens(text),
            cost_usd=self.estimated_cost or 0.0,
            cost_source="simulated",
        )
        return LLMResponse(text=text, usage=usage, model=self.model)

    def estimate_max_cost(self, request: LLMRequest) -> float | None:
        return self.estimated_cost


# --------------------------------------------------------------------------- LiteLLM


def _import_litellm() -> Any:
    # Use the cost map bundled with the installed version: no network fetch at import time
    # and a pricing source that is pinned by the lockfile.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import litellm

    litellm.telemetry = False
    litellm.suppress_debug_info = True
    return litellm


class LiteLLMClient:
    """LiteLLM Python SDK adapter (no proxy, no database)."""

    simulated = False

    def __init__(
        self,
        model_config: ModelConfig,
        *,
        api_key: Secret | None = None,
        sleep: Callable[[float], None] = time.sleep,
        completion: Callable[..., Any] | None = None,
    ) -> None:
        if model_config.is_placeholder:
            raise ConfigError("model.name is still a placeholder; set a real model id")
        self.provider: str = model_config.provider
        self.model: str = model_config.name
        self._config = model_config
        self._api_key = api_key or provider_api_key(model_config.provider)
        self._sleep = sleep
        self._litellm = _import_litellm()
        self._completion = completion or self._litellm.completion
        self._schema_supported = self._supports_schema()

    def _supports_schema(self) -> bool:
        try:
            return bool(self._litellm.supports_response_schema(model=self.model))
        except Exception:
            return False

    def estimate_max_cost(self, request: LLMRequest) -> float | None:
        try:
            prompt_cost, completion_cost = self._litellm.cost_per_token(
                model=self.model,
                prompt_tokens=estimate_tokens(request.system + request.user),
                completion_tokens=request.max_output_tokens,
            )
            total = float(prompt_cost) + float(completion_cost)
        except Exception:
            return None
        return total if total > 0 else None

    def complete(self, request: LLMRequest) -> LLMResponse:
        use_schema = self._schema_supported and request.json_schema is not None
        attempt = 0
        while True:
            try:
                response = self._completion(**self._arguments(request, use_schema=use_schema))
            except Exception as exc:
                kind = self._classify(exc)
                if kind == "schema_unsupported" and use_schema:
                    log_event(log, "llm_schema_fallback", model=self.model)
                    use_schema = False
                    continue
                if kind == "transient" and attempt < MAX_TRANSIENT_RETRIES:
                    attempt += 1
                    wait = 2.0 * attempt
                    log_event(log, "llm_retry", model=self.model, attempt=attempt, wait=wait)
                    self._sleep(wait)
                    continue
                raise LLMError(self._message(kind, exc)) from None
            return self._to_response(response)

    def _arguments(self, request: LLMRequest, *, use_schema: bool) -> dict[str, Any]:
        args: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_output_tokens,
            "timeout": self._config.timeout_seconds,
            "api_key": self._api_key.reveal(),
            "num_retries": 0,
            "drop_params": True,
        }
        if self._config.temperature is not None:
            args["temperature"] = self._config.temperature
        if use_schema and request.json_schema is not None:
            args["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "schema": dict(request.json_schema),
                    "strict": True,
                },
            }
        return args

    def _to_response(self, response: Any) -> LLMResponse:
        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError, TypeError):
            text = None
        if not isinstance(text, str) or not text.strip():
            raise LLMError("model returned an empty response")
        usage_obj = getattr(response, "usage", None)
        input_tokens = getattr(usage_obj, "prompt_tokens", None)
        output_tokens = getattr(usage_obj, "completion_tokens", None)
        cost: float | None
        try:
            cost = float(self._litellm.completion_cost(completion_response=response))
        except Exception:
            cost = None
        if cost is not None and cost <= 0:
            cost = None
        return LLMResponse(
            text=text,
            usage=Usage(
                llm_calls=1,
                input_tokens=input_tokens if isinstance(input_tokens, int) else None,
                output_tokens=output_tokens if isinstance(output_tokens, int) else None,
                cost_usd=cost,
                cost_source="litellm" if cost is not None else "unknown",
            ),
            model=self.model,
        )

    def _classify(self, exc: Exception) -> str:
        lite = self._litellm
        if isinstance(exc, lite.AuthenticationError | lite.PermissionDeniedError):
            return "auth"
        if isinstance(exc, lite.ContextWindowExceededError):
            return "context"
        if isinstance(exc, lite.RateLimitError | lite.Timeout | lite.APIConnectionError):
            return "transient"
        if isinstance(exc, lite.InternalServerError | lite.ServiceUnavailableError):
            return "transient"
        if isinstance(exc, lite.NotFoundError):
            return "not_found"
        if isinstance(exc, lite.BadRequestError):
            text = str(exc).lower()
            if "response_format" in text or "json_schema" in text or "schema" in text:
                return "schema_unsupported"
            return "bad_request"
        return "unknown"

    def _message(self, kind: str, exc: Exception) -> str:
        messages = {
            "auth": f"provider rejected the API key for {self.provider} (check the secret)",
            "context": "the request exceeds the model context window; lower max_context_chars",
            "transient": "provider unavailable or rate limited after retries",
            "not_found": f"model '{self.model}' was not found for provider {self.provider}",
            "bad_request": "provider rejected the request (invalid parameters)",
            "schema_unsupported": "provider rejected the structured output request",
        }
        # Never include str(exc): provider errors can echo request content.
        return messages.get(kind, f"unexpected provider error ({type(exc).__name__})")


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating a single surrounding Markdown code fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1 and stripped.endswith("```"):
            stripped = stripped[first_newline + 1 : -3].strip()
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
    return data
