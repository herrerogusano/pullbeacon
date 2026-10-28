"""Static security checks on the shipped workflows and action definition."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
WORKFLOWS = [
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "pullbeacon.yml",
    ROOT / "examples" / "pullbeacon.yml",
    ROOT / ".github" / "workflows" / "benchmark.yml",
]
PINNED = re.compile(r"^[\w.\-]+/[\w.\-]+(/[\w.\-/]+)?@[0-9a-f]{40}$")


def load(path: Path) -> dict[Any, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML parses the bare key `on` as boolean True.
    return workflow.get("on") or workflow.get(True) or {}


def all_steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_permissions_and_timeouts(path: Path) -> None:
    workflow = load(path)
    assert "permissions" in workflow, "top-level permissions must be explicit"
    for name, job in workflow["jobs"].items():
        assert "timeout-minutes" in job, name
        perms = workflow["permissions"] if "permissions" not in job else job["permissions"]
        if "pull_request_target" in triggers(workflow) or "issue_comment" in triggers(workflow):
            assert "permissions" in job, f"{name} must declare job permissions"
            if name != "apply":
                assert perms.get("contents") == "read", name


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_third_party_actions_are_pinned(path: Path) -> None:
    for step in all_steps(load(path)):
        uses = step.get("uses")
        if uses and not uses.startswith("./") and "<OWNER>" not in uses:
            assert PINNED.match(uses), uses


@pytest.mark.parametrize("path", WORKFLOWS[1:3], ids=lambda p: p.name)
def test_privileged_workflows_never_touch_pr_code(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    workflow = load(path)
    assert "pull_request_target" in triggers(workflow)
    forbidden = [
        "github.event.pull_request.head",
        "head_ref",
        "npm install",
        "pip install",
        "pytest",
    ]
    for step in all_steps(workflow):
        rendered = yaml.safe_dump(step)
        if "actions/checkout" in str(step.get("uses", "")):
            assert step["with"]["ref"] == "${{ github.event.repository.default_branch }}"
            assert step["with"]["persist-credentials"] is False
        for token in forbidden[1:]:
            assert token not in rendered, (token, step)
        assert "run" not in step, "privileged jobs must not run inline scripts"
    # The head repo may only be compared in `if:` guards, never used as a ref.
    for line in text.splitlines():
        if "github.event.pull_request.head" in line:
            assert "full_name == github.repository" in line


def test_apply_is_the_only_job_with_contents_write() -> None:
    for path in WORKFLOWS[1:3]:
        jobs = load(path)["jobs"]
        writers = [
            n for n, j in jobs.items() if j.get("permissions", {}).get("contents") == "write"
        ]
        assert writers == ["apply"], path
        assert "@pullbeacon apply" in jobs["apply"]["if"]
        assert jobs["apply"]["steps"][-1]["with"]["mode"] == "apply"


def test_action_passes_inputs_only_through_env() -> None:
    action = load(ROOT / "action.yml")
    assert action["runs"]["using"] == "composite"
    for step in action["runs"]["steps"]:
        if "run" in step:
            assert "${{" not in step["run"], "no expression interpolation inside scripts"
            assert "--locked" in step["run"]
            assert "GITHUB_ACTION_PATH" in step["run"]
        if "uses" in step:
            assert PINNED.match(step["uses"]), step["uses"]
            assert step["with"].get("enable-cache") is False
    declared = set(action["inputs"])
    used = set(re.findall(r"inputs\.([\w-]+)", (ROOT / "action.yml").read_text(encoding="utf-8")))
    assert used <= declared


def test_example_workflow_references_a_placeholder_not_an_invented_owner() -> None:
    text = (ROOT / "examples" / "pullbeacon.yml").read_text(encoding="utf-8")
    assert "<OWNER>/<REPO>@<COMMIT_SHA>" in text


def test_benchmark_is_manual_and_confirmed() -> None:
    workflow = load(ROOT / ".github" / "workflows" / "benchmark.yml")
    assert set(triggers(workflow)) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["benchmark"]
    assert "SPEND" in job["if"]
    run = next(s["run"] for s in job["steps"] if "run" in s)
    assert "${{" not in run
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--provider live" not in ci
