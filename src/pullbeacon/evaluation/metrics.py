"""Matching findings to ground truth and computing metrics.

Matching rule (documented and deterministic, never model-graded):
- A finding is a true positive for a ground-truth issue when it is posted inline on the same
  path, its line is within ``LINE_TOLERANCE`` of a labelled line, and its category is one of the
  issue's accepted categories. Each issue is matched at most once, most severe findings first.
- Findings without a valid diff line (non-inline) never count as true positives; they count as
  false positives and are reported separately as ``unlocated``.
- Every other finding is a false positive. ``trap_hits`` counts false positives that land on an
  ``expected_non_findings`` location.
Disagreements should be reviewed manually; literal line matching alone is not ground truth.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from pullbeacon.domain.models import ReviewReport
from pullbeacon.evaluation.dataset import EvalCase

LINE_TOLERANCE = 3


class CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    kind: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    findings: int = 0
    unlocated: int = 0
    discarded: int = 0
    duplicates: int = 0
    trap_hits: int = 0
    matched_issue_ids: tuple[str, ...] = ()
    latency_s: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: str = "unknown"
    error: str | None = None
    apply_attempted: bool = False
    apply_valid: bool = False
    apply_exact: bool = False
    apply_reason: str = ""
    # Location and labels only (no evidence/code), for manual review of disagreements.
    finding_summaries: tuple[str, ...] = ()


class ModelSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    mode: str
    cases: int
    positive_cases: int
    clean_cases: int
    errors: int
    tp: int
    fp: int
    fn: int
    recall: float | None
    precision: float | None
    false_positives_per_clean_pr: float | None
    unlocated_findings: int
    discarded_findings: int
    duplicate_findings: int
    trap_hits: int
    latency_mean_s: float | None
    latency_max_s: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    cost_per_review_usd: float | None
    cost_per_true_positive_usd: float | None
    apply_attempted: int
    apply_valid: int
    apply_exact: int


def score_case(case: EvalCase, report: ReviewReport, *, latency_s: float) -> CaseResult:
    ordered = sorted(report.findings, key=lambda f: (f.severity.rank, -f.confidence))
    matched: set[str] = set()
    tp = fp = unlocated = trap_hits = 0
    for finding in ordered:
        if not finding.inline or finding.line is None:
            unlocated += 1
            fp += 1
            continue
        issue = next(
            (
                g
                for g in case.ground_truth
                if g.id not in matched
                and g.path == finding.path
                and any(abs(finding.line - line) <= LINE_TOLERANCE for line in g.lines)
                and finding.category in g.accepted_categories
            ),
            None,
        )
        if issue is not None:
            matched.add(issue.id)
            tp += 1
            continue
        fp += 1
        if any(
            t.path == finding.path
            and any(abs(finding.line - line) <= LINE_TOLERANCE for line in t.lines)
            for t in case.expected_non_findings
        ):
            trap_hits += 1
    return CaseResult(
        case_id=case.case_id,
        kind=case.kind,
        tp=tp,
        fp=fp,
        fn=len(case.ground_truth) - len(matched),
        findings=len(report.findings),
        unlocated=unlocated,
        discarded=len(report.discarded),
        duplicates=sum(1 for d in report.discarded if "duplicate" in d),
        trap_hits=trap_hits,
        matched_issue_ids=tuple(sorted(matched)),
        finding_summaries=tuple(
            f"{f.id} {f.severity.value}/{f.category.value} {f.path}:{f.line or '-'} {f.title[:120]}"
            for f in report.findings
        ),
        latency_s=round(latency_s, 3),
        input_tokens=report.usage.input_tokens,
        output_tokens=report.usage.output_tokens,
        cost_usd=report.usage.cost_usd,
        cost_source=report.usage.cost_source,
    )


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def summarize(
    results: Sequence[CaseResult],
    cases: Sequence[EvalCase],
    *,
    provider: str,
    model: str,
    mode: str,
) -> ModelSummary:
    by_id = {c.case_id: c for c in cases}
    ok = [r for r in results if r.error is None]
    tp, fp, fn = (sum(getattr(r, k) for r in ok) for k in ("tp", "fp", "fn"))
    clean = [r for r in ok if not by_id[r.case_id].positive]
    latencies = [r.latency_s for r in ok]
    costs = [r.cost_usd for r in ok]
    cost = round(sum(c for c in costs if c is not None), 6) if ok and None not in costs else None
    tokens_in = [r.input_tokens for r in ok]
    tokens_out = [r.output_tokens for r in ok]
    return ModelSummary(
        provider=provider,
        model=model,
        mode=mode,
        cases=len(results),
        positive_cases=sum(1 for r in results if by_id[r.case_id].positive),
        clean_cases=sum(1 for r in results if not by_id[r.case_id].positive),
        errors=len(results) - len(ok),
        tp=tp,
        fp=fp,
        fn=fn,
        recall=_ratio(tp, tp + fn),
        precision=_ratio(tp, tp + fp),
        false_positives_per_clean_pr=_ratio(sum(r.fp for r in clean), len(clean)),
        unlocated_findings=sum(r.unlocated for r in ok),
        discarded_findings=sum(r.discarded for r in ok),
        duplicate_findings=sum(r.duplicates for r in ok),
        trap_hits=sum(r.trap_hits for r in ok),
        latency_mean_s=round(statistics.fmean(latencies), 3) if latencies else None,
        latency_max_s=max(latencies) if latencies else None,
        input_tokens=sum(t for t in tokens_in if t is not None) if None not in tokens_in else None,
        output_tokens=sum(t for t in tokens_out if t is not None)
        if None not in tokens_out
        else None,
        cost_usd=cost,
        cost_per_review_usd=_ratio(cost, len(ok)) if cost is not None else None,
        cost_per_true_positive_usd=_ratio(cost, tp) if cost is not None else None,
        apply_attempted=sum(r.apply_attempted for r in ok),
        apply_valid=sum(r.apply_valid for r in ok),
        apply_exact=sum(r.apply_exact for r in ok),
    )
