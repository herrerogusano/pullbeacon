"""Compare benchmark runs, optionally re-scoring them with a severity floor (offline, no calls).

    uv run python evaluation/compare_runs.py RUN.json [RUN2.json ...] [--floor medium] [--judge J]

Re-scoring uses each finding's recorded verdict (``finding_summaries``): findings below the floor
are dropped, a labelled issue counts as found when a remaining finding was its TP. Duplicates of
an issue whose only TP was dropped are not promoted, so recall with a floor is a lower bound.

Reports per run and model: recall (all / high+critical), precision, false positives per clean
PR, findings shown per review, share of ``testing`` findings, consistency of the labelled
outcomes across samples, and cost per review.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

SUMMARY = re.compile(
    r"\[(?P<verdict>[^\]]+)\] PB-\d+ (?P<sev>\w+)/(?P<cat>\w+) (?P<path>\S+?):(?P<line>\S+) "
)
RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _cases(paths: list[Path]) -> dict[str, dict]:
    out = {}
    for directory in paths:
        for p in directory.glob("*.json"):
            c = json.loads(p.read_text(encoding="utf-8"))
            out[c["case_id"]] = c
    return out


def rescore(run: dict, cases: dict[str, dict], floor: str) -> dict[str, dict]:
    limit = RANK[floor]
    report = {}
    for model, results in run["cases"].items():
        tp = fn = fp = acc = shown = testing = 0
        severe_tp = severe_fn = 0
        clean_runs = clean_fp = 0
        per_issue: dict[tuple[str, str], set[bool]] = defaultdict(set)
        cost = 0.0
        ok = [r for r in results if not r.get("error")]
        for r in ok:
            case = cases[r["case_id"]]
            found = set()
            for s in r["finding_summaries"]:
                m = SUMMARY.match(s)
                if m is None or RANK[m["sev"]] > limit:
                    continue
                verdict = m["verdict"].removeprefix("COLLAPSED:")
                shown += 1
                testing += m["cat"] == "testing"
                if verdict.startswith("TP:"):
                    found.add(verdict[3:])
                elif verdict == "ACCEPTABLE":
                    acc += 1
                else:
                    fp += 1
                    clean_fp += case["kind"] == "clean"
            clean_runs += case["kind"] == "clean"
            for g in case["ground_truth"]:
                hit = g["id"] in found
                tp += hit
                fn += not hit
                if g["severity"] in {"critical", "high"}:
                    severe_tp += hit
                    severe_fn += not hit
                per_issue[(r["case_id"], g["id"])].add(hit)
            cost += r.get("cost_usd") or 0.0
        multi = list(per_issue.values())
        report[model.split("/")[-1]] = {
            "reviews": len(ok),
            "errors": len(results) - len(ok),
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "recall_severe": round(severe_tp / (severe_tp + severe_fn), 3)
            if severe_tp + severe_fn
            else None,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "precision_lenient": round((tp + acc) / (tp + acc + fp), 3) if tp + acc + fp else None,
            "fp_per_clean_pr": round(clean_fp / clean_runs, 2) if clean_runs else None,
            "shown_per_review": round(shown / len(ok), 2) if ok else None,
            "testing_share": round(testing / shown, 3) if shown else None,
            "issue_consistency": round(sum(len(v) == 1 for v in multi) / len(multi), 3)
            if run.get("repeats", 1) > 1 and multi
            else None,
            "cost_per_review": round(cost / len(ok), 4) if ok else None,
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--floor", choices=list(RANK), default="low")
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    args = parser.parse_args()
    cases = _cases(args.dataset)
    out = {}
    for path in args.runs:
        run = json.loads(path.read_text(encoding="utf-8"))
        out[f"{path.stem} (review={run['prompt_versions']['review']}, floor={args.floor})"] = (
            rescore(run, cases, args.floor)
        )
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
