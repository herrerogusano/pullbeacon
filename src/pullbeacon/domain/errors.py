"""Error hierarchy.

Every message must be safe to print: no tokens, API keys, prompts or diff content.
Callers pass short, human-readable reasons; the redaction filter is a second line of defence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pullbeacon.domain.models import Usage


class PullBeaconError(Exception):
    """Base error. ``code`` is a stable machine-readable identifier."""

    code = "error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigError(PullBeaconError):
    code = "config_error"


class CredentialError(PullBeaconError):
    code = "missing_credentials"


class GitHubAPIError(PullBeaconError):
    code = "github_api_error"

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimitError(GitHubAPIError):
    code = "github_rate_limited"


class PermissionDenied(PullBeaconError):
    code = "permission_denied"


class UnsupportedPullRequest(PullBeaconError):
    """Forks, Dependabot, closed PRs and other cases the MVP deliberately does not handle."""

    code = "unsupported_pull_request"


class LimitExceeded(PullBeaconError):
    code = "limit_exceeded"


class BudgetExceeded(PullBeaconError):
    code = "budget_exceeded"


class LLMError(PullBeaconError):
    code = "llm_error"


class LLMResponseError(LLMError):
    """The model answered, but not with something PullBeacon can trust.

    ``usage`` is what the attempts cost: they were paid for even though the answer is not used.
    """

    code = "llm_invalid_response"

    def __init__(self, message: str, *, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class LLMOutputTruncated(LLMResponseError):
    """The answer hit the output token limit, so it is incomplete and must not be parsed.

    Retrying with the same budget would hit the same limit, so this is never retried.
    """

    code = "llm_output_truncated"


class StaleHeadError(PullBeaconError):
    code = "stale_head"


class ApplyRejected(PullBeaconError):
    code = "apply_rejected"
