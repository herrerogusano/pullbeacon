from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pullbeacon.adapters.github import GitHubClient
from pullbeacon.adapters.llm import FakeLLMClient
from pullbeacon.config import PullBeaconConfig, parse_config
from pullbeacon.orchestration.flows import Services
from pullbeacon.testing.fake_github import FakeGitHub

FIXTURES = Path(__file__).parent / "fixtures"

APPLY_CONFIG = """
version: 1
automatic_review: true
model: {provider: anthropic, name: anthropic/test-model}
policy:
  apply_enabled: true
  allowed_apply_paths: ["src/**"]
"""


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def fake_llm(data: dict[str, Any]) -> FakeLLMClient:
    return FakeLLMClient(responses={k: list(v) for k, v in data.get("llm_responses", {}).items()})


def setup(
    name: str = "pr_auth_bug.json",
    *,
    config: str | PullBeaconConfig | None = APPLY_CONFIG,
    llm: FakeLLMClient | None = None,
    config_in_repo: bool = True,
    apply_job: bool = False,
) -> tuple[FakeGitHub, Services, FakeLLMClient, dict[str, Any]]:
    """FakeGitHub + Services wired like production, config committed on the base branch."""
    data = load_fixture(name)
    if config_in_repo and isinstance(config, str):
        base_sha = data["pull_request"]["base"]["sha"]
        data.setdefault("trees", {}).setdefault(base_sha, {})[".pullbeacon.yml"] = config
    fake = FakeGitHub.from_fixture(data)
    llm = llm or fake_llm(data)
    client = GitHubClient(None, transport=fake.transport(), sleep=lambda _s: None)
    override = None
    if not config_in_repo:
        override = config if isinstance(config, PullBeaconConfig) else parse_config(config or "")
    services = Services(
        client=client,
        llm_factory=lambda _cfg: llm,
        apply_job=apply_job,
        config_override=override,
    )
    return fake, services, llm, data
