"""Git object hashing (used to verify that fetched content is exactly the tree's blob)."""

from __future__ import annotations

import hashlib


def git_sha(kind: str, data: bytes) -> str:
    header = f"{kind} {len(data)}".encode() + bytes([0])
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()
