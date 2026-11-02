# Changelog

## Unreleased

### Changed

- **Less noise on pull requests.** New `policy.min_inline_severity` (default `medium`): findings
  below it are no longer posted as inline comments but listed in a collapsed section of the review
  summary (they stay in the marker, so `explain`/`apply` by id still work). Set it to `low` for the
  0.1 behaviour. Benchmark v2 showed that most `low` findings were generic or low-value remarks.
- Review prompt `review-v2`: no generic "add tests" remarks (at most one, naming the untested case),
  no findings on behaviour the PR states as intended, no claims that depend on code outside the
  diff, and an explicit check of resource/timer release on error paths, input-dependent regex or
  loop cost and shared state.
- `pullbeacon eval`: findings below `min_inline_severity` are scored as *collapsed* (neither TP nor
  FP); `--min-inline-severity` overrides the floor.

### Security

- LiteLLM is now imported with `LITELLM_MODE=PRODUCTION` (unless already set). In its default
  `DEV` mode LiteLLM calls `load_dotenv()` at import time, so provider keys in a `.env` file of the
  current directory were picked up silently when running the CLI locally. Keys now come only from
  the environment PullBeacon is started with (e.g. `uv run --env-file .env ...`). GitHub Actions
  runs were not affected in practice: the action never checks out pull request files.

## 0.1.0 (2026-10-29)

First stable MVP release. Same code as 0.1.0rc1 plus the fixes listed under it; verified live on
GitHub with Anthropic Claude (review, explain, apply, stale apply refused, re-review) and a 16-case
benchmark (`docs/benchmarks/2026-10-28-claude.md`). OpenAI and Gemini are supported through LiteLLM
but only tested with mocks.

## 0.1.0rc1 — release candidate (2026-10-28)

First release candidate of the MVP.

### Added

- Automatic and manual (`@pullbeacon review`) pull request reviews in GitHub Actions, with inline
  comments on validated diff lines and a summary review (`COMMENT` only, never approve/request changes).
- `@pullbeacon explain PB-NNN`: explanation of a finding as a regular comment.
- `@pullbeacon apply [PB-NNN]`: validated, bounded fixes committed through the Git Data API,
  fast-forward only.
- Providers through the LiteLLM SDK: Anthropic, OpenAI, Gemini (bring your own key).
- Strict `.pullbeacon.yml` (version 1), read from the PR base commit.
- Composite `action.yml`, example consumer workflow, mock mode for zero-cost wiring tests.
- Reproducible evaluation (`pullbeacon eval`) with a 16-case labelled dataset and Markdown/CSV/JSON
  reports; manual paid benchmark workflow.
- Security: no PR code execution, API permission checks before any LLM call, fork/bot skip,
  sanitized model output, SHA-bound state markers, redacted logs, dependency audit and secret
  scanning in CI.

### Fixed during development

- Quick successive `@pullbeacon` comments could be cancelled by Actions concurrency; comment
  events now use one concurrency group per comment (#9).

### Known limitations

See the README "Limitations" section: same-repository PRs only, `@pullbeacon` is text not an
account, CI is usually not triggered by `GITHUB_TOKEN` commits, diff-only review, no file
creation/deletion in `apply`.
