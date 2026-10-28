"""Local (offline) command implementations used by the CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pullbeacon.cli_output import emit_json
from pullbeacon.config import PullBeaconConfig, default_config, load_config_file
from pullbeacon.domain.errors import ConfigError
from pullbeacon.domain.models import ReviewReport, Usage

EXIT_OK = 0
MAX_FIXTURE_BYTES = 5 * 1024 * 1024


def validate_config(path: Path) -> int:
    config = load_config_file(path)
    warnings = []
    if config.model is None:
        warnings.append("no model configured: reviews can only run with --mock-llm")
    elif config.model.is_placeholder:
        warnings.append("model.name is a placeholder: replace it before running live reviews")
    emit_json(
        {
            "valid": True,
            "file": path.name,
            "automatic_review": config.automatic_review,
            "model": config.model.name if config.model else None,
            "apply_enabled": config.policy.apply_enabled,
            "config_hash": config.config_hash(),
            "warnings": warnings,
        }
    )
    return EXIT_OK


def load_config_arg(path: Path | None) -> PullBeaconConfig:
    return load_config_file(path) if path is not None else default_config()


def load_fixture(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise ConfigError(f"fixture not readable: {path.name}") from None
    if len(raw) > MAX_FIXTURE_BYTES:
        raise ConfigError(f"fixture larger than {MAX_FIXTURE_BYTES} bytes: {path.name}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConfigError(f"fixture is not valid UTF-8 JSON: {path.name}") from None
    if not isinstance(data, dict) or "pull_request" not in data:
        raise ConfigError(f"fixture must be an object with a 'pull_request' key: {path.name}")
    return data


def review(args: argparse.Namespace) -> int:
    if args.fixture is None:
        raise ConfigError("review currently requires --fixture (GitHub mode arrives in phase 02)")
    config = load_config_arg(args.config)
    fixture = load_fixture(args.fixture)
    pr = fixture["pull_request"]
    report = ReviewReport(
        owner=pr["base"]["repo"]["owner"]["login"],
        repo=pr["base"]["repo"]["name"],
        number=pr["number"],
        head_sha=pr["head"]["sha"],
        base_sha=pr["base"]["sha"],
        provider="mock",
        model="mock/deterministic",
        prompt_version="none",
        config_hash=config.config_hash(),
        mode="local",
        simulated=True,
        summary="Simulated review: no analysis performed (phase 01 stub).",
        usage=Usage(cost_source="simulated", cost_usd=0.0),
    )
    emit_json(report)
    return EXIT_OK
