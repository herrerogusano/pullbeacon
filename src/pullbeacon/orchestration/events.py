"""Turn a GitHub Actions event payload into a PullBeacon task (or an explicit ignore)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pullbeacon.domain.errors import ConfigError
from pullbeacon.orchestration.commands import Command, parse_command

MAX_EVENT_BYTES = 2 * 1024 * 1024
AUTO_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
TaskKind = Literal["auto_review", "review", "explain", "apply", "help"]


@dataclass(frozen=True)
class Task:
    kind: TaskKind
    owner: str
    repo: str
    number: int
    actor: str | None = None
    actor_type: str = "User"
    comment_id: int | None = None
    finding_id: str | None = None
    error: str | None = None
    delivery: str = ""

    @property
    def manual(self) -> bool:
        return self.kind != "auto_review"

    @property
    def trigger(self) -> str:
        if self.comment_id is not None:
            return f"comment:{self.comment_id}"
        return self.delivery or "auto"


@dataclass(frozen=True)
class Ignored:
    reason: str


def load_event(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise ConfigError("GITHUB_EVENT_PATH is not readable") from None
    if len(raw) > MAX_EVENT_BYTES:
        raise ConfigError("event payload is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ConfigError("event payload is not JSON") from None
    if not isinstance(data, dict):
        raise ConfigError("event payload is not an object")
    return data


def _repo(event: dict[str, Any]) -> tuple[str, str]:
    repo = event.get("repository") or {}
    owner = (repo.get("owner") or {}).get("login")
    name = repo.get("name")
    if not isinstance(owner, str) or not isinstance(name, str):
        raise ConfigError("event payload has no repository")
    return owner, name


def route_event(
    event_name: str,
    event: dict[str, Any],
    *,
    bot_login: str,
    actor: str | None = None,
    run_id: str = "",
) -> Task | Ignored:
    if event_name == "pull_request_target":
        action = event.get("action")
        if action not in AUTO_ACTIONS:
            return Ignored(f"pull_request_target action '{action}' is not handled")
        pr = event.get("pull_request") or {}
        number = pr.get("number")
        if not isinstance(number, int):
            return Ignored("event has no pull request number")
        owner, repo = _repo(event)
        return Task("auto_review", owner, repo, number, delivery="auto")

    if event_name == "issue_comment":
        if event.get("action") != "created":
            return Ignored("only newly created comments are handled (edits are ignored)")
        issue = event.get("issue") or {}
        if not issue.get("pull_request"):
            return Ignored("comment is on an issue, not a pull request")
        comment = event.get("comment") or {}
        user = comment.get("user") or {}
        login, user_type = user.get("login"), user.get("type", "User")
        if not isinstance(login, str):
            return Ignored("comment has no author")
        if user_type == "Bot" or login == bot_login or login.endswith("[bot]"):
            return Ignored("comments from bots are ignored")
        command: Command | None = parse_command(comment.get("body") or "")
        if command is None:
            return Ignored("comment contains no @pullbeacon command")
        number, comment_id = issue.get("number"), comment.get("id")
        if not isinstance(number, int) or not isinstance(comment_id, int):
            return Ignored("comment event is missing ids")
        owner, repo = _repo(event)
        return Task(
            command.name,
            owner,
            repo,
            number,
            actor=login,
            actor_type=user_type,
            comment_id=comment_id,
            finding_id=command.finding_id,
            error=command.error,
        )

    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        raw = str(inputs.get("pr_number", "")).strip()
        if not raw.isdigit() or int(raw) <= 0:
            return Ignored("workflow_dispatch needs a numeric 'pr_number' input")
        sender = (event.get("sender") or {}).get("login") or actor
        if not isinstance(sender, str):
            return Ignored("workflow_dispatch has no actor")
        owner, repo = _repo(event)
        return Task(
            "review", owner, repo, int(raw), actor=sender, delivery=f"dispatch:{run_id or 'manual'}"
        )

    return Ignored(f"event '{event_name}' is not handled by PullBeacon")
