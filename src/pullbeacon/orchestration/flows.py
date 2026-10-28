"""End-to-end flows (review, explain) over an injected GitHub client and LLM factory.

Order of checks is deliberate: everything that can reject a run without spending money
(PR state, fork, bots, config, idempotency) happens before any LLM call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pullbeacon.adapters.github import GitHubClient, fetch_snapshot
from pullbeacon.adapters.llm import LLMClient
from pullbeacon.adapters.publisher import (
    DEFAULT_BOT_LOGIN,
    ReviewPublisher,
    automatic_review_exists,
    latest_review_for_head,
    post_reply,
    trigger_already_replied,
    trigger_already_reviewed,
    trusted_reviews,
)
from pullbeacon.config import CONFIG_FILENAMES, PullBeaconConfig, default_config, parse_config
from pullbeacon.core.context import fetch_context_files
from pullbeacon.core.explain import explain
from pullbeacon.core.review import ReviewEngine
from pullbeacon.core.scope import select_scope
from pullbeacon.domain.errors import ConfigError, UnsupportedPullRequest
from pullbeacon.domain.models import (
    PullRequestSnapshot,
    ReviewMode,
    RunResult,
    RunStatus,
    canonical_json,
)
from pullbeacon.logging_utils import get_logger, log_event
from pullbeacon.prompts import REVIEW_PROMPT_VERSION

log = get_logger("flows")

LLMFactory = Callable[[PullBeaconConfig], LLMClient | None]
BOT_AUTHOR_PREFIXES = ("dependabot", "renovate")


@dataclass
class Services:
    client: GitHubClient
    llm_factory: LLMFactory
    bot_login: str = DEFAULT_BOT_LOGIN
    # Set by the workflow: whether this job has `contents: write` and is allowed to apply.
    apply_job: bool = False
    config_override: PullBeaconConfig | None = None


def load_trusted_config(client: GitHubClient, snapshot: PullRequestSnapshot) -> PullBeaconConfig:
    """Config from the PR *base* commit. A PR can never configure its own review."""
    for name in CONFIG_FILENAMES:
        raw = client.get_file_content(
            snapshot.owner, snapshot.repo, name, snapshot.base_sha, max_bytes=64 * 1024
        )
        if raw is not None:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ConfigError(f"{name} on the base branch is not UTF-8") from None
            return parse_config(text, source=f"{name} (base branch)")
    return default_config()


def check_supported(snapshot: PullRequestSnapshot) -> None:
    if snapshot.state != "open":
        raise UnsupportedPullRequest("the pull request is not open")
    if not snapshot.is_same_repo:
        raise UnsupportedPullRequest(
            "pull requests from forks are not supported in this version (no secrets are used "
            "for external contributions)"
        )
    login = snapshot.author.lower()
    if snapshot.author_type == "Bot" or login.startswith(BOT_AUTHOR_PREFIXES):
        raise UnsupportedPullRequest("pull requests opened by bots (e.g. Dependabot) are skipped")


def skipped(reason: str, **details: object) -> RunResult:
    return RunResult(status=RunStatus.SKIPPED, reason=reason, details=dict(details))


def run_review(
    services: Services,
    owner: str,
    repo: str,
    number: int,
    *,
    mode: ReviewMode,
    trigger: str,
    dry_run: bool = False,
) -> RunResult:
    client = services.client
    snapshot = fetch_snapshot(client, owner, repo, number)
    check_supported(snapshot)
    config = services.config_override or load_trusted_config(client, snapshot)

    if mode == "automatic":
        if not config.automatic_review:
            return skipped("automatic_review is disabled in the base-branch config")
        if snapshot.draft:
            return skipped("draft pull request")
    reviews = trusted_reviews(client, snapshot, bot_login=services.bot_login)
    if mode == "automatic" and automatic_review_exists(
        reviews, snapshot.head_sha, config.config_hash(), REVIEW_PROMPT_VERSION
    ):
        return skipped("this commit was already reviewed automatically", head=snapshot.head_sha)
    if mode == "manual" and trigger_already_reviewed(reviews, trigger):
        return skipped("duplicate delivery of the same command", trigger=trigger)

    llm = services.llm_factory(config)
    if llm is None:
        raise ConfigError("no model configured: add 'model' to .pullbeacon.yml on the base branch")
    scope = select_scope(snapshot, config)
    context, context_warnings = fetch_context_files(
        client, snapshot, config, budget=config.limits.max_context_chars // 4
    )
    report = ReviewEngine(llm, config).review(
        snapshot, scope, mode=mode, context_files=context, context_warnings=context_warnings
    )
    log_event(
        log,
        "review_completed",
        pr=f"{owner}/{repo}#{number}",
        head=snapshot.head_sha,
        findings=len(report.findings),
        calls=report.usage.llm_calls,
        cost_source=report.usage.cost_source,
    )
    if dry_run:
        return RunResult(
            status=RunStatus.COMPLETED,
            reason="dry run: review not published",
            reviewed_head_sha=snapshot.head_sha,
            usage=report.usage,
            details={"report": canonical_json(report, indent=None)},
        )
    publisher = ReviewPublisher(client, apply_available=config.policy.apply_enabled)
    published = publisher.publish(snapshot, report, trigger=trigger)
    status = RunStatus.PARTIAL if (published.degraded or report.truncated) else RunStatus.COMPLETED
    return RunResult(
        status=status,
        reason="review published" + (" (inline comments degraded)" if published.degraded else ""),
        reviewed_head_sha=snapshot.head_sha,
        review_url=published.url,
        usage=report.usage,
        details={
            "findings": len(report.findings),
            "inline_posted": published.inline_posted,
            "not_inline": [list(item) for item in published.not_inline],
            "truncated": report.truncated,
        },
    )


def run_explain(
    services: Services,
    owner: str,
    repo: str,
    number: int,
    *,
    finding_id: str,
    trigger_comment_id: int,
) -> RunResult:
    client = services.client
    snapshot = fetch_snapshot(client, owner, repo, number)
    check_supported(snapshot)
    if trigger_already_replied(client, snapshot, trigger_comment_id, bot_login=services.bot_login):
        return skipped("duplicate delivery of the same command")
    config = services.config_override or load_trusted_config(client, snapshot)
    reviews = trusted_reviews(client, snapshot, bot_login=services.bot_login)
    latest = latest_review_for_head(reviews, snapshot.head_sha)
    if latest is None:
        comment = post_reply(
            client,
            snapshot,
            f"PullBeacon has no review for the current commit `{snapshot.head_sha[:12]}`. "
            "Run `@pullbeacon review` first.",
            trigger_comment_id=trigger_comment_id,
        )
        return skipped("no PullBeacon review for the current head", comment=comment.get("html_url"))
    finding = next((f for f in latest.marker.findings if f.id == finding_id), None)
    if finding is None:
        known = ", ".join(f.id for f in latest.marker.findings) or "none"
        comment = post_reply(
            client,
            snapshot,
            f"Finding `{finding_id}` is not in the latest PullBeacon review (known: {known}).",
            trigger_comment_id=trigger_comment_id,
        )
        return skipped("unknown finding id", comment=comment.get("html_url"))

    scope = select_scope(snapshot, config)
    diff = scope.diff_for(finding.path) if finding.path else None
    llm = services.llm_factory(config)
    result = explain(
        finding,
        head_sha=snapshot.head_sha,
        diff=diff,
        llm=llm,
        max_output_tokens=config.limits.max_output_tokens,
        max_cost_usd=config.limits.max_estimated_cost_usd,
    )
    comment = post_reply(client, snapshot, result.body, trigger_comment_id=trigger_comment_id)
    return RunResult(
        status=RunStatus.COMPLETED,
        reason="explanation posted" + ("" if result.used_llm else " (from the original finding)"),
        reviewed_head_sha=snapshot.head_sha,
        comment_url=comment.get("html_url"),
        usage=result.usage,
    )
