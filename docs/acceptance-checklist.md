# Remote acceptance checklist (sandbox)

Run in an authorized sandbox repository with one provider secret and a low provider budget.
Record each line in `PROGRESS.md` as PASS / FAIL / NOT_RUN with the link to the PR, run or comment.
Never mark PASS without having executed it.

| # | Scenario | Steps | Expected |
|---|---|---|---|
| R1 | Automatic review | Open a same-repo PR with a known bug | One `COMMENT` review, finding inline on the real line; second push of the same SHA does not re-review |
| R2 | Manual review | Comment `@pullbeacon review` | A new review even if the SHA was reviewed |
| R3 | Explain | `@pullbeacon explain PB-001` | Regular comment with the explanation; no commit |
| R4 | Apply | `@pullbeacon apply PB-001` | One commit on the PR branch, reply lists files and states that tests/CI were not run |
| R5 | Stale apply | `@pullbeacon apply` after the commit, before re-review | Refused: review is for an older commit; no commit |
| R6 | Re-review | `@pullbeacon review` after apply | Review of the new head |
| R7 | No permission | Comment from an account with read access | Denied reply, no LLM call (provider usage unchanged), no commit |
| R8 | Fork | PR from a fork | Skipped (`if:` pre-filter or runtime), no secrets used |
| R9 | Large PR | PR beyond `max_changed_files` | Partial review clearly labelled, skipped files listed |
| R10 | LLM error | Invalid key or model id | Friendly error reply, run marked failed, no key in logs |
| R11 | Protected branch | Protect the PR branch, then `apply` | Refused with explanation, branch unchanged |
| R12 | Logs | Inspect all run logs | No tokens, keys, prompts or diff content |
| R13 | Second repository | Install from README into another repo (config + workflow + secret only) | R1–R4 work without changing PullBeacon code |
