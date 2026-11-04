"""Decide which changed files are reviewed, and why the others are skipped."""

from __future__ import annotations

from dataclasses import dataclass

from pullbeacon.adapters.github import GitHubClient
from pullbeacon.config import PullBeaconConfig
from pullbeacon.core.diff import DiffParseError, FileDiff, build_file_diff, diff_from_contents
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


def _policy_skip(file: ChangedFile, config: PullBeaconConfig) -> str | None:
    """Why policy keeps this file out of the review, whatever its diff looks like."""
    try:
        path = normalize_repo_path(file.path)
        if file.previous_path:
            normalize_repo_path(file.previous_path)
    except UnsafePathError:
        return SkipReason.UNSAFE_PATH
    if is_sensitive(path) or (file.previous_path and is_sensitive(file.previous_path)):
        return SkipReason.SENSITIVE
    policy = config.policy
    if not matches_any(path, policy.include):
        return SkipReason.NOT_INCLUDED
    if matches_any(path, policy.exclude):
        return SkipReason.EXCLUDED
    if is_binary_path(path):
        return SkipReason.BINARY
    if is_generated(path):
        return SkipReason.GENERATED
    return None


def _text_at(
    client: GitHubClient, snapshot: PullRequestSnapshot, path: str, sha: str, max_bytes: int
) -> str | None:
    raw = client.get_file_content(snapshot.owner, snapshot.repo, path, sha, max_bytes=max_bytes)
    if raw is None or b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def rebuild_missing_patches(
    client: GitHubClient, snapshot: PullRequestSnapshot, config: PullBeaconConfig
) -> PullRequestSnapshot:
    """Build the diff of files GitHub returns without a patch, which it does for large ones.

    Otherwise the largest change of a pull request is the one that gets no review. Only files that
    policy would review are read, as data, at the exact commits of the pull request. Deleted files
    are left out: reading them costs more than a review of removed code is worth.
    """
    limits = config.limits
    files = []
    for file in snapshot.files:
        if (
            file.patch is not None
            or file.status in {FileStatus.UNCHANGED, FileStatus.REMOVED}
            or file.changes > limits.max_changed_lines
            or _policy_skip(file, config) is not None
        ):
            files.append(file)
            continue
        new = _text_at(client, snapshot, file.path, snapshot.head_sha, limits.max_file_bytes)
        old: str | None = ""
        if file.status is not FileStatus.ADDED:
            base_path = file.previous_path or file.path
            old = _text_at(client, snapshot, base_path, snapshot.base_sha, limits.max_file_bytes)
        if new is None or old is None:
            files.append(file)
            continue
        patch = diff_from_contents(old, new)
        files.append(file.model_copy(update={"patch": patch, "patch_rebuilt": True}))
    return snapshot.model_copy(update={"files": tuple(files)})


def _classify(file: ChangedFile, config: PullBeaconConfig) -> tuple[FileDiff | None, str | None]:
    reason = _policy_skip(file, config)
    if reason is not None:
        return None, reason
    if file.patch is None:
        # GitHub omits the patch for binary files and very large diffs, and it was not rebuilt.
        return None, SkipReason.NO_PATCH
    path = normalize_repo_path(file.path)
    try:
        diff = build_file_diff(
            path, file.status.value, file.patch, file.previous_path, rebuilt=file.patch_rebuilt
        )
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
