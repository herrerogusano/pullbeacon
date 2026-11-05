"""Output helpers for CLI commands: deterministic JSON on stdout."""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import BaseModel

from pullbeacon.domain.models import canonical_json
from pullbeacon.logging_utils import redact


def emit_json(data: BaseModel | dict[str, Any] | list[Any]) -> None:
    """Write ASCII-only JSON: a console that cannot encode a character must not lose the result.

    A model's answer can hold any character, and on Windows stdout defaults to a legacy encoding,
    so printing it failed after the paid calls had been made.
    """
    text = redact(canonical_json(data))
    sys.stdout.write(json.dumps(json.loads(text), indent=2) + "\n")
