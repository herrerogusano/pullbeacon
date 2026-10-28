"""Structured logging that never emits secrets.

Log records carry metadata (IDs, counts, statuses, URLs). Code, diffs, prompts and model output
must not be logged; the redactor is a defence in depth for credentials that slip through.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

SECRET_ENV_VARS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "PULLBEACON_GITHUB_TOKEN",
)

TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)(authorization|x-api-key|api[_-]?key)(\s*[:=]\s*)(bearer\s+)?[^\s,;\"']+"),
)

REDACTED = "[REDACTED]"


def _secret_values(extra: Iterable[str] = ()) -> list[str]:
    values = [os.environ.get(name, "") for name in SECRET_ENV_VARS]
    values.extend(extra)
    # Very short values would redact innocent text; real credentials are long.
    return sorted({v for v in values if len(v) >= 8}, key=len, reverse=True)


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    for value in _secret_values(extra_secrets):
        text = text.replace(value, REDACTED)
    for pattern in TOKEN_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            record.fields = {k: _redact_value(v) for k, v in fields.items()}
        return True


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info and record.exc_info[0] is not None:
            # Only the exception type: tracebacks may contain locals or request data.
            payload["exception"] = record.exc_info[0].__name__
        return json.dumps(payload, sort_keys=True, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger("pullbeacon")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
    # Third-party loggers can print request bodies or headers at debug level.
    for noisy in ("httpx", "httpcore", "LiteLLM", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"pullbeacon.{name}")


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})
