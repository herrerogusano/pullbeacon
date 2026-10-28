"""Safe edit planning for `@pullbeacon apply`.

The model proposes line-range replacements; this module decides whether they are acceptable.
Any invalid replacement aborts the whole apply (fail closed). Findings the model chose not to
fix are reported as skipped. Nothing here touches GitHub.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pullbeacon.adapters.llm import LLMRequest
from pullbeacon.config import PullBeaconConfig
from pullbeacon.core.markers import MarkerFinding
from pullbeacon.core.paths import UnsafePathError, is_apply_denied, matches_any, normalize_repo_path
from pullbeacon.domain.errors import ApplyRejected
from pullbeacon.domain.models import Side
from pullbeacon.logging_utils import TOKEN_PATTERNS
from pullbeacon.prompts import APPLY_SCHEMA, APPLY_SYSTEM, untrusted_block

_PLACEHOLDERS = re.compile(
    r"(?i)(\.\.\.\s*(rest|existing|remaining) (of )?(the )?code|"
    r"#\s*(rest|existing) of (the )?(code|file)|//\s*(rest|existing) of (the )?(code|file)|"
    r"<\s*placeholder\s*>|TODO:\s*implement|your code here|unchanged code)"
)


@dataclass(frozen=True)
class SourceFile:
    path: str
    content: str
    blob_sha: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def newline(self) -> str:
        crlf = self.content.count("\r\n")
        return "\r\n" if crlf and crlf == self.content.count("\n") else "\n"


@dataclass(frozen=True)
class PlannedEdit:
    path: str
    finding_ids: tuple[str, ...]
    original_sha256: str
    new_content: str
    changed_lines: int


@dataclass(frozen=True)
class ApplyPlan:
    edits: tuple[PlannedEdit, ...]
    skipped: tuple[tuple[str, str], ...]
    explanation: str

    @property
    def applied_ids(self) -> tuple[str, ...]:
        return tuple(sorted({fid for e in self.edits for fid in e.finding_ids}))


def select_findings(
    findings: Sequence[MarkerFinding], requested: str | None, config: PullBeaconConfig
) -> tuple[list[MarkerFinding], list[tuple[str, str]]]:
    """Deterministic eligibility filter. Returns ``(eligible, skipped_with_reason)``."""
    policy = config.policy
    eligible: list[MarkerFinding] = []
    skipped: list[tuple[str, str]] = []
    candidates = [f for f in findings if requested is None or f.id == requested]
    if requested is not None and not candidates:
        return [], [(requested, "not found in the latest PullBeacon review")]
    for finding in candidates:
        reason = None
        if not finding.can_apply:
            reason = "not marked as applicable by the review"
        elif finding.confidence < policy.min_apply_confidence:
            reason = f"confidence {finding.confidence:.2f} below min_apply_confidence"
        elif finding.path is None or finding.line is None or finding.side is not Side.RIGHT:
            reason = "finding has no line on the new version of the file"
        else:
            try:
                normalize_repo_path(finding.path)
            except UnsafePathError:
                reason = "unsafe path"
            else:
                if is_apply_denied(finding.path):
                    reason = "path is protected (CI, config, credentials or VCS metadata)"
                elif not matches_any(finding.path, policy.allowed_apply_paths):
                    reason = "path not in policy.allowed_apply_paths"
        if reason:
            skipped.append((finding.id, reason))
        else:
            eligible.append(finding)
    paths = sorted({f.path for f in eligible if f.path})
    if len(paths) > policy.max_apply_files:
        allowed = set(paths[: policy.max_apply_files])
        for f in eligible:
            if f.path not in allowed:
                skipped.append((f.id, "over policy.max_apply_files"))
        eligible = [f for f in eligible if f.path in allowed]
    return eligible, skipped


def number_lines(content: str) -> str:
    return "\n".join(f"{n:>5}| {line}" for n, line in enumerate(content.splitlines(), start=1))


def build_apply_request(
    findings: Sequence[MarkerFinding], files: Sequence[SourceFile], *, max_output_tokens: int
) -> LLMRequest:
    listing = "\n".join(
        f"- {f.id} [{f.severity.value}/{f.category.value}] {f.path}:{f.line} — {f.title}\n"
        f"  explanation: {f.explanation}\n  suggested fix: {f.fix or '(none)'}"
        for f in findings
    )
    parts = ["Findings to fix:", untrusted_block("FINDINGS", listing)]
    for source in files:
        parts.append(f"Current content of {source.path} (line numbers are not part of the file):")
        parts.append(untrusted_block("FILE", number_lines(source.content)))
    return LLMRequest(
        purpose="apply",
        system=APPLY_SYSTEM,
        user="\n\n".join(parts),
        max_output_tokens=max_output_tokens,
        json_schema=APPLY_SCHEMA,
        schema_name="pullbeacon_apply",
    )


class _Replacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    original: str
    replacement: str


class _Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    finding_ids: list[str] = Field(min_length=1, max_length=20)
    replacements: list[_Replacement] = Field(min_length=1, max_length=20)


class _Proposal(BaseModel):
    model_config = ConfigDict(extra="ignore")
    edits: list[_Edit] = Field(default_factory=list, max_length=20)
    explanation: str = ""


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def _normalize_newlines(text: str, newline: str) -> str:
    text = text.replace("\r\n", "\n")
    return text.replace("\n", newline) if newline != "\n" else text


def _changed_line_count(before: str, after: str) -> int:
    diff = difflib.unified_diff(_lines(before), _lines(after), n=0, lineterm="")
    return sum(
        1 for line in diff if (line.startswith(("+", "-")) and not line.startswith(("+++", "---")))
    )


def validate_proposal(
    data: dict[str, Any],
    findings: Sequence[MarkerFinding],
    files: Sequence[SourceFile],
    config: PullBeaconConfig,
) -> ApplyPlan:
    try:
        proposal = _Proposal.model_validate(data)
    except ValidationError:
        raise ApplyRejected(
            "the model's edit proposal does not match the expected format"
        ) from None

    by_id = {f.id: f for f in findings}
    by_path = {s.path: s for s in files}
    policy = config.policy
    window = policy.apply_context_window
    edits: list[PlannedEdit] = []
    seen_paths: set[str] = set()
    total_changed = 0

    for edit in proposal.edits:
        source = by_path.get(edit.path)
        if source is None:
            raise ApplyRejected("the proposal edits a file that was not provided")
        if edit.path in seen_paths:
            raise ApplyRejected(f"the proposal edits {edit.path} more than once")
        seen_paths.add(edit.path)
        ids = tuple(dict.fromkeys(fid.upper() for fid in edit.finding_ids))
        if any(fid not in by_id or by_id[fid].path != edit.path for fid in ids):
            raise ApplyRejected(f"the proposal for {edit.path} references unknown findings")

        lines = _lines(source.content)
        newline = source.newline
        ordered = sorted(edit.replacements, key=lambda r: r.start_line)
        previous_end = 0
        for rep in ordered:
            if rep.end_line < rep.start_line or rep.end_line > len(lines):
                raise ApplyRejected(
                    f"replacement outside {edit.path} (lines {rep.start_line}-{rep.end_line})"
                )
            if rep.start_line <= previous_end:
                raise ApplyRejected(f"overlapping replacements in {edit.path}")
            previous_end = rep.end_line
            actual = "".join(lines[rep.start_line - 1 : rep.end_line])
            if _normalize_newlines(rep.original, "\n").rstrip("\n") != actual.replace(
                "\r\n", "\n"
            ).rstrip("\n"):
                raise ApplyRejected(
                    f"replacement in {edit.path} does not match the current file content"
                )
            near = any(
                (f.line or 0) - window <= rep.end_line and rep.start_line <= (f.line or 0) + window
                for f in (by_id[fid] for fid in ids)
            )
            if not near:
                raise ApplyRejected(f"replacement in {edit.path} is not near its finding")
            if _PLACEHOLDERS.search(rep.replacement):
                raise ApplyRejected(f"replacement in {edit.path} contains placeholder text")
            if any(p.search(rep.replacement) for p in TOKEN_PATTERNS[:5]):
                raise ApplyRejected(
                    f"replacement in {edit.path} looks like it contains a credential"
                )

        new_lines = list(lines)
        for rep in sorted(ordered, key=lambda r: r.start_line, reverse=True):
            text = _normalize_newlines(rep.replacement, newline)
            last_original = lines[rep.end_line - 1]
            if last_original.endswith(("\n", "\r")) and text and not text.endswith("\n"):
                text += newline
            new_lines[rep.start_line - 1 : rep.end_line] = _lines(text)
        new_content = "".join(new_lines)
        if new_content == source.content:
            continue
        if not new_content.strip():
            raise ApplyRejected(f"the proposal would empty {edit.path}")
        changed = _changed_line_count(source.content, new_content)
        original_count = max(len(lines), 1)
        if changed > max(40, original_count // 2):
            raise ApplyRejected(f"the change to {edit.path} is disproportionate ({changed} lines)")
        total_changed += changed
        edits.append(
            PlannedEdit(
                path=edit.path,
                finding_ids=ids,
                original_sha256=source.sha256,
                new_content=new_content,
                changed_lines=changed,
            )
        )

    if total_changed > policy.max_apply_changed_lines:
        raise ApplyRejected(
            f"the proposal changes {total_changed} lines; the limit is "
            f"{policy.max_apply_changed_lines}"
        )
    if len(edits) > policy.max_apply_files:
        raise ApplyRejected("the proposal edits more files than policy.max_apply_files")
    applied = {fid for e in edits for fid in e.finding_ids}
    skipped = tuple(
        (f.id, "the model did not propose a safe local fix")
        for f in findings
        if f.id not in applied
    )
    return ApplyPlan(edits=tuple(edits), skipped=skipped, explanation=proposal.explanation[:1000])
