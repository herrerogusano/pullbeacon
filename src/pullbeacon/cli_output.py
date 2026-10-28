"""Output helpers for CLI commands: deterministic JSON on stdout."""

from __future__ import annotations

import sys
from typing import Any

from pydantic import BaseModel

from pullbeacon.domain.models import canonical_json
from pullbeacon.logging_utils import redact


def emit_json(data: BaseModel | dict[str, Any] | list[Any]) -> None:
    sys.stdout.write(redact(canonical_json(data)) + "\n")
