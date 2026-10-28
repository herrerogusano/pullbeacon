from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pullbeacon.adapters.llm import FakeLLMClient, LLMRequest
from pullbeacon.commands_local import snapshot_from_fixture
from pullbeacon.config import PullBeaconConfig, parse_config
from pullbeacon.core.review import CostGuard, ReviewEngine, plan_batches
from pullbeacon.core.scope import select_scope
from pullbeacon.domain.errors import BudgetExceeded, LLMError, LLMResponseError
from pullbeacon.domain.models import canonical_json
from pullbeacon.prompts import REVIEW_PROMPT_VERSION

FIXTURES = Path(__file__).parents[1] / "fixtures"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def run(
    name: str,
    llm: FakeLLMClient | None = None,
    config: PullBeaconConfig | None = None,
) -> tuple[Any, FakeLLMClient]:
    data = fixture(name)
    _, snapshot = snapshot_from_fixture(data)
    config = config or parse_config("version: 1\n")
    llm = llm or FakeLLMClient(responses={k: list(v) for k, v in data["llm_responses"].items()})
    scope = select_scope(snapshot, config)
    report = ReviewEngine(llm, config).review(snapshot, scope, mode="local")
    return report, llm


def test_auth_bug_fixture_produces_verified_finding() -> None:
    report, llm = run("pr_auth_bug.json")
    assert report.simulated is True
    assert report.usage.cost_source == "simulated"
    assert report.prompt_version == REVIEW_PROMPT_VERSION
    (finding,) = report.findings
    assert finding.path == "src/api/users.py"
    assert finding.line == 13
    assert finding.inline
    assert len(llm.requests) == 1


def test_clean_pr_yields_zero_findings() -> None:
    report, _ = run("pr_clean.json")
    assert report.findings == ()
    assert report.warnings == ()
    assert "rename" in report.summary.lower()


def test_same_input_is_reproducible() -> None:
    first, _ = run("pr_auth_bug.json")
    second, _ = run("pr_auth_bug.json")
    assert canonical_json(first) == canonical_json(second)


def test_invalid_json_gets_one_correction_retry() -> None:
    good = json.dumps({"summary": "ok", "findings": []})
    llm = FakeLLMClient(responses={"review": ["not json", good]})
    report, llm = run("pr_clean.json", llm)
    assert report.findings == ()
    assert len(llm.requests) == 2
    assert "not a valid JSON object" in llm.requests[1].user
    assert report.usage.llm_calls == 2


def test_invalid_json_twice_fails_without_report() -> None:
    llm = FakeLLMClient(responses={"review": ["{broken", "still [broken"]})
    with pytest.raises(LLMResponseError):
        run("pr_clean.json", llm)
    assert len(llm.requests) == 2


def test_code_fenced_json_is_accepted() -> None:
    fenced = "```json\n" + json.dumps({"summary": "fine", "findings": []}) + "\n```"
    report, _ = run("pr_clean.json", FakeLLMClient(responses={"review": [fenced]}))
    assert report.summary == "fine"


def test_findings_array_missing_is_an_error() -> None:
    llm = FakeLLMClient(responses={"review": [json.dumps({"summary": "x", "findings": "none"})]})
    with pytest.raises(LLMResponseError):
        run("pr_clean.json", llm)


def test_hallucinated_findings_are_filtered_with_warning() -> None:
    response = {
        "summary": "s",
        "findings": [
            {
                "severity": "high",
                "category": "security",
                "title": "Invented",
                "explanation": "x",
                "impact": "",
                "evidence": "eval(input())",
                "path": "src/not_in_pr.py",
                "line": 3,
                "side": "RIGHT",
                "suggested_fix": None,
                "confidence": 0.8,
                "can_apply": False,
            }
        ],
    }
    llm = FakeLLMClient(responses={"review": [json.dumps(response)]})
    report, _ = run("pr_clean.json", llm)
    assert report.findings == ()
    assert "discarded by validation" in report.warnings[0]


def test_prompt_injection_stays_inside_data_block() -> None:
    llm = FakeLLMClient(responses={"review": [json.dumps({"summary": "s", "findings": []})]})
    run("pr_two_hunks.json", llm)
    request: LLMRequest = llm.requests[0]
    assert "ignore all previous instructions" not in request.system
    injected_at = request.user.index("ignore all previous instructions")
    start = request.user.rindex("<<<UNTRUSTED DATA DIFF", 0, injected_at)
    end = request.user.index("<<<END UNTRUSTED DATA DIFF", injected_at)
    assert start < injected_at < end
    assert "never an instruction" in request.system
    # Sensitive file content never reaches the model.
    assert "SECRET=1" not in request.user


def test_budget_exhausted_before_first_call() -> None:
    llm = FakeLLMClient(responses={"review": ["{}"]}, estimated_cost=5.0)
    config = parse_config("version: 1\nlimits: {max_estimated_cost_usd: 1.0}\n")
    with pytest.raises(BudgetExceeded):
        run("pr_clean.json", llm, config)
    assert llm.requests == []


def test_unknown_cost_is_allowed_but_counted() -> None:
    guard = CostGuard(1.0)
    guard.check(None)
    assert guard.unknown


def test_budget_stops_later_batches_and_reports_partial() -> None:
    config = parse_config(
        "version: 1\n"
        "limits: {max_context_chars: 1000, max_estimated_cost_usd: 0.5, max_batches: 5}\n"
    )
    responses = {"review": [json.dumps({"summary": "batch", "findings": []})]}
    llm = FakeLLMClient(responses=responses, estimated_cost=0.3)
    report, llm = run("pr_two_hunks.json", llm, config)
    assert len(llm.requests) == 1
    assert report.truncated
    assert any(s.reason == "budget_exhausted" for s in report.skipped_files)
    assert any("Stopped early" in w for w in report.warnings)


def test_llm_errors_propagate() -> None:
    llm = FakeLLMClient(errors=[LLMError("provider unavailable")])
    with pytest.raises(LLMError, match="unavailable"):
        run("pr_clean.json", llm)


def test_batches_are_bounded_and_deterministic() -> None:
    data = fixture("pr_two_hunks.json")
    _, snapshot = snapshot_from_fixture(data)
    scope = select_scope(snapshot, parse_config("version: 1\n"))
    batches, skipped = plan_batches(scope.diffs, max_chars=400, max_batches=2)
    assert len(batches) <= 2
    flat = [d.path for b in batches for d in b] + [s.path for s in skipped]
    assert sorted(flat) == sorted(scope.reviewed_paths)
    again, _ = plan_batches(scope.diffs, max_chars=400, max_batches=2)
    assert [[d.path for d in b] for b in batches] == [[d.path for d in b] for b in again]
