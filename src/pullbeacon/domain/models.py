"""Typed contracts shared by every layer.

All models are immutable. Anything that originates from a pull request (title, body, patches,
file contents) or from an LLM is untrusted data: models validate shape, never meaning.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

SHA_PATTERN = r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"
Sha = Annotated[str, StringConstraints(pattern=SHA_PATTERN)]
FINDING_ID_PATTERN = r"^PB-\d{3,4}$"
FindingId = Annotated[str, StringConstraints(pattern=FINDING_ID_PATTERN)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        """Lower is more severe; used for deterministic ordering."""
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}


class Category(StrEnum):
    SECURITY = "security"
    CORRECTNESS = "correctness"
    RELIABILITY = "reliability"
    PERFORMANCE = "performance"
    TESTING = "testing"
    MAINTAINABILITY = "maintainability"
    OTHER = "other"


class Side(StrEnum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class FileStatus(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class ChangedFile(_Frozen):
    path: str = Field(min_length=1)
    status: FileStatus
    previous_path: str | None = None
    patch: str | None = None
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    changes: int = Field(ge=0)
    blob_sha: str | None = None


class PullRequestSnapshot(_Frozen):
    """A PR pinned to the head SHA that GitHub reported when it was fetched."""

    owner: str
    repo: str
    number: int = Field(gt=0)
    title: str = ""
    body: str = ""
    author: str
    author_type: str = "User"
    state: Literal["open", "closed"]
    draft: bool = False
    base_ref: str
    base_sha: Sha
    head_ref: str
    head_sha: Sha
    base_repo_full_name: str
    head_repo_full_name: str | None
    files: tuple[ChangedFile, ...] = ()
    files_truncated: bool = False
    fetched_at: datetime

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def is_same_repo(self) -> bool:
        return self.head_repo_full_name is not None and (
            self.head_repo_full_name.lower() == self.base_repo_full_name.lower()
        )


class Finding(_Frozen):
    id: FindingId
    fingerprint: str = Field(min_length=8, max_length=64)
    severity: Severity
    category: Category
    title: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=4000)
    impact: str = Field(default="", max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    path: str | None = None
    line: int | None = Field(default=None, gt=0)
    side: Side | None = None
    suggested_fix: str | None = Field(default=None, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)
    can_apply: bool = False
    inline: bool = True

    @field_validator("title", "explanation", "evidence")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class SkippedFile(_Frozen):
    path: str
    reason: str


CostSource = Literal["provider", "litellm", "simulated", "unknown"]


class Usage(_Frozen):
    llm_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cost_source: CostSource = "unknown"

    def plus(self, other: Usage) -> Usage:
        """Aggregate two usages. A cost is only kept when both parts have a known cost."""
        if self.llm_calls == 0:
            return other
        if other.llm_calls == 0:
            return self

        def add(a: int | None, b: int | None) -> int | None:
            return None if a is None or b is None else a + b

        known = self.cost_usd is not None and other.cost_usd is not None
        same_source = self.cost_source == other.cost_source
        cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0) if known and same_source else None
        source: CostSource = self.cost_source if cost is not None else "unknown"
        return Usage(
            llm_calls=self.llm_calls + other.llm_calls,
            input_tokens=add(self.input_tokens, other.input_tokens),
            output_tokens=add(self.output_tokens, other.output_tokens),
            cost_usd=cost,
            cost_source=source,
        )


ReviewMode = Literal["automatic", "manual", "local"]


class ReviewReport(_Frozen):
    owner: str
    repo: str
    number: int
    head_sha: Sha
    base_sha: Sha
    provider: str
    model: str
    prompt_version: str
    config_hash: str
    mode: ReviewMode
    simulated: bool
    findings: tuple[Finding, ...] = ()
    summary: str = ""
    reviewed_files: tuple[str, ...] = ()
    skipped_files: tuple[SkippedFile, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    usage: Usage = Usage()


class FileEdit(_Frozen):
    path: str
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_content: str


class ApplyProposal(_Frozen):
    original_head_sha: Sha
    finding_ids: tuple[FindingId, ...]
    edits: tuple[FileEdit, ...]
    explanation: str = ""


class RunStatus(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    PARTIAL = "partial"
    FAILED = "failed"


class RunResult(_Frozen):
    status: RunStatus
    reason: str
    command: str | None = None
    event_id: str | None = None
    reviewed_head_sha: str | None = None
    review_url: str | None = None
    comment_url: str | None = None
    commit_sha: str | None = None
    usage: Usage = Usage()
    details: dict[str, Any] = Field(default_factory=dict)


def canonical_json(model: BaseModel | dict[str, Any] | list[Any], *, indent: int | None = 2) -> str:
    """Deterministic JSON: sorted keys, stable separators, UTF-8 friendly."""
    data = model.model_dump(mode="json") if isinstance(model, BaseModel) else model
    if indent is None:
        return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(data, sort_keys=True, ensure_ascii=False, indent=indent)
