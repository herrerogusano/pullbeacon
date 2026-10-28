"""Commands that talk to the real GitHub API."""

from __future__ import annotations

import argparse

from pullbeacon.domain.errors import ConfigError


def review_remote(args: argparse.Namespace) -> int:
    raise ConfigError("review against GitHub requires --repo/--pr (available from phase 04)")
