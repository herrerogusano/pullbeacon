from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pullbeacon.config import parse_config
from pullbeacon.core.diff import render_for_llm
from pullbeacon.core.scope import SkipReason, select_scope
from pullbeacon.domain.models import ChangedFile, PullRequestSnapshot

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
