"""Unified diff parsing for GitHub ``patch`` fields and the commentable line map.

GitHub's pull request files API returns, per file, only the hunks (no ``---``/``+++`` header).
Review comments are addressed by real file line numbers plus a side: ``RIGHT`` for lines of the
new version (added or context), ``LEFT`` for lines of the old version (deleted or context).
A patch offset is *not* a line number; everything here is computed from hunk headers.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

from pullbeacon.domain.models import Side

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
NO_NEWLINE_MARKER = "\\ No newline at end of file"

LineKind = Literal["add", "del", "context"]


class DiffParseError(ValueError):
    """The patch is malformed or truncated; its lines must not be used for inline comments."""


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: LineKind
    old_line: int | None
    new_line: int | None
    text: str
    hunk_index: int


@dataclass(frozen=True, slots=True)
class Hunk:
    index: int
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True)
class FileDiff:
    path: str
    status: str
    previous_path: str | None
    hunks: tuple[Hunk, ...]
    crlf: bool = False
    _right: dict[int, DiffLine] = field(default_factory=dict, repr=False, compare=False)
    _left: dict[int, DiffLine] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        for line in self.lines():
            if line.new_line is not None:
                self._right[line.new_line] = line
            if line.old_line is not None:
                self._left[line.old_line] = line

    def lines(self) -> Iterator[DiffLine]:
        for hunk in self.hunks:
            yield from hunk.lines

    def is_commentable(self, line: int, side: Side) -> bool:
        return self.line_at(line, side) is not None

    def line_at(self, line: int, side: Side) -> DiffLine | None:
        return (self._right if side is Side.RIGHT else self._left).get(line)

    @property
    def added_lines(self) -> int:
        return sum(1 for line in self.lines() if line.kind == "add")

    @property
    def deleted_lines(self) -> int:
        return sum(1 for line in self.lines() if line.kind == "del")

    def changed_new_lines(self) -> list[int]:
        return [ln.new_line for ln in self.lines() if ln.kind == "add" and ln.new_line is not None]


def parse_patch(patch: str) -> tuple[tuple[Hunk, ...], bool]:
    """Parse a GitHub ``patch`` string. Returns ``(hunks, crlf)``.

    Raises ``DiffParseError`` when hunk counts do not match the header (truncated or
    malformed patches), so callers never attribute comments to guessed lines.
    """
    if patch == "":
        return (), False
    raw_lines = patch.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    crlf = any(line.endswith("\r") for line in raw_lines)

    hunks: list[Hunk] = []
    current: list[DiffLine] = []
    header: tuple[int, int, int, int, str] | None = None
    old_no = new_no = 0

    def close() -> None:
        if header is None:
            return
        old_start, old_count, new_start, new_count, section = header
        olds = sum(1 for ln in current if ln.kind != "add")
        news = sum(1 for ln in current if ln.kind != "del")
        if olds != old_count or news != new_count:
            raise DiffParseError(
                f"hunk {len(hunks) + 1} line counts do not match its header "
                f"(old {olds}/{old_count}, new {news}/{new_count})"
            )
        hunks.append(
            Hunk(len(hunks), old_start, old_count, new_start, new_count, section, tuple(current))
        )

    for raw in raw_lines:
        line = raw[:-1] if raw.endswith("\r") else raw
        match = HUNK_HEADER.match(line)
        if match:
            close()
            current = []
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            header = (old_start, old_count, new_start, new_count, match.group(5).strip())
            old_no, new_no = old_start, new_start
            continue
        if header is None:
            # GitHub patches start with a hunk header; anything else is not a patch we trust.
            raise DiffParseError("patch does not start with a hunk header")
        if line.startswith("\\"):
            # "\ No newline at end of file" (localised variants exist): no line number.
            continue
        index = len(hunks)
        if line.startswith("+"):
            current.append(DiffLine("add", None, new_no, line[1:], index))
            new_no += 1
        elif line.startswith("-"):
            current.append(DiffLine("del", old_no, None, line[1:], index))
            old_no += 1
        elif line.startswith(" ") or line == "":
            current.append(DiffLine("context", old_no, new_no, line[1:], index))
            old_no += 1
            new_no += 1
        else:
            raise DiffParseError("patch contains a line without a diff prefix")
    close()
    return tuple(hunks), crlf


def build_file_diff(
    path: str, status: str, patch: str, previous_path: str | None = None
) -> FileDiff:
    hunks, crlf = parse_patch(patch)
    return FileDiff(path=path, status=status, previous_path=previous_path, hunks=hunks, crlf=crlf)


def render_for_llm(diffs: Iterable[FileDiff]) -> str:
    """Render diffs as line-addressed data for the model.

    Every content line is prefixed with its coordinates, e.g. ``R12 +`` or ``L7 -`` or
    ``R13 L11``. Structural lines (``FILE``/``HUNK``) never carry a prefix, so file content cannot
    forge structure even if it contains text that looks like a header or an instruction.
    """
    out: list[str] = []
    for diff in diffs:
        rename = f" (renamed from {diff.previous_path})" if diff.previous_path else ""
        out.append(f"FILE {diff.path} [{diff.status}]{rename}")
        for hunk in diff.hunks:
            out.append(
                f"HUNK -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count}"
            )
            for line in hunk.lines:
                if line.kind == "add":
                    out.append(f"| R{line.new_line} + {line.text}")
                elif line.kind == "del":
                    out.append(f"| L{line.old_line} - {line.text}")
                else:
                    out.append(f"| R{line.new_line} L{line.old_line}   {line.text}")
        out.append("END FILE")
    return "\n".join(out)


def fingerprint(path: str, side: Side | None, anchor_text: str, category: str) -> str:
    """Stable identity for a finding, independent of model output order or wording."""
    normalized = " ".join(anchor_text.split())
    material = "\x00".join([path, side.value if side else "-", normalized, category])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
