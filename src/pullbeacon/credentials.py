"""Access to credentials from the environment. Values are never logged or included in errors."""

from __future__ import annotations

import os

from pullbeacon.config import PROVIDER_ENV_VARS
from pullbeacon.domain.errors import CredentialError


class Secret:
    """Opaque wrapper so a credential cannot end up in a repr, f-string or log by accident."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('**********')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


def _read(name: str) -> Secret | None:
    value = os.environ.get(name, "").strip()
    return Secret(value) if value else None


def github_token() -> Secret:
    token = _read("GITHUB_TOKEN") or _read("GH_TOKEN")
    if token is None:
        raise CredentialError("GITHUB_TOKEN is not set; GitHub operations are unavailable")
    return token


def provider_api_key(provider: str) -> Secret:
    env_var = PROVIDER_ENV_VARS.get(provider)
    if env_var is None:
        raise CredentialError(f"unsupported provider: {provider}")
    key = _read(env_var)
    if key is None:
        raise CredentialError(
            f"{env_var} is not set; configure it as a secret for provider '{provider}'"
        )
    return key
