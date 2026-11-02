"""Independent-judge workflow for a benchmark run (replaces a human blind rating).

    uv run python evaluation/judge_pack.py pack RUN.json [RUN2.json ...] OUTDIR --dataset D [...]
    uv run python evaluation/judge_pack.py agree OUTDIR JUDGE.json
    uv run python evaluation/judge_pack.py useful OUTDIR JUDGE.json

With several runs (e.g. two PullBeacon versions), items are pooled and shuffled, so the judge
cannot tell which version wrote a comment; ``useful`` reports the judge's useful share per run,
model and severity floor, weighted by how often each comment appeared across samples.

``pack`` writes ``OUTDIR/pack.json``: one item per distinct (model, case, line) finding location,
with the PR title/body, the diff and the finding titles, but **no labels and no author verdicts**
(the item order is shuffled and models are anonymised). ``OUTDIR/key.json`` maps items back and
holds the author's verdict (automatic TP/acceptable, else ``manual_review_v2.VERDICTS``); the judge
must not read it.

The judge answers ``[{"i": 1, "verdict": "USEFUL|MINOR|WRONG", "reason": "..."}]``.
``agree`` compares the judge with the author's verdicts (mapped: TP/SAME/ACCEPTABLE/VALID ->
USEFUL, MINOR -> MINOR, SPEC -> WRONG) and prints agreement and Cohen's kappa.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))

from manual_review_v2 import VERDICTS  # noqa: E402

SUMMARY = re.compile(
    r"\[(?P<verdict>[^\]]+)\] PB-\d+ (?P<sev>\w+)/(?P<cat>\w+) (?P<path>\S+?):(?P<line>\S+) "
    r"(?P<title>.*)"
)
MAP = {
    "TP": "USEFUL",
    "SAME": "USEFUL",
    "ACCEPTABLE": "USEFUL",
    "VALID": "USEFUL",
    "MINOR": "MINOR",
    "SPEC": "WRONG",
}
LABELS = ("USEFUL", "MINOR", "WRONG")
RUBRIC = """\
For each item decide, as a senior reviewer of THIS pull request, whether the automated review
comment would be worth a developer's attention. Use only the PR title/body and the diff given.
- USEFUL: a real, specific problem supported by the diff (defect, security issue, broken
  contract, data loss, real risk) that the author would want to know before merging.
- MINOR: technically true or defensible but low value: generic "add tests", style, theoretical
  edge cases, restating an intended behaviour change, nitpicks.
- WRONG: not supported by the diff, needs code that is not shown to be true, or incorrect.
Answer with a JSON array: [{"i": <item>, "verdict": "USEFUL|MINOR|WRONG", "reason": "<one line>"}]
"""


def _cases(datasets: list[Path]) -> dict[str, dict]:
    cases = {}
    for directory in datasets:
        for path in sorted(directory.glob("*.json")):
            case = json.loads(path.read_text(encoding="utf-8"))
            cases[case["case_id"]] = case
    return cases


def pack(run_paths: list[Path], out: Path, datasets: list[Path]) -> int:
    cases = _cases(datasets)
    groups: dict[tuple[str, str, str, str], dict] = {}
    for run_path in run_paths:
        run = json.loads(run_path.read_text(encoding="utf-8"))
        for model, results in run["cases"].items():
            for r in results:
                for summary in r["finding_summaries"]:
                    m = SUMMARY.match(summary)
                    if m is None:
                        continue
                    key = (run_path.stem, model, r["case_id"], m["line"])
                    g = groups.setdefault(
                        key,
                        {"titles": [], "auto": Counter(), "sev": Counter(), "cat": m["cat"],
                         "path": m["path"], "count": 0},
                    )  # fmt: skip
                    if m["title"] not in g["titles"]:
                        g["titles"].append(m["title"])
                    g["sev"][m["sev"]] += 1
                    g["count"] += 1
                    v = m["verdict"].removeprefix("COLLAPSED:")
                    g["auto"]["TP" if v.startswith("TP") else v] += 1
    items, key_rows = [], []
    order = list(groups)
    random.Random(20261102).shuffle(order)  # noqa: S311 (reproducible order, not security)
    used_cases: set[str] = set()
    for i, key in enumerate(order, 1):
        run_name, model, case_id, line = key
        g = groups[key]
        auto = g["auto"].most_common(1)[0][0]
        sev = g["sev"].most_common(1)[0][0]
        mine = (
            auto
            if auto in {"TP", "ACCEPTABLE"}
            else VERDICTS.get((model, case_id, line), "UNREVIEWED")
        )
        items.append(
            {
                "i": i,
                "case": case_id,
                "severity": sev,
                "category": g["cat"],
                "location": f"{g['path']}:{line}",
                "titles": g["titles"][:3],
            }
        )
        key_rows.append(
            {
                "i": i,
                "run": run_name,
                "model": model,
                "case": case_id,
                "line": line,
                "auto": auto,
                "severities": dict(g["sev"]),
                "count": g["count"],
                "mine": mine,
            }
        )
        used_cases.add(case_id)
    prs = {}
    for case_id in sorted(used_cases):
        fixture = cases[case_id]["fixture"]
        pr = fixture["pull_request"]
        prs[case_id] = {
            "title": pr["title"],
            "body": pr.get("body") or "",
            "diff": "\n".join(f"--- {f['filename']}\n{f['patch']}" for f in fixture["files"]),
        }
    out.mkdir(parents=True, exist_ok=True)
    (out / "pack.json").write_text(
        json.dumps({"rubric": RUBRIC, "prs": prs, "items": items}, indent=1), encoding="utf-8"
    )
    (out / "key.json").write_text(json.dumps(key_rows, indent=1), encoding="utf-8")
    print(f"{len(items)} items from {len(prs)} PRs -> {out / 'pack.json'}")
    unreviewed = [k for k in key_rows if k["mine"] == "UNREVIEWED"]
    if unreviewed:
        print(f"note: {len(unreviewed)} items without an author verdict", file=sys.stderr)
    return 0


RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def useful(out: Path, judge_path: Path) -> int:
    key = json.loads((out / "key.json").read_text(encoding="utf-8"))
    rows = json.loads(judge_path.read_text(encoding="utf-8"))
    judge = {row["i"]: row["verdict"].upper() for row in rows}
    report: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for run_name in sorted({row["run"] for row in key}):
        for model in sorted({row["model"] for row in key}):
            for floor in ("low", "medium"):
                tally: Counter[str] = Counter()
                for row in key:
                    if row["run"] != run_name or row["model"] != model:
                        continue
                    for sev, n in row["severities"].items():
                        if RANK[sev] <= RANK[floor]:
                            tally[judge[row["i"]]] += n
                total = sum(tally.values())
                report[f"{run_name} floor={floor}"][model.split("/")[-1]] = {
                    "findings": total,
                    **{label.lower(): round(tally[label] / total, 3) for label in LABELS},
                }
    print(json.dumps(report, indent=1))
    return 0


def kappa(pairs: list[tuple[str, str]]) -> float:
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(ca[label] * cb[label] for label in LABELS) / (n * n)
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def agree(out: Path, judge_path: Path) -> int:
    key = {row["i"]: row for row in json.loads((out / "key.json").read_text(encoding="utf-8"))}
    judge = {row["i"]: row for row in json.loads(judge_path.read_text(encoding="utf-8"))}
    missing = sorted(set(key) - set(judge))
    pairs, confusion = [], Counter()
    per_model: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for i, row in key.items():
        if i not in judge or row["mine"] not in MAP:
            continue
        a, b = MAP[row["mine"]], judge[i]["verdict"].upper()
        pairs.append((a, b))
        confusion[(a, b)] += 1
        per_model[row["model"]].append((a, b))
    report = {
        "items": len(key),
        "judged": len(pairs),
        "missing": missing[:20],
        "agreement": round(sum(a == b for a, b in pairs) / len(pairs), 4),
        "kappa": round(kappa(pairs), 4),
        "confusion_author_vs_judge": {f"{a}->{b}": n for (a, b), n in sorted(confusion.items())},
        "useful_share": {
            model: {
                "author": round(sum(a == "USEFUL" for a, _ in ps) / len(ps), 4),
                "judge": round(sum(b == "USEFUL" for _, b in ps) / len(ps), 4),
                "wrong_judge": round(sum(b == "WRONG" for _, b in ps) / len(ps), 4),
            }
            for model, ps in sorted(per_model.items())
        },
    }
    print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack")
    p.add_argument("paths", type=Path, nargs="+", help="RUN.json ... OUTDIR")
    p.add_argument("--dataset", type=Path, action="append", required=True)
    a = sub.add_parser("agree")
    a.add_argument("out", type=Path)
    a.add_argument("judge", type=Path)
    u = sub.add_parser("useful")
    u.add_argument("out", type=Path)
    u.add_argument("judge", type=Path)
    args = parser.parse_args()
    if args.cmd == "pack":
        return pack(args.paths[:-1], args.paths[-1], args.dataset)
    if args.cmd == "useful":
        return useful(args.out, args.judge)
    return agree(args.out, args.judge)


if __name__ == "__main__":
    raise SystemExit(main())
