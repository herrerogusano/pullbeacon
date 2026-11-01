"""Run the dataset through the real review pipeline (offline GitHub, mock or live LLM)."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from pullbeacon import __version__
from pullbeacon.adapters.github import GitHubClient, fetch_snapshot
from pullbeacon.adapters.llm import FakeLLMClient, LLMClient, LLMRequest, LLMResponse
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
    repeats: int = 1
    split: str | None = None
    total_spent_usd: float | None = None
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


@dataclass(frozen=True)
class ApplyOutcome:
    attempted: bool = False
    valid: bool = False
    exact: bool = False
    reason: str = ""
    cost_usd: float = 0.0


def _apply_check(
    case: EvalCase, llm: LLMClient, report: Any, config: PullBeaconConfig, fake: FakeGitHub
) -> ApplyOutcome:
    expectation = case.apply_expectation
    if expectation is None:
        return ApplyOutcome()
    marker = marker_from_report(report, trigger="eval")
    eligible, skipped = select_findings(marker.findings, None, config)
    eligible = [f for f in eligible if f.path == expectation.path][:1]
    if not eligible:
        reasons = "; ".join(sorted({r for _, r in skipped})) or "no finding on the expected file"
        return ApplyOutcome(attempted=True, reason=f"not eligible: {reasons}")
    head = case.fixture["pull_request"]["head"]["sha"]
    content = fake.file_at(head, expectation.path)
    if content is None:
        return ApplyOutcome(attempted=True, reason="file missing in fixture")
    blob = next(f["sha"] for f in case.fixture["files"] if f["filename"] == expectation.path)
    source = SourceFile(expectation.path, content.decode("utf-8"), blob)
    request = build_apply_request(eligible, [source], max_output_tokens=2048)
    try:
        data, usage = call_json(llm, request, CostGuard(config.limits.max_estimated_cost_usd))
    except PullBeaconError as exc:
        return ApplyOutcome(attempted=True, reason=f"llm: {exc.message}")
    cost = usage.cost_usd or 0.0
    try:
        plan = validate_proposal(data, eligible, [source], config)
    except PullBeaconError as exc:
        return ApplyOutcome(attempted=True, reason=f"rejected: {exc.message}", cost_usd=cost)
    if not plan.edits:
        return ApplyOutcome(attempted=True, reason="no edit proposed", cost_usd=cost)
    exact = plan.edits[0].new_content == expectation.expected_content
    return ApplyOutcome(
        attempted=True,
        valid=True,
        exact=exact,
        reason="" if exact else "valid edit, differs from the reference fix",
        cost_usd=cost,
    )


class _SpendTracker:
    """LLM client proxy that adds up the cost of every completed call.

    A review can fail after paid calls (e.g. malformed JSON after the retry): the review report
    is lost then, but the money is spent and must still count against the evaluation budget.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.simulated = inner.simulated
        self.spent_usd = 0.0
        self.unknown = False

    def complete(self, request: LLMRequest) -> LLMResponse:
        response = self._inner.complete(request)
        if response.usage.cost_usd is None:
            self.unknown = True
        else:
            self.spent_usd += response.usage.cost_usd
        return response

    def estimate_max_cost(self, request: LLMRequest) -> float | None:
        return self._inner.estimate_max_cost(request)


def _run_one(
    case: EvalCase, llm_client: LLMClient, config: PullBeaconConfig, sample: int
) -> tuple[CaseResult, float]:
    """Review one case once; returns the scored result and the money it spent."""
    llm = _SpendTracker(llm_client)
    fake = FakeGitHub.from_fixture(case.fixture)
    number = int(case.fixture["pull_request"]["number"])
    with GitHubClient(None, transport=fake.transport(), max_retries=0) as client:
        snapshot = fetch_snapshot(client, fake.owner, fake.repo, number)
    scope = select_scope(snapshot, config)
    start = time.perf_counter()
    try:
        report = ReviewEngine(llm, config).review(snapshot, scope, mode="local")
    except PullBeaconError as exc:
        error = CaseResult(
            case_id=case.case_id,
            kind=case.kind,
            sample=sample,
            error=f"{exc.code}: {exc.message}",
            cost_usd=None if llm.unknown else round(llm.spent_usd, 6),
        )
        return error, llm.spent_usd
    result = score_case(case, report, latency_s=time.perf_counter() - start, sample=sample)
    outcome = _apply_check(case, llm, report, config, fake) if sample == 0 else ApplyOutcome()
    spent = llm.spent_usd
    return (
        result.model_copy(
            update={
                "apply_attempted": outcome.attempted,
                "apply_valid": outcome.valid,
                "apply_exact": outcome.exact,
                "apply_reason": outcome.reason,
            }
        ),
        spent,
    )


def evaluate_model(
    cases: Sequence[EvalCase],
    factory: ClientFactory,
    *,
    config: PullBeaconConfig,
    mode: str,
    total_budget_usd: float,
    spent_usd: float = 0.0,
    repeats: int = 1,
    workers: int = 1,
) -> tuple[ModelSummary, tuple[CaseResult, ...], float]:
    """Run every case ``repeats`` times. The apply check runs on the first sample only.

    With ``workers > 1`` reviews run in threads; the budget is checked before each review
    starts, so a run may exceed it by at most ``workers`` reviews already in flight.
    """
    jobs = [(case, sample) for sample in range(repeats) for case in cases]
    first = factory(cases[0]) if cases else None
    provider, model = (first.provider, first.model) if first else ("unknown", "unknown")
    lock = threading.Lock()
    spent = [spent_usd]

    def job(item: tuple[EvalCase, int]) -> CaseResult:
        case, sample = item
        with lock:
            over = spent[0] >= total_budget_usd
        if over:
            return CaseResult(
                case_id=case.case_id, kind=case.kind, sample=sample, error="total budget reached"
            )
        result, cost = _run_one(case, factory(case), config, sample)
        with lock:
            spent[0] += cost
        return result

    if workers <= 1:
        results = [job(item) for item in jobs]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(job, jobs))
    summary = summarize(results, cases, provider=provider, model=model, mode=mode)
    return summary, tuple(results), spent[0]


def run_evaluation(
    cases: Sequence[EvalCase],
    factories: Sequence[ClientFactory],
    *,
    dataset_sha256: str,
    mode: str,
    total_budget_usd: float = 2.0,
    config: PullBeaconConfig | None = None,
    repeats: int = 1,
    workers: int = 1,
    split: str | None = None,
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
            repeats=repeats,
            workers=workers,
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
            f"Live run: {repeats} sample(s) per case at the configured temperature. Small "
            "datasets: differences of one or two cases are not statistically meaningful.",
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
        repeats=repeats,
        split=split,
        total_spent_usd=round(spent, 6),
        summaries=tuple(summaries),
        cases=per_case,
    )
