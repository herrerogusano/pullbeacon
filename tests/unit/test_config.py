from __future__ import annotations

from pathlib import Path

import pytest

from pullbeacon.config import default_config, load_config_file, parse_config
from pullbeacon.domain.errors import ConfigError

EXAMPLE = Path(__file__).parents[2] / "examples" / ".pullbeacon.yml"


def test_example_config_is_valid() -> None:
    config = load_config_file(EXAMPLE)
    assert config.automatic_review is True
    assert config.model is not None
    assert config.model.provider == "anthropic"
    assert config.model.is_placeholder


def test_empty_file_gives_conservative_defaults() -> None:
    config = parse_config("")
    assert config == default_config()
    assert config.automatic_review is False
    assert config.policy.apply_enabled is False
    assert config.model is None


def test_custom_values() -> None:
    config = parse_config(
        """
version: 1
model: {provider: openai, name: openai/some-model}
limits: {max_changed_files: 10, max_estimated_cost_usd: 0.25}
policy:
  include: ["src/**/*.py"]
  apply_enabled: true
  allowed_apply_paths: ["src/**"]
"""
    )
    assert config.limits.max_changed_files == 10
    assert config.model is not None
    assert config.model.name == "openai/some-model"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("version: 1\nunknown_key: true\n", "unknown key"),
        ("version: 1\nlimits: {max_files: 3}\n", "unknown key"),
        ("version: 2\n", "version"),
        ("version: 1\nmodel: {provider: mistral, name: mistral/x}\n", "provider"),
        ("version: 1\nmodel: {provider: openai, name: anthropic/x}\n", "starting with 'openai/'"),
        ("version: 1\nmodel: {provider: openai, name: openai/<model>}\n", "placeholder"),
        ("version: 1\nlimits: {max_changed_files: 0}\n", "max_changed_files"),
        ("version: 1\nlimits: {max_changed_files: 100000}\n", "max_changed_files"),
        ("version: 1\nlimits: {max_estimated_cost_usd: -1}\n", "max_estimated_cost_usd"),
        ("version: 1\npolicy: {include: ['../secrets/**']}\n", "'..'"),
        ("version: 1\npolicy: {include: ['/etc/passwd']}\n", "relative"),
        ("version: 1\npolicy: {include: ['C:/x']}\n", "relative"),
        ("version: 1\npolicy: {include: ['src/a**b']}\n", "whole path segment"),
        ("version: 1\npolicy: {apply_enabled: true, allowed_apply_paths: ['**']}\n", "too broad"),
        ("version: 1\npolicy: {apply_enabled: true}\n", "allowed_apply_paths"),
        ("- just\n- a list\n", "mapping"),
        ("version: [1\n", "invalid YAML"),
    ],
)
def test_invalid_config_is_rejected(text: str, fragment: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(text)
    assert fragment in info.value.message


def test_yaml_aliases_are_rejected() -> None:
    text = "version: 1\npolicy:\n  include: &a ['src/**']\n  exclude: *a\n"
    with pytest.raises(ConfigError, match="aliases"):
        parse_config(text)


def test_python_object_tags_are_not_constructed() -> None:
    with pytest.raises(ConfigError, match="invalid YAML"):
        parse_config("!!python/object/apply:os.system ['echo pwned']\n")


def test_oversized_config_is_rejected() -> None:
    with pytest.raises(ConfigError, match="larger than"):
        parse_config("# " + "x" * 70_000)


def test_missing_file_is_readable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config_file(tmp_path / ".pullbeacon.yml")


def test_validation_errors_do_not_echo_values() -> None:
    secret_like = "sk-ant-THIS-SHOULD-NEVER-BE-PRINTED-123456"
    with pytest.raises(ConfigError) as info:
        parse_config(f"version: 1\nmodel: {{provider: openai, name: '{secret_like}'}}\n")
    assert secret_like not in info.value.message


def test_config_hash_is_stable_and_sensitive() -> None:
    a = parse_config("version: 1\nautomatic_review: true\n")
    b = parse_config("automatic_review: true\nversion: 1\n")
    c = parse_config("version: 1\nautomatic_review: false\n")
    assert a.config_hash() == b.config_hash()
    assert a.config_hash() != c.config_hash()
