from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from pullbeacon.adapters.github import (
    API_VERSION,
    GitHubClient,
    fetch_snapshot,
    snapshot_from_api,
)
from pullbeacon.credentials import Secret
from pullbeacon.domain.errors import GitHubAPIError, PermissionDenied, RateLimitError
from pullbeacon.testing.fake_github import FakeGitHub

FIXTURES = Path(__file__).parents[1] / "fixtures"
TOKEN = "ghs_" + "T" * 36
API = "https://api.github.com"


def load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def client_for(fake: FakeGitHub, **kwargs: object) -> GitHubClient:
    return GitHubClient(Secret(TOKEN), transport=fake.transport(), sleep=lambda _s: None, **kwargs)  # type: ignore[arg-type]


def make_files(count: int) -> list[dict[str, object]]:
    return [
        {
            "filename": f"src/f{i:04}.py",
            "status": "modified",
            "patch": "@@ -1 +1 @@\n-a\n+b",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
        }
        for i in range(count)
    ]


def fake_with_files(count: int) -> FakeGitHub:
    fake = FakeGitHub()
    base = fake.add_commit({"a": "1"})
    head = fake.add_commit({"a": "2"}, parents=[base])
    fake.add_pull(5, base_sha=base, head_sha=head, files=make_files(count))
    return fake


def test_headers_and_auth() -> None:
    fake = fake_with_files(1)
    seen: dict[str, str] = {}

    def capture(request: httpx.Request) -> None:
        seen.update(request.headers)

    fake.before_request = capture
    with client_for(fake) as client:
        client.get_pull_request("acme", "shop", 5)
    assert seen["authorization"] == f"Bearer {TOKEN}"
    assert seen["x-github-api-version"] == API_VERSION
    assert seen["accept"] == "application/vnd.github+json"


def test_pagination_two_pages() -> None:
    fake = fake_with_files(150)
    with client_for(fake) as client:
        files, truncated = client.list_pull_files("acme", "shop", 5)
    assert len(files) == 150
    assert truncated is False
    pages = [c for c in fake.calls if c.path.endswith("/files")]
    assert len(pages) == 2


def test_pagination_respects_max_and_reports_truncation() -> None:
    fake = fake_with_files(250)
    with client_for(fake) as client:
        files, truncated = client.list_pull_files("acme", "shop", 5, max_files=120)
    assert len(files) == 120
    assert truncated is True


def test_api_file_ceiling_marks_snapshot_partial() -> None:
    fake = fake_with_files(10)
    fake.pulls[5]["changed_files"] = 3500  # GitHub only lists the first 3000
    with client_for(fake) as client:
        snapshot = fetch_snapshot(client, "acme", "shop", 5)
    assert snapshot.files_truncated is True


def test_pagination_refuses_foreign_hosts() -> None:
    with respx.mock(base_url=API) as mock:
        mock.get("/repos/acme/shop/pulls/1/files").respond(
            200, json=[], headers={"link": '<https://evil.example/steal?page=2>; rel="next"'}
        )
        with (
            GitHubClient(Secret(TOKEN)) as client,
            pytest.raises(GitHubAPIError, match="outside"),
        ):
            client.list_pull_files("acme", "shop", 1)


def test_retries_on_5xx_then_succeeds() -> None:
    fake = fake_with_files(1)
    fake.failures[("GET", r"/pulls/5$")] = [502, 503]
    with client_for(fake) as client:
        assert client.get_pull_request("acme", "shop", 5)["number"] == 5


def test_retries_are_bounded() -> None:
    fake = fake_with_files(1)
    fake.failures[("GET", r"/pulls/5$")] = [500, 500, 500]
    with client_for(fake) as client, pytest.raises(GitHubAPIError) as info:
        client.get_pull_request("acme", "shop", 5)
    assert info.value.status == 500
    assert TOKEN not in str(info.value)


def test_rate_limit_uses_retry_after() -> None:
    waits: list[float] = []
    with respx.mock(base_url=API) as mock:
        route = mock.get("/repos/acme/shop/pulls/1")
        route.side_effect = [
            httpx.Response(429, headers={"retry-after": "3"}, json={"message": "slow down"}),
            httpx.Response(200, json={"number": 1}),
        ]
        with GitHubClient(Secret(TOKEN), sleep=waits.append) as client:
            client.get_pull_request("acme", "shop", 1)
    assert waits == [3.0]


def test_rate_limit_exhausted_raises_rate_limit_error() -> None:
    with respx.mock(base_url=API) as mock:
        mock.get("/repos/acme/shop/pulls/1").respond(
            429, headers={"retry-after": "1"}, json={"message": "API rate limit exceeded"}
        )
        with (
            GitHubClient(Secret(TOKEN), sleep=lambda _s: None) as client,
            pytest.raises(RateLimitError),
        ):
            client.get_pull_request("acme", "shop", 1)


def test_4xx_is_not_retried() -> None:
    fake = fake_with_files(1)
    with client_for(fake) as client, pytest.raises(GitHubAPIError) as info:
        client.get_pull_request("acme", "shop", 999)
    assert info.value.status == 404
    assert len(fake.calls) == 1


def test_timeout_is_retried_then_reported() -> None:
    with respx.mock(base_url=API) as mock:
        mock.get("/repos/acme/shop/pulls/1").mock(side_effect=httpx.ConnectTimeout("t"))
        with (
            GitHubClient(Secret(TOKEN), sleep=lambda _s: None) as client,
            pytest.raises(GitHubAPIError, match="timed out"),
        ):
            client.get_pull_request("acme", "shop", 1)


@pytest.mark.parametrize("owner", ["../x", "a/b", "", "a b", "x?y"])
def test_invalid_names_are_rejected_before_any_request(owner: str) -> None:
    fake = fake_with_files(1)
    with client_for(fake) as client, pytest.raises(GitHubAPIError):
        client.get_pull_request(owner, "shop", 5)
    assert fake.calls == []


def test_file_content_at_exact_sha_and_size_limit() -> None:
    fake = FakeGitHub()
    base = fake.add_commit({"src/a.py": "print('base')\n"})
    head = fake.add_commit({"src/a.py": "print('head')\n" * 10}, parents=[base])
    with client_for(fake) as client:
        assert client.get_file_content("acme", "shop", "src/a.py", base, max_bytes=1000) == (
            b"print('base')\n"
        )
        assert client.get_file_content("acme", "shop", "src/a.py", head, max_bytes=50) is None
        assert client.get_file_content("acme", "shop", "missing.py", head, max_bytes=50) is None
        with pytest.raises(GitHubAPIError, match="SHA"):
            client.get_file_content("acme", "shop", "src/a.py", "main", max_bytes=1000)
        with pytest.raises(GitHubAPIError, match="unsafe"):
            client.get_file_content("acme", "shop", "../etc/passwd", head, max_bytes=1000)
    content_calls = [c for c in fake.calls if "/contents/" in c.path]
    assert all(c.query["ref"][0] in (base, head) for c in content_calls)


def test_symlinks_are_not_returned_as_files() -> None:
    fake = FakeGitHub()
    base = fake.add_commit({"src/a.py": "x"})
    with_link = fake.add_symlink(base, "src/link.py", "/etc/passwd")
    with client_for(fake) as client:
        assert (
            client.get_file_content("acme", "shop", "src/link.py", with_link, max_bytes=100) is None
        )


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("admin", True), ("maintain", True), ("write", True), ("triage", False), ("read", False)],
)
def test_collaborator_permission(role: str, allowed: bool) -> None:
    fake = fake_with_files(1)
    fake.permissions["someone"] = role
    with client_for(fake) as client:
        if allowed:
            client.require_write_permission("acme", "shop", "someone")
        else:
            with pytest.raises(PermissionDenied):
                client.require_write_permission("acme", "shop", "someone")


def test_permission_404_and_403_are_denied() -> None:
    fake = fake_with_files(1)
    fake.failures[("GET", r"/collaborators/blocked/permission")] = [403]
    with client_for(fake) as client:
        assert client.get_collaborator_permission("acme", "shop", "stranger") == "none"
        with pytest.raises(PermissionDenied):
            client.require_write_permission("acme", "shop", "stranger")
        with pytest.raises(PermissionDenied, match="could not verify"):
            client.require_write_permission("acme", "shop", "blocked")


def test_snapshot_uses_pr_head_not_workflow_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    data = load("pr_two_hunks.json")
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)  # default-branch SHA in issue_comment events
    fake = FakeGitHub.from_fixture(data)  # type: ignore[arg-type]
    with client_for(fake) as client:
        snapshot = fetch_snapshot(client, "acme", "shop", 12)
    assert snapshot.head_sha == data["pull_request"]["head"]["sha"]  # type: ignore[index]
    assert snapshot.head_sha != "f" * 40
    assert snapshot.is_same_repo
    assert snapshot.head_ref == "feature/two-hunks"


def test_snapshot_rejects_incomplete_payload() -> None:
    with pytest.raises(GitHubAPIError, match="missing required fields"):
        snapshot_from_api({"number": 1}, [], files_truncated=False)


def test_snapshot_of_fork_and_deleted_head_repo() -> None:
    fake = FakeGitHub()
    base = fake.add_commit({"a": "1"})
    head = fake.add_commit({"a": "2"}, parents=[base])
    fake.add_pull(1, base_sha=base, head_sha=head, files=[], head_repo="mallory/shop")
    fake.add_pull(2, base_sha=base, head_sha=head, files=[], head_repo="")
    with client_for(fake) as client:
        assert not fetch_snapshot(client, "acme", "shop", 1).is_same_repo
        assert not fetch_snapshot(client, "acme", "shop", 2).is_same_repo
