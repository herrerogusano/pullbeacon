"""`@pullbeacon apply [PB-NNN]`: authorized, bounded fixes committed through the Git Data API.

Fail-closed sequence: job/permission/PR checks -> trusted review for the current head ->
deterministic finding eligibility -> exact-SHA file reads (blob-verified) -> LLM proposal ->
strict validation -> permission re-check -> fast-forward-only commit -> truthful reply.
PullBeacon never runs tests or CI, and never claims it did.
"""

from __future__ import annotations

from pullbeacon.adapters.git_commit import commit_edits, verify_blob
from pullbeacon.adapters.github import check_branch, fetch_snapshot
from pullbeacon.adapters.publisher import (
    latest_review_for_head,
    post_reply,
    trigger_already_replied,
    trusted_reviews,
)
from pullbeacon.core.apply import (
    ApplyPlan,
    SourceFile,
    build_apply_request,
    select_findings,
    validate_proposal,
)
from pullbeacon.core.render import DISCLAIMER, sanitize
from pullbeacon.core.review import CostGuard, call_json
from pullbeacon.domain.errors import ApplyRejected, ConfigError, GitHubAPIError
from pullbeacon.domain.models import FileStatus, PullRequestSnapshot, RunResult, RunStatus
from pullbeacon.orchestration.events import Task
from pullbeacon.orchestration.flows import Services, check_supported

NO_CI_NOTE = (
    "PullBeacon did **not** run tests, builds or CI. Commits pushed with the workflow's "
    "`GITHUB_TOKEN` usually do not trigger other workflows, so CI may not have run on this "
    "commit. Comment `@pullbeacon review` to review the new commit."
)


def _skips(items: list[tuple[str, str]]) -> list[str]:
    return [f"- `{fid}`: {sanitize(reason, 200)}" for fid, reason in items]


def _reply(services: Services, snapshot: PullRequestSnapshot, task: Task, text: str) -> str | None:
    comment = post_reply(services.client, snapshot, text, trigger_comment_id=task.comment_id)
    url = comment.get("html_url")
    return url if isinstance(url, str) else None


def _stop(
    services: Services, snapshot: PullRequestSnapshot, task: Task, text: str, reason: str
) -> RunResult:
    url = _reply(services, snapshot, task, f"**PullBeacon apply: nothing changed.** {text}")
    return RunResult(status=RunStatus.SKIPPED, reason=reason, comment_url=url)


def run_apply(services: Services, task: Task, *, dry_run: bool) -> RunResult:
    client = services.client
    if not services.apply_job:
        raise ApplyRejected("apply must run in the dedicated apply job (contents: write)")
    if task.comment_id is None or not task.actor:
        raise ApplyRejected("apply must be requested by a PR comment from a person")

    snapshot = fetch_snapshot(client, task.owner, task.repo, task.number)
    check_supported(snapshot)
    check_branch(snapshot.head_ref)
    if trigger_already_replied(client, snapshot, task.comment_id, bot_login=services.bot_login):
        return RunResult(status=RunStatus.SKIPPED, reason="duplicate delivery of the same command")

    config = services.config_for(snapshot)
    if not config.policy.apply_enabled:
        return _stop(
            services,
            snapshot,
            task,
            "`apply` is disabled (`policy.apply_enabled: false` on the base branch).",
            "apply disabled",
        )

    reviews = trusted_reviews(client, snapshot, bot_login=services.bot_login)
    latest = latest_review_for_head(reviews, snapshot.head_sha)
    if latest is None:
        older = " The latest review is for an older commit." if reviews else ""
        return _stop(
            services,
            snapshot,
            task,
            f"There is no PullBeacon review for commit `{snapshot.head_sha[:12]}`.{older} "
            "Run `@pullbeacon review` first.",
            "no review for current head",
        )

    eligible, skipped = select_findings(latest.marker.findings, task.finding_id, config)
    changed_paths = {
        f.path for f in snapshot.files if f.status not in (FileStatus.REMOVED, FileStatus.UNCHANGED)
    }
    outside = [f for f in eligible if f.path not in changed_paths]
    skipped += [(f.id, "file is not changed by this pull request") for f in outside]
    eligible = [f for f in eligible if f not in outside]
    if not eligible:
        return _stop(
            services,
            snapshot,
            task,
            "No finding is eligible for an automatic fix:\n\n" + "\n".join(_skips(skipped)),
            "no eligible findings",
        )

    sources: list[SourceFile] = []
    for path in sorted({f.path for f in eligible if f.path}):
        blob_sha = next((f.blob_sha for f in snapshot.files if f.path == path), None)
        raw = client.get_file_content(
            snapshot.owner,
            snapshot.repo,
            path,
            snapshot.head_sha,
            max_bytes=config.limits.max_file_bytes,
        )
        if raw is None or blob_sha is None:
            raise ApplyRejected(f"{path} could not be read safely (missing, symlink or too large)")
        verify_blob(raw, blob_sha)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ApplyRejected(f"{path} is not UTF-8 text") from None
        if text.startswith("﻿"):
            raise ApplyRejected(f"{path} has a byte-order mark; refusing to rewrite its encoding")
        sources.append(SourceFile(path=path, content=text, blob_sha=blob_sha))

    llm = services.llm_factory(config)
    if llm is None:
        raise ConfigError("no model configured: add 'model' to .pullbeacon.yml on the base branch")
    request = build_apply_request(
        eligible, sources, max_output_tokens=config.limits.max_output_tokens
    )
    data, usage = call_json(llm, request, CostGuard(config.limits.max_estimated_cost_usd))
    plan: ApplyPlan = validate_proposal(data, eligible, sources, config)
    all_skipped = skipped + list(plan.skipped)
    if not plan.edits:
        result = _stop(
            services,
            snapshot,
            task,
            "The model proposed no safe change.\n\n" + "\n".join(_skips(all_skipped)),
            "no safe changes",
        )
        return result.model_copy(update={"usage": usage})

    details = {
        "files": [e.path for e in plan.edits],
        "changed_lines": sum(e.changed_lines for e in plan.edits),
        "findings": list(plan.applied_ids),
        "skipped": [list(s) for s in all_skipped],
    }
    if dry_run:
        return RunResult(
            status=RunStatus.COMPLETED,
            reason="dry run: changes validated, nothing committed",
            reviewed_head_sha=snapshot.head_sha,
            usage=usage,
            details=details,
        )

    # The commenter's permission is checked again right before writing.
    client.require_write_permission(snapshot.owner, snapshot.repo, task.actor)
    ids = ", ".join(plan.applied_ids)
    message = (
        f"PullBeacon: apply {ids} to PR #{snapshot.number}\n\n"
        f"Findings: {ids}\n"
        f"Source head: {snapshot.head_sha}\n"
        f"Review: {latest.review_id}\n"
        f"Requested by: {task.actor} (comment {task.comment_id})\n"
        "Tests and CI were not run by PullBeacon."
    )
    try:
        commit = commit_edits(client, snapshot, plan.edits, message=message)
    except GitHubAPIError as exc:
        raise ApplyRejected(
            f"GitHub rejected the commit ({exc.status}); nothing was changed"
        ) from exc

    lines = [
        f"**PullBeacon applied {ids}** in commit `{commit.sha[:12]}`"
        + (f" ({commit.url})" if commit.url else "")
        + ".",
        "",
        "Files changed:",
        *[f"- `{sanitize(e.path, 200)}` ({e.changed_lines} line(s))" for e in plan.edits],
    ]
    if plan.explanation:
        lines += ["", sanitize(plan.explanation, 800)]
    if all_skipped:
        lines += ["", "Not applied:", *_skips(all_skipped)]
    cost = (
        "simulated"
        if llm.simulated
        else (f"~{usage.cost_usd:.4f} USD" if usage.cost_usd is not None else "unknown")
    )
    lines += ["", f"<sub>LLM cost: {cost}. {NO_CI_NOTE} {DISCLAIMER}</sub>"]
    url = _reply(services, snapshot, task, "\n".join(lines))
    return RunResult(
        status=RunStatus.PARTIAL if all_skipped else RunStatus.COMPLETED,
        reason=f"committed {ids}",
        reviewed_head_sha=snapshot.head_sha,
        commit_sha=commit.sha,
        comment_url=url,
        usage=usage,
        details=details,
    )
