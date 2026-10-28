"""Parser for ``@pullbeacon`` commands in PR conversation comments.

``@pullbeacon`` is plain text interpreted by PullBeacon, not a GitHub account mention; GitHub
will not autocomplete it. A command must start a line (after optional whitespace). Text inside
fenced code blocks, inline code, block quotes and HTML comments is ignored, so quoting a
command never triggers it. Matching is case-insensitive. Only the first command runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MAX_COMMENT_CHARS = 20_000
CommandName = Literal["review", "explain", "apply", "help"]

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_COMMAND = re.compile(
    r"^\s*@pullbeacon(?![\w\-])(?:[ \t]+(?P<verb>[A-Za-z\-]+))?(?:[ \t]+(?P<args>[^\n]*))?$",
    re.IGNORECASE,
)
_FINDING_ID = re.compile(r"^PB-\d{3,4}$", re.IGNORECASE)

ALIASES = {"review": "review", "re-review": "review", "explain": "explain", "apply": "apply"}

HELP_TEXT = (
    "PullBeacon commands (write a command at the start of a line in a PR comment):\n\n"
    "- `@pullbeacon review` — run a new review of the current commit\n"
    "- `@pullbeacon explain PB-001` — explain one finding (no code changes)\n"
    "- `@pullbeacon apply` — apply all applicable findings of the latest review\n"
    "- `@pullbeacon apply PB-001` — apply one finding\n\n"
    "Commands require write access to the repository. `@pullbeacon` is interpreted by the "
    "PullBeacon workflow; it is not a GitHub user and may not autocomplete."
)


@dataclass(frozen=True)
class Command:
    name: CommandName
    finding_id: str | None = None
    error: str | None = None  # set for malformed commands; answered with help, no LLM


def _visible_lines(body: str) -> list[str]:
    text = _HTML_COMMENT.sub("", body[:MAX_COMMENT_CHARS].replace("\r\n", "\n"))
    lines: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker[0] * len(marker)
            elif marker.startswith(fence):
                fence = None
            continue
        if fence is not None or line.lstrip().startswith(">"):
            continue
        lines.append(_INLINE_CODE.sub("", line))
    return lines


def parse_command(body: str) -> Command | None:
    """Return the first command in the comment, or ``None`` if there is none."""
    for line in _visible_lines(body):
        match = _COMMAND.match(line)
        if not match:
            continue
        verb = (match.group("verb") or "").lower()
        args = (match.group("args") or "").split()
        if not verb or verb == "help":
            return Command("help")
        name = ALIASES.get(verb)
        if name is None:
            return Command("help", error=f"unknown command '{verb[:30]}'")
        if name == "review":
            return (
                Command("review")
                if not args
                else Command("help", error="review takes no arguments")
            )
        if len(args) > 1:
            return Command("help", error=f"{name} accepts at most one finding id")
        finding = args[0].upper() if args else None
        if finding is not None and not _FINDING_ID.match(finding):
            return Command("help", error="finding ids look like PB-001")
        if name == "explain":
            if finding is None:
                return Command("help", error="explain needs a finding id, e.g. PB-001")
            return Command("explain", finding_id=finding)
        return Command("apply", finding_id=finding)
    return None
