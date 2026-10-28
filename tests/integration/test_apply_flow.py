from __future__ import annotations

from typing import Any

import pytest

from pullbeacon.domain.models import RunStatus
from pullbeacon.orchestration.flows import run_review
from pullbeacon.orchestration.runner import ActionInputs, run_action
from tests.helpers import APPLY_CONFIG, setup
from tests.integration.test_action import comment

PR = 21
APPLY_JOB = ActionInputs(mode="apply")
BRANCH = "heads/feature/auth-bug"


def prepared(**kwargs: Any) -> tuple[Any, Any, Any, dict[str, Any]]:
    fake, services, llm, data = setup(apply_job=True, **kwargs)
    run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    return fake, services, llm, data


def apply(services: Any, body: str = "@pullbeacon apply PB-001", **kw: Any) -> Any:
    return run_action(
        services, kw.pop("inputs", APPLY_JOB), event_name="issue_comment", event=comment(body, **kw)
    )


def apply_writes(fake: Any) -> list[Any]:
    return [c for c in fake.writes() if "/git/" in c.path]


def test_authorized_apply_creates_one_bounded_commit() -> None:
    fake, services, llm, data = prepared()
    head = data["pull_request"]["head"]["sha"]
    result = apply(services)
    assert result.status is RunStatus.COMPLETED, result.reason
    assert result.commit_sha == fake.refs[BRANCH]
    commit = fake.commits[result.commit_sha]
    assert commit["parents"] == [{"sha": head}]
    assert "PB-001" in commit["message"]
    assert head in commit["message"]
    content = fake.file_at(result.commit_sha, "src/api/users.py")
    assert content is not None
    assert content.decode() == data["expected_fixed_content"]["src/api/users.py"]
    reply = fake.issue_comments[PR][-1]["body"]
    assert "PullBeacon applied PB-001" in reply
    assert "did **not** run tests" in reply
    assert llm.requests[-1].purpose == "apply"
    assert sum(1 for c in fake.calls if c.method == "PATCH") == 1
    assert not any("merge" in c.path for c in fake.calls)


def test_apply_all_applicable_findings() -> None:
    _fake, services, _, _ = prepared()
    result = apply(services, "@pullbeacon apply")
    assert result.status is RunStatus.COMPLETED
    assert result.details["findings"] == ["PB-001"]


@pytest.mark.parametrize("login", ["reader-bob", "stranger"])
def test_without_write_no_llm_and_no_git_writes(login: str) -> None:
    fake, services, llm, _ = prepared()
    calls_before = len(llm.requests)
    result = apply(services, login=login)
    assert result.reason == "permission denied"
    assert len(llm.requests) == calls_before
    assert apply_writes(fake) == []


def test_head_moved_between_proposal_and_commit_aborts() -> None:
    fake, services, _, data = prepared()
    other = fake.add_commit(
        {"src/api/users.py": "pushed by someone\n"}, parents=[data["pull_request"]["head"]["sha"]]
    )

    def push_mid_apply(request: Any) -> None:
        if request.method == "POST" and request.url.path.endswith("/git/trees"):
            fake.refs[BRANCH] = other

    fake.before_request = push_mid_apply
    result = apply(services)
    assert result.status is RunStatus.FAILED
    assert "stale_head" in result.reason
    assert fake.refs[BRANCH] == other
    assert not [c for c in fake.calls if c.method == "PATCH"]
    assert "nothing was changed" in fake.issue_comments[PR][-1]["body"]


def test_review_of_older_sha_is_not_used() -> None:
    fake, services, llm, data = prepared()
    newer = fake.add_commit(
        {"src/api/users.py": "x\n"}, parents=[data["pull_request"]["head"]["sha"]]
    )
    fake.move_head(PR, newer)
    calls_before = len(llm.requests)
    result = apply(services)
    assert result.status is RunStatus.SKIPPED
    assert "older commit" in fake.issue_comments[PR][-1]["body"]
    assert len(llm.requests) == calls_before
    assert apply_writes(fake) == []


def test_spoofed_review_is_not_trusted() -> None:
    fake, services, _, _ = prepared()
    for review in fake.reviews[PR]:
        review["user"] = {"login": "mallory", "type": "User"}
    result = apply(services)
    assert result.status is RunStatus.SKIPPED
    assert "review` first" in fake.issue_comments[PR][-1]["body"]
    assert apply_writes(fake) == []


def test_unknown_id_and_duplicate_delivery() -> None:
    fake, services, _, _ = prepared()
    unknown = apply(services, "@pullbeacon apply PB-077", comment_id=601)
    assert unknown.status is RunStatus.SKIPPED
    first = apply(services, comment_id=602)
    again = apply(services, comment_id=602)
    assert first.status is RunStatus.COMPLETED
    assert again.status is RunStatus.SKIPPED
    assert sum(1 for c in fake.calls if c.method == "PATCH") == 1


def test_content_hash_mismatch_aborts() -> None:
    fake, services, _, _ = prepared()
    fake.files[PR][0]["sha"] = "0" * 40
    result = apply(services)
    assert result.status is RunStatus.FAILED
    assert "does not match the commit tree" in result.reason
    assert apply_writes(fake) == []


def test_file_no_longer_in_diff_is_skipped() -> None:
    fake, services, _, _ = prepared()
    fake.files[PR] = []
    result = apply(services)
    assert result.status is RunStatus.SKIPPED
    assert "not changed by this pull request" in fake.issue_comments[PR][-1]["body"]


def test_fork_is_refused() -> None:
    fake, services, _, _ = prepared()
    fake.pulls[PR]["head"]["repo"] = {
        "name": "shop",
        "full_name": "evil/shop",
        "owner": {"login": "evil"},
    }
    result = apply(services)
    assert result.status is RunStatus.SKIPPED
    assert apply_writes(fake) == []


def test_protected_branch_reports_without_mutation() -> None:
    fake, services, _, data = prepared()
    fake.protected_branches.add("feature/auth-bug")
    result = apply(services)
    assert result.status is RunStatus.FAILED
    assert "protected" in result.reason
    assert fake.refs[BRANCH] == data["pull_request"]["head"]["sha"]


def test_github_403_on_ref_update() -> None:
    fake, services, _, data = prepared()
    fake.failures[("PATCH", r"/git/refs/")] = [403]
    result = apply(services)
    assert result.status is RunStatus.FAILED
    assert result.commit_sha is None
    assert fake.refs[BRANCH] == data["pull_request"]["head"]["sha"]
    assert "applied" not in fake.issue_comments[PR][-1]["body"].split("**")[1]


def test_apply_disabled_in_base_config() -> None:
    _fake, services, llm, _ = prepared(
        config=APPLY_CONFIG.replace("apply_enabled: true", "apply_enabled: false")
    )
    calls_before = len(llm.requests)
    result = apply(services)
    assert result.status is RunStatus.SKIPPED
    assert len(llm.requests) == calls_before


def test_review_job_cannot_apply() -> None:
    fake, services, _, _ = prepared()
    services.apply_job = False
    result = apply(services)
    assert result.status is RunStatus.FAILED
    assert "apply job" in result.reason
    assert apply_writes(fake) == []


def test_invalid_model_proposal_aborts_whole_apply() -> None:
    fake, services, llm, _ = prepared()
    llm.responses["apply"] = [
        '{"edits": [{"path": ".github/workflows/ci.yml", "finding_ids": ["PB-001"], '
        '"replacements": [{"start_line": 1, "end_line": 1, "original": "x", "replacement": "y"}]}],'
        ' "explanation": "x"}'
    ]
    result = apply(services)
    assert result.status is RunStatus.FAILED
    assert "not provided" in result.reason
    assert apply_writes(fake) == []


def test_dry_run_validates_without_writing() -> None:
    fake, services, _, _ = prepared()
    writes_before = len(fake.writes())
    result = apply(services, inputs=ActionInputs(mode="apply", dry_run=True))
    assert result.status is RunStatus.COMPLETED
    assert result.details["files"] == ["src/api/users.py"]
    assert len(fake.writes()) == writes_before
