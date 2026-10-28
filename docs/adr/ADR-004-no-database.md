# ADR-004: No database — GitHub reviews are the state

- Status: accepted (2026-10-28)

## Context

`explain` and `apply` need the findings of an earlier run; idempotency needs to know what was
already done. A database would add infrastructure, cost, data retention and a new trust boundary.

## Decision

- Each review ends with a versioned marker (`<!-- pullbeacon:review:v1 <base64 JSON> -->`) holding
  the head SHA, idempotency inputs, trigger and a bounded canonical copy of the validated findings
  (ids, fingerprints, locations, titles, short fix/explanation — **no code evidence**).
- A marker is trusted only on a review authored by the configured bot login with `type: Bot`, and
  only when `marker.head_sha == review.commit_id`; `explain`/`apply` use the latest such review for
  the **current** head.
- Replies carry `<!-- pullbeacon:reply:v1 trigger=<comment id> -->` to make redeliveries idempotent.

## Consequences

- Zero infrastructure; state is visible and auditable on the PR.
- State is lost if reviews are deleted (then the user runs `@pullbeacon review` again).
- Markers are bounded (24 KB) and keep code out of comments beyond what the review shows.

## Alternatives rejected

External store (infrastructure and secrets), Actions cache/artifacts (not a durable or trusted
store, poisoning risk), hidden files committed to the branch (pollutes the PR).
