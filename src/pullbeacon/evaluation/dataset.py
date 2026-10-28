"""Evaluation case schema and loader. Cases are validated against their own diffs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pullbeacon.core.diff import DiffParseError, build_file_diff
from pullbeacon.domain.errors import ConfigError
from pullbeacon.domain.models import Category, Severity, Side


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GroundTruthIssue(_Strict):
    id: str
    path: str
    lines: tuple[int, ...] = Field(min_length=1)
    category: Category
    accepted_categories: tuple[Category, ...] = Field(min_length=1)
    severity: Severity
    rationale: str = Field(min_length=10)


class ExpectedNonFinding(_Strict):
    path: str
    lines: tuple[int, ...] = Field(min_length=1)
    rationale: str


class ApplyExpectation(_Strict):
    path: str
    expected_content: str


class EvalCase(_Strict):
    case_id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    title: str
    language: str
    kind: Literal["bug", "clean", "injection"]
    criticality: Literal["critical", "high", "medium", "low", "none"]
    size: Literal["small", "medium", "large"]
    fixture: dict[str, Any]
    ground_truth: tuple[GroundTruthIssue, ...] = ()
    expected_non_findings: tuple[ExpectedNonFinding, ...] = ()
    mock_responses: dict[str, list[str]]
    apply_expectation: ApplyExpectation | None = None

    @model_validator(mode="after")
    def _labels_are_consistent(self) -> EvalCase:
        if self.kind in ("bug", "injection") and not self.ground_truth:
            raise ValueError("positive cases need at least one ground-truth issue")
        if self.kind == "clean" and self.ground_truth:
            raise ValueError("clean cases must not have ground-truth issues")
        diffs = {}
        for f in self.fixture.get("files", []):
            try:
                diffs[f["filename"]] = build_file_diff(f["filename"], f["status"], f["patch"])
            except (DiffParseError, KeyError) as exc:
                raise ValueError(f"fixture file is not a valid diff: {exc}") from None
        labels: list[GroundTruthIssue | ExpectedNonFinding] = [
            *self.ground_truth,
            *self.expected_non_findings,
        ]
        for issue in labels:
            diff = diffs.get(issue.path)
            if diff is None:
                raise ValueError(f"label path {issue.path} is not in the fixture diff")
            for line in issue.lines:
                if not diff.is_commentable(line, Side.RIGHT):
                    raise ValueError(f"label line {issue.path}:{line} is not in the diff")
        return self

    @property
    def positive(self) -> bool:
        return self.kind != "clean"


def load_cases(directory: Path) -> tuple[list[EvalCase], str]:
    """Load all ``*.json`` cases (sorted). Returns ``(cases, dataset_sha256)``."""
    files = sorted(directory.glob("*.json"))
    if not files:
        raise ConfigError(f"no evaluation cases found in {directory}")
    digest = hashlib.sha256()
    cases = []
    for path in files:
        raw = path.read_bytes()
        digest.update(path.name.encode() + b"\0" + raw.replace(b"\r\n", b"\n"))
        try:
            cases.append(EvalCase.model_validate(json.loads(raw)))
        except (ValueError, ValidationError) as exc:
            raise ConfigError(f"invalid evaluation case {path.name}: {exc}") from None
    ids = [c.case_id for c in cases]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate case_id in dataset")
    return cases, digest.hexdigest()
