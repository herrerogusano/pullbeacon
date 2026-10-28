"""CLI commands that talk to the real GitHub API (token from GITHUB_TOKEN)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from pullbeacon.adapters.github import GitHubClient
from pullbeacon.adapters.llm import FakeLLMClient, LiteLLMClient, LLMClient
from pullbeacon.cli_output import emit_json
from pullbeacon.config import PullBeaconConfig, load_config_file
from pullbeacon.credentials import github_token
from pullbeacon.domain.errors import ConfigError
from pullbeacon.domain.models import RunStatus
from pullbeacon.orchestration.flows import LLMFactory, Services, run_review

_REPO = re.compile(r"^([A-Za-z0-9_.\-]{1,100})/([A-Za-z0-9_.\-]{1,100})$")


def parse_repo(value: str) -> tuple[str, str]:
    match = _REPO.match(value or "")
    if not match:
        raise ConfigError("--repo must look like owner/name")
    return match.group(1), match.group(2)


def llm_factory_from_args(args: argparse.Namespace) -> LLMFactory:
    if getattr(args, "mock_llm", False):
        responses_path: Path | None = getattr(args, "responses", None)
        if responses_path is None:
            raise ConfigError("--mock-llm against GitHub requires --responses <file.json>")
        try:
            data: Any = json.loads(responses_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ConfigError("--responses must be a readable JSON file") from None
        if not isinstance(data, dict):
            raise ConfigError("--responses must map purposes to lists of responses")
        fake = FakeLLMClient(responses={k: list(v) for k, v in data.items()})
        return lambda _config: fake
    if getattr(args, "provider", None) == "live":

        def live(config: PullBeaconConfig) -> LLMClient | None:
            return LiteLLMClient(config.model) if config.model is not None else None

        return live
    raise ConfigError("choose --mock-llm --responses FILE or --provider live")


def review_remote(args: argparse.Namespace) -> int:
    if not args.repo or not args.pr:
        raise ConfigError("use --fixture for offline runs, or --repo owner/name --pr N")
    owner, repo = parse_repo(args.repo)
    override = load_config_file(args.config) if args.config else None
    with GitHubClient(github_token()) as client:
        services = Services(
            client=client, llm_factory=llm_factory_from_args(args), config_override=override
        )
        result = run_review(
            services,
            owner,
            repo,
            int(args.pr),
            mode="manual",
            trigger="cli",
            dry_run=bool(args.dry_run),
        )
    emit_json(result)
    return 0 if result.status is not RunStatus.FAILED else 1
