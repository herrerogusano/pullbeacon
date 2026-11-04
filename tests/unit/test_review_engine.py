from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pullbeacon.adapters.llm import FakeLLMClient, LLMRequest
from pullbeacon.commands_local import snapshot_from_fixture
from pullbeacon.config import PullBeaconConfig, parse_config
from pullbeacon.core.review import CostGuard, ReviewEngine, call_json, plan_batches
from pullbeacon.core.scope import select_scope
from pullbeacon.domain.errors import (
    BudgetExceeded,
    LLMError,
    LLMOutputTruncated,
    LLMResponseError,
)
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


GOOD = json.dumps({"summary": "ok", "findings": []})
CUT_OFF = '{"summary": "ok", "findings": [{"title": "unterminated'


def test_truncated_output_is_a_clear_error_and_is_not_retried() -> None:
    llm = FakeLLMClient(responses={"review": [CUT_OFF, GOOD]}, finish_reasons=["length"])
    with pytest.raises(LLMOutputTruncated) as info:
        run("pr_clean.json", llm)
    assert len(llm.requests) == 1  # the "invalid JSON" retry would hit the same limit
    assert info.value.code == "llm_output_truncated"
    message = info.value.message
    assert "output limit (4096 tokens)" in message
    assert "limits.max_output_tokens" in message
    assert "limits.max_context_chars" in message
    assert isinstance(info.value, LLMResponseError)  # still an LLMError for existing handlers


def test_truncated_output_is_never_parsed_even_if_it_looks_complete() -> None:
    llm = FakeLLMClient(responses={"review": [GOOD]}, finish_reasons=["length"])
    with pytest.raises(LLMOutputTruncated):
        run("pr_clean.json", llm)
    assert len(llm.requests) == 1


def test_truncated_empty_answer_is_reported_as_truncation_not_as_empty() -> None:
    # Reasoning/tool-call tokens can use the whole budget and leave no text at all.
    llm = FakeLLMClient(responses={"review": [""]}, finish_reasons=["length"])
    with pytest.raises(LLMOutputTruncated):
        run("pr_clean.json", llm)
    assert len(llm.requests) == 1


def test_truncation_on_the_correction_attempt_is_still_truncation() -> None:
    # The observed failure: attempt 1 unusable, attempt 2 cut off at the limit.
    llm = FakeLLMClient(
        responses={"review": ["{broken", CUT_OFF]}, finish_reasons=["stop", "length"]
    )
    with pytest.raises(LLMOutputTruncated):
        run("pr_clean.json", llm)
    assert len(llm.requests) == 2


def test_truncated_call_still_counts_against_the_budget() -> None:
    llm = FakeLLMClient(responses={"review": [CUT_OFF]}, finish_reasons=["length"])
    llm.estimated_cost = 0.25
    guard = CostGuard(1.0)
    request = LLMRequest(purpose="review", system="s", user="u", max_output_tokens=512)
    with pytest.raises(LLMOutputTruncated):
        call_json(llm, request, guard)
    assert guard.spent_usd == pytest.approx(0.25)


def test_truncation_message_for_non_review_purposes_only_mentions_the_output_limit() -> None:
    llm = FakeLLMClient(responses={"apply": [CUT_OFF]}, finish_reasons=["length"])
    request = LLMRequest(purpose="apply", system="s", user="u", max_output_tokens=2048)
    with pytest.raises(LLMOutputTruncated) as info:
        call_json(llm, request, CostGuard(1.0))
    assert "limits.max_output_tokens" in info.value.message
    assert "max_context_chars" not in info.value.message


def test_empty_answer_gets_one_retry_and_recovers() -> None:
    llm = FakeLLMClient(responses={"review": ["  ", GOOD]})
    report, llm = run("pr_clean.json", llm)
    assert report.findings == ()
    assert len(llm.requests) == 2
    assert "previous answer was empty" in llm.requests[1].user
    assert report.usage.llm_calls == 2


def test_empty_answer_twice_fails_with_a_specific_message() -> None:
    llm = FakeLLMClient(responses={"review": ["", ""]})
    with pytest.raises(LLMResponseError, match="empty response after one correction") as info:
        run("pr_clean.json", llm)
    assert not isinstance(info.value, LLMOutputTruncated)
    assert len(llm.requests) == 2


def test_content_filter_is_not_retried() -> None:
    llm = FakeLLMClient(responses={"review": ["", GOOD]}, finish_reasons=["content_filter"])
    with pytest.raises(LLMResponseError, match="content filter or refusal"):
        run("pr_clean.json", llm)
    assert len(llm.requests) == 1


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


def _file(path: str, lines: int = 3) -> dict[str, Any]:
    body = "\n".join(f"+line {n} of {path}" for n in range(1, lines + 1))
    return {
        "filename": path,
        "status": "added",
        "additions": lines,
        "deletions": 0,
        "changes": lines,
        "patch": f"@@ -0,0 +1,{lines} @@\n{body}",
    }


def _multi_file_run(files: list[dict[str, Any]], limits: str) -> tuple[Any, list[LLMRequest]]:
    data = fixture("pr_clean.json")
    data["files"] = files
    _, snapshot = snapshot_from_fixture(data)
    config = parse_config(f"version: 1\nlimits: {limits}\n")
    answers = [json.dumps({"summary": "part", "findings": []})] * 20
    llm = FakeLLMClient(responses={"review": answers})
    scope = select_scope(snapshot, config)
    report = ReviewEngine(llm, config).review(snapshot, scope, mode="local")
    return report, llm.requests


def _listed(request: LLMRequest) -> str:
    start = request.user.index("<<<UNTRUSTED DATA FILE_LIST")
    return request.user[start : request.user.index("<<<END UNTRUSTED DATA FILE_LIST")]


def test_each_batch_knows_the_files_of_the_other_batches() -> None:
    files = [_file("src/app.py", 40), _file("tests/test_app.py", 40)]
    _, requests = _multi_file_run(files, "{max_context_chars: 1900, max_batches: 4}")
    first, second = requests
    assert "src/app.py (shown to you)" in _listed(first)
    assert "tests/test_app.py (in another part of this review)" in _listed(first)
    assert "src/app.py (in another part of this review)" in _listed(second)
    assert "tests/test_app.py (shown to you)" in _listed(second)
    assert "line 1 of tests/test_app.py" not in first.user


def test_files_outside_the_policy_are_listed_with_their_reason() -> None:
    files = [_file("src/app.py"), _file("assets/logo.png")]
    _, requests = _multi_file_run(files, "{max_context_chars: 80000}")
    (request,) = requests
    assert "src/app.py (shown to you)" in _listed(request)
    assert "assets/logo.png (not reviewed: " in _listed(request)
    assert "line 1 of assets/logo.png" not in request.user


def test_file_skipped_by_the_batch_limit_is_listed_as_not_reviewed() -> None:
    files = [_file("a.py", 40), _file("b.py", 40), _file("c.py", 40)]
    report, requests = _multi_file_run(files, "{max_context_chars: 1900, max_batches: 2}")
    assert report.truncated
    assert "c.py (not reviewed: limit max context chars)" in _listed(requests[0])


def test_no_file_list_when_the_batch_sees_the_whole_pull_request() -> None:
    _, requests = _multi_file_run([_file("a.py"), _file("b.py")], "{max_context_chars: 80000}")
    (request,) = requests
    assert "FILE_LIST" not in request.user


def test_file_list_counts_against_the_context_budget() -> None:
    files = [_file(f"pkg/module_{n:02d}.py", 10) for n in range(20)]
    report, requests = _multi_file_run(files, "{max_context_chars: 4000, max_batches: 20}")
    assert len(requests) > 1
    assert not report.truncated
    for request in requests:
        diff = request.user[request.user.index("<<<UNTRUSTED DATA DIFF") :]
        listed = _listed(request)
        assert len(diff) + len(listed) <= 4000


def test_large_new_file_is_split_and_every_line_is_sent() -> None:
    report, requests = _multi_file_run([_file("big.py", 200)], "{max_context_chars: 3000}")
    assert len(requests) > 1
    assert not report.truncated
    assert report.reviewed_files == ("big.py",)
    assert report.skipped_files == ()
    sent = "\n".join(request.user for request in requests)
    for number in range(1, 201):
        assert f"| R{number} + line {number} of big.py" in sent
    assert f"FILE big.py [added] (part 1 of {len(requests)})" in requests[0].user
    assert f"big.py (part 2 of {len(requests)} shown to you)" in _listed(requests[1])


def test_each_part_fits_the_budget_and_parts_overlap() -> None:
    _, requests = _multi_file_run([_file("big.py", 200)], "{max_context_chars: 3000}")
    first, second = (r.user[r.user.index("<<<UNTRUSTED DATA DIFF") :] for r in requests[:2])
    assert all(len(r.user[r.user.index("<<<UNTRUSTED DATA DIFF") :]) <= 3100 for r in requests)
    shared = set(first.splitlines()) & set(second.splitlines())
    assert 1 <= len([line for line in shared if line.startswith("| R")]) <= 10


def test_finding_in_a_later_part_is_validated_against_the_whole_file() -> None:
    data = fixture("pr_clean.json")
    data["files"] = [_file("big.py", 200)]
    _, snapshot = snapshot_from_fixture(data)
    config = parse_config("version: 1\nlimits: {max_context_chars: 3000}\n")
    clean = json.dumps({"summary": "part", "findings": []})
    finding = {
        "severity": "high",
        "category": "correctness",
        "title": "Bug near the end",
        "explanation": "Wrong.",
        "impact": "i",
        "evidence": "line 190 of big.py",
        "path": "big.py",
        "line": 190,
        "side": "RIGHT",
        "suggested_fix": None,
        "confidence": 0.9,
        "can_apply": False,
    }
    # The same finding from two parts that share the line is reported once.
    with_finding = json.dumps({"summary": "part", "findings": [finding]})
    scope = select_scope(snapshot, config)
    calls = len(plan_batches(scope.diffs, max_chars=3000, max_batches=4)[0])
    answers = [clean] * (calls - 2) + [with_finding, with_finding]
    llm = FakeLLMClient(responses={"review": answers})
    report = ReviewEngine(llm, config).review(snapshot, scope, mode="local")
    (found,) = report.findings
    assert found.line == 190
    assert found.inline


def test_parts_beyond_the_batch_limit_mark_the_review_partial() -> None:
    report, requests = _multi_file_run(
        [_file("big.py", 400)], "{max_context_chars: 3000, max_batches: 2}"
    )
    assert len(requests) == 2
    assert report.truncated
    assert report.reviewed_files == ("big.py",)
    assert [s.path for s in report.skipped_files] == ["big.py"]
    assert "shown to you; some parts not reviewed" in _listed(requests[0])


def test_a_single_line_too_long_for_any_batch_is_skipped_not_sent() -> None:
    long_line = "x" * 5000
    file = _file("min.js", 1)
    file["patch"] = f"@@ -0,0 +1,2 @@\n+short line\n+{long_line}"
    report, requests = _multi_file_run([file], "{max_context_chars: 3000}")
    assert report.truncated
    assert all(long_line not in request.user for request in requests)
    assert "short line" in requests[0].user


def test_other_files_of_a_split_file_batch_see_it_as_elsewhere() -> None:
    files = [_file("a_small.py", 3), _file("big.py", 200), _file("z_small.py", 3)]
    _, requests = _multi_file_run(files, "{max_context_chars: 3000, max_batches: 10}")
    only_first = requests[0]
    assert "a_small.py (shown to you)" in _listed(only_first)
    last = requests[-1]
    assert "z_small.py (shown to you)" in _listed(last)
    assert "a_small.py (in another part of this review)" in _listed(last)


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
    # A split file can be in several batches, or in a batch and in the skipped list.
    assert set(flat) == set(scope.reviewed_paths)
    again, _ = plan_batches(scope.diffs, max_chars=400, max_batches=2)
    assert [[d.path for d in b] for b in batches] == [[d.path for d in b] for b in again]
