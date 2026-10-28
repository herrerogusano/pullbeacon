"""Create one commit on a PR branch through the Git Data API (no checkout, no force, no merge)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pullbeacon.adapters.github import GitHubClient, check_branch
from pullbeacon.core.apply import PlannedEdit
from pullbeacon.core.gitobjects import git_sha
from pullbeacon.domain.errors import ApplyRejected, GitHubAPIError, StaleHeadError
from pullbeacon.domain.models import PullRequestSnapshot

REGULAR_FILE_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True)
class CommitResult:
    sha: str
    url: str | None


def verify_blob(content: bytes, expected_blob_sha: str) -> None:
    if git_sha("blob", content) != expected_blob_sha:
        raise ApplyRejected("file content does not match the commit tree (refusing to edit)")


def commit_edits(
    client: GitHubClient,
    snapshot: PullRequestSnapshot,
    edits: Sequence[PlannedEdit],
    *,
    message: str,
) -> CommitResult:
    owner, repo, branch = snapshot.owner, snapshot.repo, check_branch(snapshot.head_ref)
    if not edits:
        raise ApplyRejected("nothing to commit")

    if client.get_branch(owner, repo, branch).get("protected"):
        raise ApplyRejected(f"branch '{branch}' is protected; PullBeacon does not push to it")
    if client.get_ref_sha(owner, repo, branch) != snapshot.head_sha:
        raise StaleHeadError("the branch moved since the review; nothing was changed")

    head_commit = client.get_commit(owner, repo, snapshot.head_sha)
    base_tree = (head_commit.get("tree") or {}).get("sha")
    if not isinstance(base_tree, str):
        raise GitHubAPIError("head commit has no tree")
    tree = client.get_tree(owner, repo, base_tree)
    if tree.get("truncated"):
        raise ApplyRejected("repository tree is too large to verify file modes safely")
    entries = {e.get("path"): e for e in tree.get("tree", []) if isinstance(e, dict)}

    new_entries = []
    for edit in edits:
        entry = entries.get(edit.path)
        if entry is None or entry.get("type") != "blob":
            raise ApplyRejected(f"{edit.path} is not a regular file at the head commit")
        mode = entry.get("mode")
        if mode not in REGULAR_FILE_MODES:
            raise ApplyRejected(f"{edit.path} is a symlink or special file")
        new_entries.append(
            {"path": edit.path, "mode": mode, "type": "blob", "content": edit.new_content}
        )

    new_tree = client.create_tree(owner, repo, base_tree=base_tree, entries=new_entries)
    commit = client.create_commit(
        owner, repo, message=message, tree=new_tree, parents=[snapshot.head_sha]
    )
    commit_sha = commit.get("sha")
    if not isinstance(commit_sha, str):
        raise GitHubAPIError("commit creation returned no SHA")

    # Re-check right before moving the branch: a push in between must win, never be overwritten.
    if client.get_ref_sha(owner, repo, branch) != snapshot.head_sha:
        raise StaleHeadError("the branch moved while preparing the commit; nothing was changed")
    try:
        client.update_ref(owner, repo, branch, commit_sha)
    except GitHubAPIError as exc:
        if exc.status == 422:
            raise StaleHeadError(
                "the branch moved (not a fast-forward); nothing was changed"
            ) from exc
        if exc.status in (403, 409):
            raise ApplyRejected(
                "GitHub refused the update (branch protection or token permissions)"
            ) from exc
        raise
    if client.get_ref_sha(owner, repo, branch) != commit_sha:
        raise GitHubAPIError("branch does not point at the new commit after the update")
    url = commit.get("html_url")
    return CommitResult(sha=commit_sha, url=url if isinstance(url, str) else None)
