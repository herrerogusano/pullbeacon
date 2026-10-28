"""Minimal GitHub REST client (httpx, injectable transport).

Token scopes needed (fine-grained / GITHUB_TOKEN permissions):
- review:  ``pull-requests: write`` (reviews, comments), ``contents: read`` (file content),
  ``issues: write`` (PR conversation comments); collaborator permission lookups work with the
  repository's own ``GITHUB_TOKEN``.
- apply:   additionally ``contents: write`` (Git Data API blobs/trees/commits/refs).

Every URL is built from validated components; pagination only follows ``Link`` URLs on the
configured API host, so the token is never sent anywhere else.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from pullbeacon.core.paths import UnsafePathError, normalize_repo_path
from pullbeacon.credentials import Secret
from pullbeacon.domain.errors import GitHubAPIError, PermissionDenied, RateLimitError
from pullbeacon.domain.models import SHA_PATTERN, ChangedFile, PullRequestSnapshot
from pullbeacon.logging_utils import get_logger, log_event

API_VERSION = "2026-03-10"
DEFAULT_BASE_URL = "https://api.github.com"
USER_AGENT = "pullbeacon"
MAX_FILES_API = 3000  # GitHub's documented ceiling for GET /pulls/{n}/files
MAX_RETRY_WAIT_SECONDS = 60.0
WRITE_PERMISSIONS = frozenset({"admin", "write"})

_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,38}|[A-Za-z0-9\-]{0,30}\[bot\])$")
_SHA = re.compile(SHA_PATTERN)
_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')

log = get_logger("github")


def _check_name(value: str, what: str) -> str:
    if not _NAME.match(value) or value in (".", ".."):
        raise GitHubAPIError(f"invalid {what}")
    return value


def repo_path(owner: str, repo: str) -> str:
    return f"/repos/{_check_name(owner, 'owner')}/{_check_name(repo, 'repo')}"


def _check_sha(value: str) -> str:
    if not _SHA.match(value):
        raise GitHubAPIError("invalid commit SHA")
    return value


def _check_login(value: str) -> str:
    if not _LOGIN.match(value):
        raise GitHubAPIError("invalid GitHub login")
    return value


class GitHubClient:
    def __init__(
        self,
        token: Secret | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = urlsplit(self._base_url).netloc
        self._max_retries = max_retries
        self._sleep = sleep
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token.reveal()}"
        self._http = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ transport

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json: Any = None,
        expected: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        # Relative API paths, or absolute URLs already validated by ``_next_link``.
        if not path.startswith("/") and not path.startswith(self._base_url + "/"):
            raise GitHubAPIError("refusing to call a non-API URL")
        url = path
        safe_path = urlsplit(url).path
        attempt = 0
        while True:
            try:
                response = self._http.request(method, url, params=params, json=json)
            except httpx.TimeoutException:
                if attempt < self._max_retries:
                    attempt += 1
                    self._sleep(min(2.0**attempt, MAX_RETRY_WAIT_SECONDS))
                    continue
                raise GitHubAPIError(f"GitHub API {method} {safe_path} timed out") from None
            except httpx.HTTPError:
                raise GitHubAPIError(f"GitHub API {method} {safe_path} network error") from None

            if response.status_code in expected:
                return response
            wait = self._retry_wait(response)
            if wait is not None and attempt < self._max_retries:
                attempt += 1
                log_event(
                    log, "github_retry", status=response.status_code, path=safe_path, wait=wait
                )
                self._sleep(wait)
                continue
            raise self._error(method, safe_path, response)

    def _retry_wait(self, response: httpx.Response) -> float | None:
        status = response.status_code
        rate_limited = status == 429 or (
            status == 403
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
            )
        )
        if not rate_limited and status not in (500, 502, 503, 504):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), MAX_RETRY_WAIT_SECONDS)
            except ValueError:
                try:
                    delta = parsedate_to_datetime(retry_after) - datetime.now(UTC)
                    return min(max(delta.total_seconds(), 0.0), MAX_RETRY_WAIT_SECONDS)
                except (TypeError, ValueError):
                    return 5.0
        reset = response.headers.get("x-ratelimit-reset")
        if rate_limited and reset and reset.isdigit():
            wait = int(reset) - time.time()
            # Waiting longer than the cap is pointless inside a CI job: fail instead.
            return max(wait, 0.0) if wait <= MAX_RETRY_WAIT_SECONDS else None
        return 1.0 if not rate_limited else 5.0

    @staticmethod
    def _error(method: str, path: str, response: httpx.Response) -> GitHubAPIError:
        status = response.status_code
        message = ""
        try:
            body = response.json()
            if isinstance(body, dict) and isinstance(body.get("message"), str):
                message = body["message"][:200]
        except ValueError:
            pass
        text = f"GitHub API {method} {path} returned {status}" + (f": {message}" if message else "")
        if status == 429 or (status == 403 and "rate limit" in message.lower()):
            return RateLimitError(text, status=status)
        return GitHubAPIError(text, status=status)

    def _paginate(
        self, path: str, *, params: Mapping[str, str | int] | None = None, max_items: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Follow ``Link: rel=next``. Returns ``(items, truncated)``."""
        items: list[dict[str, Any]] = []
        url: str | None = path
        query: Mapping[str, str | int] | None = {"per_page": 100, **(params or {})}
        while url is not None:
            response = self.request("GET", url, params=query)
            page = response.json()
            if not isinstance(page, list):
                raise GitHubAPIError(f"GitHub API GET {urlsplit(url).path} returned a non-list")
            items.extend(page)
            if len(items) >= max_items:
                return items[:max_items], True
            url = self._next_link(response.headers.get("link"))
            query = None  # the next URL already carries its query string
        return items, False

    def _next_link(self, header: str | None) -> str | None:
        if not header:
            return None
        match = _LINK_NEXT.search(header)
        if not match:
            return None
        url = match.group(1)
        parts = urlsplit(url)
        if parts.scheme != urlsplit(self._base_url).scheme or parts.netloc != self._host:
            raise GitHubAPIError("pagination link points outside the GitHub API host")
        return url

    # ------------------------------------------------------------------ reads

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        path = f"{repo_path(owner, repo)}/pulls/{number}"
        data = self.request("GET", path).json()
        if not isinstance(data, dict):
            raise GitHubAPIError("unexpected pull request payload")
        return data

    def list_pull_files(
        self, owner: str, repo: str, number: int, *, max_files: int = MAX_FILES_API
    ) -> tuple[list[dict[str, Any]], bool]:
        path = f"{repo_path(owner, repo)}/pulls/{number}/files"
        return self._paginate(path, max_items=min(max_files, MAX_FILES_API))

    def get_file_content(
        self, owner: str, repo: str, path: str, ref_sha: str, *, max_bytes: int
    ) -> bytes | None:
        """File bytes at an exact commit SHA, or ``None`` if missing/not a regular file/too big."""
        try:
            safe = normalize_repo_path(path)
        except UnsafePathError:
            raise GitHubAPIError("refusing to fetch an unsafe path") from None
        _check_sha(ref_sha)
        encoded = "/".join(quote(part, safe="") for part in safe.split("/"))
        api_path = f"{repo_path(owner, repo)}/contents/{encoded}"
        response = self.request("GET", api_path, params={"ref": ref_sha}, expected=(200, 404))
        if response.status_code == 404:
            return None
        data = response.json()
        if not isinstance(data, dict) or data.get("type") != "file":
            return None  # directory, symlink or submodule
        size = data.get("size")
        if not isinstance(size, int) or size > max_bytes:
            return None
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            return None
        content = base64.b64decode(data["content"])
        if len(content) != size:
            return None
        return content

    def get_collaborator_permission(self, owner: str, repo: str, username: str) -> str:
        """Effective legacy permission: ``admin``/``write``/``read``/``none``.

        ``maintain`` maps to ``write`` and ``triage`` to ``read`` in this field; custom roles are
        mapped to their base role by GitHub. 404 (not a collaborator) is ``none``.
        """
        path = (
            f"{repo_path(owner, repo)}"
            f"/collaborators/{quote(_check_login(username), safe='')}/permission"
        )
        response = self.request("GET", path, expected=(200, 404))
        if response.status_code == 404:
            return "none"
        data = response.json()
        permission = data.get("permission") if isinstance(data, dict) else None
        return permission if isinstance(permission, str) else "none"

    def require_write_permission(self, owner: str, repo: str, username: str) -> str:
        try:
            permission = self.get_collaborator_permission(owner, repo, username)
        except GitHubAPIError as exc:
            raise PermissionDenied(f"could not verify permission for {username}") from exc
        if permission not in WRITE_PERMISSIONS:
            raise PermissionDenied(f"{username} has '{permission}' access; write is required")
        return permission

    def list_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        path = f"{repo_path(owner, repo)}/pulls/{number}/reviews"
        items, _ = self._paginate(path, max_items=1000)
        return items

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        path = f"{repo_path(owner, repo)}/issues/{number}/comments"
        items, _ = self._paginate(path, max_items=1000)
        return items

    # ------------------------------------------------------------------ writes

    def create_review(
        self, owner: str, repo: str, number: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        path = f"{repo_path(owner, repo)}/pulls/{number}/reviews"
        data = self.request("POST", path, json=payload, expected=(200, 201)).json()
        return data if isinstance(data, dict) else {}

    def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
        path = f"{repo_path(owner, repo)}/issues/{number}/comments"
        data = self.request("POST", path, json={"body": body}, expected=(201,)).json()
        return data if isinstance(data, dict) else {}


def snapshot_from_api(
    pr: Mapping[str, Any],
    files: list[dict[str, Any]],
    *,
    files_truncated: bool,
    fetched_at: datetime | None = None,
) -> PullRequestSnapshot:
    """Build an immutable snapshot pinned to ``pr.head.sha`` (never the workflow's GITHUB_SHA)."""
    try:
        base_repo = pr["base"]["repo"]
        head_repo = pr["head"].get("repo") or None
        changed = tuple(
            ChangedFile(
                path=f["filename"],
                status=f.get("status", "modified"),
                previous_path=f.get("previous_filename"),
                patch=f.get("patch"),
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                changes=f.get("changes", 0),
                blob_sha=f.get("sha"),
            )
            for f in files
        )
        changed_files = pr.get("changed_files")
        truncated = files_truncated or (
            isinstance(changed_files, int) and changed_files > len(changed)
        )
        return PullRequestSnapshot(
            owner=base_repo["owner"]["login"],
            repo=base_repo["name"],
            number=pr["number"],
            title=pr.get("title") or "",
            body=pr.get("body") or "",
            author=pr["user"]["login"],
            author_type=pr["user"].get("type", "User"),
            state=pr["state"],
            draft=bool(pr.get("draft", False)),
            base_ref=pr["base"]["ref"],
            base_sha=pr["base"]["sha"],
            head_ref=pr["head"]["ref"],
            head_sha=pr["head"]["sha"],
            base_repo_full_name=base_repo["full_name"],
            head_repo_full_name=head_repo["full_name"] if head_repo else None,
            files=changed,
            files_truncated=truncated,
            fetched_at=fetched_at or datetime.now(UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubAPIError("pull request payload is missing required fields") from exc


def fetch_snapshot(
    client: GitHubClient, owner: str, repo: str, number: int, *, max_files: int = MAX_FILES_API
) -> PullRequestSnapshot:
    pr = client.get_pull_request(owner, repo, number)
    files, truncated = client.list_pull_files(owner, repo, number, max_files=max_files)
    return snapshot_from_api(pr, files, files_truncated=truncated)
