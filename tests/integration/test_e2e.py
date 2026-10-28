"""Full pipeline with isolated fakes: event -> auth -> snapshot -> LLM -> validation ->
review -> explain -> apply commit -> stale protection -> re-review. No network."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from pullbeacon.domain.models import RunStatus
from pullbeacon.logging_utils import configure_logging
from pullbeacon.orchestration.runner import ActionInputs, run_action
from tests.helpers import setup
from tests.integration.test_action import comment, pr_target

PR = 21
GH_TOKEN = "ghs_" + "E2E0token0value0never0logged0x"
API_KEY = "sk-ant-api03-" + "e2e-secret-key-" * 3


def test_review_explain_apply_rereview(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", GH_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    configure_logging("DEBUG")
    fake, services, llm, data = setup(apply_job=True)
    review_job, apply_job = ActionInputs(mode="review"), ActionInputs(mode="apply")
    head0 = data["pull_request"]["head"]["sha"]

    def run(name: str, event: dict[str, Any], inputs: ActionInputs) -> Any:
        return run_action(services, inputs, event_name=name, event=event, actor="dev-alice")

    # 1. Automatic review on PR open.
    auto = run("pull_request_target", pr_target(), review_job)
    assert auto.status is RunStatus.COMPLETED
    (first_review,) = fake.reviews[PR]
    assert first_review["commit_id"] == head0
    (inline,) = fake.review_comments[PR]
    assert (inline["path"], inline["line"], inline["side"]) == ("src/api/users.py", 13, "RIGHT")

    # 2. Explain: a comment, no code changes.
    explained = run(
        "issue_comment", comment("@pullbeacon explain PB-001", comment_id=1), review_job
    )
    assert explained.status is RunStatus.COMPLETED
    assert fake.refs["heads/feature/auth-bug"] == head0

    # 3. Apply in the apply job: one commit on the PR branch.
    applied = run("issue_comment", comment("@pullbeacon apply PB-001", comment_id=2), apply_job)
    assert applied.status is RunStatus.COMPLETED, applied.reason
    head1 = fake.refs["heads/feature/auth-bug"]
    assert head1 == applied.commit_sha != head0
    fixed = fake.file_at(head1, "src/api/users.py")
    assert fixed is not None
    assert fixed.decode() == data["expected_fixed_content"]["src/api/users.py"]

    # 4. Applying again uses the old review -> refused, nothing written.
    patches_before = sum(1 for c in fake.calls if c.method == "PATCH")
    stale = run("issue_comment", comment("@pullbeacon apply", comment_id=3), apply_job)
    assert stale.status is RunStatus.SKIPPED
    assert sum(1 for c in fake.calls if c.method == "PATCH") == patches_before

    # 5. Manual re-review targets the new head (not re-triggered automatically).
    rereview = run("issue_comment", comment("@pullbeacon review", comment_id=4), review_job)
    assert rereview.status is RunStatus.COMPLETED
    assert fake.reviews[PR][-1]["commit_id"] == head1

    # Never: merges, force pushes, approvals.
    assert not any("merge" in c.path for c in fake.calls)
    assert all(c.body.get("force") is False for c in fake.calls if c.method == "PATCH")
    assert all(
        c.body.get("event") == "COMMENT"
        for c in fake.calls
        if c.path.endswith("/reviews") and c.method == "POST"
    )

    # No credential anywhere: GitHub payloads, LLM prompts, logs, stdout/stderr.
    published = json.dumps([c.body for c in fake.calls if c.body is not None])
    prompts = json.dumps([r.system + r.user for r in llm.requests])
    out = capsys.readouterr()
    for secret in (GH_TOKEN, API_KEY):
        assert secret not in published
        assert secret not in prompts
        assert secret not in out.out + out.err
    logging.getLogger("pullbeacon").handlers.clear()
