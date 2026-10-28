# Security policy

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting on this repository
("Security" → "Report a vulnerability"). Include the version (commit SHA), a description and, if
possible, a minimal reproduction. You will get an acknowledgement as soon as possible; please allow
time for a fix before any disclosure.

## Threat model

### Assets

Repository contents and branches, the workflow `GITHUB_TOKEN`, LLM provider API keys, provider
spend, and the integrity of PullBeacon's published reviews (which are also its state).

### Untrusted inputs

Everything that a PR author or commenter controls: diffs, file contents, PR title/body, comments,
branch names, file paths — and **all LLM output**.

### Controls

| Threat | Control |
|---|---|
| Running attacker code with secrets (`pull_request_target` "pwn request") | The PR is never checked out, installed, built or executed. The action runs its own pinned code with `uv run --locked`; files are read through the API at exact SHAs. Static tests enforce this on the shipped workflows. |
| Forks / external contributors receiving secrets | Only same-repository PRs are processed (`head.repo == base.repo`); forks and bot PRs are skipped before any secret-backed call. |
| Unauthorized commands / spend | Every manual command checks the commenter's real permission via `GET /collaborators/{user}/permission` (admin/write, incl. maintain) **before** any LLM call; failures deny. `apply` re-checks right before writing. Bots and edited comments are ignored. |
| PR configuring its own review (raise limits, enable apply) | `.pullbeacon.yml` is read only from the **base** commit. Workflow-level overrides come from the default-branch workflow. |
| Script injection in Actions | Inputs reach the tool only as environment variables; no `${{ }}` inside `run:` scripts (tested). |
| Prompt injection | Untrusted content is wrapped in content-hashed data markers and the system prompt forbids following embedded instructions. The model has **no tools**. More importantly, nothing the model says is trusted: findings are validated against the diff, and edits against the file, path policy and limits. |
| Model output abuse on GitHub | Model text is sanitized (no `@` pings, HTML comments, `javascript:`), so it cannot notify people or forge state markers. |
| Forged state (fake review markers) | Markers are trusted only from the configured bot login with `type: Bot`, and only when bound to the review's `commit_id`. Markers hold no code. |
| Destructive or broad edits | Line-range replacements must repeat the exact original text, stay within ±N lines of a finding, match allow-lists, avoid hard deny-lists (`.github/**`, config, credentials, VCS metadata), and respect size limits; any violation aborts the whole apply. Content is verified against the git blob id. |
| Races / overwriting pushes | Fast-forward only (`force: false`); the ref must equal the reviewed head before creating and right before moving the branch. No merges, no force pushes, protected branches refused. |
| Secret leakage | Credentials are wrapped and redacted from logs and errors; provider error messages are never echoed; logs contain IDs/counts, not code or prompts. Gitleaks runs in CI. |
| Cost exhaustion | Preventive per-run budget (estimated worst case before each call), bounded retries, file/line/context limits, idempotency per commit and per comment. |
| Supply chain | Dependencies locked with hashes (`uv.lock`); third-party actions pinned by commit SHA; `uv audit` runs in CI. `litellm` 1.82.7/1.82.8 (compromised, yanked) are far below the required floor. |

### Accepted risks

- **filelock < 4 (GHSA-567q-5qvw-pf92, GHSA-9vhv-jxww-xfm5, GHSA-v4x8-3wr5-3wq8):** required by
  `litellm` (`filelock<4.0`). The issues need another local user on the same machine and affect
  `SoftFileLock`/`ReadWriteLock`, which `litellm` only uses in its proxy CLI (not used by
  PullBeacon). GitHub-hosted runners are ephemeral and single-tenant. These three IDs are ignored
  explicitly in CI; any other advisory fails the build. Revisit when `litellm` allows filelock 4.
- **Same-repository writers are trusted.** Someone with write access can already push code and
  workflows; PullBeacon does not try to defend a repository against its own maintainers.
- The `GITHUB_TOKEN` identity (`github-actions[bot]`) is shared by all workflows of the
  repository; another workflow of the same repository could post a review that looks like
  PullBeacon's. Only maintainers can add such workflows.

## Privacy

GitHub Actions is **not** a private environment for your code with respect to the LLM: the reviewed
diff and configured context files are sent to the provider you configure, and are processed under
that provider's terms. Sensitive file patterns are never sent. Confirm your organization's policy
before enabling PullBeacon on confidential code.

## Handling a compromised key

1. Revoke the key in the provider console immediately and create a new one.
2. Replace the repository secret (Settings → Secrets and variables → Actions).
3. Review provider usage logs and billing for the exposure window.
4. If the key appeared in a log, issue or commit, delete it there too (rotation is still
   mandatory: deletion does not undo exposure).

## Protecting pull requests

Recommended repository settings: branch protection on the default branch (PullBeacon never pushes
there), required reviews (PullBeacon only posts `COMMENT` reviews, never approves), and
*Actions → Fork pull request workflows* left at their restrictive defaults.
