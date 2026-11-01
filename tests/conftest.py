from __future__ import annotations

import pytest

from pullbeacon.logging_utils import SECRET_ENV_VARS


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see real credentials from the developer's shell."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    # Tests that import litellm directly must not load a developer's .env either.
    monkeypatch.setenv("LITELLM_MODE", "PRODUCTION")
