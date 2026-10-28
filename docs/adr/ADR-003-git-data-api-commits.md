# ADR-003: Commit fixes through the Git Data API, fast-forward only

- Status: accepted (2026-10-28)

## Context

`apply` must change files on the PR branch without checking out untrusted code in a job that holds
a write token, and without ever overwriting someone else's push.

## Decision

- Read files through the contents API at the exact head SHA and verify that their git blob id
  matches the PR's file entry.
- Build the commit with the Git Data API: `GET commit` → `GET tree` (verify regular file modes) →
  `POST trees` with `base_tree` → `POST commits` with the head as the only parent →
  `PATCH refs/heads/<branch>` with `force: false`.
- Re-check that the branch still points at the reviewed head before creating the commit and again
  right before moving the ref; verify the ref afterwards. Refuse protected branches.
- One commit per command; no merges, no force pushes, no new/deleted files.

## Consequences

- No working copy and no code execution in the write job.
- Concurrent pushes always win; PullBeacon reports a stale head and changes nothing.
- Commits made with `GITHUB_TOKEN` usually do not trigger other workflows; replies state that CI
  was not run and suggest `@pullbeacon review`.

## Alternatives rejected

Checkout + `git push` (executes nothing by itself, but puts untrusted files on disk next to a write
token and invites future misuse); the contents API `PUT` per file (one commit per file, no atomic
multi-file change).
