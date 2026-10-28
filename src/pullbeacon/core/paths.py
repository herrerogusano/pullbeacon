"""Repository path normalisation and glob matching.

Globs use gitignore-like semantics restricted to what PullBeacon needs:
``*`` matches within one segment, ``**`` matches any number of segments, ``?`` one character
and ``[...]`` a character class. Patterns are always relative to the repository root.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

MAX_PATH_LENGTH = 1024

# Never sent to an LLM and never edited, whatever the configuration says.
SENSITIVE_PATTERNS: tuple[str, ...] = (
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.keystore",
    "**/*.jks",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.npmrc",
    "**/.pypirc",
    "**/.netrc",
    "**/.git-credentials",
    "**/*credentials*.json",
    "**/secrets/**",
    "**/.secrets/**",
)

# Never edited by `apply`: CI, PullBeacon's own trust anchors and VCS metadata.
APPLY_DENY_PATTERNS: tuple[str, ...] = (
    *SENSITIVE_PATTERNS,
    ".github/**",
    "**/.pullbeacon.yml",
    "**/.pullbeacon.yaml",
    "action.yml",
    "action.yaml",
    "**/.git/**",
    "**/.gitmodules",
    "**/.gitattributes",
    "**/CODEOWNERS",
)

BINARY_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip", ".gz",
        ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".class", ".so", ".dll",
        ".dylib", ".exe", ".bin", ".o", ".a", ".pyc", ".woff", ".woff2", ".ttf", ".otf",
        ".eot", ".mp3", ".mp4", ".mov", ".avi", ".wasm", ".sqlite", ".db", ".parquet",
    }
)  # fmt: skip

GENERATED_PATTERNS: tuple[str, ...] = (
    "**/*.lock",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.map",
    "**/generated/**",
    "**/__generated__/**",
    "**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
)


class UnsafePathError(ValueError):
    """A path or pattern escapes the repository or is otherwise unsafe."""


def normalize_repo_path(path: str) -> str:
    """Return a canonical repository-relative POSIX path or raise ``UnsafePathError``."""
    if not path or len(path) > MAX_PATH_LENGTH:
        raise UnsafePathError("path is empty or too long")
    if "\x00" in path or "\\" in path:
        raise UnsafePathError("path contains a NUL byte or backslash")
    if any(ord(ch) < 32 for ch in path):
        raise UnsafePathError("path contains control characters")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or path.startswith("~"):
        raise UnsafePathError("path must be relative to the repository root")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError("path contains empty, '.' or '..' segments")
    return path


def validate_glob(pattern: str) -> str:
    """Validate a configuration glob. Same rules as paths, plus wildcard syntax."""
    if not pattern or len(pattern) > 256:
        raise UnsafePathError("glob is empty or longer than 256 characters")
    if "\x00" in pattern or "\\" in pattern or any(ord(ch) < 32 for ch in pattern):
        raise UnsafePathError("glob contains NUL, backslash or control characters")
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:", pattern) or pattern.startswith("~"):
        raise UnsafePathError("glob must be relative to the repository root")
    for part in pattern.split("/"):
        if part in ("", ".", ".."):
            raise UnsafePathError("glob contains empty, '.' or '..' segments")
        if "**" in part and part != "**":
            raise UnsafePathError("'**' must be a whole path segment")
    if pattern.count("[") != pattern.count("]"):
        raise UnsafePathError("glob has unbalanced brackets")
    _compile(pattern)
    return pattern


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    regex = ""
    segments = pattern.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            regex += ".*" if last else "(?:[^/]+/)*"
            continue
        regex += _segment_to_regex(segment)
        if not last:
            regex += "/"
    return re.compile(rf"^{regex}$", re.DOTALL)


def _segment_to_regex(segment: str) -> str:
    out = ""
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "*":
            out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "[":
            end = segment.find("]", i + 1)
            if end == -1:
                raise UnsafePathError("glob has an unterminated character class")
            body = segment[i + 1 : end]
            negate = body.startswith("!")
            if negate:
                body = body[1:]
            body = body.replace("\\", "\\\\").replace("^", "\\^")
            out += f"[{'^' if negate else ''}{body}]"
            i = end
        else:
            out += re.escape(ch)
        i += 1
    return out


def matches(path: str, pattern: str) -> bool:
    return _compile(pattern).match(path) is not None


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(matches(path, pattern) for pattern in patterns)


def is_sensitive(path: str) -> bool:
    return matches_any(path, SENSITIVE_PATTERNS)


def is_apply_denied(path: str) -> bool:
    return matches_any(path, APPLY_DENY_PATTERNS)


def is_binary_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    dot = name.rfind(".")
    return dot != -1 and name[dot:] in BINARY_EXTENSIONS


def is_generated(path: str) -> bool:
    return matches_any(path, GENERATED_PATTERNS)
