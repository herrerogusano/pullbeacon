"""Build benchmark v2 cases from readable specs (format: evaluation/SPEC_V2.md).

    uv run python evaluation/build_cases_v2.py            # specs-v2 -> cases-v2
    uv run python evaluation/build_cases_v2.py --oss      # also specs-v2-oss -> cases-v2-oss
    uv run python evaluation/build_cases_v2.py --check    # validate only, write nothing

Every built case is validated with the harness's own loader (labels must land on commentable
diff lines). The mock reviewer response reports exactly the labelled bugs, so mock runs of v2
check the plumbing only; they measure nothing about a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evaluation"))

from build_cases import line_of, patch, sha  # noqa: E402

from pullbeacon.core.gitobjects import git_sha  # noqa: E402
from pullbeacon.domain.errors import ConfigError  # noqa: E402
from pullbeacon.evaluation.dataset import load_cases  # noqa: E402

PAIRS = {
    "specs-v2": "cases-v2",
    "specs-v2-oss": "cases-v2-oss",
}
ALLOWED_ROOTS = (
    "app/",
    "web/",
    "lib/",
    "src/",
    "tests/",
    "scripts/",
    "docs/",
    ".github/workflows/",
)
SPEC_KEYS = {
    "case_id", "title", "pr_body", "language", "kind", "criticality", "split", "source", "author",
    "provenance", "tags", "files", "bugs", "acceptable", "traps",
}  # fmt: skip


class SpecError(ValueError):
    pass


def _lines(after: str, anchor: str, where: str) -> int:
    try:
        return line_of(after, anchor)
    except ValueError as exc:
        raise SpecError(f"{where}: {exc}") from None


def build_spec(spec: dict[str, Any]) -> dict[str, Any]:
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        raise SpecError(f"unknown keys: {sorted(unknown)}")
    case_id = spec["case_id"]
    files_by_path: dict[str, tuple[str | None, str]] = {}
    for f in spec["files"]:
        path = f["path"]
        if path.startswith("/") or ".." in path.split("/") or not path.startswith(ALLOWED_ROOTS):
            raise SpecError(f"path not allowed: {path}")
        before, after = f.get("before"), f["after"]
        for text in (before, after):
            if text is not None and "\r" in text:
                raise SpecError(f"{path}: use LF line endings")
        if before == after:
            raise SpecError(f"{path}: file is unchanged (not part of the diff)")
        files_by_path[path] = (before, after)

    def after_of(path: str, where: str) -> str:
        if path not in files_by_path:
            raise SpecError(f"{where}: path {path} is not a file of the case")
        return files_by_path[path][1]

    ground_truth, findings = [], []
    for bug in spec.get("bugs", []):
        where = f"bug {bug['id']}"
        after = after_of(bug["path"], where)
        lines = [_lines(after, bug["anchor"], where)]
        lines += [_lines(after, a, where) for a in bug.get("extra_anchors", [])]
        ground_truth.append(
            {
                "id": bug["id"],
                "path": bug["path"],
                "lines": lines,
                "category": bug["category"],
                "accepted_categories": sorted({bug["category"], *bug.get("accepted", [])}),
                "severity": bug["severity"],
                "rationale": bug["rationale"],
            }
        )
        evidence = after.splitlines()[lines[0] - 1].strip() or bug["anchor"]
        findings.append(
            {
                "severity": bug["severity"],
                "category": bug["category"],
                "title": bug["title"],
                "explanation": bug["rationale"],
                "impact": "See explanation.",
                "evidence": evidence,
                "path": bug["path"],
                "line": lines[0],
                "side": "RIGHT",
                "suggested_fix": None,
                "confidence": 0.85,
                "can_apply": False,
            }
        )

    def located(items: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        return [
            {
                "path": item["path"],
                "lines": [_lines(after_of(item["path"], kind), item["anchor"], kind)],
                "rationale": item["rationale"],
            }
            for item in items
        ]

    number = int(hashlib.sha1(case_id.encode(), usedforsecurity=False).hexdigest()[:4], 16) % 900
    base_sha, head_sha = sha(f"{case_id}:base"), sha(f"{case_id}:head")
    repo = {
        "name": "bench",
        "full_name": "pullbeacon-eval/bench",
        "owner": {"login": "pullbeacon-eval"},
    }
    pr = {
        "number": number + 100,
        "state": "open",
        "title": spec["title"],
        "body": spec.get("pr_body", ""),
        "draft": False,
        "user": {"login": "eval-author", "type": "User"},
        "base": {"ref": "main", "sha": base_sha, "repo": repo},
        "head": {"ref": f"eval/{case_id}", "sha": head_sha, "repo": repo},
    }
    files, base_tree, head_tree, changed = [], {}, {}, 0
    for path, (before, after) in files_by_path.items():
        text_patch = patch(before or "", after)
        adds = sum(1 for ln in text_patch.splitlines() if ln.startswith("+"))
        dels = sum(1 for ln in text_patch.splitlines() if ln.startswith("-"))
        changed += adds + dels
        files.append(
            {
                "sha": git_sha("blob", after.encode()),
                "filename": path,
                "status": "added" if before is None else "modified",
                "additions": adds,
                "deletions": dels,
                "changes": adds + dels,
                "patch": text_patch,
            }
        )
        if before is not None:
            base_tree[path] = before
        head_tree[path] = after
    size = "small" if changed < 80 else "medium" if changed <= 400 else "large"
    review = {"summary": f"Mock review of {case_id}.", "findings": findings}
    return {
        "case_id": case_id,
        "title": spec["title"],
        "language": spec["language"],
        "kind": spec["kind"],
        "criticality": spec["criticality"],
        "size": size,
        "split": spec["split"],
        "source": spec.get("source", "synthetic"),
        "author": spec.get("author", "claude"),
        "provenance": spec.get("provenance", ""),
        "tags": sorted(spec.get("tags", [])),
        "fixture": {
            "pull_request": pr,
            "files": files,
            "trees": {base_sha: base_tree, head_sha: head_tree},
        },
        "ground_truth": ground_truth,
        "expected_non_findings": located(spec.get("traps", []), "trap"),
        "acceptable_findings": located(spec.get("acceptable", []), "acceptable"),
        "mock_responses": {"review": [json.dumps(review, sort_keys=True)]},
    }


def forward_fix(spec: dict[str, Any]) -> dict[str, Any]:
    """The real upstream fix applied forwards: a reviewed, merged change used as a clean PR."""
    bug_title = spec["bugs"][0]["title"]
    return {
        **spec,
        "case_id": "ossfix_" + spec["case_id"].removeprefix("oss_"),
        "title": f"Fix: {bug_title[0].lower()}{bug_title[1:]}",
        "pr_body": "",
        "kind": "clean",
        "criticality": "none",
        "tags": ["oss_fix_forward"],
        "files": [{**f, "before": f["after"], "after": f["before"]} for f in spec["files"]],
        "bugs": [],
        "acceptable": [],
        "traps": [],
    }


def build_dir(specs: Path, out: Path | None) -> tuple[int, list[str]]:
    errors: list[str] = []
    built: dict[str, str] = {}
    for path in sorted(specs.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            if spec.get("case_id") != path.stem:
                raise SpecError("case_id must equal the file name")
            built[path.name] = json.dumps(build_spec(spec), indent=1, sort_keys=True) + "\n"
            if spec.get("source") == "oss_revert":
                fwd = forward_fix(spec)
                text = json.dumps(build_spec(fwd), indent=1, sort_keys=True) + "\n"
                built[f"{fwd['case_id']}.json"] = text
        except (SpecError, KeyError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    # Validate with the harness loader in a scratch directory before writing anything.
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in built.items():
            single = Path(tmp) / name.removesuffix(".json")
            single.mkdir()
            (single / name).write_text(text, encoding="utf-8", newline="\n")
            try:
                load_cases(single)
            except ConfigError as exc:
                errors.append(f"{name}: {exc}")
    if out is not None and not errors:
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.json"):
            if old.name not in built:
                old.unlink()
        for name, text in built.items():
            (out / name).write_text(text, encoding="utf-8", newline="\n")
    return len(built), errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oss", action="store_true", help="also build specs-v2-oss")
    parser.add_argument("--check", action="store_true", help="validate only")
    args = parser.parse_args()
    failed = False
    for spec_name, case_name in PAIRS.items():
        if spec_name == "specs-v2-oss" and not args.oss:
            continue
        specs = ROOT / "evaluation" / spec_name
        if not specs.is_dir():
            print(f"{spec_name}: missing (skipped)")
            continue
        count, errors = build_dir(specs, None if args.check else ROOT / "evaluation" / case_name)
        print(f"{spec_name}: {count} spec(s), {len(errors)} error(s)")
        for error in errors:
            print(f"  - {error}")
        failed |= bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
