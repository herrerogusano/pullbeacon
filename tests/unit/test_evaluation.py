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


def test_case_results_keep_finding_summaries_and_apply_reasons(cases: list[EvalCase]) -> None:
    run = run_evaluation(cases, [mock_factory], dataset_sha256="x", mode="mock")
    by_case = {r.case_id: r for r in run.cases["mock/deterministic"]}
    summary = by_case["py_missing_authz"].finding_summaries[0]
    assert summary.startswith("[TP:missing-owner-check] PB-001 critical/security app/invoices.py:")
    assert by_case["py_unbounded_limit"].apply_valid
    assert by_case["py_missing_authz"].apply_reason == ""


def test_acceptable_findings_are_neither_tp_nor_fp(cases: list[EvalCase]) -> None:
    case = next(c for c in cases if c.case_id == "py_missing_authz")
    issue = case.ground_truth[0]
    data = json.loads(case.model_dump_json())
    data["acceptable_findings"] = [
        {"path": issue.path, "lines": list(issue.lines), "rationale": "secondary issue"}
    ]
    with_acceptable = EvalCase.model_validate(data)
    report = _report([_finding(line=issue.lines[0], category="performance")])
    result = score_case(with_acceptable, report, latency_s=0)
    assert (result.tp, result.fp, result.fn, result.acceptable) == (0, 0, 1, 1)
    assert result.finding_summaries[0].startswith("[ACCEPTABLE]")


def test_split_filter_and_multiple_directories(cases: list[EvalCase], tmp_path: Path) -> None:
    dev, test = tmp_path / "dev", tmp_path / "test"
    dev.mkdir()
    test.mkdir()
    for directory, split, case in ((dev, "dev", cases[0]), (test, "test", cases[1])):
        data = json.loads(case.model_dump_json())
        data.update(split=split, tags=["logic"])
        (directory / f"{case.case_id}.json").write_text(json.dumps(data), encoding="utf-8")
    both, _ = load_cases([dev, test])
    assert [c.case_id for c in both] == [cases[0].case_id, cases[1].case_id]
    only_test, digest_test = load_cases([dev, test], split="test")
    assert [c.case_id for c in only_test] == [cases[1].case_id]
    assert digest_test != load_cases([dev, test])[1]
    with pytest.raises(ConfigError, match="no evaluation cases in split"):
        load_cases(dev, split="test")


def test_repeats_and_workers_aggregate_samples(cases: list[EvalCase]) -> None:
    single = run_evaluation(cases, [mock_factory], dataset_sha256="x", mode="mock")
    repeated = run_evaluation(
        cases, [mock_factory], dataset_sha256="x", mode="mock", repeats=2, workers=4
    )
    (one,), (two,) = single.summaries, repeated.summaries
    assert two.samples == 2
    assert repeated.repeats == 2
    assert two.cases == one.cases == len(cases)
    assert (two.tp, two.fp, two.fn) == (2 * one.tp, 2 * one.fp, 2 * one.fn)
    assert two.recall == one.recall
    # Deterministic mock: every outcome identical in both samples.
    assert two.consistency == 1.0
    # The apply check only runs on the first sample.
    assert two.apply_attempted == one.apply_attempted
    results = repeated.cases["mock/deterministic"]
    assert sorted({r.sample for r in results}) == [0, 1]
    assert one.recall_severe is not None
    assert one.injection_cases == 1
    assert one.injection_missed == 0


def test_cli_repeats_split_and_limits(tmp_path: Path) -> None:
    out = tmp_path / "mock.json"
    args = ["eval", "--dataset", str(DATASET), "--mock-llm", "--output", str(out)]
    assert main([*args, "--repeats", "2", "--workers", "2"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["repeats"] == 2
    assert main([*args, "--repeats", "9"]) == 2
    assert main([*args, "--workers", "0"]) == 2
    # v1 cases have no split: selecting one is a clear error.
    assert main([*args, "--split", "dev"]) == 2


DATASET_V2 = Path(__file__).parents[2] / "evaluation" / "cases-v2"


def test_v2_dataset_loads_with_splits_and_authors() -> None:
    cases_v2, _ = load_cases(DATASET_V2)
    assert len(cases_v2) >= 30
    assert {c.split for c in cases_v2} == {"dev", "test"}
    assert {c.kind for c in cases_v2} == {"bug", "clean", "injection"}
    assert {c.author for c in cases_v2} >= {"claude", "codex"}
    assert all(c.tags for c in cases_v2)
    dev, _ = load_cases(DATASET_V2, split="dev")
    test, _ = load_cases(DATASET_V2, split="test")
    assert len(dev) + len(test) == len(cases_v2)


def test_v3_holdout_dataset_loads() -> None:
    cases_v3, _ = load_cases(Path(__file__).parents[2] / "evaluation" / "cases-v3")
    assert len(cases_v3) >= 18  # synthetic x3_* cases; the 28 OSS cases are fetched
    assert {c.split for c in cases_v3} == {"test"}
    assert {c.kind for c in cases_v3} == {"bug", "clean", "injection"}
    assert {c.author for c in cases_v3} == {"codex"}
    assert any(c.size == "large" for c in cases_v3)


def test_v2_specs_build_reproducibly() -> None:
    import subprocess
    import sys

    root = Path(__file__).parents[2]
    out = subprocess.run(  # noqa: S603
        [sys.executable, str(root / "evaluation" / "build_cases_v2.py"), "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "0 error(s)" in out.stdout


def test_failed_review_still_counts_its_spend(cases: list[EvalCase]) -> None:
    """A review that fails after paid calls must still consume the evaluation budget."""
    from pullbeacon.adapters.llm import FakeLLMClient
    from pullbeacon.domain.models import Usage

    class PaidGarbage(FakeLLMClient):
        def complete(self, request: Any) -> Any:
            response = super().complete(request)
            return type(response)(
                text="not json", usage=Usage(cost_usd=0.4, cost_source="litellm"), model=self.model
            )

    def factory(_case: EvalCase) -> Any:
        return PaidGarbage(responses={"review": ["x", "x", "x"]})

    run = run_evaluation(
        cases[:3], [factory], dataset_sha256="x", mode="mock", total_budget_usd=1.0
    )
    results = next(iter(run.cases.values()))
    assert results[0].error is not None
    assert results[0].cost_usd == pytest.approx(0.8)  # first call + one retry for malformed JSON
    assert results[1].error is not None
    assert results[2].error == "total budget reached"
    assert run.total_spent_usd == pytest.approx(1.6)
