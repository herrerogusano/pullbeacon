from __future__ import annotations

import pytest

from pullbeacon.core.paths import (
    UnsafePathError,
    is_apply_denied,
    is_binary_path,
    is_generated,
    is_sensitive,
    matches,
    normalize_repo_path,
)


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("src/app.py", "**/*.py", True),
        ("app.py", "**/*.py", True),
        ("src/deep/x/app.py", "src/**", True),
        ("srcx/app.py", "src/**", False),
        ("src/app.py", "*.py", False),
        ("app.py", "*.py", True),
        ("a/generated/b.ts", "**/generated/**", True),
        ("src/a1.py", "src/a?.py", True),
        ("src/ab.py", "src/a[bc].py", True),
        ("src/ad.py", "src/a[!bc].py", True),
        ("src/a.py.bak", "**/*.py", False),
    ],
)
def test_glob_matching(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, pattern) is expected


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "../x", "a/../b", "a//b", "./a", "a\\b", "C:/x", "~/x", "a\x00b", "a\nb"],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    with pytest.raises(UnsafePathError):
        normalize_repo_path(path)


def test_unusual_but_safe_paths_are_kept() -> None:
    for path in ["docs/my file.md", "src/ünïcode.py", "a/b c/$(rm -rf).py", "x/`id`.ts"]:
        assert normalize_repo_path(path) == path


def test_sensitive_and_denied_paths() -> None:
    assert is_sensitive(".env")
    assert is_sensitive("config/.env.production")
    assert is_sensitive("deploy/server.pem")
    assert not is_sensitive("src/environment.py")
    assert is_apply_denied(".github/workflows/ci.yml")
    assert is_apply_denied(".pullbeacon.yml")
    assert not is_apply_denied("src/app.py")


def test_binary_and_generated_detection() -> None:
    assert is_binary_path("assets/logo.PNG")
    assert not is_binary_path("Makefile")
    assert is_generated("uv.lock")
    assert is_generated("web/package-lock.json")
    assert is_generated("static/app.min.js")
    assert not is_generated("src/app.js")
