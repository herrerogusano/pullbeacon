"""Machine-readable markers embedded in PullBeacon's GitHub reviews and comments.

GitHub is the only state store (no database). A review marker carries the head SHA, the
idempotency key and a bounded, canonical copy of the validated findings so `explain`/`apply`
can recover them later. Markers are only trusted when the review was authored by the expected
bot identity *and* targets the current head SHA; anyone can type a marker in a comment.
Evidence (code) is deliberately not stored in markers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pullbeacon.domain.models import (
    Category,
    Finding,
    FindingId,
    ReviewMode,
    ReviewReport,
    Severity,
    Sha,
    Side,
)

MARKER_VERSION = 1
MAX_MARKER_BYTES = 24_000
_REVIEW_MARKER = re.compile(r"<!-- pullbeacon:review:v1 ([A-Za-z0-9_\-=]+) -->\s*$")
_REPLY_MARKER = re.compile(r"<!-- pullbeacon:reply:v1 trigger=(\d+) -->")


class MarkerFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: FindingId
    fp: str
    severity: Severity
    category: Category
    title: str = Field(max_length=200)
    path: str | None
    line: int | None
    side: Side | None
    inline: bool
    confidence: float = Field(ge=0, le=1)
    can_apply: bool
    fix: str | None = Field(default=None, max_length=600)
    explanation: str = Field(default="", max_length=800)


class ReviewMarker(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int = MARKER_VERSION
    head_sha: Sha
    config_hash: str
    prompt_version: str
    mode: ReviewMode
    trigger: str  # "auto" or "comment:<id>" or "dispatch:<run>"
    model: str
    findings: tuple[MarkerFinding, ...] = ()

    @property
    def auto_key(self) -> str:
        return idempotency_key(self.head_sha, self.config_hash, self.prompt_version, self.mode)


def idempotency_key(head_sha: str, config_hash: str, prompt_version: str, mode: str) -> str:
    return hashlib.sha256(f"{head_sha}|{config_hash}|{prompt_version}|{mode}".encode()).hexdigest()


def marker_from_report(report: ReviewReport, *, trigger: str) -> ReviewMarker:
    def to_marker(f: Finding) -> MarkerFinding:
        return MarkerFinding(
            id=f.id,
            fp=f.fingerprint,
            severity=f.severity,
            category=f.category,
            title=f.title[:200],
            path=f.path,
            line=f.line,
            side=f.side,
            inline=f.inline,
            confidence=f.confidence,
            can_apply=f.can_apply,
            fix=(f.suggested_fix or "")[:600] or None,
            explanation=f.explanation[:800],
        )

    marker = ReviewMarker(
        head_sha=report.head_sha,
        config_hash=report.config_hash,
        prompt_version=report.prompt_version,
        mode=report.mode,
        trigger=trigger,
        model=report.model,
        findings=tuple(to_marker(f) for f in report.findings),
    )
    # Keep the marker bounded: drop explanations, then low-priority findings.
    while len(encode_marker(marker)) > MAX_MARKER_BYTES and marker.findings:
        if any(f.explanation for f in marker.findings):
            marker = marker.model_copy(
                update={
                    "findings": tuple(
                        f.model_copy(update={"explanation": ""}) for f in marker.findings
                    )
                }
            )
        else:
            marker = marker.model_copy(update={"findings": marker.findings[:-1]})
    return marker


def encode_marker(marker: ReviewMarker) -> str:
    raw = json.dumps(marker.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    token = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    return f"<!-- pullbeacon:review:v1 {token} -->"


def decode_marker(body: str) -> ReviewMarker | None:
    """Parse the marker at the very end of a review body; ``None`` if absent or invalid."""
    match = _REVIEW_MARKER.search(body or "")
    if match is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(match.group(1).encode("ascii"))
        if len(raw) > MAX_MARKER_BYTES * 2:
            return None
        data: Any = json.loads(raw)
        return ReviewMarker.model_validate(data)
    except (ValueError, ValidationError):
        return None


def reply_marker(trigger_comment_id: int) -> str:
    return f"<!-- pullbeacon:reply:v1 trigger={trigger_comment_id} -->"


def replied_trigger(body: str) -> int | None:
    match = _REPLY_MARKER.search(body or "")
    return int(match.group(1)) if match else None
