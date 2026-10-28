# PullBeacon — PROGRESS

Living log for the implementation of the plans in `00_PLAN_MAESTRO.md` … `09_FASE_*.md`.
Status values: `DONE_LOCAL` (implemented + verified locally), `DONE_REMOTE` (also verified on
GitHub), `BLOCKED_BY_AUTH` (needs a human action), `IN_PROGRESS`, `NOT_STARTED`.

## Environment (verified 2026-10-28)

- Windows 11, Git Bash + PowerShell 5.1. Python 3.13.13 system, Python **3.12.13** via uv (project target).
- uv 0.11.7, gh 2.90.0 (authenticated as the repo owner), git 2.53.
- Locked versions (uv.lock): litellm 1.106.5, pydantic 2.14.1, httpx 0.28.1, pytest 9.2.0,
  ruff 0.17.3, mypy 2.4.0, respx 0.23.1.
- GitHub REST API versions supported today: `2026-03-10`, `2022-11-28` (`gh api versions`).
- Repository: https://github.com/herrerogusano/pullbeacon (private, created 2026-10-28). Default branch `main`; one branch + PR per phase.

## Phase checklist

| Phase | Status | Notes |
|---|---|---|
| 01 Foundation | DONE_LOCAL | see log below |
| 02 GitHub + diff | DONE_LOCAL | client, diff map, scope, fixtures |
| 03 LLM + review engine | DONE_LOCAL | live provider path NOT_VERIFIED (no paid calls authorised) |
| 04 Publish reviews | DONE_LOCAL | real GitHub publish NOT_VERIFIED_REMOTELY |
| 05 Actions + commands | DONE_LOCAL | workflows `NOT_VERIFIED_REMOTELY` |
| 06 Apply + commits | DONE_LOCAL | real commit on GitHub NOT_VERIFIED_REMOTELY |
| 07 Evaluation | DONE_LOCAL | live benchmark BLOCKED_BY_AUTH (API key not yet available to the agent) |
| 08 Hardening + release | NOT_STARTED | |
| 09 GitHub App (optional) | NOT_STARTED | requires explicit approval after MVP |

## Decisions (not already fixed by the plans)

| # | Decision | Why | Alternatives rejected |
|---|---|---|---|
| D1 | `argparse` CLI instead of `typer` | stdlib, zero extra dependency, fast `--help` | typer (extra deps for 5 commands) |
| D2 | `mypy --strict` with the pydantic plugin | plan allowed mypy or pyright; mypy is the uv/pydantic default | pyright (needs Node in CI) |
| D3 | GitHub REST API version `2026-03-10` | newest supported; its breaking changes (removed `merge_commit_sha`, singular `assignee`, `has_downloads`) do not touch fields PullBeacon reads | `2022-11-28` (still valid, fallback if needed) |
| D4 | LiteLLM imported lazily and with `LITELLM_LOCAL_MODEL_COST_MAP=True` | import costs ~4.5 s and by default fetches a remote cost map; offline + deterministic pricing source | importing at module load |
| D5 | litellm floor `>=1.106` + lockfile | 1.82.7/1.82.8 were a PyPI supply-chain compromise (March 2026, yanked); lock pins hashes | unpinned range |
| D6 | Missing `.pullbeacon.yml` ⇒ conservative defaults: `automatic_review: false`, `apply_enabled: false`, no model | fail closed; a repo must opt in explicitly | enabling reviews by default |
| D7 | Hard-coded deny lists (sensitive files never sent to LLM; `.github/**`, config, CODEOWNERS never edited by apply) that config cannot override | config errors must not open security holes | configurable-only lists |
| D8 | YAML aliases rejected in config | prevents alias-expansion DoS during validation; config never needs them | allowing aliases |
| D10 | `context_files` are literal base-branch paths (no globs), max 20 | avoids tree enumeration and keeps context explicit/auditable | glob over the base tree |
| D11 | Shipped in-memory `FakeGitHub` (`pullbeacon.testing`) behind `httpx.MockTransport` | fixture CLI mode, tests and evaluation all exercise the real client code | hand-written respx stubs per test |
| D12 | Diff rendered for the LLM with a coordinate prefix on every data line (`| R12 + ...`) | the model cites real line numbers and file content cannot forge structural lines | XML-like tags (forgeable by content) |
| D13 | Evidence must literally appear in the cited file's diff (whitespace-normalised, coordinate prefix tolerated); otherwise the finding is dropped | deterministic anti-hallucination gate; the plan's "evidencia fundada" made testable | trusting model line numbers |
| D14 | Wrong/non-commentable line ⇒ finding kept as non-inline (summary only) with no line; invented path ⇒ dropped | never attribute to a guessed line, but keep real issues in that file | dropping all mis-located findings |
| D15 | Structured output via LiteLLM `response_format: json_schema (strict)` when `supports_response_schema`, automatic fallback to JSON-in-text with the same strict validation on schema rejection | portable across providers; fallback never trusted more | provider-specific tool calling |
| D16 | Untrusted data wrapped in markers that embed a hash of the content | content cannot contain its own closing marker | fixed delimiters |
| D17 | Budget: per-call upper-bound estimate (prompt tokens + full output budget) from the bundled LiteLLM cost map; unknown pricing ⇒ allowed with an explicit warning | the plan forbids promising a hard cap; blocking unknown models would block valid setups | refusing unknown-price models |
| D18 | State in GitHub only: base64 JSON marker at the end of the review body (`<!-- pullbeacon:review:v1 … -->`), trusted only if author is the bot login with `type: Bot` and `marker.head_sha == review.commit_id` | no DB; spoofed/copied markers from users are ignored | external store |
| D19 | Markers store ids, fingerprints, location, title, fix (≤600) and explanation (≤800) but **no evidence/code**; bounded to 24 KB | plan forbids storing private code in markers | storing full findings |
| D20 | All model text rendered through `sanitize` (no `@` pings, no HTML comments, no `javascript:`), code in dynamic-length fences | model output is untrusted and must not forge markers or notify people | raw Markdown |
| D21 | On 422 the whole review is re-posted without inline comments, listing them as `not_posted_inline` | GitHub rejects the whole review on one bad line; never claim inline that did not happen | per-comment bisection (more API calls) |
| D22 | Idempotency: automatic = `sha256(head|config_hash|prompt_version|automatic)` checked **before** the LLM call; manual = trigger `comment:<id>` in the marker; replies carry `reply:v1 trigger=<id>` | saves cost on redeliveries and repeated events | Actions cache / artifacts |
| D23 | Commands must start a line; fenced/inline code, quotes and HTML comments are ignored; first command wins; malformed ⇒ help without LLM | explicit recognition, no partial matches | free-text search for `@pullbeacon` |
| D24 | One workflow, two jobs: `review` (`contents: read`) and `apply` (`contents: write`, only when the comment contains `@pullbeacon apply`); each job ignores the other's commands | privilege separation without two files to keep in sync | single job with write access |
| D25 | Composite action runs `uv run --project $GITHUB_ACTION_PATH --locked --no-dev python -I -m pullbeacon run-action`; inputs only via `env`; no uv cache | trusted pinned code, reproducible deps, no expression injection, no cache poisoning | Docker action (slower, image to publish) |
| D26 | Provider keys as explicit action inputs (`anthropic-api-key`, …) | explicit and documented; GitHub masks secrets | implicit step env |
| D27 | `mock-llm: true` input answers with empty canned results | lets users smoke-test the wiring on a real repo at zero cost | none |
| D28 | Dogfood workflow checks out the **default branch** into `.pullbeacon-action` and runs `uses: ./.pullbeacon-action`; live only if repo variable `PULLBEACON_LIVE == 'true'` | the action used on our own PRs is never the PR's version; no accidental spend | `uses: ./` on the PR checkout |
| D29 | Apply edits are line-range replacements that must repeat the exact original text; one invalid replacement aborts the whole apply; omitted findings are listed as skipped | fail closed, never silently partial | full-file rewrites by the model |
| D30 | Before editing, fetched content must hash to the PR file's git blob id (`sha` in the files API) and the tree entry must be a regular file (100644/100755) | proves we edit exactly the reviewed bytes; excludes symlinks/submodules | trusting the contents API alone |
| D31 | Commit flow: refuse protected branches; ref must equal the reviewed head before and right before `PATCH refs` (`force: false`); verify the ref after | a concurrent push always wins; no force, no merge | optimistic update without re-check |
| D32 | The commenter's permission is re-checked right before the write | permission can change during the run | single check |
| D33 | Benchmark matching: inline, same path, ±3 lines, accepted category; unlocated ⇒ FP; deterministic, never model-graded | reproducible and explainable; plan forbids self-grading | LLM-as-judge |
| D34 | Mock reviewer is deliberately imperfect (1 miss, 1 FP trap, 1 invented line, 1 duplicate) and its metrics are asserted exactly | proves every metric path works; clearly labelled as harness test | perfect oracle (would hide metric bugs) |
| D35 | Live benchmark only via CLI `--confirm-spend` or manual `benchmark.yml` (`confirm: SPEND`); CI runs mock eval only | plan: no live benchmarking in CI | scheduled benchmark |
| D36 | Findings sorted by severity, then inline before non-inline, then confidence | stable ids favour locatable findings (`apply` targets) | confidence first |
| D9 | Planning documents stay at the repository root with their original names | required by `README_PLANES.md` | moving to `docs/plans/` |

## Log

### Phase 01 — Foundation (2026-10-28) — DONE_LOCAL

Created: `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.env.example`, `LICENSE`,
`README.md` (dev section), `SECURITY.md` (initial), `examples/.pullbeacon.yml`,
`.github/workflows/ci.yml`, `src/pullbeacon/{__init__,__main__,cli,cli_output,commands_local,config,credentials,logging_utils}.py`,
`src/pullbeacon/domain/{models,errors}.py`, `src/pullbeacon/core/paths.py`, tests in `tests/unit/`.

Commands executed (all passed):

```
uv sync --dev
uv run ruff check .            -> All checks passed
uv run ruff format --check .   -> already formatted
uv run mypy src                -> Success: no issues found
uv run pytest -q               -> 74 passed
uv run python -m pullbeacon validate-config --file examples/.pullbeacon.yml -> valid (+ placeholder warning)
```

CI workflow written with actions pinned by SHA (checkout v7.0.1, setup-uv v10.3.3, resolved via
`gh api` on 2026-10-28). **Not executed on GitHub** (no remote): `NOT_VERIFIED_REMOTELY`.

Risks / notes: the example config uses a placeholder model id on purpose; `validate-config`
warns and live reviews will refuse it.

### Phase 02 — GitHub client and diff map (2026-10-28) — DONE_LOCAL

Created: `adapters/github.py` (httpx client: API version header, bounded retries for 429/5xx/timeouts
honouring `Retry-After`/`x-ratelimit-reset`, `Link` pagination restricted to the API host,
3000-file ceiling detection, contents at exact SHA with size limit and symlink/submodule refusal,
collaborator permission with fail-closed semantics), `core/diff.py` (hunk parser with RIGHT/LEFT
line map, CRLF, `\ No newline`, malformed/truncated patch detection, LLM rendering, fingerprints),
`core/scope.py` (include/exclude, sensitive, binary, generated, no-patch, limits — all with
reasons), `core/context.py`, `testing/fake_github.py`, `scripts/build_fixtures.py`,
`tests/fixtures/pr_{two_hunks,auth_bug,clean}.json`, `.gitattributes` (LF in repo).

Commands executed (all passed): `ruff check`, `ruff format --check`, `mypy src`, `pytest -q`
(118 passed), demo `uv run python -m pullbeacon review --fixture tests/fixtures/pr_two_hunks.json --dry-run`
→ 3 files with exact commentable ranges, skips: `.env.production` sensitive, `assets/logo.png`
binary, `uv.lock` generated.

### Phase 03 — LLM adapters and review engine (2026-10-28) — DONE_LOCAL

Created: `adapters/llm.py` (`LLMClient` protocol, `FakeLLMClient`, `LiteLLMClient` with no tools,
`num_retries=0` + own bounded retries: 2 for 429/timeout/5xx, none for auth/4xx; schema fallback;
error messages never echo provider text), `prompts.py` (versioned `review-v1`/`explain-v1`/`apply-v1`
prompts and JSON schemas), `core/findings.py` (validation, evidence check, dedupe by fingerprint,
severity ordering, stable `PB-NNN` ids, inline cap), `core/review.py` (batching, `CostGuard`,
one JSON correction retry), CLI flags `--mock-llm` / `--provider live`.

Commands executed (all passed): ruff check/format, `mypy src`, `pytest -q` (157 passed), demo
`uv run python -m pullbeacon review --fixture tests/fixtures/pr_auth_bug.json --mock-llm --dry-run`
→ one critical finding on `src/api/users.py:13` RIGHT, `usage.cost_source = simulated`.

Not verified: real Anthropic/OpenAI/Gemini calls (`LiteLLMClient` is tested with an injected
`completion` function and real LiteLLM exception classes only). Requires authorisation to spend.

### Phase 04 — Publishing reviews (2026-10-28) — DONE_LOCAL

Created: `core/markers.py`, `core/render.py`, `core/explain.py`, `adapters/publisher.py`,
`orchestration/flows.py` (`run_review`, `run_explain`, trusted base-branch config,
fork/bot/closed checks before any LLM call), `commands_github.py` (`review --repo --pr`),
`tests/integration/test_publish.py`, `tests/helpers.py`.

Commands executed (all passed): ruff, mypy, `pytest -q` (175 passed). Covered: `line`/`side`
payload without `position`, stale head ⇒ no publish, 422 ⇒ explicit degraded summary, automatic
idempotency per SHA, manual re-review and redelivery idempotency, spoofed/tampered/other-SHA markers
ignored, 403/429/5xx surfaced, explain via comment only (LLM or deterministic fallback).

Not verified remotely: publishing to a real PR (no remote/authorisation yet).

### Phase 05 — Workflows and commands (2026-10-28) — DONE_LOCAL

Created: `orchestration/commands.py` (parser), `orchestration/events.py` (router for
`pull_request_target`, `issue_comment`, `workflow_dispatch`), `orchestration/runner.py`
(job-mode filter, API permission check before anything else, friendly idempotent replies,
`GITHUB_OUTPUT`/`GITHUB_STEP_SUMMARY`), `pullbeacon run-action`, `action.yml`,
`examples/pullbeacon.yml`, `.github/workflows/pullbeacon.yml` (dogfood), `.pullbeacon.yml`,
tests `test_commands.py`, `test_action.py`, `test_workflows.py` (static checks: pinned SHAs,
explicit permissions, no PR checkout/install/test, no `${{ }}` in scripts, only `apply` can write).

Commands executed (all passed): ruff, mypy, `pytest -q` (238 passed), including a CLI
`run-action` end-to-end test against the in-memory GitHub with `INPUT_MOCK_LLM=true`.

Validation externa: `NOT_VERIFIED_REMOTELY` (no remote repository yet). Checklist ready in phase 08.

### Phase 06 — `@pullbeacon apply` (2026-10-28) — DONE_LOCAL

Created: `core/apply.py` (eligibility, prompt with numbered lines, strict proposal validation:
exact original text, locality window, no overlap, placeholders/credentials, CRLF preserved,
disproportion and line/file limits), `adapters/git_commit.py` (Git Data API commit),
Git Data methods + `check_branch` in `adapters/github.py`, `core/gitobjects.py`,
`orchestration/apply_flow.py`, tests `tests/unit/test_apply.py`, `tests/integration/test_apply_flow.py`.
Fixtures now carry real git blob ids.

Commands executed (all passed): ruff, mypy, `pytest -q` (285 passed). Remote: CI for phases
01–05 passed on GitHub (run 40738993439, Python 3.12 and 3.13).

### Phase 07 — Evaluation (2026-10-28) — DONE_LOCAL (live run pending)

Created: `evaluation/build_cases.py`, 16 cases in `evaluation/cases/`, `evaluation/README.md`,
`src/pullbeacon/evaluation/{dataset,metrics,runner,report}.py`, `pullbeacon eval` CLI,
`.github/workflows/benchmark.yml` (manual, paid), CI smoke for fixture review + mock eval,
`ReviewReport.discarded`, `tests/unit/test_evaluation.py`.

Commands executed (all passed): ruff, mypy, `pytest -q` (297 passed),
`uv run python -m pullbeacon eval --dataset evaluation/cases --mock-llm --output evaluation/results/mock.json`
→ mock (harness test, not model quality): TP 11, FP 2, FN 1, recall 0.9167, precision 0.8462,
FP/clean PR 0.25, unlocated 1, duplicates 1, apply exact 2/2.

Live benchmark: authorised by the user on 2026-10-28 (budget proposal ≤ 2 USD); waiting for
`ANTHROPIC_API_KEY` to be provided securely (GitHub secret and/or user env var).

## Acceptance matrix (MATRIZ_ACEPTACION.md)

| ID | Result | Evidence |
|---|---|---|
| T01 | PASS (local) | `tests/unit/test_cli.py` (socket guarded), CLI smoke commands above |
| T02 | PASS (local) | `tests/unit/test_config.py::test_invalid_config_is_rejected` |
| T03 | PASS (local) | `test_github_client.py::test_pagination_two_pages`, `test_diff.py::test_two_hunks_line_numbers` |
| T04 | PASS (local) | `test_scope.py::test_skip_reasons`, demo output above |
| T05 | PASS (local) | `test_review_engine.py::test_invalid_json_*` |
| T06 | PASS (local, engine) | `test_findings.py` invented path / line outside diff |
| T07 | PASS (local, mock) | `test_review_engine.py::test_clean_pr_yields_zero_findings` |
| T08 | PASS (local, fake GitHub) | `test_publish.py::test_automatic_review_is_idempotent_per_sha` |
| T09 | PASS (local, fake GitHub) | `test_manual_commands_review_again_but_redelivery_is_idempotent` |
| T10 | PASS (local, fake GitHub) | `test_explain_posts_comment_without_code_changes` |
| T11 | PASS (local) | `test_action.py::test_ignored_comments_cost_nothing` |
| T12 | PASS (local, review/explain) | `test_users_without_write_are_denied_before_llm` (apply in phase 06) |
| T13 | PASS (local, review) | `test_fork_pr_is_skipped_without_secrets_or_llm`, `test_dependabot_pr_is_skipped` |
| T14 | PASS (local, fake GitHub) | `test_apply_flow.py::test_authorized_apply_creates_one_bounded_commit` |
| T15 | PASS (local) | `test_review_of_older_sha_is_not_used`, `test_head_moved_between_proposal_and_commit_aborts` |
| T16 | PASS (local) | `test_invalid_model_proposal_aborts_whole_apply`, `test_finding_eligibility` |
| T17 | PASS (local) | `test_shell_text_in_replacement_is_just_file_content` |
| T18 | PASS (local) | reply text asserted in `test_authorized_apply_creates_one_bounded_commit` |
| T23 | PASS (local) | `test_protected_branch_reports_without_mutation`, `test_github_403_on_ref_update` |
| T19 | PASS (local) | `test_evaluation.py::test_mock_run_metrics_are_exact` |
| T20 | PASS (local) | `test_live_eval_requires_confirmation_and_never_runs_without_it` |
| T22 | PARTIAL | redaction tests in `tests/unit/test_logging_and_secrets.py`; full check in phase 08 |
| T24 | IN_PROGRESS | local-only repo |
| others | NOT_RUN | later phases |

## Human actions pending

None yet. (Remote repo, secrets and paid calls will be requested when phases 05–08 need them.)
