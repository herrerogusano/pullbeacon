"""Decide which changed files are reviewed, and why the others are skipped."""

from __future__ import annotations

from dataclasses import dataclass

from pullbeacon.config import PullBeaconConfig
from pullbeacon.core.diff import DiffParseError, FileDiff, build_file_diff
from pullbeacon.core.paths import (
    UnsafePathError,
    is_binary_path,
    is_generated,
    is_sensitive,
    matches_any,
    normalize_repo_path,
)
from pullbeacon.domain.models import ChangedFile, FileStatus, PullRequestSnapshot, SkippedFile


class SkipReason:
    UNSAFE_PATH = "unsafe_path"
    SENSITIVE = "sensitive_path"
    NOT_INCLUDED = "not_included_by_policy"
    EXCLUDED = "excluded_by_policy"
    BINARY = "binary_file"
    GENERATED = "generated_or_vendored"
    NO_PATCH = "no_patch_available"
    UNPARSEABLE = "unparseable_patch"
    UNCHANGED = "no_textual_changes"
    MAX_FILES = "limit_max_changed_files"
    MAX_LINES = "limit_max_changed_lines"
    MAX_CONTEXT = "limit_max_context_chars"


@dataclass(frozen=True)
class ReviewScope:
    diffs: tuple[FileDiff, ...]
    skipped: tuple[SkippedFile, ...]
    truncated: bool
    total_files: int

    @property
    def reviewed_paths(self) -> tuple[str, ...]:
        return tuple(d.path for d in self.diffs)

    def diff_for(self, path: str) -> FileDiff | None:
        return next((d for d in self.diffs if d.path == path), None)


def _classify(file: ChangedFile, config: PullBeaconConfig) -> tuple[FileDiff | None, str | None]:
    try:
        path = normalize_repo_path(file.path)
        if file.previous_path:
            normalize_repo_path(file.previous_path)
    except UnsafePathError:
        return None, SkipReason.UNSAFE_PATH
    if is_sensitive(path) or (file.previous_path and is_sensitive(file.previous_path)):
        return None, SkipReason.SENSITIVE
    policy = config.policy
    if not matches_any(path, policy.include):
        return None, SkipReason.NOT_INCLUDED
    if matches_any(path, policy.exclude):
        return None, SkipReason.EXCLUDED
    if is_binary_path(path):
        return None, SkipReason.BINARY
    if is_generated(path):
        return None, SkipReason.GENERATED
    if file.patch is None:
        # GitHub omits the patch for binary files and very large diffs.
        return None, SkipReason.NO_PATCH
    try:
        diff = build_file_diff(path, file.status.value, file.patch, file.previous_path)
    except DiffParseError:
        return None, SkipReason.UNPARSEABLE
    if not diff.hunks:
        return None, SkipReason.UNCHANGED
    return diff, None


def select_scope(snapshot: PullRequestSnapshot, config: PullBeaconConfig) -> ReviewScope:
    """Deterministic selection: files are considered in path order, limits applied in order."""
    limits = config.limits
    diffs: list[FileDiff] = []
    skipped: list[SkippedFile] = []
    changed_lines = 0
    truncated = snapshot.files_truncated

    for file in sorted(snapshot.files, key=lambda f: f.path):
        if file.status is FileStatus.UNCHANGED:
            skipped.append(SkippedFile(path=file.path, reason=SkipReason.UNCHANGED))
            continue
        diff, reason = _classify(file, config)
        if diff is None:
            skipped.append(SkippedFile(path=file.path, reason=reason or SkipReason.UNPARSEABLE))
            continue
        if len(diffs) >= limits.max_changed_files:
            skipped.append(SkippedFile(path=diff.path, reason=SkipReason.MAX_FILES))
            truncated = True
            continue
        size = diff.added_lines + diff.deleted_lines
        if changed_lines + size > limits.max_changed_lines:
            skipped.append(SkippedFile(path=diff.path, reason=SkipReason.MAX_LINES))
            truncated = True
            continue
        changed_lines += size
        diffs.append(diff)

    return ReviewScope(
        diffs=tuple(diffs),
        skipped=tuple(skipped),
        truncated=truncated,
        total_files=len(snapshot.files),
    )
