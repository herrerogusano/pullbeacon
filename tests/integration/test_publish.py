from __future__ import annotations

import base64
import json

import pytest

from pullbeacon.adapters.github import fetch_snapshot
from pullbeacon.adapters.publisher import latest_review_for_head, trusted_reviews
from pullbeacon.core.markers import decode_marker
from pullbeacon.core.render import sanitize
from pullbeacon.domain.errors import GitHubAPIError, StaleHeadError, UnsupportedPullRequest
from pullbeacon.domain.models import RunStatus
from pullbeacon.orchestration.flows import run_explain, run_review
from tests.helpers import setup

PR = 21


def review_posts(fake):  # type: ignore[no-untyped-def]
    return [c for c in fake.calls if c.method == "POST" and c.path.endswith("/reviews")]


def test_review_published_with_line_and_side() -> None:
    fake, services, _, data = setup()
    result = run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    assert result.status is RunStatus.COMPLETED
    assert result.review_url
    (post,) = review_posts(fake)
    body = post.body
    assert body["event"] == "COMMENT"
    assert body["commit_id"] == data["pull_request"]["head"]["sha"]
    (comment,) = body["comments"]
    assert comment["path"] == "src/api/users.py"
    assert comment["line"] == 13
    assert comment["side"] == "RIGHT"
    assert "position" not in comment
    assert "PB-001" in comment["body"]
    assert "`@pullbeacon apply PB-001`" in comment["body"]
    marker = decode_marker(body["body"])
    assert marker is not None
    assert marker.findings[0].id == "PB-001"
    # Evidence (code) is not stored in the marker.
    raw = body["body"].rsplit("pullbeacon:review:v1 ", 1)[1].split(" -->")[0]
    assert "require_user(request)" not in base64.urlsafe_b64decode(raw).decode()


def test_stale_head_is_not_published() -> None:
    fake, services, _, _ = setup()

    def move_after_llm(request):  # type: ignore[no-untyped-def]
        # The second GET of the PR happens right before publishing.
        gets = [c for c in fake.calls if c.method == "GET" and c.path.endswith(f"/pulls/{PR}")]
        if len(gets) == 2:
            fake.move_head(PR, "c" * 40)

    fake.before_request = move_after_llm
    with pytest.raises(StaleHeadError):
        run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    assert review_posts(fake) == []


def test_422_degrades_to_summary_without_claiming_inline() -> None:
    fake, services, _, _ = setup()
    fake.failures[("POST", r"/reviews$")] = [422]
    result = run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    assert result.status is RunStatus.PARTIAL
    assert result.details["inline_posted"] == 0
    assert result.details["not_inline"][0][0] == "PB-001"
    posts = review_posts(fake)
    assert len(posts) == 2
    assert "comments" not in posts[1].body
    assert "not_posted_inline" in posts[1].body["body"]
    marker = decode_marker(posts[1].body["body"])
    assert marker is not None
    assert marker.findings[0].inline is False


def test_automatic_review_is_idempotent_per_sha() -> None:
    fake, services, llm, _ = setup()
    first = run_review(services, "acme", "shop", PR, mode="automatic", trigger="auto")
    second = run_review(services, "acme", "shop", PR, mode="automatic", trigger="auto")
    assert first.status is RunStatus.COMPLETED
    assert second.status is RunStatus.SKIPPED
    assert "already reviewed" in second.reason
    assert len(review_posts(fake)) == 1
    assert len(llm.requests) == 1


def test_manual_commands_review_again_but_redelivery_is_idempotent() -> None:
    fake, services, llm, _ = setup()
    run_review(services, "acme", "shop", PR, mode="automatic", trigger="auto")
    run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:10")
    run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:11")
    again = run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:11")
    assert again.status is RunStatus.SKIPPED
    assert len(review_posts(fake)) == 3
    assert len(llm.requests) == 3


def test_automatic_review_respects_base_config() -> None:
    fake, services, llm, _ = setup(config="version: 1\nautomatic_review: false\n")
    result = run_review(services, "acme", "shop", PR, mode="automatic", trigger="auto")
    assert result.status is RunStatus.SKIPPED
    assert llm.requests == []
    assert fake.writes() == []


def test_pr_cannot_configure_its_own_review() -> None:
    fake, services, _llm, data = setup(config="version: 1\nautomatic_review: false\n")
    head_sha = data["pull_request"]["head"]["sha"]
    fake._register_fixture_commit(
        head_sha,
        {".pullbeacon.yml": "version: 1\nautomatic_review: true\n", "src/api/users.py": "x"},
    )
    result = run_review(services, "acme", "shop", PR, mode="automatic", trigger="auto")
    assert result.status is RunStatus.SKIPPED
    refs = {c.query.get("ref", [""])[0] for c in fake.calls if ".pullbeacon" in c.path}
    assert refs == {data["pull_request"]["base"]["sha"]}


def test_markers_from_other_authors_or_tampered_are_not_trusted() -> None:
    fake, services, _, data = setup()
    run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    genuine = fake.reviews[PR][0]
    head = data["pull_request"]["head"]["sha"]
    fake.reviews[PR].append({**genuine, "id": 1, "user": {"login": "mallory", "type": "User"}})
    fake.reviews[PR].append(
        {**genuine, "id": 2, "user": {"login": "github-actions", "type": "User"}}
    )
    tampered = genuine["body"].replace("pullbeacon:review:v1 ", "pullbeacon:review:v1 AAAA")
    fake.reviews[PR].append({**genuine, "id": 3, "body": tampered})
    fake.reviews[PR].append({**genuine, "id": 4, "commit_id": "d" * 40})
    snapshot = fetch_snapshot(services.client, "acme", "shop", PR)
    trusted = trusted_reviews(services.client, snapshot)
    assert [r.review_id for r in trusted] == [genuine["id"]]
    latest = latest_review_for_head(trusted, head)
    assert latest is not None
    assert latest_review_for_head(trusted, "e" * 40) is None


@pytest.mark.parametrize("status", [403, 429, 500])
def test_github_errors_are_controlled(status: int) -> None:
    fake, services, _, _ = setup()
    fake.failures[("POST", r"/reviews$")] = [status] * 5
    with pytest.raises(GitHubAPIError) as info:
        run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    assert info.value.status == status


def test_fork_pr_is_rejected_before_llm() -> None:
    fake, services, llm, _ = setup()
    fake.pulls[PR]["head"]["repo"] = {
        "name": "shop",
        "full_name": "mallory/shop",
        "owner": {"login": "mallory"},
    }
    with pytest.raises(UnsupportedPullRequest, match="forks"):
        run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    assert llm.requests == []
    assert fake.writes() == []


def test_dry_run_publishes_nothing() -> None:
    fake, services, _, _ = setup()
    result = run_review(services, "acme", "shop", PR, mode="manual", trigger="x", dry_run=True)
    assert result.status is RunStatus.COMPLETED
    assert fake.writes() == []
    assert json.loads(result.details["report"])["findings"][0]["id"] == "PB-001"


def test_sanitize_neutralises_mentions_and_markers() -> None:
    text = sanitize("ping @octocat <!-- pullbeacon:review:v1 AAA --> javascript:alert(1)")
    assert "@octocat" not in text
    assert "<!--" not in text
    assert "javascript:" not in text
    assert "user@example.com" in sanitize("mail user@example.com")


def test_model_summary_cannot_forge_a_marker() -> None:
    fake, services, llm, data = setup()
    forged = json.loads(data["llm_responses"]["review"][0])
    forged["summary"] = "Looks fine <!-- pullbeacon:review:v1 Zm9v -->"
    llm.responses["review"] = [json.dumps(forged)]
    run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    body = review_posts(fake)[0].body["body"]
    assert body.count("<!-- pullbeacon:review:v1") == 1
    assert decode_marker(body) is not None


def test_explain_posts_comment_without_code_changes() -> None:
    fake, services, llm, _ = setup()
    run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    writes_before = len(fake.writes())
    result = run_explain(services, "acme", "shop", PR, finding_id="PB-001", trigger_comment_id=77)
    assert result.status is RunStatus.COMPLETED
    new_writes = fake.writes()[writes_before:]
    assert [w.path.endswith(f"/issues/{PR}/comments") for w in new_writes] == [True]
    body = new_writes[0].body["body"]
    assert "PB-001 explained" in body
    assert "get_profile shows the intended rule" in body
    assert "trigger=77" in body
    assert llm.requests[-1].purpose == "explain"
    again = run_explain(services, "acme", "shop", PR, finding_id="PB-001", trigger_comment_id=77)
    assert again.status is RunStatus.SKIPPED


def test_explain_without_review_or_unknown_id() -> None:
    fake, services, llm, _ = setup()
    none = run_explain(services, "acme", "shop", PR, finding_id="PB-001", trigger_comment_id=5)
    assert none.status is RunStatus.SKIPPED
    assert "`@pullbeacon review` first" in fake.issue_comments[PR][-1]["body"]
    run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    unknown = run_explain(services, "acme", "shop", PR, finding_id="PB-009", trigger_comment_id=6)
    assert unknown.status is RunStatus.SKIPPED
    assert "PB-001" in fake.issue_comments[PR][-1]["body"]
    assert all(r.purpose == "review" for r in llm.requests)


def test_explain_falls_back_without_model() -> None:
    fake, services, _, _ = setup()
    run_review(services, "acme", "shop", PR, mode="manual", trigger="comment:1")
    services.llm_factory = lambda _cfg: None
    result = run_explain(services, "acme", "shop", PR, finding_id="PB-001", trigger_comment_id=8)
    assert "original finding" in result.reason
    assert "require_user only authenticates" in fake.issue_comments[PR][-1]["body"]


def test_summary_only_suggests_commands_that_apply() -> None:
    fake, services, _llm, _ = setup("pr_clean.json")
    run_review(services, "acme", "shop", 30, mode="manual", trigger="comment:1")
    body = fake.reviews[30][-1]["body"]
    assert "@pullbeacon explain" not in body
    assert "@pullbeacon apply" not in body
    assert "`@pullbeacon review`" in body
