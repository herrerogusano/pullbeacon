"""Command line interface. Heavy dependencies (LiteLLM, HTTP) are imported lazily per command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pullbeacon import __version__
from pullbeacon.domain.errors import PullBeaconError
from pullbeacon.logging_utils import configure_logging, redact

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pullbeacon",
        description="BYOK, multi-model pull request reviewer for GitHub Actions.",
    )
    parser.add_argument("--version", action="version", version=f"pullbeacon {__version__}")
    parser.add_argument("--log-level", default="WARNING", help="log level (default: WARNING)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    validate = sub.add_parser("validate-config", help="validate a .pullbeacon.yml file")
    validate.add_argument("--file", type=Path, default=Path(".pullbeacon.yml"))

    review = sub.add_parser("review", help="review a pull request (fixture or GitHub)")
    review.add_argument("--fixture", type=Path, help="local PR fixture JSON (no network)")
    review.add_argument("--repo", help="owner/name of a GitHub repository (uses GITHUB_TOKEN)")
    review.add_argument("--pr", type=int, help="pull request number (with --repo)")
    review.add_argument("--responses", type=Path, help="canned LLM responses for --mock-llm")
    review.add_argument(
        "--config",
        type=Path,
        help="local config file (default: built-in defaults for --fixture, base branch for --repo)",
    )
    review.add_argument("--dry-run", action="store_true", help="print the report, publish nothing")
    _add_llm_arguments(review)

    return parser


def _add_llm_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--mock-llm",
        action="store_true",
        help="use the fixture's canned LLM responses (no provider call, no cost)",
    )
    group.add_argument(
        "--provider",
        choices=["live"],
        help="'live' calls the configured provider (requires its API key; may cost money)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    try:
        return _dispatch(args)
    except PullBeaconError as exc:
        print(redact(f"error [{exc.code}]: {exc.message}"), file=sys.stderr)
        return EXIT_USAGE if exc.code == "config_error" else EXIT_FAILURE


def _dispatch(args: argparse.Namespace) -> int:
    from pullbeacon import commands_local

    if args.command == "validate-config":
        return commands_local.validate_config(args.file)
    if args.command == "review":
        return commands_local.review(args)
    raise AssertionError(f"unhandled command {args.command}")  # pragma: no cover
