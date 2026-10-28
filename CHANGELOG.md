# Changelog

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
