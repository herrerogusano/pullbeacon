# ADR-001: Run as a trusted GitHub Action that never executes PR code

- Status: accepted (2026-10-28)

## Context

Reviews must post comments and `apply` must push commits, so the workflow needs a write token and
an LLM key. `pull_request` workflows from forks get neither; `pull_request_target` and
`issue_comment` get both but run in the base repository's context, which is dangerous if any PR
code is checked out or executed ("pwn request").

## Decision

- Trigger on `pull_request_target` and `issue_comment` from the default-branch workflow.
- Never check out, install, build or run anything from the PR. Read PR data and files only through
  the REST API at exact SHAs.
- Ship PullBeacon as a composite action that runs its **own** source at a pinned commit SHA with
  `uv run --locked`; inputs are passed only through environment variables; no caches.
- Handle only same-repository PRs in the MVP.
- Separate `review` (read) and `apply` (write) jobs.

## Consequences

- Safe use of secrets for internal PRs; forks are explicitly unsupported (documented).
- Static tests guard the workflows against regressions (checkout of PR head, inline scripts,
  unpinned actions).
- No tests or linters of the PR can be run by PullBeacon; this is a deliberate limitation.

## Alternatives rejected

Checking out the PR in a privileged job (unsafe); a Docker action (slower, image to publish and pin);
`pull_request` with a PAT (secrets in fork context, broader token).
