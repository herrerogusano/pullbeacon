"""Publish reviews and replies to GitHub, and recover PullBeacon state from them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pullbeacon.adapters.github import GitHubClient
from pullbeacon.core.markers import (
    ReviewMarker,
    decode_marker,
    encode_marker,
    idempotency_key,
    marker_from_report,
    replied_trigger,
    reply_marker,
)
from pullbeacon.core.render import finding_comment, review_summary
from pullbeacon.domain.errors import GitHubAPIError, StaleHeadError
from pullbeacon.domain.models import Finding, PullRequestSnapshot, ReviewReport
from pullbeacon.logging_utils import get_logger, log_event

DEFAULT_BOT_LOGIN = "github-actions[bot]"

log = get_logger("publisher")


@dataclass(frozen=True)
class TrustedReview:
    review_id: int
    commit_id: str
    url: str | None
    marker: ReviewMarker


@dataclass(frozen=True)
class PublishResult:
    review_id: int | None
    url: str | None
    inline_posted: int
    not_inline: tuple[tuple[str, str], ...]  # (finding id, reason)
    degraded: bool


def _is_bot(user: Any, bot_login: str) -> bool:
    return isinstance(user, dict) and user.get("login") == bot_login and user.get("type") == "Bot"


def trusted_reviews(
    client: GitHubClient, snapshot: PullRequestSnapshot, *, bot_login: str = DEFAULT_BOT_LOGIN
) -> list[TrustedReview]:
    """PullBeacon reviews authored by the bot identity, with a valid marker matching commit_id."""
    result: list[TrustedReview] = []
    for review in client.list_reviews(snapshot.owner, snapshot.repo, snapshot.number):
        if not _is_bot(review.get("user"), bot_login):
            continue
        marker = decode_marker(review.get("body") or "")
        commit_id = review.get("commit_id")
        if marker is None or not isinstance(commit_id, str) or marker.head_sha != commit_id:
            continue
        result.append(
            TrustedReview(
                review_id=int(review["id"]),
                commit_id=commit_id,
                url=review.get("html_url"),
                marker=marker,
            )
        )
    return sorted(result, key=lambda r: r.review_id)


def latest_review_for_head(reviews: list[TrustedReview], head_sha: str) -> TrustedReview | None:
    current = [r for r in reviews if r.commit_id == head_sha]
    return current[-1] if current else None


def automatic_review_exists(
    reviews: list[TrustedReview], head_sha: str, config_hash: str, prompt_version: str
) -> bool:
    key = idempotency_key(head_sha, config_hash, prompt_version, "automatic")
    return any(r.marker.mode == "automatic" and r.marker.auto_key == key for r in reviews)


def trigger_already_reviewed(reviews: list[TrustedReview], trigger: str) -> bool:
    return any(r.marker.trigger == trigger for r in reviews)


def trigger_already_replied(
    client: GitHubClient,
    snapshot: PullRequestSnapshot,
    trigger_comment_id: int,
    *,
    bot_login: str = DEFAULT_BOT_LOGIN,
) -> bool:
    for comment in client.list_issue_comments(snapshot.owner, snapshot.repo, snapshot.number):
        if _is_bot(comment.get("user"), bot_login) and (
            replied_trigger(comment.get("body") or "") == trigger_comment_id
        ):
            return True
    return False


def post_reply(
    client: GitHubClient,
    snapshot: PullRequestSnapshot,
    body: str,
    *,
    trigger_comment_id: int | None,
) -> dict[str, Any]:
    if trigger_comment_id is not None:
        body = f"{body}\n\n{reply_marker(trigger_comment_id)}"
    return client.create_issue_comment(snapshot.owner, snapshot.repo, snapshot.number, body)


class ReviewPublisher:
    def __init__(self, client: GitHubClient, *, apply_available: bool) -> None:
        self.client = client
        self.apply_available = apply_available

    def publish(
        self, snapshot: PullRequestSnapshot, report: ReviewReport, *, trigger: str
    ) -> PublishResult:
        current = self.client.get_pull_request(snapshot.owner, snapshot.repo, snapshot.number)
        if current.get("head", {}).get("sha") != report.head_sha:
            raise StaleHeadError(
                "the pull request head moved while the review ran; results were not published"
            )
        inline = [f for f in report.findings if f.inline and f.line and f.side]
        not_inline: list[tuple[Finding, str]] = [
            (f, "outside the diff or over the inline limit")
            for f in report.findings
            if f not in inline
        ]
        payload = self._payload(report, inline, not_inline, trigger)
        try:
            review = self.client.create_review(
                snapshot.owner, snapshot.repo, snapshot.number, payload
            )
            degraded = False
        except GitHubAPIError as exc:
            if exc.status != 422 or not inline:
                raise
            log_event(log, "inline_rejected_fallback", count=len(inline))
            demoted = [(f, "not_posted_inline: GitHub rejected the line") for f in inline]
            report = report.model_copy(
                update={
                    "findings": tuple(
                        f.model_copy(update={"inline": False}) if f in inline else f
                        for f in report.findings
                    )
                }
            )
            not_inline = demoted + not_inline
            inline = []
            payload = self._payload(report, inline, not_inline, trigger)
            review = self.client.create_review(
                snapshot.owner, snapshot.repo, snapshot.number, payload
            )
            degraded = True
        return PublishResult(
            review_id=review.get("id"),
            url=review.get("html_url"),
            inline_posted=len(inline),
            not_inline=tuple((f.id, reason) for f, reason in not_inline),
            degraded=degraded,
        )

    def _payload(
        self,
        report: ReviewReport,
        inline: list[Finding],
        not_inline: list[tuple[Finding, str]],
        trigger: str,
    ) -> dict[str, Any]:
        marker = encode_marker(marker_from_report(report, trigger=trigger))
        body = review_summary(
            report, not_inline=not_inline, apply_available=self.apply_available, marker=marker
        )
        # Never APPROVE / REQUEST_CHANGES in the MVP: humans decide.
        payload: dict[str, Any] = {"commit_id": report.head_sha, "event": "COMMENT", "body": body}
        if inline:
            payload["comments"] = [
                {
                    "path": f.path,
                    "line": f.line,
                    "side": f.side.value if f.side else "RIGHT",
                    "body": finding_comment(f, apply_available=self.apply_available),
                }
                for f in inline
            ]
        return payload
