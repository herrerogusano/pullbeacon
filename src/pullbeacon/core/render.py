"""Markdown rendering for GitHub. All model-generated text passes through ``sanitize``."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from pullbeacon.domain.models import Finding, ReviewReport, Severity, SkippedFile, Usage

MAX_REVIEW_BODY = 60_000
MAX_COMMENT_BODY = 60_000
MAX_FIELD = 1500
DISCLAIMER = (
    "PullBeacon does not run code or tests. Findings are model-generated and only validated "
    "against the diff; verify them before acting."
)

_MENTION = re.compile(r"(?<![\w`])@(?=[A-Za-z0-9])")
_HTML_COMMENT = re.compile(r"<!--|-->")
_IMAGE_OR_LINK_JS = re.compile(r"(?i)javascript:")


def sanitize(text: str, limit: int = MAX_FIELD) -> str:
    """Neutralise untrusted text: no mentions/pings, no hidden HTML comments or fake markers."""
    text = text.replace("\r\n", "\n")
    text = _HTML_COMMENT.sub(lambda m: "&lt;!--" if m.group(0) == "<!--" else "--&gt;", text)
    text = _MENTION.sub("@\u200b", text)
    text = _IMAGE_OR_LINK_JS.sub("javascript&#58;", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def code_block(text: str, limit: int = 800) -> str:
    """Fenced block whose fence cannot be closed by the content."""
    content = sanitize(text, limit)
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{content}\n{fence}"


SEVERITY_LABEL = {
    Severity.CRITICAL: "CRITICAL",
    Severity.HIGH: "HIGH",
    Severity.MEDIUM: "MEDIUM",
    Severity.LOW: "LOW",
}


def finding_comment(finding: Finding, *, apply_available: bool) -> str:
    parts = [
        f"**{finding.id} · {SEVERITY_LABEL[finding.severity]} · {finding.category.value}** — "
        f"{sanitize(finding.title, 200)}",
        "",
        sanitize(finding.explanation),
    ]
    if finding.impact:
        parts += ["", f"**Impact:** {sanitize(finding.impact, 600)}"]
    parts += ["", "**Evidence:**", code_block(finding.evidence, 400)]
    if finding.suggested_fix:
        parts += ["", f"**Suggested fix (not verified):** {sanitize(finding.suggested_fix, 1200)}"]
    commands = [f"`@pullbeacon explain {finding.id}`"]
    if apply_available and finding.can_apply:
        commands.append(f"`@pullbeacon apply {finding.id}`")
    parts += [
        "",
        f"<sub>PullBeacon · confidence {finding.confidence:.2f} · {' · '.join(commands)}</sub>",
    ]
    return "\n".join(parts)[:MAX_COMMENT_BODY]


def usage_line(usage: Usage, simulated: bool) -> str:
    tokens = (
        f"{usage.input_tokens or 0} in / {usage.output_tokens or 0} out tokens"
        if usage.input_tokens is not None
        else "tokens unknown"
    )
    if simulated:
        cost = "simulated (no provider call)"
    elif usage.cost_usd is None:
        cost = "cost unknown"
    else:
        cost = f"~{usage.cost_usd:.4f} USD estimated by LiteLLM pricing"
    return f"{usage.llm_calls} LLM call(s), {tokens}, {cost}"


def _skips(skipped: Iterable[SkippedFile]) -> list[str]:
    counts = Counter(s.reason for s in skipped)
    return [f"{reason.replace('_', ' ')}: {n}" for reason, n in sorted(counts.items())]


def review_summary(
    report: ReviewReport,
    *,
    not_inline: Iterable[tuple[Finding, str]] = (),
    apply_available: bool,
    marker: str,
) -> str:
    counts = Counter(f.severity for f in report.findings)
    severity_line = (
        ", ".join(f"{counts[s]} {s.value}" for s in Severity if counts[s]) or "no findings"
    )
    lines = [
        "## PullBeacon review",
        "",
        f"Commit `{report.head_sha[:12]}` · model `{sanitize(report.model, 100)}` "
        f"({report.provider}) · {report.mode} review",
        "",
        f"**Result:** {severity_line}. Reviewed {len(report.reviewed_files)} file(s)"
        + (f", skipped {len(report.skipped_files)}" if report.skipped_files else "")
        + ".",
    ]
    if report.truncated:
        lines += [
            "",
            "> **Partial review:** some files were not reviewed because of size, file-count or "
            "budget limits. The PR was not reviewed in full.",
        ]
    if report.summary:
        lines += ["", sanitize(report.summary)]
    extra = list(not_inline)
    if extra:
        lines += ["", "### Findings not posted inline", ""]
        for finding, reason in extra:
            where = f"`{sanitize(finding.path or '?', 200)}" + (
                f":{finding.line}`" if finding.line else "`"
            )
            lines.append(
                f"- **{finding.id}** ({finding.severity.value}, {finding.category.value}) "
                f"{where} — {sanitize(finding.title, 200)} _(reason: {reason})_"
            )
    if report.skipped_files:
        lines += ["", "<details><summary>Skipped files</summary>", ""]
        lines += [f"- {s}" for s in _skips(report.skipped_files)]
        lines += ["", "</details>"]
    if report.warnings:
        lines += ["", "**Warnings:**"]
        lines += [f"- {sanitize(w, 300)}" for w in report.warnings[:10]]
    commands = ["`@pullbeacon review`"]
    if report.findings:
        commands.append(f"`@pullbeacon explain {report.findings[0].id}`")
    applicable = [f for f in report.findings if f.can_apply]
    if apply_available and applicable:
        commands += ["`@pullbeacon apply`", f"`@pullbeacon apply {applicable[0].id}`"]
    lines += [
        "",
        f"<sub>{usage_line(report.usage, report.simulated)} · "
        f"Commands: {', '.join(commands)}</sub>",
        "",
        f"<sub>{DISCLAIMER}</sub>",
        "",
        marker,
    ]
    body = "\n".join(lines)
    if len(body) > MAX_REVIEW_BODY:
        head = body[: MAX_REVIEW_BODY - len(marker) - 40]
        body = f"{head}\n\n…(truncated)\n\n{marker}"
    return body
