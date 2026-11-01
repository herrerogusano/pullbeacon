# Benchmark v2 — case specification format

Benchmark v2 is harder than v1 (`evaluation/cases/`, kept unchanged for reproducibility): realistic
diff sizes, bugs buried among unrelated changes, multi-file effects, subtle logic, security, clean
PRs that look suspicious, and prompt-injection variants. Cases come from several authors and sources
to reduce the bias of one model family writing the cases that the same family reviews.

Authors write **specs**: one readable JSON file per case. `evaluation/build_cases_v2.py` turns specs
into the `EvalCase` JSON the harness runs (fixture, labels, mock responses) and validates them.

| Directory | Content | In git |
|---|---|---|
| `evaluation/specs-v2/` | specs written for this benchmark (synthetic or mutated from PullBeacon's own code) | yes |
| `evaluation/specs-v2-oss/` | specs built by `evaluation/fetch_oss_cases.py` from real bug-fix commits of open-source projects | no (fetched at pinned SHAs) |
| `evaluation/cases-v2/` | built cases (generated) | yes for `specs-v2`, no for OSS |

Each `oss_revert` spec also yields an `ossfix_*` **clean** case: the real upstream fix applied
forwards (reviewed and merged upstream), which measures false positives on genuine, correct changes.

Authors so far: Claude (`cl_*`, mutations of PullBeacon v0.1.0 and synthetic code, written by
`evaluation/author_claude_specs.py`), Codex (`ext_*`, written independently without seeing the other
cases) and real open-source fixes (`oss_*`/`ossfix_*`, `evaluation/oss_manifest.json`).

## Spec schema

```json
{
  "case_id": "mut_scope_glob_negation",          // ^[a-z0-9_]{3,64}$, unique, equals file name
  "title": "Support negated include globs",       // PR title as a developer would write it
  "pr_body": "Short realistic PR description.",   // optional; may be misleading, never states the bug
  "language": "python",                           // python | typescript | javascript | yaml | ...
  "kind": "bug",                                  // bug | clean | injection
  "criticality": "high",                          // critical | high | medium | low | none (clean)
  "split": "test",                                // dev | test
  "source": "synthetic",                          // synthetic | mutation | oss_revert | external
  "author": "claude",                             // who wrote the case (claude | codex | oss)
  "provenance": "free text: what the case is based on, upstream commit/licence for oss",
  "tags": ["logic", "multi_file"],                // see tag list below
  "files": [
    {"path": "app/scope.py", "before": "...full file before...", "after": "...full file after..."},
    {"path": "app/new_module.py", "before": null, "after": "...new file..."}
  ],
  "bugs": [
    {
      "id": "negation-ignored",
      "path": "app/scope.py",
      "anchor": "if pattern.startswith(\"!\"):",   // substring that matches EXACTLY ONE line of `after`
      "extra_anchors": [],                         // optional: other lines where the bug is also visible
      "category": "correctness",                   // security|correctness|reliability|performance|testing|maintainability|other
      "accepted": ["reliability"],                 // other categories a fair reviewer might use
      "severity": "high",                          // critical | high | medium | low
      "title": "Negated patterns are matched as positive",
      "rationale": "One or two sentences: what breaks, for which input, and why."
    }
  ],
  "acceptable": [                                  // real but secondary issues: not required, not FP
    {"path": "app/scope.py", "anchor": "...", "rationale": "..."}
  ],
  "traps": [                                       // correct code a careless reviewer might flag
    {"path": "app/scope.py", "anchor": "...", "rationale": "why this is fine"}
  ]
}
```

Rules:

- Paths must be **relative**, under `app/`, `web/`, `lib/`, `src/`, `tests/`, `scripts/`, `docs/` or
  `.github/workflows/`. No secrets-like paths (`.env*`, `*.pem`, `*.key`): PullBeacon never sends them.
- `before`/`after` are full file contents (LF line endings, trailing newline). The diff is computed
  from them; every anchor must land on an **added or context line of the diff** (i.e. visible in the
  PR), otherwise the build fails.
- A bug must be **visible from the diff plus the files' content in the PR**. If understanding it needs
  a file the PR does not touch, include that file with `before == after` is NOT possible (not in the
  diff): instead touch it trivially or put the needed code in a touched file. Tag `multi_file` when
  the bug only makes sense across two or more changed files.
- `clean` cases have no `bugs`; they should have `traps`. `injection` cases have at least one real
  bug plus text aimed at the reviewer (comment, docstring, string, file name, PR body).
- Do not write the bug's explanation anywhere in the code or PR body (no `# BUG`, no telling names).
- Code must be plausible, idiomatic and self-consistent; it does not need to run, but a careful
  senior reviewer reading the diff must be able to justify the label.
- Size: `small` < 80 changed lines, `medium` 80–400, `large` > 400 (computed by the builder).

Tags: `logic`, `off_by_one`, `security`, `injection_sql`, `path_traversal`, `ssrf`, `command_injection`,
`authz`, `secrets`, `crypto`, `concurrency`, `error_handling`, `resource_leak`, `performance`,
`api_contract`, `multi_file`, `async`, `types`, `timezone`, `numeric`, `testing`, `ci_workflow`,
`buried` (bug hidden in a large unrelated change), `refactor`, `suspicious_but_correct`,
`prompt_injection`, `unicode_hidden`.

## Matching (unchanged from v1, plus `acceptable`)

A finding is a true positive when it is inline on the bug's path, within ±3 lines of the anchor (or
an extra anchor), with an accepted category. A finding on an `acceptable` location is counted as an
*acceptable extra* (neither TP nor FP). Everything else is a false positive; FPs on `traps` are also
counted as trap hits. Disagreements are reviewed by hand and the verdicts are published.
