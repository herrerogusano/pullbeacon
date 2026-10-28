<img src="assets/icon.svg" width="64" height="64" alt="">

# PullBeacon

**Bring-your-own-key, multi-model pull request reviewer and controlled fixer for GitHub Actions.**

PullBeacon reviews internal pull requests with the LLM provider you choose (Anthropic, OpenAI or
Gemini through the [LiteLLM](https://docs.litellm.ai/) SDK), posts findings on the exact diff
lines, and — only when a person with write access asks — commits small, validated fixes to the PR
branch.

> Status: **v0.1.0 release candidate.** See [What has been verified](#what-has-been-verified)
> and [`PROGRESS.md`](PROGRESS.md) for exactly what was tested and how.

## What it does

| Command (PR comment) | Effect |
|---|---|
| *(PR opened / updated)* | Automatic review, once per commit, if `automatic_review: true` |
| `@pullbeacon review` | New review of the current commit (even if already reviewed) |
| `@pullbeacon explain PB-001` | Explanation of one finding as a normal comment; no code changes |
| `@pullbeacon apply` | Commits fixes for all applicable findings of the latest review of the current commit |
| `@pullbeacon apply PB-001` | Commits the fix for one finding |
| `@pullbeacon` | Help |

Commands must start a line. Quoted text, code blocks and HTML comments are ignored.
`@pullbeacon` is plain text read by the workflow — **it is not a GitHub user** and GitHub will not
autocomplete it.

### Why another reviewer?

- **Your keys, your model:** switch provider/model in one config line; no subscription or vendor
  lock-in. Credentials stay in your repository secrets.
- **Deterministic validation before anything is posted:** findings must cite a real file of the
  diff, a commentable line on the correct side, and evidence copied from that diff. Hallucinated
  paths or lines are dropped or downgraded, never posted on a guessed line.
- **Fixes that fail closed:** edits must match the current file byte-for-byte, stay next to the
  finding, respect path allow/deny lists and size limits, and are committed fast-forward only.
- **Measurable:** a reproducible benchmark ([`evaluation/`](evaluation/README.md)) for detection,
  false positives, cost and latency.

Alternatives worth considering: the official
[Claude Code Action](https://github.com/anthropics/claude-code-action) (agentic, Claude-only,
broader capabilities) and [PR-Agent](https://github.com/qodo-ai/pr-agent) (mature, many tools).
PullBeacon is intentionally narrower: no agent, no tools for the model, diff-only context.

## Install in your repository

1. **Add a provider key as a secret:** Settings → Secrets and variables → Actions → New
   repository secret, e.g. `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` / `GEMINI_API_KEY`). Never put
   keys in files, issues or chat.
2. **Commit the configuration to your default branch** as `.pullbeacon.yml` — start from
   [`examples/.pullbeacon.yml`](examples/.pullbeacon.yml) and set a real LiteLLM model id
   (`anthropic/<model>`, `openai/<model>` or `gemini/<model>`). Validate it locally:
   `uvx --from git+https://github.com/<OWNER>/<REPO>@<COMMIT_SHA> pullbeacon validate-config --file .pullbeacon.yml`
3. **Add the workflow** [`examples/pullbeacon.yml`](examples/pullbeacon.yml) as
   `.github/workflows/pullbeacon.yml` on the default branch and replace
   `<OWNER>/<REPO>@<COMMIT_SHA>` with this repository and a **full commit SHA** you trust (tags can
   be moved; SHAs cannot).
4. Open a pull request **from a branch of the same repository**. Comment `@pullbeacon review`.

Nothing in PullBeacon's code needs to change; everything is configuration, workflow and secrets.
If PullBeacon lives in a private repository, enable *Settings → Actions → General → Access* so
your other repositories may use it.

Test the wiring at zero cost first with `mock-llm: "true"` in the workflow step: PullBeacon then
posts an empty, clearly labelled mock review without calling any provider.

### Action inputs

| Input | Default | Meaning |
|---|---|---|
| `mode` | — | `review` (reviews, explain, help) or `apply` (needs `contents: write`) |
| `anthropic-api-key` / `openai-api-key` / `gemini-api-key` | `""` | Pass the secret for your provider |
| `config-file` | `.pullbeacon.yml` | Always read from the PR **base** commit |
| `automatic-review` | `config` | `config`, `true` or `false` |
| `model` | `""` | Override of the model id (same provider as the config) |
| `dry-run` | `false` | Run everything, publish nothing |
| `mock-llm` | `false` | Canned empty responses, no provider call |
| `bot-login` | `github-actions[bot]` | Identity whose reviews are trusted as state |
| `github-token` | `github.token` | Token for the GitHub API |

Outputs: `status`, `review-url`, `comment-url`, `commit-sha`.

### Permissions

| Job | Permissions | Why |
|---|---|---|
| `review` | `contents: read`, `pull-requests: write` | read files at exact SHAs, post reviews and comments |
| `apply` | `contents: write`, `pull-requests: write` | create one commit through the Git Data API |

The example workflow sets `permissions: {}` at the top and grants these per job.

## Configuration (`.pullbeacon.yml`, version 1)

```yaml
version: 1
automatic_review: true            # default false when the file is missing
model:
  provider: anthropic             # anthropic | openai | gemini
  name: anthropic/<model-id>      # LiteLLM id; placeholders are rejected
  temperature: 0                  # null to use the provider default
  timeout_seconds: 120
limits:
  max_changed_files: 40           # files beyond the limit are skipped and reported
  max_changed_lines: 1500
  max_context_chars: 120000       # per batch, including context files
  max_file_bytes: 200000
  max_output_tokens: 4096
  max_estimated_cost_usd: 1.00    # preventive per-run estimate, not an accounting cap
  max_inline_comments: 15         # the rest are listed in the summary
  max_batches: 4
policy:
  include: ["**/*.py", "**/*.ts"]
  exclude: ["**/generated/**"]
  context_files: ["docs/ARCHITECTURE.md"]   # literal base-branch paths, read as extra context
  apply_enabled: false
  allowed_apply_paths: ["src/**"]           # required when apply_enabled is true
  min_apply_confidence: 0.80
  max_apply_files: 5
  max_apply_changed_lines: 120
  apply_context_window: 10                  # edits must be within ±N lines of a finding
```

Unknown keys, unsafe globs (`..`, absolute paths), YAML aliases and placeholder model ids are
rejected with a readable error. Some paths are **never** sent to the model (`.env*`, keys,
certificates, credentials) or edited by `apply` (`.github/**`, `.pullbeacon.yml`, `action.yml`,
`CODEOWNERS`, `.gitmodules`), regardless of configuration.

## Cost and privacy

- PullBeacon has no servers; you pay GitHub Actions minutes (per your plan) and your LLM
  provider per token. One review is one LLM call per batch (usually one), plus at most one retry for
  malformed JSON and two for transient errors.
- The per-run budget uses the LiteLLM price table pinned in the lockfile to estimate the worst case
  *before* each call. Real billing may differ; set limits in your provider console too. Unknown
  prices are reported as "cost unknown".
- **Running in GitHub Actions does not keep your code local:** the diff (and configured context
  files) is sent to the provider you configure, under that provider's terms. Check your company's
  policy first.

## Limitations (v0.1)

- Only PRs from branches of the same repository. Forks and Dependabot/bot PRs are skipped without
  using secrets.
- `@pullbeacon` is text, not a mentionable account (a GitHub App is a possible future step).
- Commits pushed with `GITHUB_TOKEN` normally **do not trigger other workflows**: after `apply`,
  CI may not run on the new commit, and PullBeacon never claims it did. Comment
  `@pullbeacon review` to re-review.
- `apply` does not create, delete or rename files, does not touch symlinks, submodules, binaries,
  protected branches, or files with a BOM.
- Diff-only review: no repository-wide analysis, no tests are executed. Files without a patch
  (binary or very large) are skipped and listed.
- GitHub lists at most 3000 files per PR; larger PRs are reported as partial.
- No dashboard, no database: state lives in PullBeacon's own review comments.

## Local development

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python -m pullbeacon review --fixture tests/fixtures/pr_auth_bug.json --mock-llm --dry-run
uv run python -m pullbeacon eval --dataset evaluation/cases --mock-llm --output evaluation/results/mock.json
```

No network or API key is needed for any of these. Architecture: [`docs/architecture.md`](docs/architecture.md),
decisions: [`docs/adr/`](docs/adr/), security: [`SECURITY.md`](SECURITY.md).

## What has been verified

| Area | How | Result |
|---|---|---|
| Unit, integration and end-to-end tests (fake GitHub + fake LLM) | `pytest` locally and in CI (Python 3.12, 3.13) | passing |
| Automatic review, manual `review`, `explain`, help, quoted commands ignored | real PRs in this repository with `mock-llm` | working (see PR #8) |
| Live LLM reviews, `apply` commit on a real PR, real model benchmark | — | see `PROGRESS.md` for current status |

## License

MIT
