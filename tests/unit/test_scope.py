from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from pullbeacon.config import parse_config
from pullbeacon.core.diff import render_for_llm
from pullbeacon.core.scope import (
    MAX_REBUILT_FILES,
    SkipReason,
    rebuild_missing_patches,
    select_scope,
)
from pullbeacon.domain.errors import GitHubAPIError
from pullbeacon.domain.models import ChangedFile, PullRequestSnapshot, Side

FIXTURES = Path(__file__).parents[1] / "fixtures"


def snapshot(files: list[ChangedFile]) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        owner="acme",
        repo="shop",
        number=1,
        author="dev",
        state="open",
        base_ref="main",
        base_sha="a" * 40,
        head_ref="feature",
        head_sha="b" * 40,
        base_repo_full_name="acme/shop",
        head_repo_full_name="acme/shop",
        files=tuple(files),
        fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def changed(path: str, patch: str | None = "@@ -1 +1 @@\n-a\n+b", **kw: object) -> ChangedFile:
    return ChangedFile.model_validate(
        {
            "path": path,
            "status": "modified",
            "patch": patch,
            "additions": 1,
            "deletions": 1,
            "changes": 2,
            **kw,
        }
    )


def reasons(scope_skipped: tuple[object, ...]) -> dict[str, str]:
    return {s.path: s.reason for s in scope_skipped}  # type: ignore[attr-defined]


def test_skip_reasons() -> None:
    config = parse_config("version: 1\npolicy: {include: ['**'], exclude: ['docs/**']}\n")
    scope = select_scope(
        snapshot(
            [
                changed("src/ok.py"),
                changed("src/big.py", patch=None),
                changed("img/a.png", patch=None),
                changed("config/.env"),
                changed("docs/guide.md"),
                changed("poetry.lock"),
                changed("src/broken.py", patch="@@ -1,5 +1,5 @@\n-a\n+b"),
                changed("src/empty.py", patch=""),
                changed("src/../../etc/passwd"),
            ]
        ),
        config,
    )
    assert scope.reviewed_paths == ("src/ok.py",)
    assert reasons(scope.skipped) == {
        "src/big.py": SkipReason.NO_PATCH,
        "img/a.png": SkipReason.BINARY,
        "config/.env": SkipReason.SENSITIVE,
        "docs/guide.md": SkipReason.EXCLUDED,
        "poetry.lock": SkipReason.GENERATED,
        "src/broken.py": SkipReason.UNPARSEABLE,
        "src/empty.py": SkipReason.UNCHANGED,
        "src/../../etc/passwd": SkipReason.UNSAFE_PATH,
    }


class _Contents:
    """Just what ``rebuild_missing_patches`` reads, with a record of what it asked for."""

    def __init__(self, files: dict[tuple[str, str], bytes]) -> None:
        self.files = files
        self.read: list[tuple[str, str]] = []

    def get_file_content(
        self, owner: str, repo: str, path: str, ref_sha: str, *, max_bytes: int
    ) -> bytes | None:
        self.read.append((path, ref_sha))
        data = self.files.get((path, ref_sha))
        return data if data is not None and len(data) <= max_bytes else None


BASE, HEAD = "a" * 40, "b" * 40


def _rebuilt(file: ChangedFile, contents: _Contents, config: str = "version: 1\n") -> ChangedFile:
    parsed = parse_config(config)
    client: Any = contents
    return rebuild_missing_patches(client, snapshot([file]), parsed).files[0]


def test_new_file_without_a_patch_is_rebuilt_with_real_line_numbers() -> None:
    contents = _Contents({("src/big.py", HEAD): b"one\ntwo\nthree\n"})
    file = _rebuilt(changed("src/big.py", patch=None, status="added"), contents)
    assert file.patch_rebuilt
    (diff,) = select_scope(snapshot([file]), parse_config("version: 1\n")).diffs
    assert diff.rebuilt
    assert [(ln.new_line, ln.text) for ln in diff.lines()] == [
        (1, "one"),
        (2, "two"),
        (3, "three"),
    ]
    assert contents.read == [("src/big.py", HEAD)]


def test_modified_file_without_a_patch_is_diffed_against_the_base() -> None:
    old = "".join(f"line {n}\n" for n in range(1, 21))
    new = old.replace("line 10\n", "line ten\n")
    contents = _Contents({("a.py", BASE): old.encode(), ("a.py", HEAD): new.encode()})
    file = _rebuilt(changed("a.py", patch=None), contents)
    (diff,) = select_scope(snapshot([file]), parse_config("version: 1\n")).diffs
    added = [ln for ln in diff.lines() if ln.kind == "add"]
    assert [(ln.new_line, ln.text) for ln in added] == [(10, "line ten")]
    deleted = [ln for ln in diff.lines() if ln.kind == "del"]
    assert [(ln.old_line, ln.text) for ln in deleted] == [(10, "line 10")]


def test_rebuilt_line_numbers_count_lines_as_git_does() -> None:
    # A form feed and U+2028 are one line each for git, but not for str.splitlines.
    new = "first\fstill first\nsecond\u2028still second\nthird\n"
    contents = _Contents({("a.py", HEAD): new.encode()})
    file = _rebuilt(changed("a.py", patch=None, status="added"), contents)
    (diff,) = select_scope(snapshot([file]), parse_config("version: 1\n")).diffs
    assert [ln.new_line for ln in diff.lines()] == [1, 2, 3]
    assert diff.line_at(3, Side.RIGHT).text == "third"  # type: ignore[union-attr]


def test_rebuilding_stops_at_the_file_limit() -> None:
    names = [f"src/f{n:03d}.py" for n in range(MAX_REBUILT_FILES + 3)]
    contents = _Contents({(name, HEAD): b"x\n" for name in names})
    files = [changed(name, patch=None, status="added") for name in names]
    rebuilt = rebuild_missing_patches(
        cast(Any, contents), snapshot(files), parse_config("version: 1\n")
    ).files
    assert sum(f.patch_rebuilt for f in rebuilt) == MAX_REBUILT_FILES
    assert len(contents.read) == MAX_REBUILT_FILES


def test_rebuilding_stops_when_changes_exceed_the_line_limit() -> None:
    contents = _Contents({("a.py", HEAD): b"x\n", ("b.py", HEAD): b"y\n"})
    files = [
        changed("a.py", patch=None, status="added", changes=8),
        changed("b.py", patch=None, status="added", changes=8),
    ]
    config = parse_config("version: 1\nlimits: {max_changed_lines: 10}\n")
    rebuilt = rebuild_missing_patches(cast(Any, contents), snapshot(files), config).files
    assert [f.patch_rebuilt for f in rebuilt] == [True, False]


def test_a_rename_without_changes_is_not_read() -> None:
    contents = _Contents({})
    file = changed("new.py", patch=None, status="renamed", previous_path="old.py", changes=0)
    assert _rebuilt(file, contents).patch is None
    assert contents.read == []


def test_a_failed_read_leaves_the_file_skipped_and_the_rest_reviewed() -> None:
    class Failing(_Contents):
        def get_file_content(
            self, owner: str, repo: str, path: str, ref_sha: str, *, max_bytes: int
        ) -> bytes | None:
            raise GitHubAPIError("server error", status=502)

    file = changed("a.py", patch=None, status="added")
    assert _rebuilt(file, Failing({})).patch is None


def test_renamed_file_without_a_patch_reads_the_old_path_at_the_base() -> None:
    contents = _Contents({("old.py", BASE): b"a\n", ("new.py", HEAD): b"b\n"})
    file = _rebuilt(
        changed("new.py", patch=None, status="renamed", previous_path="old.py"), contents
    )
    assert file.patch_rebuilt
    assert ("old.py", BASE) in contents.read


@pytest.mark.parametrize(
    ("file", "config"),
    [
        (changed("config/.env", patch=None), "version: 1\n"),
        (changed("poetry.lock", patch=None), "version: 1\n"),
        (changed("img/a.png", patch=None), "version: 1\n"),
        (changed("web/a.ts", patch=None), "version: 1\npolicy: {include: ['**/*.py']}\n"),
        (changed("gone.py", patch=None, status="removed"), "version: 1\n"),
        (
            changed("huge.py", patch=None, changes=9000),
            "version: 1\nlimits: {max_changed_lines: 10}\n",
        ),
    ],
)
def test_files_that_would_not_be_reviewed_are_never_read(file: ChangedFile, config: str) -> None:
    contents = _Contents({(file.path, HEAD): b"secret\n", (file.path, BASE): b"secret\n"})
    rebuilt = _rebuilt(file, contents, config)
    assert contents.read == []
    assert rebuilt.patch is None


@pytest.mark.parametrize(
    ("data", "config"),
    [
        (b"binary\0data\n", "version: 1\n"),
        (b"\xff\xfe not utf-8", "version: 1\n"),
        (b"x" * 2000, "version: 1\nlimits: {max_file_bytes: 1024}\n"),
    ],
)
def test_content_that_cannot_be_read_as_text_stays_skipped(data: bytes, config: str) -> None:
    file = changed("src/a.py", patch=None, status="added")
    rebuilt = _rebuilt(file, _Contents({("src/a.py", HEAD): data}), config)
    assert rebuilt.patch is None
    scope = select_scope(snapshot([rebuilt]), parse_config(config))
    assert reasons(scope.skipped) == {"src/a.py": SkipReason.NO_PATCH}


def test_include_policy() -> None:
    config = parse_config("version: 1\npolicy: {include: ['**/*.py']}\n")
    scope = select_scope(snapshot([changed("src/a.py"), changed("web/a.ts")]), config)
    assert scope.reviewed_paths == ("src/a.py",)
    assert reasons(scope.skipped) == {"web/a.ts": SkipReason.NOT_INCLUDED}


def test_file_and_line_limits_truncate_deterministically() -> None:
    config = parse_config("version: 1\nlimits: {max_changed_files: 2}\n")
    scope = select_scope(snapshot([changed("c.py"), changed("a.py"), changed("b.py")]), config)
    assert scope.reviewed_paths == ("a.py", "b.py")
    assert scope.truncated is True
    assert reasons(scope.skipped) == {"c.py": SkipReason.MAX_FILES}

    config = parse_config("version: 1\nlimits: {max_changed_lines: 3}\n")
    scope = select_scope(snapshot([changed("a.py"), changed("b.py")]), config)
    assert scope.reviewed_paths == ("a.py",)
    assert reasons(scope.skipped) == {"b.py": SkipReason.MAX_LINES}


def test_fixture_scope_and_injection_stays_data() -> None:
    from pullbeacon.commands_local import snapshot_from_fixture

    data = json.loads((FIXTURES / "pr_two_hunks.json").read_text(encoding="utf-8"))
    _, snap = snapshot_from_fixture(data)
    scope = select_scope(snap, parse_config("version: 1\n"))
    assert scope.reviewed_paths == ("docs/README.md", "src/orders.py", "src/text_utils.py")
    rendered = render_for_llm(scope.diffs)
    injected = [line for line in rendered.splitlines() if "ignore all previous" in line]
    assert len(injected) == 1
    assert injected[0].startswith("| R5 + ")
    renamed = scope.diff_for("src/text_utils.py")
    assert renamed is not None
    assert renamed.previous_path == "src/utils.py"
