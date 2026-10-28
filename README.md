<img src="assets/icon.svg" width="64" height="64" alt="">

# PullBeacon

BYOK, multi-model pull request reviewer and controlled fixer for GitHub Actions.

> Status: under active development (pre-release). See `PROGRESS.md` for what is implemented and
> verified. The full installation guide is written in phase 08.

## Local development

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python -m pullbeacon --help
uv run python -m pullbeacon validate-config --file examples/.pullbeacon.yml
```

No API key or network access is needed for any of the commands above.

## License

MIT
