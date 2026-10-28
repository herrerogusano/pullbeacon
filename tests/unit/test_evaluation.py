from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pullbeacon.cli import main
from pullbeacon.domain.errors import ConfigError
from pullbeacon.domain.models import Finding, ReviewReport
from pullbeacon.evaluation.dataset import EvalCase, load_cases
from pullbeacon.evaluation.metrics import score_case
from pullbeacon.evaluation.report import to_csv, to_markdown
from pullbeacon.evaluation.runner import mock_factory, run_evaluation

DATASET = Path(__file__).parents[2] / "evaluation" / "cases"


@pytest.fixture(scope="module")
def cases() -> list[EvalCase]:
    return load_cases(DATASET)[0]


def test_dataset_loads_with_valid_labels(cases: list[EvalCase]) -> None:
    assert 12 <= len(cases) <= 20
    kinds = {c.kind for c in cases}
    assert kinds == {"bug", "clean", "injection"}
    assert sum(1 for c in cases if c.kind == "clean") >= 4
    for case in cases:
        if case.positive:
            assert case.ground_truth, case.case_id
        else:
            assert not case.ground_truth
            assert case.expected_non_findings, case.case_id
    languages = {c.language for c in cases}
    assert {"python", "typescript", "javascript"} <= languages


def test_dataset_hash_is_stable() -> None:
    assert load_cases(DATASET)[1] == load_cases(DATASET)[1]


def test_invalid_labels_are_rejected(cases: list[EvalCase], tmp_path: Path) -> None:
    data = json.loads(next(c for c in cases if c.positive).model_dump_json())
    data["ground_truth"][0]["lines"] = [9999]
    (tmp_path / "bad.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="not in the diff"):
        load_cases(tmp_path)
    data["ground_truth"] = []
    (tmp_path / "bad.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="at least one ground-truth"):
        load_cases(tmp_path)


def test_mock_run_metrics_are_exact(cases: list[EvalCase]) -> None:
    run = run_evaluation(cases, [mock_factory], dataset_sha256="x", mode="mock")
    (summary,) = run.summaries
    # 12 labelled issues; the mock misses one (N+1), adds a trap FP on a clean PR and an
    # unlocated finding, and emits one duplicate that validation discards.
    assert (summary.tp, summary.fp, summary.fn) == (11, 2, 1)
    assert summary.recall == pytest.approx(11 / 12, abs=1e-4)
    assert summary.precision == pytest.approx(11 / 13, abs=1e-4)
    assert summary.false_positives_per_clean_pr == pytest.approx(0.25)
    assert summary.unlocated_findings == 1
    assert summary.duplicate_findings == 1
    assert summary.trap_hits == 1
    assert summary.errors == 0
    assert (summary.apply_attempted, summary.apply_valid, summary.apply_exact) == (2, 2, 2)
    assert summary.cost_usd == 0.0
    assert "MOCK RUN" in run.notes[0]
    by_case = {r.case_id: r for r in run.cases["mock/deterministic"]}
    assert by_case["py_n_plus_one"].fn == 1
    assert by_case["clean_extract_function"].fp == 1
    assert by_case["injection_sql"].tp == 1


def _report(findings: list[Finding]) -> ReviewReport:
    return ReviewReport(
        owner="o",
        repo="r",
        number=1,
        head_sha="a" * 40,
        base_sha="b" * 40,
        provider="p",
        model="m",
        prompt_version="v",
        config_hash="c",
        mode="local",
        simulated=True,
        findings=tuple(findings),
    )


def _finding(**overrides: Any) -> Finding:
    data: dict[str, Any] = {
        "id": "PB-001",
        "fingerprint": "f" * 16,
        "severity": "high",
        "category": "security",
        "title": "t",
        "explanation": "e",
        "evidence": "x",
        "path": "app/invoices.py",
        "line": 12,
        "side": "RIGHT",
        "confidence": 0.9,
    }
    data.update(overrides)
    return Finding.model_validate(data)


def test_scoring_rules(cases: list[EvalCase]) -> None:
    case = next(c for c in cases if c.case_id == "py_missing_authz")
    line = case.ground_truth[0].lines[0]
    hit = score_case(case, _report([_finding(line=line + 3)]), latency_s=0.1)
    assert (hit.tp, hit.fp, hit.fn) == (1, 0, 0)
    far = score_case(case, _report([_finding(line=line + 4)]), latency_s=0.1)
    assert (far.tp, far.fp, far.fn) == (0, 1, 1)
    wrong_category = score_case(
        case, _report([_finding(line=line, category="performance")]), latency_s=0
    )
    assert (wrong_category.tp, wrong_category.fn) == (0, 1)
    unlocated = score_case(
        case, _report([_finding(line=None, side=None, inline=False)]), latency_s=0
    )
    assert (unlocated.tp, unlocated.fp, unlocated.unlocated) == (0, 1, 1)
    twice = score_case(
        case, _report([_finding(line=line), _finding(line=line, fingerprint="e" * 16)]), latency_s=0
    )
    assert (twice.tp, twice.fp) == (1, 1)


def test_clean_case_false_positive_and_trap(cases: list[EvalCase]) -> None:
    case = next(c for c in cases if c.case_id == "clean_rename")
    trap = case.expected_non_findings[0]
    result = score_case(case, _report([_finding(path=trap.path, line=trap.lines[0])]), latency_s=0)
    assert (result.tp, result.fp, result.fn, result.trap_hits) == (0, 1, 0, 1)


def test_reports_render(cases: list[EvalCase]) -> None:
    run = run_evaluation(cases[:3], [mock_factory], dataset_sha256="abc", mode="mock")
    markdown = to_markdown(run)
    assert "MOCK RUN" in markdown
    assert "| `mock/deterministic` |" in markdown
    rows = to_csv(run).splitlines()
    assert rows[0].startswith("model,case_id")
    assert len(rows) == 4


def test_cli_mock_eval_without_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "mock.json"
    assert main(["eval", "--dataset", str(DATASET), "--mock-llm", "--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mode"] == "mock"
    assert out.with_suffix(".md").exists()
    assert out.with_suffix(".csv").exists()
    datetime.fromisoformat(data["started_at"]).astimezone(UTC)


def test_live_eval_requires_confirmation_and_never_runs_without_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import litellm

    def forbidden(**_kwargs: object) -> None:
        raise AssertionError("a provider was called")

    monkeypatch.setattr(litellm, "completion", forbidden)
    args = [
        "eval",
        "--dataset",
        str(DATASET),
        "--provider",
        "live",
        "--output",
        str(tmp_path / "x.json"),
    ]
    assert main([*args, "--models", "anthropic/claude-haiku-5-5"]) == 2
    assert "--confirm-spend" in capsys.readouterr().err
    assert main(args) == 2
    # With confirmation but without a key: clear credential error, still no call.
    assert main([*args, "--models", "anthropic/claude-haiku-5-5", "--confirm-spend"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "x.json").exists()
