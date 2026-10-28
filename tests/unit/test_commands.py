from __future__ import annotations

import pytest

from pullbeacon.orchestration.commands import Command, parse_command


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("@pullbeacon review", Command("review")),
        ("  @PullBeacon Review  ", Command("review")),
        ("@pullbeacon re-review", Command("review")),
        ("@pullbeacon explain PB-001", Command("explain", finding_id="PB-001")),
        ("@pullbeacon explain pb-012", Command("explain", finding_id="PB-012")),
        ("@pullbeacon apply", Command("apply")),
        ("@pullbeacon apply PB-002", Command("apply", finding_id="PB-002")),
        ("Thanks!\n\n@pullbeacon review\n", Command("review")),
        ("@pullbeacon", Command("help")),
        ("@pullbeacon help", Command("help")),
    ],
)
def test_recognised_commands(body: str, expected: Command) -> None:
    assert parse_command(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "please run @pullbeacon review",  # not at the start of a line
        "> @pullbeacon apply",  # quoted
        "`@pullbeacon apply`",  # inline code
        "```\n@pullbeacon apply\n```",  # fenced
        "~~~text\n@pullbeacon apply\n~~~",
        "<!-- @pullbeacon apply -->",
        "[@pullbeacon review](https://example.com)",
        "@pullbeaconbot review",
        "@pullbeacon-x review",
        "email me at x@pullbeacon.dev",
        "",
    ],
)
def test_non_commands_are_ignored(body: str) -> None:
    assert parse_command(body) is None


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("@pullbeacon deploy", "unknown command"),
        ("@pullbeacon explain", "needs a finding id"),
        ("@pullbeacon explain 001", "PB-001"),
        ("@pullbeacon apply PB-001 PB-002", "at most one"),
        ("@pullbeacon review now please", "no arguments"),
        ("@pullbeacon apply $(rm -rf /)", "at most one"),
    ],
)
def test_malformed_commands_become_help(body: str, error: str) -> None:
    command = parse_command(body)
    assert command is not None
    assert command.name == "help"
    assert command.error is not None
    assert error in command.error


def test_first_command_wins_and_unclosed_fence_hides_rest() -> None:
    assert parse_command("@pullbeacon explain PB-003\n@pullbeacon apply") == Command(
        "explain", finding_id="PB-003"
    )
    assert parse_command("```\n@pullbeacon apply") is None


def test_huge_comment_is_bounded() -> None:
    assert parse_command("x\n" * 50_000 + "@pullbeacon review") is None
