"""`.pullbeacon.yml` schema (version 1).

Trust boundary: in GitHub Actions the configuration is always read from the **base branch**
(trusted, reviewed) and never from the PR head. A PR can therefore not raise limits, enable
`apply`, widen paths or switch providers for its own review.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pullbeacon.core.paths import UnsafePathError, validate_glob
from pullbeacon.domain.errors import ConfigError
from pullbeacon.domain.models import canonical_json

CONFIG_FILENAMES = (".pullbeacon.yml", ".pullbeacon.yaml")
MAX_CONFIG_BYTES = 64 * 1024

Provider = Literal["anthropic", "openai", "gemini"]

PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

_TOO_BROAD = {"**", "*", "**/*"}


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelConfig(_Strict):
    provider: Provider
    # LiteLLM model id, e.g. "anthropic/<model>", "openai/<model>", "gemini/<model>".
    name: str = Field(min_length=3, max_length=200)
    temperature: float | None = Field(default=0.0, ge=0.0, le=1.0)
    timeout_seconds: int = Field(default=120, ge=5, le=600)

    @model_validator(mode="after")
    def _name_matches_provider(self) -> ModelConfig:
        prefix = f"{self.provider}/"
        if not self.name.startswith(prefix) or len(self.name) == len(prefix):
            raise ValueError(f"model.name must be a LiteLLM id starting with '{prefix}'")
        if "<" in self.name or ">" in self.name:
            raise ValueError("model.name still contains a placeholder; set a real model id")
        if any(ch.isspace() for ch in self.name):
            raise ValueError("model.name must not contain whitespace")
        return self

    @property
    def is_placeholder(self) -> bool:
        return "REPLACE" in self.name.upper()


class LimitsConfig(_Strict):
    max_changed_files: int = Field(default=40, ge=1, le=300)
    max_changed_lines: int = Field(default=1500, ge=1, le=20000)
    max_context_chars: int = Field(default=120_000, ge=1000, le=1_000_000)
    max_file_bytes: int = Field(default=200_000, ge=1000, le=1_000_000)
    max_output_tokens: int = Field(default=4096, ge=256, le=32000)
    max_estimated_cost_usd: float = Field(default=1.0, gt=0, le=50)
    max_inline_comments: int = Field(default=15, ge=0, le=50)
    max_batches: int = Field(default=4, ge=1, le=20)


class PolicyConfig(_Strict):
    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    apply_enabled: bool = False
    allowed_apply_paths: tuple[str, ...] = ()
    min_apply_confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    max_apply_files: int = Field(default=5, ge=1, le=20)
    max_apply_changed_lines: int = Field(default=120, ge=1, le=1000)
    apply_context_window: int = Field(default=10, ge=0, le=50)

    @field_validator("include", "exclude", "context_files", "allowed_apply_paths")
    @classmethod
    def _valid_globs(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        if len(patterns) > 100:
            raise ValueError("at most 100 patterns are allowed")
        try:
            return tuple(validate_glob(p) for p in patterns)
        except UnsafePathError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("context_files", "allowed_apply_paths")
    @classmethod
    def _not_too_broad(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        broad = [p for p in patterns if p in _TOO_BROAD]
        if broad:
            raise ValueError(f"pattern {broad[0]!r} is too broad; list directories explicitly")
        return patterns

    @model_validator(mode="after")
    def _apply_needs_paths(self) -> PolicyConfig:
        if self.apply_enabled and not self.allowed_apply_paths:
            raise ValueError("apply_enabled requires at least one allowed_apply_paths entry")
        return self


class PullBeaconConfig(_Strict):
    version: Literal[1] = 1
    automatic_review: bool = False
    model: ModelConfig | None = None
    limits: LimitsConfig = LimitsConfig()
    policy: PolicyConfig = PolicyConfig()

    def config_hash(self) -> str:
        """Stable hash used for idempotency of automatic reviews."""
        digest = hashlib.sha256(canonical_json(self, indent=None).encode()).hexdigest()
        return digest[:16]


def default_config() -> PullBeaconConfig:
    """Used when the base branch has no config: conservative, no automatic review, no apply."""
    return PullBeaconConfig()


def parse_config(text: str, *, source: str = "config") -> PullBeaconConfig:
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigError(f"{source}: file is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        # Aliases allow "billion laughs" expansion during validation; config never needs them.
        if any(isinstance(ev, yaml.AliasEvent) for ev in yaml.parse(text, Loader=yaml.SafeLoader)):
            raise ConfigError(f"{source}: YAML aliases are not supported")
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"{source}: invalid YAML{where}") from None
    if data is None:
        return default_config()
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping")
    try:
        return PullBeaconConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{source}: {_format_validation(exc)}") from None


def load_config_file(path: Path) -> PullBeaconConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path.name}") from None
    except (OSError, UnicodeDecodeError):
        raise ConfigError(f"config file could not be read as UTF-8: {path.name}") from None
    return parse_config(text, source=path.name)


def _format_validation(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:10]:
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        msg = err["msg"]
        if err["type"] == "extra_forbidden":
            msg = "unknown key"
        parts.append(f"{loc}: {msg}")
    more = len(exc.errors()) - 10
    if more > 0:
        parts.append(f"... and {more} more")
    return "; ".join(parts)
