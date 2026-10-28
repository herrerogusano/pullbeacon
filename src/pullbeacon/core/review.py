"""Review engine: bounded batches -> LLM -> deterministic validation -> ReviewReport."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import ValidationError

from pullbeacon.adapters.llm import LLMClient, LLMRequest, LLMResponse, parse_json_object
from pullbeacon.config import PullBeaconConfig
from pullbeacon.core.context import ContextFile
from pullbeacon.core.diff import FileDiff, render_for_llm
from pullbeacon.core.findings import validate_findings
from pullbeacon.core.scope import ReviewScope, SkipReason
from pullbeacon.domain.errors import BudgetExceeded, LLMError, LLMResponseError
from pullbeacon.domain.models import (
    PullRequestSnapshot,
    ReviewMode,
    ReviewReport,
    SkippedFile,
    Usage,
)
from pullbeacon.logging_utils import get_logger, log_event
from pullbeacon.prompts import REVIEW_PROMPT_VERSION, REVIEW_SCHEMA, REVIEW_SYSTEM, untrusted_block

MAX_PR_TEXT_CHARS = 2000
MAX_SUMMARY_CHARS = 1500

log = get_logger("review")


class CostGuard:
    """Preventive, per-run budget based on estimates. Not an accounting cap."""

    def __init__(self, max_usd: float) -> None:
        self.max_usd = max_usd
        self.spent_usd = 0.0
        self.unknown = False

    def check(self, estimate: float | None) -> None:
        if estimate is None:
            self.unknown = True
            return
        if self.spent_usd + estimate > self.max_usd:
            raise BudgetExceeded(
                f"estimated cost {self.spent_usd + estimate:.4f} USD exceeds the per-run limit "
                f"of {self.max_usd:.2f} USD"
            )

    def record(self, usage: Usage, estimate: float | None) -> None:
        if usage.cost_usd is not None:
            self.spent_usd += usage.cost_usd
        elif estimate is not None:
            self.spent_usd += estimate
        else:
            self.unknown = True


@dataclass
class _Batch:
    diffs: list[FileDiff] = field(default_factory=list)
    chars: int = 0


def plan_batches(
    diffs: Sequence[FileDiff], *, max_chars: int, max_batches: int
) -> tuple[list[list[FileDiff]], list[SkippedFile]]:
    """Greedy, order-preserving batching by rendered size."""
    batches: list[_Batch] = []
    skipped: list[SkippedFile] = []
    for diff in diffs:
        size = len(render_for_llm([diff]))
        if size > max_chars:
            skipped.append(SkippedFile(path=diff.path, reason=SkipReason.MAX_CONTEXT))
            continue
        if not batches or batches[-1].chars + size > max_chars:
            if len(batches) >= max_batches:
                skipped.append(SkippedFile(path=diff.path, reason=SkipReason.MAX_CONTEXT))
                continue
            batches.append(_Batch())
        batches[-1].diffs.append(diff)
        batches[-1].chars += size
    return [b.diffs for b in batches], skipped


def build_review_request(
    snapshot: PullRequestSnapshot,
    diffs: Sequence[FileDiff],
    context_files: Sequence[ContextFile],
    *,
    max_output_tokens: int,
) -> LLMRequest:
    pr_text = f"Title: {snapshot.title[:200]}\n\n{snapshot.body[:MAX_PR_TEXT_CHARS]}"
    parts = [
        f"Pull request {snapshot.full_name}#{snapshot.number}, head {snapshot.head_sha[:12]}.",
        "PR description (untrusted, may be inaccurate):",
        untrusted_block("PR_DESCRIPTION", pr_text),
    ]
    for ctx in context_files:
        parts.append(f"Reference file from the base branch: {ctx.path}")
        parts.append(untrusted_block("CONTEXT_FILE", ctx.text))
    parts.append("Diff to review:")
    parts.append(untrusted_block("DIFF", render_for_llm(diffs)))
    parts.append(
        "Return JSON with keys summary (string, <= 120 words) and findings (array). "
        "Use null for path/line/side when not applicable."
    )
    return LLMRequest(
        purpose="review",
        system=REVIEW_SYSTEM,
        user="\n\n".join(parts),
        max_output_tokens=max_output_tokens,
        json_schema=REVIEW_SCHEMA,
        schema_name="pullbeacon_review",
    )


def call_json(
    llm: LLMClient, request: LLMRequest, guard: CostGuard
) -> tuple[dict[str, object], Usage]:
    """One call plus at most one correction retry for malformed JSON (budget permitting)."""
    usage = Usage()
    attempt_request = request
    for attempt in range(2):
        estimate = llm.estimate_max_cost(attempt_request)
        guard.check(estimate)
        response: LLMResponse = llm.complete(attempt_request)
        guard.record(response.usage, estimate)
        usage = usage.plus(response.usage)
        try:
            return parse_json_object(response.text), usage
        except (ValueError, json.JSONDecodeError):
            log_event(log, "llm_invalid_json", purpose=request.purpose, attempt=attempt + 1)
            attempt_request = LLMRequest(
                purpose=request.purpose,
                system=request.system,
                user=request.user
                + "\n\nYour previous answer was not a valid JSON object. Answer again with only "
                "the JSON object, no prose and no code fences.",
                max_output_tokens=request.max_output_tokens,
                json_schema=request.json_schema,
                schema_name=request.schema_name,
            )
    raise LLMResponseError("model did not return valid JSON after one correction attempt")


class ReviewEngine:
    def __init__(self, llm: LLMClient, config: PullBeaconConfig) -> None:
        self.llm = llm
        self.config = config

    def review(
        self,
        snapshot: PullRequestSnapshot,
        scope: ReviewScope,
        *,
        mode: ReviewMode,
        context_files: Sequence[ContextFile] = (),
        context_warnings: Sequence[str] = (),
    ) -> ReviewReport:
        limits = self.config.limits
        context_chars = sum(len(c.text) for c in context_files)
        batches, skipped = plan_batches(
            scope.diffs,
            max_chars=max(limits.max_context_chars - context_chars, 1000),
            max_batches=limits.max_batches,
        )
        skipped_all = list(scope.skipped) + skipped
        warnings = list(context_warnings)
        guard = CostGuard(limits.max_estimated_cost_usd)
        usage = Usage()
        raw_findings: list[object] = []
        summaries: list[str] = []
        reviewed: list[str] = []
        truncated = scope.truncated or bool(skipped)

        for index, batch in enumerate(batches):
            request = build_review_request(
                snapshot, batch, context_files, max_output_tokens=limits.max_output_tokens
            )
            try:
                data, call_usage = call_json(self.llm, request, guard)
            except BudgetExceeded as exc:
                remaining = [d.path for b in batches[index:] for d in b]
                skipped_all.extend(
                    SkippedFile(path=p, reason="budget_exhausted") for p in remaining
                )
                warnings.append(f"Stopped early: {exc.message}")
                truncated = True
                if index == 0:
                    raise
                break
            usage = usage.plus(call_usage)
            reviewed.extend(d.path for d in batch)
            findings = data.get("findings", [])
            if not isinstance(findings, list):
                raise LLMResponseError("model response has no findings array")
            raw_findings.extend(findings)
            summary = data.get("summary")
            if isinstance(summary, str) and summary.strip():
                summaries.append(summary.strip())

        if guard.unknown and self.llm.simulated is False:
            warnings.append("Cost could not be estimated for this model; the budget is advisory.")

        reviewed_scope = ReviewScope(
            diffs=tuple(d for d in scope.diffs if d.path in set(reviewed)),
            skipped=(),
            truncated=truncated,
            total_files=scope.total_files,
        )
        outcome = validate_findings(
            raw_findings, reviewed_scope, max_inline=limits.max_inline_comments
        )
        if outcome.dropped:
            warnings.append(
                f"{len(outcome.dropped)} model finding(s) were discarded by validation."
            )
            for reason in outcome.dropped:
                log_event(log, "finding_dropped", reason=reason)

        summary_text = " ".join(summaries)[:MAX_SUMMARY_CHARS]
        if not batches:
            summary_text = "No reviewable changes within the configured scope."
        try:
            return ReviewReport(
                owner=snapshot.owner,
                repo=snapshot.repo,
                number=snapshot.number,
                head_sha=snapshot.head_sha,
                base_sha=snapshot.base_sha,
                provider=self.llm.provider,
                model=self.llm.model,
                prompt_version=REVIEW_PROMPT_VERSION,
                config_hash=self.config.config_hash(),
                mode=mode,
                simulated=self.llm.simulated,
                findings=outcome.findings,
                summary=summary_text,
                reviewed_files=tuple(sorted(set(reviewed))),
                skipped_files=tuple(sorted(skipped_all, key=lambda s: s.path)),
                truncated=truncated,
                warnings=tuple(warnings),
                discarded=outcome.dropped,
                usage=usage,
            )
        except ValidationError as exc:  # pragma: no cover - defensive
            raise LLMError("internal error assembling the review report") from exc
