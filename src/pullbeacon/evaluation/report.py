"""Markdown and CSV renderings of an evaluation run (no dashboard)."""

from __future__ import annotations

import csv
import io

from pullbeacon.evaluation.runner import EvalRun


def _fmt(value: object, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def to_markdown(run: EvalRun) -> str:
    lines = [
        f"# PullBeacon evaluation — {run.mode}",
        "",
        f"- Date (UTC): {run.started_at}",
        f"- PullBeacon {run.pullbeacon_version}, commit `{run.git_commit}`",
        f"- Prompts: {', '.join(f'{k}={v}' for k, v in sorted(run.prompt_versions.items()))}",
        f"- Dataset sha256: `{run.dataset_sha256[:16]}…`, config hash `{run.config_hash}`",
        f"- Split: {run.split or 'all'} · samples per case: {run.repeats} · "
        f"temperature: {_fmt(run.temperature, 1)}",
        f"- Total spent (estimate): {_fmt(run.total_spent_usd, 4)} USD",
        "",
        *[f"> {note}" for note in run.notes],
        "",
        "| Model | Recall | Recall high+crit | Precision | Precision (lenient) | FP / clean PR "
        "| TP | FP | FN | Acceptable | Unlocated | Errors | Consistency | Injection runs missed "
        "| Mean latency (s) | Cost (USD) | Cost / review | Apply exact |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in run.summaries:
        lines.append(
            f"| `{s.model}` | {_fmt(s.recall)} | {_fmt(s.recall_severe)} | {_fmt(s.precision)} | "
            f"{_fmt(s.precision_lenient)} | {_fmt(s.false_positives_per_clean_pr)} | "
            f"{s.tp} | {s.fp} | {s.fn} | {s.acceptable_findings} | {s.unlocated_findings} | "
            f"{s.errors} | {_fmt(s.consistency)} | {s.injection_missed}/{s.injection_cases} | "
            f"{_fmt(s.latency_mean_s, 2)} | {_fmt(s.cost_usd, 4)} | "
            f"{_fmt(s.cost_per_review_usd, 4)} | {s.apply_exact}/{s.apply_attempted} |"
        )
    lines += [
        "",
        "Matching rule: inline finding on the labelled path, within ±3 lines, with an accepted "
        "category. Findings on labelled secondary issues are *acceptable* (neither TP nor FP); "
        "unlocated findings count as false positives. Totals add up all samples. "
        "Review disagreements manually.",
    ]
    if any(s.by_tag for s in run.summaries):
        lines += [
            "",
            "## By tag",
            "",
            "| Model | Tag | Cases | TP | FN | FP | Recall |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in run.summaries:
            for t in s.by_tag:
                lines.append(
                    f"| `{s.model}` | {t.tag} | {t.cases} | {t.tp} | {t.fn} | {t.fp} | "
                    f"{_fmt(t.recall)} |"
                )
    lines += [
        "",
        "## Per case",
        "",
        "| Model | Case | Sample | Kind | TP | FP | FN | Acceptable | Unlocated | Discarded "
        "| Latency (s) | Error |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for model, results in run.cases.items():
        for r in results:
            lines.append(
                f"| `{model}` | {r.case_id} | {r.sample} | {r.kind} | {r.tp} | {r.fp} | {r.fn} "
                f"| {r.acceptable} | {r.unlocated} | {r.discarded} | {_fmt(r.latency_s, 2)} "
                f"| {r.error or ''} |"
            )
    return "\n".join(lines) + "\n"


def to_csv(run: EvalRun) -> str:
    buffer = io.StringIO()
    fields = [
        "model", "case_id", "sample", "kind", "tp", "fp", "fn", "acceptable", "findings",
        "unlocated", "discarded", "duplicates", "trap_hits", "latency_s", "input_tokens",
        "output_tokens", "cost_usd", "cost_source", "apply_attempted", "apply_valid",
        "apply_exact", "apply_reason", "error",
    ]  # fmt: skip
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for model, results in run.cases.items():
        for r in results:
            row = r.model_dump(include=set(fields))
            row["model"] = model
            writer.writerow(row)
    return buffer.getvalue()
