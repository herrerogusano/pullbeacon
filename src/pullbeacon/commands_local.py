"""Local (offline) command implementations used by the CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pullbeacon.adapters.github import GitHubClient, fetch_snapshot
from pullbeacon.adapters.llm import FakeLLMClient, LiteLLMClient, LLMClient
from pullbeacon.cli_output import emit_json
from pullbeacon.config import PullBeaconConfig, default_config, load_config_file
from pullbeacon.core.context import fetch_context_files
from pullbeacon.core.review import ReviewEngine
from pullbeacon.core.scope import ReviewScope, select_scope
from pullbeacon.domain.errors import ConfigError
from pullbeacon.domain.models import PullRequestSnapshot
from pullbeacon.testing.fake_github import FakeGitHub

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


def _ranges(numbers: list[int]) -> list[list[int]]:
    ranges: list[list[int]] = []
    for n in sorted(set(numbers)):
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return ranges


def scope_summary(snapshot: PullRequestSnapshot, scope: ReviewScope) -> dict[str, Any]:
    return {
        "pull_request": f"{snapshot.full_name}#{snapshot.number}",
        "head_sha": snapshot.head_sha,
        "base_sha": snapshot.base_sha,
        "truncated": scope.truncated,
        "total_files": scope.total_files,
        "files": [
            {
                "path": d.path,
                "status": d.status,
                "previous_path": d.previous_path,
                "hunks": len(d.hunks),
                "added_lines": d.added_lines,
                "deleted_lines": d.deleted_lines,
                "commentable_right": _ranges(
                    [ln.new_line for ln in d.lines() if ln.new_line is not None]
                ),
                "commentable_left": _ranges(
                    [ln.old_line for ln in d.lines() if ln.old_line is not None]
                ),
            }
            for d in scope.diffs
        ],
        "skipped": [s.model_dump() for s in scope.skipped],
    }


def snapshot_from_fixture(fixture: dict[str, Any]) -> tuple[FakeGitHub, PullRequestSnapshot]:
    """Run the real GitHub client against an in-memory GitHub built from the fixture."""
    fake = FakeGitHub.from_fixture(fixture)
    number = int(fixture["pull_request"]["number"])
    with GitHubClient(None, transport=fake.transport(), max_retries=0) as client:
        snapshot = fetch_snapshot(client, fake.owner, fake.repo, number)
    return fake, snapshot


def llm_for_fixture(
    args: argparse.Namespace, config: PullBeaconConfig, fixture: dict[str, Any]
) -> LLMClient:
    if getattr(args, "mock_llm", False):
        responses = fixture.get("llm_responses")
        if not isinstance(responses, dict):
            raise ConfigError("fixture has no 'llm_responses' for --mock-llm")
        return FakeLLMClient(responses={k: list(v) for k, v in responses.items()})
    if getattr(args, "provider", None) == "live":
        if config.model is None:
            raise ConfigError("--provider live requires 'model' in the config file (--config)")
        return LiteLLMClient(config.model)
    raise ConfigError("choose --mock-llm (offline) or --provider live (paid API call)")


def review(args: argparse.Namespace) -> int:
    if args.fixture is None:
        from pullbeacon.commands_github import review_remote

        return review_remote(args)
    config = load_config_arg(args.config)
    fixture = load_fixture(args.fixture)
    fake, snapshot = snapshot_from_fixture(fixture)
    scope = select_scope(snapshot, config)
    if not (getattr(args, "mock_llm", False) or getattr(args, "provider", None)):
        # Scope-only dry run: shows exactly what would be sent, without any LLM.
        emit_json(scope_summary(snapshot, scope))
        return EXIT_OK
    llm = llm_for_fixture(args, config, fixture)
    with GitHubClient(None, transport=fake.transport(), max_retries=0) as client:
        context, warnings = fetch_context_files(
            client, snapshot, config, budget=config.limits.max_context_chars // 4
        )
    report = ReviewEngine(llm, config).review(
        snapshot, scope, mode="local", context_files=context, context_warnings=warnings
    )
    emit_json(report)
    return EXIT_OK
