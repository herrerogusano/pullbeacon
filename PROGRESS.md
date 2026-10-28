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
- Repository: local only, no remote yet (branch `master` from the initial T3 commit, work on `feature/mvp`).

## Phase checklist

| Phase | Status | Notes |
|---|---|---|
| 01 Foundation | DONE_LOCAL | see log below |
| 02 GitHub + diff | DONE_LOCAL | client, diff map, scope, fixtures |
| 03 LLM + review engine | NOT_STARTED | |
| 04 Publish reviews | NOT_STARTED | |
| 05 Actions + commands | NOT_STARTED | |
| 06 Apply + commits | NOT_STARTED | |
| 07 Evaluation | NOT_STARTED | |
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

## Acceptance matrix (MATRIZ_ACEPTACION.md)

| ID | Result | Evidence |
|---|---|---|
| T01 | PASS (local) | `tests/unit/test_cli.py` (socket guarded), CLI smoke commands above |
| T02 | PASS (local) | `tests/unit/test_config.py::test_invalid_config_is_rejected` |
| T03 | PASS (local) | `test_github_client.py::test_pagination_two_pages`, `test_diff.py::test_two_hunks_line_numbers` |
| T04 | PASS (local) | `test_scope.py::test_skip_reasons`, demo output above |
| T22 | PARTIAL | redaction tests in `tests/unit/test_logging_and_secrets.py`; full check in phase 08 |
| T24 | IN_PROGRESS | local-only repo |
| others | NOT_RUN | later phases |

## Human actions pending

None yet. (Remote repo, secrets and paid calls will be requested when phases 05–08 need them.)
