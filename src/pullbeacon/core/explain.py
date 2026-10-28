"""`@pullbeacon explain PB-001`: a read-only, more detailed explanation of one finding."""

from __future__ import annotations

from dataclasses import dataclass

from pullbeacon.adapters.llm import LLMClient, LLMRequest
from pullbeacon.core.diff import FileDiff
from pullbeacon.core.markers import MarkerFinding
from pullbeacon.core.render import DISCLAIMER, sanitize
from pullbeacon.core.review import CostGuard, call_json
from pullbeacon.domain.errors import BudgetExceeded, LLMError
from pullbeacon.domain.models import Side, Usage
from pullbeacon.prompts import EXPLAIN_SYSTEM, untrusted_block

EXCERPT_RADIUS = 6


@dataclass(frozen=True)
class Explanation:
    body: str
    usage: Usage
    used_llm: bool


def diff_excerpt(diff: FileDiff | None, line: int | None, side: Side | None) -> str:
    if diff is None:
        return ""
    lines = list(diff.lines())
    if line is None or side is None:
        selected = lines[: EXCERPT_RADIUS * 2]
    else:
        index = next(
            (
                i
                for i, ln in enumerate(lines)
                if (ln.new_line if side is Side.RIGHT else ln.old_line) == line
            ),
            None,
        )
        if index is None:
            return ""
        selected = lines[max(index - EXCERPT_RADIUS, 0) : index + EXCERPT_RADIUS + 1]
    rendered = []
    for ln in selected:
        if ln.kind == "add":
            rendered.append(f"| R{ln.new_line} + {ln.text}")
        elif ln.kind == "del":
            rendered.append(f"| L{ln.old_line} - {ln.text}")
        else:
            rendered.append(f"| R{ln.new_line} L{ln.old_line}   {ln.text}")
    return "\n".join(rendered)


def _deterministic(finding: MarkerFinding, note: str) -> str:
    parts = [sanitize(finding.explanation or finding.title)]
    if finding.fix:
        parts += ["", f"**Suggested fix (not verified):** {sanitize(finding.fix)}"]
    parts += ["", f"_{note}_"]
    return "\n".join(parts)


def explain(
    finding: MarkerFinding,
    *,
    head_sha: str,
    diff: FileDiff | None,
    llm: LLMClient | None,
    max_output_tokens: int,
    max_cost_usd: float,
) -> Explanation:
    header = (
        f"### PullBeacon · {finding.id} explained\n\n"
        f"**{finding.severity.value} · {finding.category.value}** — {sanitize(finding.title, 200)}"
        + (f" (`{sanitize(finding.path or '', 200)}:{finding.line}`)" if finding.line else "")
        + f" · commit `{head_sha[:12]}`\n"
    )
    footer = f"\n\n<sub>No code was changed. {DISCLAIMER}</sub>"
    if llm is None:
        text = _deterministic(finding, "No model is configured; this is the original finding.")
        return Explanation(header + "\n" + text + footer, Usage(), used_llm=False)

    excerpt = diff_excerpt(diff, finding.line, finding.side)
    finding_text = (
        f"id: {finding.id}\nseverity: {finding.severity.value}\n"
        f"category: {finding.category.value}\ntitle: {finding.title}\n"
        f"explanation: {finding.explanation}\nsuggested_fix: {finding.fix}"
    )
    request = LLMRequest(
        purpose="explain",
        system=EXPLAIN_SYSTEM,
        user="\n\n".join(
            [
                "Finding (from a previous automated review):",
                untrusted_block("FINDING", finding_text),
                "Diff excerpt around the finding:",
                untrusted_block("DIFF_EXCERPT", excerpt or "(no excerpt available)"),
            ]
        ),
        max_output_tokens=min(max_output_tokens, 1200),
    )
    try:
        data, usage = call_json(llm, request, CostGuard(max_cost_usd))
        raw = data.get("explanation")
        if not isinstance(raw, str) or not raw.strip():
            raise LLMError("empty explanation")
        text = raw
    except (LLMError, BudgetExceeded):
        text = _deterministic(finding, "The model was unavailable; this is the original finding.")
        return Explanation(header + "\n" + text + footer, Usage(), used_llm=False)
    return Explanation(header + "\n" + sanitize(text, 3000) + footer, usage, used_llm=True)
