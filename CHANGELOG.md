# Changelog

## Unreleased

### Fixed

- `pullbeacon review --fixture` and other JSON commands no longer fail after the paid calls when
  the answer has a character the console cannot encode (real case: `≈` on a Windows console). JSON
  on stdout is now ASCII-only, with other characters escaped.

### Changed

- **One summary for the whole review.** Summaries of the batches were pasted together, so a review
  could say "the production code is not in this diff" and then describe it. When a review has
  several batches, a short extra model call (at most 512 output tokens) now writes one summary from
  them and the validated findings. If that call fails, the review is still published with the
  summaries joined as before.
- **Files GitHub returns without a patch are reviewed.** GitHub omits the patch of large files, and
  they were skipped (real case: the 1,753-line runner of LegalDesk#77). PullBeacon now reads both
  versions at the exact commits and builds the diff, if policy would review the file, it is
  within `limits.max_file_bytes` and it is not deleted. GitHub cannot take comments on such a file,
  so its findings are listed in the summary with their line, not posted inline.
- **A file too large for one batch is split, not skipped.** It was left out entirely, which meant
  the largest change of a pull request could get no review at all (real case: a test file of
  LegalDesk#77). It is now split by line into parts that share 10 lines, each reviewed in a batch
  with its real line numbers. Parts that do not fit in `limits.max_batches` are reported: the file
  appears as reviewed and as skipped, and the review is marked partial. This can make such pull
  requests cost more, up to `limits.max_batches` and `limits.max_estimated_cost_usd`.
- **Each batch knows the rest of the pull request.** A large pull request is reviewed in batches,
  and a batch saw only its own diff, so it reported code and tests from other batches as missing
  (real cases: the only HIGH in 51 findings on LegalDesk was one of these, in a partial review).
  When a batch does not see the whole pull request, the request now lists its files and says which
  are shown, in another part or not reviewed, and why. The list counts against
  `limits.max_context_chars`. Review prompt version `review-v3`: never report a listed file as
  missing, no claims about the pull request as a whole, and no remarks on Markdown table layout.
- **A finding id now means one finding for the whole pull request.** It used to restart at
  `PB-001` on every review, so `PB-001` in a commit message or a reply could mean different things
  (real case: two different `PB-001` on one pull request). A finding seen before keeps its id, and a
  new one continues after the highest id in use. Identity is the existing fingerprint (path, side,
  commented line, category), so a finding whose line changes counts as new.

## 0.2.1 (2026-11-04)

### Fixed

- **Answers cut off by the output token limit are now detected and reported as such.** A large
  pull request with `max_output_tokens: 4096` could produce a truncated JSON answer; PullBeacon
  logged `llm_invalid_json`, spent a second call with the same budget, and then failed with the
  misleading `model returned an empty response`. It now reads the provider's `finish_reason`
  (`length` for OpenAI, Anthropic `max_tokens` and Gemini `MAX_TOKENS` through LiteLLM), never
  parses or retries a truncated answer, and fails with the new error code `llm_output_truncated`
  and a message telling you to raise `limits.max_output_tokens` or lower
  `limits.max_context_chars` (also posted as a reply to `@pullbeacon` commands and shown in the
  job summary). See the new README section.
- An empty answer that is not a truncation now gets the same single correction retry as malformed
  JSON (it used to fail immediately), and a `content_filter` stop fails with its own message
  without a retry.
- The cost of an empty or cut-off answer now counts against the per-run budget and the benchmark
  budget (it used to be lost because the adapter raised before returning the usage).

### Changed

- `LLMResponse` has a new `finish_reason` field and `LiteLLMClient.complete` no longer raises for
  an empty answer: `call_json` decides. Internal API only.

## 0.2.0 (2026-11-02)

Less noise, same detection. Validated on a new hold-out benchmark (46 cases, 3 samples) with an
independent blind judge: useful comments 68 % → 90 % (Claude Haiku 5.5) and 79 % → 96 % (Claude
Sonnet 5.5), no labelled defect lost (`docs/benchmarks/2026-11-02-v3.md`). The review prompt version
changes to `review-v2`.

### Changed

- **Less noise on pull requests.** New `policy.min_inline_severity` (default `medium`): findings
  below it are no longer posted as inline comments but listed in a collapsed section of the review
  summary (they stay in the marker, so `explain`/`apply` by id still work). Set it to `low` for the
  0.1 behaviour. Benchmark v2 showed that most `low` findings were generic or low-value remarks.
- Review prompt `review-v2`: no generic "add tests" remarks (at most one, naming the untested
  case), no comments on minor behaviour changes the PR describes as intended (PR text is an
  unverified claim: security issues and defects are always reported), no claims that depend on
  code outside the diff, and an explicit check of resource/timer release on error paths,
  input-dependent regex or loop cost and shared state.
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
