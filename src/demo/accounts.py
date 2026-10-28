"""Demo module for the PullBeacon live smoke test (this PR is closed without merging)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class User:
    id: int
    is_admin: bool = False


class AccountStore:
    def __init__(self) -> None:
        self._accounts: dict[int, dict[str, object]] = {}

    def get(self, account_id: int) -> dict[str, object]:
        return self._accounts[account_id]

    def delete(self, account_id: int) -> None:
        self._accounts.pop(account_id, None)


def get_profile(store: AccountStore, current: User, account_id: int) -> dict[str, object]:
    if current.id != account_id and not current.is_admin:
        raise PermissionError("forbidden")
    return store.get(account_id)


def delete_account(store: AccountStore, current: User, account_id: int) -> dict[str, int]:
    store.delete(account_id)
    return {"deleted": account_id}
