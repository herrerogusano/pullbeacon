"""Run the dataset through the real review pipeline (offline GitHub, mock or live LLM)."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from pullbeacon import __version__
from pullbeacon.adapters.github import GitHubClient, fetch_snapshot
from pullbeacon.adapters.llm import FakeLLMClient, LLMClient
from pullbeacon.config import PullBeaconConfig, parse_config
from pullbeacon.core.apply import (
    SourceFile,
    build_apply_request,
    select_findings,
    validate_proposal,
)
from pullbeacon.core.markers import marker_from_report
from pullbeacon.core.review import CostGuard, ReviewEngine, call_json
from pullbeacon.core.scope import select_scope
from pullbeacon.domain.errors import PullBeaconError
from pullbeacon.evaluation.dataset import EvalCase
from pullbeacon.evaluation.metrics import CaseResult, ModelSummary, score_case, summarize
from pullbeacon.prompts import APPLY_PROMPT_VERSION, REVIEW_PROMPT_VERSION
from pullbeacon.testing.fake_github import FakeGitHub

EVAL_CONFIG = """
version: 1
limits: {max_output_tokens: 4096, max_estimated_cost_usd: 0.5}
policy:
  apply_enabled: true
  allowed_apply_paths: ["app/**", "web/**", "tests/**"]
"""

ClientFactory = Callable[[EvalCase], LLMClient]


class EvalRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: str
    started_at: str
    pullbeacon_version: str
    git_commit: str
    prompt_versions: dict[str, str]
    dataset_sha256: str
    config_hash: str
    temperature: float | None
    notes: tuple[str, ...]
    summaries: tuple[ModelSummary, ...]
    cases: dict[str, tuple[CaseResult, ...]]


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = out.stdout.strip()
    return value if out.returncode == 0 and len(value) == 40 else "unknown"


def mock_factory(case: EvalCase) -> LLMClient:
    return FakeLLMClient(responses={k: list(v) for k, v in case.mock_responses.items()})


def _apply_check(
    case: EvalCase, llm: LLMClient, report: Any, config: PullBeaconConfig, fake: FakeGitHub
) -> tuple[bool, bool, bool]:
    expectation = case.apply_expectation
    if expectation is None:
        return False, False, False
    marker = marker_from_report(report, trigger="eval")
    eligible, _ = select_findings(marker.findings, None, config)
    eligible = [f for f in eligible if f.path == expectation.path][:1]
    if not eligible:
        return True, False, False
    head = case.fixture["pull_request"]["head"]["sha"]
    content = fake.file_at(head, expectation.path)
    if content is None:
        return True, False, False
    blob = next(f["sha"] for f in case.fixture["files"] if f["filename"] == expectation.path)
    source = SourceFile(expectation.path, content.decode("utf-8"), blob)
    request = build_apply_request(eligible, [source], max_output_tokens=2048)
    try:
        data, _ = call_json(llm, request, CostGuard(config.limits.max_estimated_cost_usd))
        plan = validate_proposal(data, eligible, [source], config)
    except PullBeaconError:
        return True, False, False
    if not plan.edits:
        return True, False, False
    return True, True, plan.edits[0].new_content == expectation.expected_content


def evaluate_model(
    cases: Sequence[EvalCase],
    factory: ClientFactory,
    *,
    config: PullBeaconConfig,
    mode: str,
    total_budget_usd: float,
    spent_usd: float = 0.0,
) -> tuple[ModelSummary, tuple[CaseResult, ...], float]:
    results: list[CaseResult] = []
    provider = model = "unknown"
    for case in cases:
        llm = factory(case)
        provider, model = llm.provider, llm.model
        if spent_usd >= total_budget_usd:
            results.append(
                CaseResult(case_id=case.case_id, kind=case.kind, error="total budget reached")
            )
            continue
        fake = FakeGitHub.from_fixture(case.fixture)
        number = int(case.fixture["pull_request"]["number"])
        with GitHubClient(None, transport=fake.transport(), max_retries=0) as client:
            snapshot = fetch_snapshot(client, fake.owner, fake.repo, number)
        scope = select_scope(snapshot, config)
        start = time.perf_counter()
        try:
            report = ReviewEngine(llm, config).review(snapshot, scope, mode="local")
        except PullBeaconError as exc:
            results.append(
                CaseResult(case_id=case.case_id, kind=case.kind, error=f"{exc.code}: {exc.message}")
            )
            continue
        result = score_case(case, report, latency_s=time.perf_counter() - start)
        spent_usd += result.cost_usd or 0.0
        attempted, valid, exact = _apply_check(case, llm, report, config, fake)
        results.append(
            result.model_copy(
                update={"apply_attempted": attempted, "apply_valid": valid, "apply_exact": exact}
            )
        )
    summary = summarize(results, cases, provider=provider, model=model, mode=mode)
    return summary, tuple(results), spent_usd


def run_evaluation(
    cases: Sequence[EvalCase],
    factories: Sequence[ClientFactory],
    *,
    dataset_sha256: str,
    mode: str,
    total_budget_usd: float = 2.0,
    config: PullBeaconConfig | None = None,
) -> EvalRun:
    config = config or parse_config(EVAL_CONFIG)
    summaries: list[ModelSummary] = []
    per_case: dict[str, tuple[CaseResult, ...]] = {}
    spent = 0.0
    for factory in factories:
        summary, results, spent = evaluate_model(
            cases,
            factory,
            config=config,
            mode=mode,
            total_budget_usd=total_budget_usd,
            spent_usd=spent,
        )
        summaries.append(summary)
        per_case[summary.model] = results
    notes = (
        (
            "MOCK RUN: canned responses. These numbers test the harness, they do NOT measure any "
            "model's review quality.",
        )
        if mode == "mock"
        else (
            "Live run: single sample per case at the configured temperature; small synthetic "
            "dataset, so differences of one or two cases are not statistically meaningful.",
        )
    )
    return EvalRun(
        mode=mode,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        pullbeacon_version=__version__,
        git_commit=git_commit(),
        prompt_versions={"review": REVIEW_PROMPT_VERSION, "apply": APPLY_PROMPT_VERSION},
        dataset_sha256=dataset_sha256,
        config_hash=config.config_hash(),
        temperature=0.0,
        notes=notes,
        summaries=tuple(summaries),
        cases=per_case,
    )
