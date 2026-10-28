# Security policy

PullBeacon is pre-release software. This file is completed in phase 08 with the full threat
model; the core rules below already apply to the code in this repository.

## Core guarantees (design rules)

- Code from a pull request is never checked out, installed, built or executed by PullBeacon.
- Configuration that controls cost and permissions is read from the base branch only.
- Pull request content and LLM output are treated as untrusted data, never as instructions.
- Credentials are read from environment variables / GitHub Secrets and are never logged.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository.
