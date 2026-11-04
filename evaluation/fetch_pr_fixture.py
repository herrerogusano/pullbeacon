"""Build a local review fixture from a real pull request of a public repository.

Lets a change be checked against what PullBeacon really did on a real pull request, without
touching that pull request: the fixture goes to ``pullbeacon review --fixture``.

    uv run python evaluation/fetch_pr_fixture.py herrerogusano/LegalDesk 77 --head 44e6dc9... \
        --context AGENTS.md --out evaluation/results/replay/legaldesk-77.json

``--head`` is the commit that was reviewed, because a pull request can move after its review. The
diff comes from GitHub's compare API, as three-dot from the pull request's base, and ``--context``
files are read at the base commit, as PullBeacon does. Fixtures hold third-party code, so they stay
in ``evaluation/results/``, which is git-ignored.

Stdlib only. Uses ``GITHUB_TOKEN`` if set; public repositories work without it at a low rate limit.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.github.com/repos/{repo}"
RAW = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"
MAX_COMPARE_FILES = 300


def _get(url: str, *, api: bool) -> bytes:
    request = urllib.request.Request(url)  # noqa: S310 - fixed https hosts
    token = os.environ.get("GITHUB_TOKEN")
    if api:
        request.add_header("Accept", "application/vnd.github+json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https hosts
        data: bytes = response.read()
    return data


def build_fixture(repo: str, number: int, head: str | None, context: list[str]) -> dict[str, Any]:
    base_url = API.format(repo=repo)
    pull = json.loads(_get(f"{base_url}/pulls/{number}", api=True))
    head_ref = head or pull["head"]["sha"]
    head_sha = json.loads(_get(f"{base_url}/commits/{head_ref}", api=True))["sha"]
    base_sha = pull["base"]["sha"]
    compare = json.loads(_get(f"{base_url}/compare/{base_sha}...{head_sha}", api=True))
    files = compare["files"]
    if len(files) >= MAX_COMPARE_FILES:
        raise SystemExit(f"compare returned {len(files)} files, which may be incomplete")

    def text_at(sha: str, path: str) -> str:
        return _get(RAW.format(repo=repo, sha=sha, path=path), api=False).decode()

    # GitHub omits the patch of large files; PullBeacon then reads both versions.
    no_patch = [entry for entry in files if "patch" not in entry and entry["status"] != "removed"]
    head_tree = {entry["filename"]: text_at(head_sha, entry["filename"]) for entry in no_patch}
    base_tree = {path: text_at(base_sha, path) for path in context}
    for entry in no_patch:
        if entry["status"] != "added":
            old = entry.get("previous_filename", entry["filename"])
            base_tree[old] = text_at(base_sha, old)
    owner, name = repo.split("/")
    repo_payload = {"full_name": repo, "name": name, "owner": {"login": owner}}
    return {
        "provenance": {
            "source": pull["html_url"],
            "reviewed_head": head_sha,
            "note": "Built by evaluation/fetch_pr_fixture.py; state is forced to open.",
        },
        "pull_request": {
            "number": number,
            "title": pull["title"],
            "body": pull["body"] or "",
            "state": "open",
            "draft": False,
            "html_url": pull["html_url"],
            "user": {"login": pull["user"]["login"], "type": pull["user"]["type"]},
            "base": {"ref": pull["base"]["ref"], "sha": base_sha, "repo": repo_payload},
            "head": {"ref": pull["head"]["ref"], "sha": head_sha, "repo": repo_payload},
        },
        "files": [
            {
                key: entry[key]
                for key in (
                    "filename",
                    "status",
                    "additions",
                    "deletions",
                    "changes",
                    "sha",
                    "patch",
                    "previous_filename",
                )
                if key in entry
            }
            for entry in files
        ],
        "trees": {base_sha: base_tree, head_sha: head_tree},
        "permissions": {},
        "llm_responses": {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("number", type=int)
    parser.add_argument("--head", help="commit that was reviewed (default: the pull request head)")
    parser.add_argument("--context", action="append", default=[], help="file to read at the base")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    fixture = build_fixture(args.repo, args.number, args.head, args.context)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, indent=1), encoding="utf-8")
    print(f"{args.out}: {len(fixture['files'])} files at {fixture['provenance']['reviewed_head']}")


if __name__ == "__main__":
    main()
