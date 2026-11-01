"""Build benchmark v2 "revert a real fix" specs from open-source bug-fix commits.

For every entry in ``evaluation/oss_manifest.json`` this script downloads the affected file(s)
from ``raw.githubusercontent.com`` at two pinned commits:

* ``fix_commit``    -> spec ``before`` (the fixed code)
* ``parent_commit`` -> spec ``after``  (the buggy code)

so the case's pull request re-introduces a bug that was really shipped and later fixed upstream.
One spec per entry is written to ``evaluation/specs-v2-oss/<case_id>.json`` (format:
``evaluation/SPEC_V2.md``).

Licence notice: the fetched files belong to their upstream projects and remain under their own
licences (MIT, BSD-3-Clause, Apache-2.0; recorded per entry in the manifest and in each spec's
``provenance``). They are downloaded only for local evaluation and are NEVER committed: the
``evaluation/specs-v2-oss/`` directory and the cases built from it are git-ignored. Only the
manifest (pinned SHAs, paths, labels) and this script live in the repository. Anyone
redistributing the fetched files must keep the upstream copyright and licence notices.

Checks (run on every build, and alone with ``--check``):

* every anchor (and extra anchor) matches exactly one line of ``after``;
* it lies on a line visible in ``difflib.unified_diff(before, after, n=3)`` and within 3 lines
  of a changed line;
* the diff is non-empty and has at most ``MAX_CHANGED_LINES`` changed lines;
* files above ``MAX_FILE_BYTES`` are only accepted when trimmed (manifest ``trim``) or when the
  diff is small (``SMALL_DIFF_LINES``).

Usage::

    uv run python evaluation/fetch_oss_cases.py            # fetch, validate, write specs
    uv run python evaluation/fetch_oss_cases.py --check    # fetch and validate only
    uv run python evaluation/fetch_oss_cases.py --only oss_x --check

Stdlib only (urllib); no API token needed for public repositories.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "oss_manifest.json"
OUT_DIR = ROOT / "specs-v2-oss"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"

MAX_CHANGED_LINES = 600
MAX_FILE_BYTES = 150 * 1024
SMALL_DIFF_LINES = 80
ANCHOR_WINDOW = 3
ALLOWED_PREFIXES = ("app/", "web/", "lib/", "src/", "tests/", "scripts/", "docs/")
ALLOWED_LICENSES = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "HPND", "MIT-CMU"}
REQUIRED_KEYS = (
    "case_id",
    "repo",
    "license",
    "fix_commit",
    "parent_commit",
    "reference",
    "language",
    "kind",
    "split",
    "criticality",
    "tags",
    "bug",
    "title",
)


class CaseError(Exception):
    """A manifest entry cannot be turned into a valid spec."""


@dataclass
class FilePair:
    path: str  # target path inside the case
    before: str
    after: str


def fetch(repo: str, sha: str, path: str, retries: int = 3) -> str:
    url = RAW_URL.format(repo=repo, sha=sha, path=path)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pullbeacon-eval-fetch"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            return raw.decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise CaseError(f"404 for {url}") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(1 + attempt)
    raise CaseError(f"failed to fetch {url}: {last}")


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("﻿"):
        text = text[1:]
    if not text.endswith("\n"):
        text += "\n"
    return text


def trim(text: str, spec: dict[str, str] | None, where: str) -> str:
    """Keep lines from the first one containing ``start`` up to (excluding) ``end``."""
    if not spec:
        return text
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if spec["start"] in ln), None)
    if start is None:
        raise CaseError(f"{where}: trim start marker not found: {spec['start']!r}")
    end = len(lines)
    if spec.get("end"):
        end = next((i for i, ln in enumerate(lines) if i > start and spec["end"] in ln), None)
        if end is None:
            raise CaseError(f"{where}: trim end marker not found: {spec['end']!r}")
    return "".join(lines[start:end])


def diff_info(before: str, after: str) -> tuple[int, set[int], set[int]]:
    """Return (changed line count, visible `after` lines, `after` positions of changes).

    Line numbers are 1-based in `after`. A removal is attributed to the `after` line where it
    happened (the following line) and the preceding one.
    """
    a = before.splitlines(keepends=True)
    b = after.splitlines(keepends=True)
    changed = 0
    visible: set[int] = set()
    change_pos: set[int] = set()
    new_line = 0
    for line in difflib.unified_diff(a, b, n=ANCHOR_WINDOW):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            new_part = line.split()[2]  # "+start,len"
            new_line = int(new_part[1:].split(",")[0])
            continue
        if line.startswith("+"):
            visible.add(new_line)
            change_pos.add(new_line)
            changed += 1
            new_line += 1
        elif line.startswith("-"):
            change_pos.update({new_line, max(new_line - 1, 1)})
            changed += 1
        else:
            visible.add(new_line)
            new_line += 1
    return changed, visible, change_pos


def check_anchor(
    case_id: str, pair: FilePair, anchor: str, visible: set[int], changes: set[int]
) -> int:
    hits = [i for i, ln in enumerate(pair.after.splitlines(), start=1) if anchor in ln]
    if len(hits) != 1:
        raise CaseError(
            f"{case_id}: anchor {anchor!r} matches {len(hits)} lines of {pair.path} (need 1)"
        )
    line = hits[0]
    if line not in visible:
        raise CaseError(f"{case_id}: anchor {anchor!r} (line {line}) is not visible in the diff")
    if not any(abs(line - c) <= ANCHOR_WINDOW for c in changes):
        raise CaseError(
            f"{case_id}: anchor {anchor!r} (line {line}) is not within {ANCHOR_WINDOW} lines of a change"
        )
    return line


def entry_files(entry: dict[str, Any]) -> list[dict[str, Any]]:
    if "files" in entry:
        return entry["files"]
    return [{"path": entry["path"], "target_path": entry["target_path"], "trim": entry.get("trim")}]


def validate_entry_shape(entry: dict[str, Any]) -> None:
    cid = entry.get("case_id", "<missing>")
    missing = [k for k in REQUIRED_KEYS if k not in entry]
    if missing:
        raise CaseError(f"{cid}: missing keys {missing}")
    if not cid.startswith("oss_"):
        raise CaseError(f"{cid}: case_id must start with 'oss_'")
    for key in ("fix_commit", "parent_commit"):
        sha = entry[key]
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise CaseError(f"{cid}: {key} must be a full 40-char lowercase SHA")
    if entry["license"] not in ALLOWED_LICENSES:
        raise CaseError(f"{cid}: licence {entry['license']!r} not in the permissive allow-list")
    for f in entry_files(entry):
        if not f["target_path"].startswith(ALLOWED_PREFIXES):
            raise CaseError(f"{cid}: target_path {f['target_path']!r} not under an allowed prefix")


def build_spec(entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_entry_shape(entry)
    cid = entry["case_id"]
    pairs: list[FilePair] = []
    for f in entry_files(entry):
        before = normalize(fetch(entry["repo"], entry["fix_commit"], f["path"]))
        after = normalize(fetch(entry["repo"], entry["parent_commit"], f["path"]))
        before = trim(before, f.get("trim"), f"{cid} before")
        after = trim(after, f.get("trim"), f"{cid} after")
        pairs.append(FilePair(f["target_path"], before, after))

    total_changed = 0
    by_path: dict[str, tuple[FilePair, set[int], set[int]]] = {}
    for pair in pairs:
        changed, visible, changes = diff_info(pair.before, pair.after)
        if changed == 0:
            raise CaseError(f"{cid}: {pair.path} has no changes between fix and parent")
        size = max(len(pair.before.encode()), len(pair.after.encode()))
        if size > MAX_FILE_BYTES and changed > SMALL_DIFF_LINES:
            raise CaseError(
                f"{cid}: {pair.path} is {size} bytes with {changed} changed lines; add a trim"
            )
        total_changed += changed
        by_path[pair.path] = (pair, visible, changes)
    if total_changed > MAX_CHANGED_LINES:
        raise CaseError(f"{cid}: {total_changed} changed lines > {MAX_CHANGED_LINES}")

    bug_in = entry["bug"]
    bug_path = bug_in.get("path", entry_files(entry)[0]["target_path"])
    if bug_path not in by_path:
        raise CaseError(f"{cid}: bug path {bug_path!r} is not a case file")
    pair, visible, changes = by_path[bug_path]
    anchor_lines = [check_anchor(cid, pair, bug_in["anchor"], visible, changes)]
    for extra in bug_in.get("extra_anchors", []):
        anchor_lines.append(check_anchor(cid, pair, extra, visible, changes))
    for group in ("acceptable", "traps"):
        for item in entry.get(group, []):
            p = item.get("path", bug_path)
            if p not in by_path:
                raise CaseError(f"{cid}: {group} path {p!r} is not a case file")
            hits = [ln for ln in by_path[p][0].after.splitlines() if item["anchor"] in ln]
            if len(hits) != 1:
                raise CaseError(
                    f"{cid}: {group} anchor {item['anchor']!r} matches {len(hits)} lines"
                )

    bug = {
        "id": bug_in["id"],
        "path": bug_path,
        "anchor": bug_in["anchor"],
        "extra_anchors": bug_in.get("extra_anchors", []),
        "category": bug_in["category"],
        "accepted": bug_in.get("accepted", []),
        "severity": bug_in["severity"],
        "title": bug_in["title"],
        "rationale": bug_in["rationale"],
    }
    spec = {
        "case_id": cid,
        "title": entry["title"],
        "pr_body": entry.get("pr_body", ""),
        "language": entry["language"],
        "kind": entry["kind"],
        "criticality": entry["criticality"],
        "split": entry["split"],
        "source": "oss_revert",
        "author": "oss",
        "provenance": (
            f"{entry['repo']}@{entry['fix_commit']} ({entry['license']}), reverted fix; "
            f"{entry['reference']}"
        ),
        "tags": entry["tags"],
        "files": [{"path": p.path, "before": p.before, "after": p.after} for p in pairs],
        "bugs": [bug],
        "acceptable": [{"path": a.get("path", bug_path), **a} for a in entry.get("acceptable", [])],
        "traps": [{"path": t.get("path", bug_path), **t} for t in entry.get("traps", [])],
    }
    stats = {"changed": total_changed, "anchor_lines": anchor_lines}
    return spec, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="validate only; do not write specs")
    parser.add_argument("--only", action="append", default=[], help="restrict to these case_ids")
    args = parser.parse_args(argv)

    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = [e.get("case_id") for e in entries]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        print(f"ERROR: duplicate case_ids: {sorted(dupes)}", file=sys.stderr)
        return 1
    if args.only:
        entries = [e for e in entries if e.get("case_id") in set(args.only)]

    failures = 0
    if not args.check:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        cid = entry.get("case_id", "<missing>")
        try:
            spec, stats = build_spec(entry)
        except CaseError as exc:
            failures += 1
            print(f"FAIL {cid}: {exc}", file=sys.stderr)
            continue
        if not args.check:
            out = OUT_DIR / f"{cid}.json"
            out.write_text(
                json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        print(
            f"OK   {cid:<36} {entry['split']:<4} changed={stats['changed']:<4} "
            f"anchor_lines={stats['anchor_lines']}"
        )
    print(
        f"{len(entries) - failures}/{len(entries)} cases valid"
        + ("" if args.check else f"; specs in {OUT_DIR}")
    )
    if failures:
        print(f"ERROR: {failures} case(s) failed validation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
