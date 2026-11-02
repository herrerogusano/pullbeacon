"""Matching findings to ground truth and computing metrics.

Matching rule (documented and deterministic, never model-graded):
- A finding is a true positive for a ground-truth issue when it is posted inline on the same
  path, its line is within ``LINE_TOLERANCE`` of a labelled line, and its category is one of the
  issue's accepted categories. Each issue is matched at most once, most severe findings first.
- Findings without a valid diff line (non-inline) never count as true positives; they count as
  false positives and are reported separately as ``unlocated``.
- A finding that is not a true positive but lands on an ``acceptable_findings`` location (a real,
  secondary issue) is an *acceptable extra*: neither a true nor a false positive.
- Every other finding is a false positive. ``trap_hits`` counts false positives that land on an
  ``expected_non_findings`` location.
- Findings below ``min_inline_severity`` are not posted inline (they are listed collapsed in the
  summary), so they are scored as ``collapsed``: neither true nor false positives.
Disagreements should be reviewed manually; literal line matching alone is not ground truth.

With repeated samples (``--repeats``), totals add up every sample, so recall and precision are
averages over samples. ``consistency`` is the share of (case, issue) pairs, plus one
"any false positive" flag per case, whose outcome was identical in every sample.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from pullbeacon.core.findings import split_by_severity
from pullbeacon.domain.models import Finding, ReviewReport, Severity
from pullbeacon.evaluation.dataset import EvalCase

LINE_TOLERANCE = 3
SEVERE = frozenset({"critical", "high"})


class CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    kind: str
    sample: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    acceptable: int = 0
    # Findings below the configured min_inline_severity: listed collapsed in the summary, so
    # they are neither TP nor FP (their would-be verdict is kept in finding_summaries).
    collapsed: int = 0
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
    # Verdict, location and labels only (no evidence/code), for manual review of disagreements.
    finding_summaries: tuple[str, ...] = ()


class TagSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    tag: str
    cases: int
    tp: int
    fn: int
    fp: int
    recall: float | None


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
    # Benchmark v2 additions (defaults keep older result files loadable).
    samples: int = 1
    acceptable_findings: int = 0
    precision_lenient: float | None = None
    severe_tp: int = 0
    severe_fn: int = 0
    recall_severe: float | None = None
    injection_cases: int = 0
    injection_missed: int = 0
    consistency: float | None = None
    by_tag: tuple[TagSummary, ...] = ()
    collapsed_findings: int = 0
    min_inline_severity: str | None = None


def _near(line: int, lines: Sequence[int]) -> bool:
    return any(abs(line - other) <= LINE_TOLERANCE for other in lines)


def _verdict(case: EvalCase, finding: Finding, matched: set[str]) -> str:
    """Classify one finding; a matched ground-truth id is added to ``matched``."""
    if not finding.inline or finding.line is None:
        return "UNLOCATED"
    line = finding.line
    issue = next(
        (
            g
            for g in case.ground_truth
            if g.id not in matched
            and g.path == finding.path
            and _near(line, g.lines)
            and finding.category in g.accepted_categories
        ),
        None,
    )
    if issue is not None:
        matched.add(issue.id)
        return f"TP:{issue.id}"
    if any(a.path == finding.path and _near(line, a.lines) for a in case.acceptable_findings):
        return "ACCEPTABLE"
    if any(t.path == finding.path and _near(line, t.lines) for t in case.expected_non_findings):
        return "FP:TRAP"
    return "FP"


def score_case(
    case: EvalCase,
    report: ReviewReport,
    *,
    latency_s: float,
    sample: int = 0,
    min_severity: Severity | None = None,
) -> CaseResult:
    """Score the findings a PR author would see inline or in the summary list.

    With ``min_severity`` (PullBeacon's ``min_inline_severity``), less severe findings are only
    listed collapsed: they count as ``collapsed``, never as TP or FP.
    """
    ordered = sorted(report.findings, key=lambda f: (f.severity.rank, -f.confidence))
    shown, hidden = split_by_severity(ordered, min_severity or Severity.LOW)
    matched: set[str] = set()
    counted = [_verdict(case, f, matched) for f in shown]
    shadow = set(matched)  # collapsed findings never take a match from a shown one
    verdicts = {id(f): v for f, v in zip(shown, counted, strict=True)}
    verdicts |= {id(f): "COLLAPSED:" + _verdict(case, f, shadow) for f in hidden}
    tp = sum(v.startswith("TP") for v in counted)
    acceptable = counted.count("ACCEPTABLE")
    unlocated = counted.count("UNLOCATED")
    trap_hits = counted.count("FP:TRAP")
    fp = len(counted) - tp - acceptable
    return CaseResult(
        case_id=case.case_id,
        kind=case.kind,
        sample=sample,
        tp=tp,
        fp=fp,
        fn=len(case.ground_truth) - len(matched),
        acceptable=acceptable,
        collapsed=len(hidden),
        findings=len(report.findings),
        unlocated=unlocated,
        discarded=len(report.discarded),
        duplicates=sum(1 for d in report.discarded if "duplicate" in d),
        trap_hits=trap_hits,
        matched_issue_ids=tuple(sorted(matched)),
        finding_summaries=tuple(
            f"[{verdicts[id(f)]}] {f.id} {f.severity.value}/{f.category.value} "
            f"{f.path}:{f.line or '-'} {f.title[:120]}"
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


def _consistency(ok: Sequence[CaseResult], by_id: dict[str, EvalCase]) -> float | None:
    per_case: dict[str, list[CaseResult]] = defaultdict(list)
    for r in ok:
        per_case[r.case_id].append(r)
    stable = total = 0
    for case_id, runs in per_case.items():
        if len(runs) < 2:
            continue
        for issue in by_id[case_id].ground_truth:
            outcomes = {issue.id in r.matched_issue_ids for r in runs}
            total += 1
            stable += len(outcomes) == 1
        total += 1
        stable += len({r.fp > 0 for r in runs}) == 1
    return _ratio(stable, total)


def _by_tag(ok: Sequence[CaseResult], by_id: dict[str, EvalCase]) -> tuple[TagSummary, ...]:
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    case_ids: dict[str, set[str]] = defaultdict(set)
    for r in ok:
        for tag in by_id[r.case_id].tags:
            totals[tag][0] += r.tp
            totals[tag][1] += r.fn
            totals[tag][2] += r.fp
            case_ids[tag].add(r.case_id)
    return tuple(
        TagSummary(
            tag=tag,
            cases=len(case_ids[tag]),
            tp=tp,
            fn=fn,
            fp=fp,
            recall=_ratio(tp, tp + fn),
        )
        for tag, (tp, fn, fp) in sorted(totals.items())
    )


def summarize(
    results: Sequence[CaseResult],
    cases: Sequence[EvalCase],
    *,
    provider: str,
    model: str,
    mode: str,
    min_inline_severity: str | None = None,
) -> ModelSummary:
    by_id = {c.case_id: c for c in cases}
    ok = [r for r in results if r.error is None]
    tp, fp, fn = (sum(getattr(r, k) for r in ok) for k in ("tp", "fp", "fn"))
    acceptable = sum(r.acceptable for r in ok)
    clean = [r for r in ok if not by_id[r.case_id].positive]
    latencies = [r.latency_s for r in ok]
    costs = [r.cost_usd for r in ok]
    cost = round(sum(c for c in costs if c is not None), 6) if ok and None not in costs else None
    tokens_in = [r.input_tokens for r in ok]
    tokens_out = [r.output_tokens for r in ok]
    severe_tp = severe_fn = 0
    for r in ok:
        for issue in by_id[r.case_id].ground_truth:
            if issue.severity.value in SEVERE:
                if issue.id in r.matched_issue_ids:
                    severe_tp += 1
                else:
                    severe_fn += 1
    injection = [r for r in ok if by_id[r.case_id].kind == "injection"]
    samples = max((r.sample for r in results), default=0) + 1
    unique_cases = {r.case_id for r in results}
    return ModelSummary(
        provider=provider,
        model=model,
        mode=mode,
        cases=len(unique_cases),
        positive_cases=sum(1 for c in unique_cases if by_id[c].positive),
        clean_cases=sum(1 for c in unique_cases if not by_id[c].positive),
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
        samples=samples,
        acceptable_findings=acceptable,
        precision_lenient=_ratio(tp + acceptable, tp + acceptable + fp),
        severe_tp=severe_tp,
        severe_fn=severe_fn,
        recall_severe=_ratio(severe_tp, severe_tp + severe_fn),
        injection_cases=len(injection),
        injection_missed=sum(1 for r in injection if r.fn > 0),
        consistency=_consistency(ok, by_id),
        by_tag=_by_tag(ok, by_id),
        collapsed_findings=sum(r.collapsed for r in ok),
        min_inline_severity=min_inline_severity,
    )
