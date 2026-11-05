from __future__ import annotations

import io
import json
import socket
import sys
from pathlib import Path

import pytest

from pullbeacon.cli import main
from pullbeacon.cli_output import emit_json

ROOT = Path(__file__).parents[2]


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def guard(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)


@pytest.mark.usefixtures("no_network")
def test_help_works_offline(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    assert "validate-config" in capsys.readouterr().out


@pytest.mark.usefixtures("no_network")
def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage" in capsys.readouterr().out


@pytest.mark.usefixtures("no_network")
def test_validate_config_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate-config", "--file", str(ROOT / "examples" / ".pullbeacon.yml")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["valid"] is True
    assert out["warnings"]


def test_validate_config_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / ".pullbeacon.yml"
    bad.write_text("version: 1\nnope: 1\n", encoding="utf-8")
    assert main(["validate-config", "--file", str(bad)]) == 2
    assert "unknown key" in capsys.readouterr().err


def test_json_output_survives_a_console_that_cannot_encode_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    console = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    emit_json({"summary": "about \u2248 3 \U0001f600"})
    console.flush()
    assert json.loads(buffer.getvalue().decode("ascii")) == {"summary": "about \u2248 3 \U0001f600"}
