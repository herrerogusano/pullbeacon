from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from pullbeacon.domain.models import (
    Category,
    Finding,
    PullRequestSnapshot,
    ReviewReport,
    RunResult,
    RunStatus,
    Severity,
    Side,
    Usage,
    canonical_json,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def make_finding(**overrides: object) -> Finding:
    data: dict[str, object] = {
        "id": "PB-001",
        "fingerprint": "0123456789abcdef",
        "severity": "high",
        "category": "security",
        "title": "Missing authorization check",
        "explanation": "The endpoint returns data for any user id.",
        "impact": "Data leak",
        "evidence": "return db.get(user_id)",
        "path": "src/app.py",
        "line": 12,
        "side": "RIGHT",
        "confidence": 0.9,
    }
    data.update(overrides)
    return Finding.model_validate(data)


def test_finding_round_trip() -> None:
    finding = make_finding()
    again = Finding.model_validate_json(finding.model_dump_json())
    assert again == finding
    assert again.severity is Severity.HIGH
    assert again.side is Side.RIGHT
    assert again.category is Category.SECURITY


@pytest.mark.parametrize(
    "overrides",
    [
        {"severity": "blocker"},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"line": 0},
        {"id": "X-1"},
        {"evidence": "   "},
        {"side": "MIDDLE"},
        {"unexpected": True},
    ],
)
def test_finding_rejects_invalid(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_finding(**overrides)


def test_snapshot_sha_validation_and_same_repo() -> None:
    base = {
        "owner": "acme",
        "repo": "shop",
        "number": 7,
        "author": "dev",
        "state": "open",
        "base_ref": "main",
        "base_sha": SHA_A,
        "head_ref": "feature",
        "head_sha": SHA_B,
        "base_repo_full_name": "acme/shop",
        "head_repo_full_name": "ACME/shop",
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    snapshot = PullRequestSnapshot.model_validate(base)
    assert snapshot.is_same_repo
    fork = PullRequestSnapshot.model_validate({**base, "head_repo_full_name": "fork/shop"})
    assert not fork.is_same_repo
    deleted = PullRequestSnapshot.model_validate({**base, "head_repo_full_name": None})
    assert not deleted.is_same_repo
    with pytest.raises(ValidationError):
        PullRequestSnapshot.model_validate({**base, "head_sha": "main"})


def test_report_canonical_json_is_deterministic() -> None:
    report = ReviewReport(
        owner="acme",
        repo="shop",
        number=1,
        head_sha=SHA_B,
        base_sha=SHA_A,
        provider="mock",
        model="mock/x",
        prompt_version="v1",
        config_hash="abc",
        mode="local",
        simulated=True,
        findings=(make_finding(),),
    )
    first = canonical_json(report)
    assert first == canonical_json(ReviewReport.model_validate_json(first))
    assert list(json.loads(first)) == sorted(json.loads(first))


def test_usage_aggregation() -> None:
    a = Usage(llm_calls=1, input_tokens=10, output_tokens=5, cost_usd=0.01, cost_source="litellm")
    b = Usage(llm_calls=1, input_tokens=20, output_tokens=5, cost_usd=0.02, cost_source="litellm")
    total = a.plus(b)
    assert total.llm_calls == 2
    assert total.input_tokens == 30
    assert total.cost_usd == pytest.approx(0.03)
    unknown = a.plus(Usage(llm_calls=1, input_tokens=1, output_tokens=1))
    assert unknown.cost_usd is None
    assert unknown.cost_source == "unknown"
    assert Usage().plus(a) == a


def test_run_result_status_values() -> None:
    assert {s.value for s in RunStatus} == {"completed", "skipped", "partial", "failed"}
    result = RunResult(status=RunStatus.SKIPPED, reason="fork")
    assert json.loads(canonical_json(result))["status"] == "skipped"
