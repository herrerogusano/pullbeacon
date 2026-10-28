"""Optional extra context: literal files from the trusted base commit, bounded by size."""

from __future__ import annotations

from dataclasses import dataclass

from pullbeacon.adapters.github import GitHubClient
from pullbeacon.config import PullBeaconConfig
from pullbeacon.core.paths import is_sensitive
from pullbeacon.domain.models import PullRequestSnapshot


@dataclass(frozen=True)
class ContextFile:
    path: str
    text: str


def fetch_context_files(
    client: GitHubClient, snapshot: PullRequestSnapshot, config: PullBeaconConfig, *, budget: int
) -> tuple[tuple[ContextFile, ...], tuple[str, ...]]:
    """Read ``policy.context_files`` at ``base_sha``. Returns ``(files, warnings)``.

    Only the base commit is used: the PR cannot inject "context" that the reviewer would treat
    as trusted project documentation.
    """
    files: list[ContextFile] = []
    warnings: list[str] = []
    remaining = budget
    for path in config.policy.context_files:
        if is_sensitive(path):
            warnings.append(f"context file {path} skipped: sensitive path")
            continue
        raw = client.get_file_content(
            snapshot.owner,
            snapshot.repo,
            path,
            snapshot.base_sha,
            max_bytes=config.limits.max_file_bytes,
        )
        if raw is None:
            warnings.append(f"context file {path} skipped: missing, not a file or too large")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(f"context file {path} skipped: not UTF-8 text")
            continue
        if len(text) > remaining:
            warnings.append(f"context file {path} skipped: context budget exhausted")
            continue
        remaining -= len(text)
        files.append(ContextFile(path=path, text=text))
    return tuple(files), tuple(warnings)
