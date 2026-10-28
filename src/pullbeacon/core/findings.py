"""Deterministic validation of model findings against the actual diff.

Nothing the model says is published unless it survives these checks:
schema/enums/ranges, path in the reviewed scope, line present in the diff on the stated side,
and evidence that literally appears in that file's diff. Failures are dropped or downgraded to
non-inline with a recorded reason; nothing is ever attributed to a guessed line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from pullbeacon.core.diff import FileDiff, fingerprint
from pullbeacon.core.scope import ReviewScope
from pullbeacon.domain.models import Category, Finding, Severity, Side

MAX_FINDINGS = 50
MIN_EVIDENCE_MATCH = 6
_COORDINATES = re.compile(r"^\|?\s*(?:[RL]\d+\s+){1,2}[+-]?\s?")


class RawFinding(BaseModel):
    """Shape check only; semantics are verified in ``validate_findings``."""

    model_config = ConfigDict(extra="ignore")

    severity: Severity
    category: str = "other"
    title: str
    explanation: str
    impact: str = ""
    evidence: str
    path: str | None = None
    line: int | None = None
    side: Side | None = None
    suggested_fix: str | None = None
    confidence: float
    can_apply: bool = False


@dataclass(frozen=True)
class ValidationOutcome:
    findings: tuple[Finding, ...]
    dropped: tuple[str, ...]  # human-readable reasons, no code content


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _strip_coordinates(evidence: str) -> str:
    """Models sometimes copy the ``| R12 +`` prefix; remove it before matching."""
    return _COORDINATES.sub("", evidence.strip(), count=1)


def evidence_in_diff(evidence: str, diff: FileDiff) -> bool:
    needle = _normalize(_strip_coordinates(evidence))
    if len(needle) < MIN_EVIDENCE_MATCH:
        # Very short evidence must match a whole line exactly.
        return any(_normalize(line.text) == needle for line in diff.lines() if needle)
    for line in diff.lines():
        hay = _normalize(line.text)
        if not hay:
            continue
        if needle in hay or (len(hay) >= MIN_EVIDENCE_MATCH and hay in needle):
            return True
    return False


def _category(value: str) -> Category:
    try:
        return Category(value.strip().lower())
    except ValueError:
        return Category.OTHER


def validate_findings(
    raw_items: list[Any],
    scope: ReviewScope,
    *,
    max_inline: int,
) -> ValidationOutcome:
    dropped: list[str] = []
    candidates: dict[str, Finding] = {}

    for index, item in enumerate(raw_items[: MAX_FINDINGS * 2]):
        label = f"finding #{index + 1}"
        try:
            raw = RawFinding.model_validate(item)
        except ValidationError as exc:
            fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
            dropped.append(f"{label}: invalid fields {', '.join(fields) or 'shape'}")
            continue
        if not 0.0 <= raw.confidence <= 1.0:
            dropped.append(f"{label}: confidence out of range")
            continue
        if not raw.evidence.strip() or not raw.title.strip() or not raw.explanation.strip():
            dropped.append(f"{label}: missing title, explanation or evidence")
            continue

        category = _category(raw.category)
        inline = True
        path, line, side = raw.path, raw.line, raw.side or Side.RIGHT
        anchor = raw.evidence

        if path is None:
            matching = next((d for d in scope.diffs if evidence_in_diff(raw.evidence, d)), None)
            if matching is None:
                dropped.append(f"{label}: no path and evidence not found in the reviewed diff")
                continue
            path, line, inline = matching.path, None, False
        else:
            diff = scope.diff_for(path)
            if diff is None:
                dropped.append(f"{label}: path is not part of the reviewed diff")
                continue
            if not evidence_in_diff(raw.evidence, diff):
                dropped.append(f"{label}: evidence not found in {path}")
                continue
            diff_line = diff.line_at(line, side) if line is not None and line > 0 else None
            if diff_line is None:
                # Real issue in this file, but the cited line is not commentable: no inline.
                inline, line = False, None
            else:
                anchor = diff_line.text

        can_apply = (
            raw.can_apply
            and inline
            and side is Side.RIGHT
            and bool(raw.suggested_fix and raw.suggested_fix.strip())
        )
        fp = fingerprint(path, side if inline else None, anchor, category.value)
        try:
            finding = Finding(
                id="PB-001",  # renumbered after sorting
                fingerprint=fp,
                severity=raw.severity,
                category=category,
                title=raw.title.strip()[:200],
                explanation=raw.explanation.strip()[:4000],
                impact=raw.impact.strip()[:2000],
                evidence=_strip_coordinates(raw.evidence)[:2000],
                path=path,
                line=line if inline else None,
                side=side if inline else None,
                suggested_fix=(raw.suggested_fix or "").strip()[:4000] or None,
                confidence=raw.confidence,
                can_apply=can_apply,
                inline=inline,
            )
        except ValidationError:
            dropped.append(f"{label}: failed final validation")
            continue
        previous = candidates.get(fp)
        if previous is not None:
            dropped.append(f"{label}: duplicate of an earlier finding")
            if (finding.severity.rank, -finding.confidence) >= (
                previous.severity.rank,
                -previous.confidence,
            ):
                continue
        candidates[fp] = finding

    ordered = sorted(
        candidates.values(),
        key=lambda f: (
            f.severity.rank,
            not f.inline,
            -f.confidence,
            f.path or "",
            f.line or 0,
            f.fingerprint,
        ),
    )[:MAX_FINDINGS]
    numbered: list[Finding] = []
    inline_count = 0
    for number, finding in enumerate(ordered, start=1):
        update: dict[str, Any] = {"id": f"PB-{number:03d}"}
        if finding.inline:
            if inline_count >= max_inline:
                update.update(inline=False, can_apply=False)
            else:
                inline_count += 1
        numbered.append(finding.model_copy(update=update))
    return ValidationOutcome(findings=tuple(numbered), dropped=tuple(dropped))
