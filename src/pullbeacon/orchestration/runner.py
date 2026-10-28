"""GitHub Action entry point: event -> task -> authorization -> flow -> result/outputs."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pullbeacon.adapters.github import fetch_snapshot
from pullbeacon.adapters.publisher import post_reply, trigger_already_replied
from pullbeacon.core.render import sanitize
from pullbeacon.domain.errors import (
    ApplyRejected,
    BudgetExceeded,
    ConfigError,
    CredentialError,
    GitHubAPIError,
    LimitExceeded,
    LLMError,
    PermissionDenied,
    PullBeaconError,
    StaleHeadError,
    UnsupportedPullRequest,
)
from pullbeacon.domain.models import RunResult, RunStatus
from pullbeacon.logging_utils import get_logger, log_event
from pullbeacon.orchestration.commands import HELP_TEXT
from pullbeacon.orchestration.events import Ignored, Task, route_event
from pullbeacon.orchestration.flows import Services, run_explain, run_review

JobMode = Literal["review", "apply"]
log = get_logger("runner")


@dataclass(frozen=True)
class ActionInputs:
    mode: JobMode = "review"
    dry_run: bool = False
    mock_llm: bool = False
    config_file: str = ".pullbeacon.yml"
    model: str | None = None
    automatic_review: Literal["config", "true", "false"] = "config"
    bot_login: str = "github-actions[bot]"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ActionInputs:
        def flag(name: str) -> bool:
            value = env.get(name, "false").strip().lower()
            if value not in ("true", "false", ""):
                raise ConfigError(f"{name} must be 'true' or 'false'")
            return value == "true"

        mode = env.get("INPUT_MODE", "review").strip().lower()
        if mode not in ("review", "apply"):
            raise ConfigError("input 'mode' must be 'review' or 'apply'")
        auto = env.get("INPUT_AUTOMATIC_REVIEW", "config").strip().lower() or "config"
        if auto not in ("config", "true", "false"):
            raise ConfigError("input 'automatic-review' must be config, true or false")
        return cls(
            mode=mode,  # type: ignore[arg-type]
            dry_run=flag("INPUT_DRY_RUN"),
            mock_llm=flag("INPUT_MOCK_LLM"),
            config_file=env.get("INPUT_CONFIG_FILE", ".pullbeacon.yml").strip()
            or ".pullbeacon.yml",
            model=env.get("INPUT_MODEL", "").strip() or None,
            automatic_review=auto,  # type: ignore[arg-type]
            bot_login=env.get("INPUT_BOT_LOGIN", "").strip() or "github-actions[bot]",
        )


FRIENDLY = {
    ApplyRejected: "Apply was rejected and nothing was changed",
    ConfigError: "PullBeacon configuration problem",
    CredentialError: "PullBeacon is missing a credential (check the repository secrets)",
    PermissionDenied: "Permission denied",
    UnsupportedPullRequest: "This pull request is not supported",
    BudgetExceeded: "The estimated cost exceeds the configured per-run limit",
    LimitExceeded: "The pull request exceeds the configured limits",
    LLMError: "The model provider could not complete the request",
    StaleHeadError: "The pull request changed while PullBeacon was running; run the command again",
    GitHubAPIError: "GitHub API error",
}


def _friendly(exc: PullBeaconError) -> str:
    for kind, title in FRIENDLY.items():
        if isinstance(exc, kind):
            return f"**{title}.** {sanitize(exc.message, 500)}"
    return f"**PullBeacon failed.** {sanitize(exc.message, 500)}"


def _reply(services: Services, task: Task, text: str) -> str | None:
    """Reply once per triggering comment (redeliveries do not produce duplicates)."""
    if task.comment_id is None:
        return None
    snapshot = fetch_snapshot(services.client, task.owner, task.repo, task.number)
    if trigger_already_replied(
        services.client, snapshot, task.comment_id, bot_login=services.bot_login
    ):
        return None
    comment = post_reply(services.client, snapshot, text, trigger_comment_id=task.comment_id)
    url = comment.get("html_url")
    return url if isinstance(url, str) else None


def handle_task(services: Services, task: Task, inputs: ActionInputs) -> RunResult:
    if inputs.mode == "review" and task.kind == "apply":
        return RunResult(status=RunStatus.SKIPPED, reason="apply commands run in the apply job")
    if inputs.mode == "apply" and task.kind != "apply":
        return RunResult(status=RunStatus.SKIPPED, reason="the apply job only handles apply")

    if task.manual:
        if not task.actor:
            raise PermissionDenied("the command has no verifiable actor")
        # Real permission from the API, before any LLM call or write. Fails closed.
        services.client.require_write_permission(task.owner, task.repo, task.actor)

    if task.kind == "help":
        prefix = f"{sanitize(task.error, 200)}.\n\n" if task.error else ""
        url = _reply(services, task, prefix + HELP_TEXT)
        return RunResult(status=RunStatus.COMPLETED, reason="help posted", comment_url=url)
    if task.kind == "auto_review":
        return run_review(
            services,
            task.owner,
            task.repo,
            task.number,
            mode="automatic",
            trigger="auto",
            dry_run=inputs.dry_run,
        )
    if task.kind == "review":
        return run_review(
            services,
            task.owner,
            task.repo,
            task.number,
            mode="manual",
            trigger=task.trigger,
            dry_run=inputs.dry_run,
        )
    if task.kind == "explain" and task.finding_id and task.comment_id is not None:
        return run_explain(
            services,
            task.owner,
            task.repo,
            task.number,
            finding_id=task.finding_id,
            trigger_comment_id=task.comment_id,
        )
    if task.kind == "apply":
        from pullbeacon.orchestration.apply_flow import run_apply

        return run_apply(services, task, dry_run=inputs.dry_run)
    return RunResult(status=RunStatus.SKIPPED, reason="nothing to do")


def run_action(
    services: Services,
    inputs: ActionInputs,
    *,
    event_name: str,
    event: dict[str, object],
    actor: str | None = None,
    run_id: str = "",
) -> RunResult:
    routed = route_event(
        event_name, event, bot_login=services.bot_login, actor=actor, run_id=run_id
    )
    if isinstance(routed, Ignored):
        return RunResult(status=RunStatus.SKIPPED, reason=routed.reason)
    task = routed
    try:
        result = handle_task(services, task, inputs)
    except PermissionDenied as exc:
        log_event(log, "permission_denied", actor=task.actor, kind=task.kind)
        url = _reply(services, task, _friendly(exc)) if not inputs.dry_run else None
        return RunResult(
            status=RunStatus.SKIPPED, reason="permission denied", command=task.kind, comment_url=url
        )
    except UnsupportedPullRequest as exc:
        url = None
        if task.manual and not inputs.dry_run:
            url = _reply(services, task, _friendly(exc))
        return RunResult(
            status=RunStatus.SKIPPED, reason=exc.message, command=task.kind, comment_url=url
        )
    except PullBeaconError as exc:
        log_event(log, "run_failed", code=exc.code, kind=task.kind)
        url = None
        if task.manual and not inputs.dry_run:
            try:
                url = _reply(services, task, _friendly(exc))
            except PullBeaconError:
                url = None
        return RunResult(
            status=RunStatus.FAILED,
            reason=f"{exc.code}: {exc.message}",
            command=task.kind,
            comment_url=url,
        )
    return result.model_copy(update={"command": task.kind, "event_id": task.trigger})


def write_action_outputs(result: RunResult, env: Mapping[str, str] = os.environ) -> None:
    output = env.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as fh:
            fh.write(f"status={result.status.value}\n")
            fh.write(f"review-url={result.review_url or ''}\n")
            fh.write(f"comment-url={result.comment_url or ''}\n")
            fh.write(f"commit-sha={result.commit_sha or ''}\n")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [
            "### PullBeacon",
            "",
            f"- Command: `{result.command or 'none'}`",
            f"- Status: **{result.status.value}** — {sanitize(result.reason, 300)}",
        ]
        if result.review_url:
            lines.append(f"- Review: {result.review_url}")
        if result.commit_sha:
            lines.append(f"- Commit: `{result.commit_sha}`")
        if result.usage.llm_calls:
            cost = (
                f"{result.usage.cost_usd:.4f} USD ({result.usage.cost_source})"
                if result.usage.cost_usd is not None
                else result.usage.cost_source
            )
            lines.append(f"- LLM calls: {result.usage.llm_calls}, cost: {cost}")
        with Path(summary).open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
