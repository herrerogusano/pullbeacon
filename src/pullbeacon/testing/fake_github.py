"""In-memory GitHub REST API served through ``httpx.MockTransport``.

It implements just the endpoints PullBeacon uses, with GitHub's response shapes, pagination
headers and status codes. Used by tests, by ``--fixture`` CLI mode and by the evaluation
harness, so the production client code path is exercised without network access.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

API = "https://api.github.com"
Handler = Callable[[httpx.Request], httpx.Response | None]


def git_sha(kind: str, data: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()


@dataclass
class TreeEntry:
    path: str
    mode: str
    type: str
    sha: str


@dataclass
class RecordedCall:
    method: str
    path: str
    query: dict[str, list[str]]
    body: Any


@dataclass
class FakeGitHub:
    owner: str = "acme"
    repo: str = "shop"
    pulls: dict[int, dict[str, Any]] = field(default_factory=dict)
    files: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    permissions: dict[str, str] = field(default_factory=dict)
    reviews: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    review_comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    issue_comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    trees: dict[str, list[TreeEntry]] = field(default_factory=dict)
    commits: dict[str, dict[str, Any]] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    protected_branches: set[str] = field(default_factory=set)
    bot_login: str = "github-actions[bot]"
    calls: list[RecordedCall] = field(default_factory=list)
    # (METHOD, path regex) -> list of statuses to return before behaving normally.
    failures: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    # Called before every request; lets tests mutate state (e.g. move a branch) mid-flow.
    before_request: Handler | None = None
    per_page_cap: int = 100
    _ids: int = 1000

    # ------------------------------------------------------------------ setup helpers

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    def add_commit(
        self, files: dict[str, str | bytes], *, parents: list[str] | None = None, message: str = ""
    ) -> str:
        entries = []
        for path, content in sorted(files.items()):
            data = content.encode("utf-8") if isinstance(content, str) else content
            sha = git_sha("blob", data)
            self.blobs[sha] = data
            entries.append(TreeEntry(path=path, mode="100644", type="blob", sha=sha))
        tree_sha = self._store_tree(entries)
        return self._store_commit(tree_sha, parents or [], message)

    def add_symlink(self, commit_sha: str, path: str, target: str) -> str:
        """Return a new commit equal to ``commit_sha`` plus a symlink entry."""
        tree = list(self.trees[self.commits[commit_sha]["tree"]["sha"]])
        data = target.encode()
        sha = git_sha("blob", data)
        self.blobs[sha] = data
        tree.append(TreeEntry(path=path, mode="120000", type="blob", sha=sha))
        return self._store_commit(self._store_tree(tree), [commit_sha], "symlink")

    def _store_tree(self, entries: list[TreeEntry]) -> str:
        entries = sorted(entries, key=lambda e: e.path)
        material = "\n".join(f"{e.mode} {e.type} {e.sha} {e.path}" for e in entries).encode()
        sha = git_sha("tree", material)
        self.trees[sha] = entries
        return sha

    def _store_commit(self, tree_sha: str, parents: list[str], message: str) -> str:
        material = f"tree {tree_sha}\n" + "".join(f"parent {p}\n" for p in parents) + message
        sha = git_sha("commit", material.encode())
        self.commits[sha] = {
            "sha": sha,
            "tree": {"sha": tree_sha},
            "parents": [{"sha": p} for p in parents],
            "message": message,
        }
        return sha

    def file_at(self, commit_sha: str, path: str) -> bytes | None:
        commit = self.commits.get(commit_sha)
        if commit is None:
            return None
        for entry in self.trees[commit["tree"]["sha"]]:
            if entry.path == path and entry.type == "blob":
                return self.blobs[entry.sha]
        return None

    def tree_of(self, commit_sha: str) -> dict[str, TreeEntry]:
        return {e.path: e for e in self.trees[self.commits[commit_sha]["tree"]["sha"]]}

    def add_pull(
        self,
        number: int,
        *,
        base_sha: str,
        head_sha: str,
        files: list[dict[str, Any]],
        author: str = "dev",
        head_ref: str = "feature",
        base_ref: str = "main",
        head_repo: str | None = None,
        state: str = "open",
        title: str = "Change",
        body: str = "",
        author_type: str = "User",
        draft: bool = False,
    ) -> dict[str, Any]:
        head_full = head_repo if head_repo is not None else self.full_name
        pr = {
            "number": number,
            "state": state,
            "title": title,
            "body": body,
            "draft": draft,
            "user": {"login": author, "type": author_type},
            "html_url": f"https://github.com/{self.full_name}/pull/{number}",
            "changed_files": len(files),
            "base": {
                "ref": base_ref,
                "sha": base_sha,
                "repo": {
                    "name": self.repo,
                    "full_name": self.full_name,
                    "owner": {"login": self.owner},
                },
            },
            "head": {
                "ref": head_ref,
                "sha": head_sha,
                "repo": None
                if head_repo == ""
                else {
                    "name": head_full.split("/")[1],
                    "full_name": head_full,
                    "owner": {"login": head_full.split("/")[0]},
                },
            },
        }
        self.pulls[number] = pr
        self.files[number] = files
        if head_full == self.full_name:
            self.refs[f"heads/{head_ref}"] = head_sha
        self.refs.setdefault(f"heads/{base_ref}", base_sha)
        return pr

    def move_head(self, number: int, new_sha: str) -> None:
        pr = self.pulls[number]
        pr["head"]["sha"] = new_sha
        self.refs[f"heads/{pr['head']['ref']}"] = new_sha

    @classmethod
    def from_fixture(cls, data: dict[str, Any]) -> FakeGitHub:
        """Build from a fixture: ``{"pull_request", "files", "trees"?, "permissions"?}``."""
        pr = data["pull_request"]
        base = pr["base"]["repo"]
        fake = cls(owner=base["owner"]["login"], repo=base["name"])
        for commit_sha, files in (data.get("trees") or {}).items():
            fake._register_fixture_commit(commit_sha, files)
        fake.pulls[pr["number"]] = copy.deepcopy(pr)
        fake.files[pr["number"]] = copy.deepcopy(data.get("files", []))
        head_repo = (pr["head"].get("repo") or {}).get("full_name")
        if head_repo == fake.full_name:
            fake.refs[f"heads/{pr['head']['ref']}"] = pr["head"]["sha"]
        fake.permissions.update(data.get("permissions") or {})
        for review in data.get("reviews") or []:
            fake.reviews.setdefault(pr["number"], []).append(copy.deepcopy(review))
        return fake

    def _register_fixture_commit(self, commit_sha: str, files: dict[str, str]) -> None:
        """Fixture commits keep their declared SHA so they match the PR payload."""
        entries = []
        for path, content in sorted(files.items()):
            data = content.encode("utf-8")
            sha = git_sha("blob", data)
            self.blobs[sha] = data
            entries.append(TreeEntry(path=path, mode="100644", type="blob", sha=sha))
        tree_sha = self._store_tree(entries)
        self.commits[commit_sha] = {
            "sha": commit_sha,
            "tree": {"sha": tree_sha},
            "parents": [],
            "message": "fixture",
        }

    # ------------------------------------------------------------------ transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def writes(self) -> list[RecordedCall]:
        return [c for c in self.calls if c.method != "GET"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        path = unquote(parts.path)
        query = parse_qs(parts.query)
        body = json.loads(request.content) if request.content else None
        self.calls.append(RecordedCall(request.method, path, query, body))
        if self.before_request is not None:
            early = self.before_request(request)
            if early is not None:
                return early
        for (method, pattern), statuses in self.failures.items():
            if method == request.method and re.search(pattern, path) and statuses:
                status = statuses.pop(0)
                return httpx.Response(status, json={"message": f"simulated {status}"})
        prefix = f"/repos/{self.owner}/{self.repo}"
        if not path.startswith(prefix + "/"):
            return _not_found()
        route = path[len(prefix) :]
        try:
            return self._route(request.method, route, query, body)
        except KeyError:
            return _not_found()

    def _route(
        self, method: str, route: str, query: dict[str, list[str]], body: Any
    ) -> httpx.Response:
        m = re.fullmatch(r"/pulls/(\d+)", route)
        if m and method == "GET":
            return httpx.Response(200, json=self.pulls[int(m.group(1))])
        m = re.fullmatch(r"/pulls/(\d+)/files", route)
        if m and method == "GET":
            return self._page(self.files[int(m.group(1))], query, route)
        m = re.fullmatch(r"/pulls/(\d+)/reviews", route)
        if m and method == "GET":
            return self._page(self.reviews.get(int(m.group(1)), []), query, route)
        if m and method == "POST":
            return self._create_review(int(m.group(1)), body)
        m = re.fullmatch(r"/pulls/(\d+)/comments", route)
        if m and method == "GET":
            return self._page(self.review_comments.get(int(m.group(1)), []), query, route)
        m = re.fullmatch(r"/issues/(\d+)/comments", route)
        if m and method == "GET":
            return self._page(self.issue_comments.get(int(m.group(1)), []), query, route)
        if m and method == "POST":
            return self._create_issue_comment(int(m.group(1)), body)
        m = re.fullmatch(r"/collaborators/([^/]+)/permission", route)
        if m and method == "GET":
            login = m.group(1)
            if login not in self.permissions:
                return _not_found()
            perm = self.permissions[login]
            legacy = {"maintain": "write", "triage": "read"}.get(perm, perm)
            return httpx.Response(200, json={"permission": legacy, "role_name": perm})
        m = re.fullmatch(r"/contents/(.+)", route)
        if m and method == "GET":
            return self._contents(m.group(1), query.get("ref", [""])[0])
        m = re.fullmatch(r"/branches/(.+)", route)
        if m and method == "GET":
            name = m.group(1)
            if f"heads/{name}" not in self.refs:
                return _not_found()
            sha = self.refs[f"heads/{name}"]
            return httpx.Response(
                200,
                json={
                    "name": name,
                    "protected": name in self.protected_branches,
                    "commit": {"sha": sha},
                },
            )
        return self._git_route(method, route, query, body)

    def _git_route(
        self, method: str, route: str, query: dict[str, list[str]], body: Any
    ) -> httpx.Response:
        m = re.fullmatch(r"/git/commits/([0-9a-f]{40})", route)
        if m and method == "GET":
            return httpx.Response(200, json=self.commits[m.group(1)])
        m = re.fullmatch(r"/git/trees/([0-9a-f]{40})", route)
        if m and method == "GET":
            entries = self.trees[m.group(1)]
            return httpx.Response(
                200,
                json={
                    "sha": m.group(1),
                    "truncated": False,
                    "tree": [
                        {
                            "path": e.path,
                            "mode": e.mode,
                            "type": e.type,
                            "sha": e.sha,
                            "size": len(self.blobs.get(e.sha, b"")),
                        }
                        for e in entries
                    ],
                },
            )
        if route == "/git/blobs" and method == "POST":
            if body.get("encoding") == "base64":
                data = base64.b64decode(body["content"])
            else:
                data = body["content"].encode("utf-8")
            sha = git_sha("blob", data)
            self.blobs[sha] = data
            return httpx.Response(201, json={"sha": sha})
        if route == "/git/trees" and method == "POST":
            return self._create_tree(body)
        if route == "/git/commits" and method == "POST":
            if body["tree"] not in self.trees or any(
                p not in self.commits for p in body["parents"]
            ):
                return httpx.Response(422, json={"message": "Tree or parent SHA not found"})
            sha = self._store_commit(body["tree"], list(body["parents"]), body["message"])
            return httpx.Response(
                201,
                json={
                    **self.commits[sha],
                    "html_url": f"https://github.com/{self.full_name}/commit/{sha}",
                },
            )
        m = re.fullmatch(r"/git/ref/(heads/.+)", route)
        if m and method == "GET":
            ref = m.group(1)
            return httpx.Response(
                200,
                json={"ref": f"refs/{ref}", "object": {"sha": self.refs[ref], "type": "commit"}},
            )
        m = re.fullmatch(r"/git/refs/(heads/.+)", route)
        if m and method == "PATCH":
            return self._update_ref(m.group(1), body)
        if method == "POST" and route.startswith("/merges"):
            return httpx.Response(403, json={"message": "merges are not allowed in tests"})
        return _not_found()

    def _page(
        self, items: list[dict[str, Any]], query: dict[str, list[str]], route: str
    ) -> httpx.Response:
        per_page = min(int(query.get("per_page", ["30"])[0]), self.per_page_cap)
        page = int(query.get("page", ["1"])[0])
        start = (page - 1) * per_page
        chunk = items[start : start + per_page]
        headers = {}
        if start + per_page < len(items):
            url = f"{API}/repos/{self.owner}/{self.repo}{route}?per_page={per_page}&page={page + 1}"
            headers["link"] = f'<{url}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

    def _contents(self, path: str, ref: str) -> httpx.Response:
        commit = self.commits.get(ref)
        if commit is None:
            return _not_found()
        for entry in self.trees[commit["tree"]["sha"]]:
            if entry.path == path:
                data = self.blobs[entry.sha]
                kind = "symlink" if entry.mode == "120000" else "file"
                return httpx.Response(
                    200,
                    json={
                        "type": kind,
                        "path": path,
                        "sha": entry.sha,
                        "size": len(data),
                        "encoding": "base64",
                        "content": base64.b64encode(data).decode(),
                    },
                )
        return _not_found()

    def _create_review(self, number: int, body: dict[str, Any]) -> httpx.Response:
        pr = self.pulls[number]
        if body.get("commit_id") and body["commit_id"] != pr["head"]["sha"]:
            # GitHub accepts older commits; PullBeacon must not rely on that.
            pass
        valid_lines = self._commentable(number)
        for comment in body.get("comments", []):
            if "position" in comment:
                return httpx.Response(422, json={"message": "position is not supported in fake"})
            key = (comment.get("path"), comment.get("line"), comment.get("side", "RIGHT"))
            if key not in valid_lines:
                return httpx.Response(
                    422,
                    json={
                        "message": "Unprocessable Entity",
                        "errors": ["Line could not be resolved"],
                    },
                )
        review_id = self.next_id()
        review = {
            "id": review_id,
            "user": {"login": self.bot_login, "type": "Bot"},
            "body": body.get("body", ""),
            "state": "COMMENTED" if body.get("event") == "COMMENT" else body.get("event"),
            "commit_id": body.get("commit_id", pr["head"]["sha"]),
            "html_url": f"https://github.com/{self.full_name}/pull/{number}#pullrequestreview-{review_id}",
        }
        self.reviews.setdefault(number, []).append(review)
        for comment in body.get("comments", []):
            self.review_comments.setdefault(number, []).append(
                {"id": self.next_id(), "pull_request_review_id": review_id, **comment}
            )
        return httpx.Response(200, json=review)

    def _commentable(self, number: int) -> set[tuple[str, int, str]]:
        from pullbeacon.core.diff import DiffParseError, build_file_diff

        lines: set[tuple[str, int, str]] = set()
        for f in self.files.get(number, []):
            if not f.get("patch"):
                continue
            try:
                diff = build_file_diff(f["filename"], f.get("status", "modified"), f["patch"])
            except DiffParseError:
                continue
            for line in diff.lines():
                if line.new_line is not None:
                    lines.add((f["filename"], line.new_line, "RIGHT"))
                if line.old_line is not None:
                    lines.add((f["filename"], line.old_line, "LEFT"))
        return lines

    def _create_issue_comment(self, number: int, body: dict[str, Any]) -> httpx.Response:
        comment_id = self.next_id()
        comment = {
            "id": comment_id,
            "body": body["body"],
            "user": {"login": self.bot_login, "type": "Bot"},
            "html_url": f"https://github.com/{self.full_name}/pull/{number}#issuecomment-{comment_id}",
        }
        self.issue_comments.setdefault(number, []).append(comment)
        return httpx.Response(201, json=comment)

    def _create_tree(self, body: dict[str, Any]) -> httpx.Response:
        base = body.get("base_tree")
        entries = {e.path: e for e in self.trees.get(base, [])} if base else {}
        if base and base not in self.trees:
            return httpx.Response(422, json={"message": "base_tree not found"})
        for item in body["tree"]:
            if item.get("sha") is None and "content" not in item:
                entries.pop(item["path"], None)
                continue
            if "content" in item:
                data = item["content"].encode("utf-8")
                sha = git_sha("blob", data)
                self.blobs[sha] = data
            else:
                sha = item["sha"]
                if sha not in self.blobs:
                    return httpx.Response(422, json={"message": "blob not found"})
            entries[item["path"]] = TreeEntry(item["path"], item["mode"], item["type"], sha)
        sha = self._store_tree(list(entries.values()))
        return httpx.Response(201, json={"sha": sha})

    def _update_ref(self, ref: str, body: dict[str, Any]) -> httpx.Response:
        if ref not in self.refs:
            return _not_found()
        branch = ref.removeprefix("heads/")
        if branch in self.protected_branches:
            return httpx.Response(403, json={"message": "Protected branch update failed"})
        new_sha = body["sha"]
        current = self.refs[ref]
        if not body.get("force", False):
            parents = [p["sha"] for p in self.commits.get(new_sha, {}).get("parents", [])]
            if current not in parents and current != new_sha:
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
        self.refs[ref] = new_sha
        for pr in self.pulls.values():
            if (
                pr["head"]["ref"] == branch
                and (pr["head"].get("repo") or {}).get("full_name") == self.full_name
            ):
                pr["head"]["sha"] = new_sha
        return httpx.Response(200, json={"ref": f"refs/{ref}", "object": {"sha": new_sha}})


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"message": "Not Found"})
