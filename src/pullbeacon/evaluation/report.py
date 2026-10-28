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
        f"# PullBeacon evaluation â€” {run.mode}",
        "",
        f"- Date (UTC): {run.started_at}",
        f"- PullBeacon {run.pullbeacon_version}, commit `{run.git_commit}`",
        f"- Prompts: {', '.join(f'{k}={v}' for k, v in sorted(run.prompt_versions.items()))}",
        f"- Dataset sha256: `{run.dataset_sha256[:16]}â€¦`, config hash `{run.config_hash}`",
        f"- Temperature: {_fmt(run.temperature, 1)}",
        "",
        *[f"> {note}" for note in run.notes],
        "",
        "| Model | Recall | Precision | FP / clean PR | TP | FP | FN | Unlocated | Discarded "
        "| Errors | Mean latency (s) | Cost (USD) | Cost / review | Cost / TP | Apply exact |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in run.summaries:
        lines.append(
            f"| `{s.model}` | {_fmt(s.recall)} | {_fmt(s.precision)} | "
            f"{_fmt(s.false_positives_per_clean_pr)} | {s.tp} | {s.fp} | {s.fn} | "
            f"{s.unlocated_findings} | {s.discarded_findings} | {s.errors} | "
            f"{_fmt(s.latency_mean_s, 2)} | {_fmt(s.cost_usd, 4)} | "
            f"{_fmt(s.cost_per_review_usd, 4)} | "
            f"{_fmt(s.cost_per_true_positive_usd, 4)} | {s.apply_exact}/{s.apply_attempted} |"
        )
    lines += [
        "",
        "Matching rule: inline finding on the labelled path, within Â±3 lines, with an accepted "
        "category. Unlocated findings count as false positives. Review disagreements manually.",
        "",
        "## Per case",
        "",
        "| Model | Case | Kind | TP | FP | FN | Unlocated | Discarded | Latency (s) | Error |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for model, results in run.cases.items():
        for r in results:
            lines.append(
                f"| `{model}` | {r.case_id} | {r.kind} | {r.tp} | {r.fp} | {r.fn} | {r.unlocated} "
                f"| {r.discarded} | {_fmt(r.latency_s, 2)} | {r.error or ''} |"
            )
    return "\n".join(lines) + "\n"


def to_csv(run: EvalRun) -> str:
    buffer = io.StringIO()
    fields = [
        "model", "case_id", "kind", "tp", "fp", "fn", "findings", "unlocated", "discarded",
        "duplicates", "trap_hits", "latency_s", "input_tokens", "output_tokens", "cost_usd",
        "cost_source", "apply_attempted", "apply_valid", "apply_exact", "apply_reason", "error",
    ]  # fmt: skip
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for model, results in run.cases.items():
        for r in results:
            row = r.model_dump(include=set(fields))
            row["model"] = model
            writer.writerow(row)
    return buffer.getvalue()
