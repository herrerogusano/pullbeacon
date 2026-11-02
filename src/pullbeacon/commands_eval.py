"""`pullbeacon eval`: mock by default; live runs need explicit models and --confirm-spend."""

from __future__ import annotations

import argparse
from pathlib import Path

from pullbeacon.adapters.llm import LiteLLMClient, LLMClient
from pullbeacon.cli_output import emit_json
from pullbeacon.config import PROVIDER_ENV_VARS, ModelConfig, parse_config
from pullbeacon.domain.errors import ConfigError
from pullbeacon.domain.models import Severity, canonical_json
from pullbeacon.evaluation.dataset import EvalCase, load_cases
from pullbeacon.evaluation.report import to_csv, to_markdown
from pullbeacon.evaluation.runner import (
    EVAL_CONFIG,
    ClientFactory,
    mock_factory,
    run_evaluation,
)


def _live_factory(model_id: str) -> ClientFactory:
    provider = model_id.split("/", 1)[0]
    if provider not in PROVIDER_ENV_VARS:
        raise ConfigError(f"model '{model_id}' must start with anthropic/, openai/ or gemini/")
    client = LiteLLMClient(ModelConfig.model_validate({"provider": provider, "name": model_id}))

    def factory(_case: EvalCase) -> LLMClient:
        return client

    return factory


def add_eval_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = sub.add_parser("eval", help="run the evaluation dataset (mock by default)")
    parser.add_argument(
        "--dataset",
        type=Path,
        action="append",
        help="case directory; repeat to combine several (default: evaluation/cases)",
    )
    parser.add_argument("--split", choices=["dev", "test"], help="only cases of this split")
    parser.add_argument("--repeats", type=int, default=1, help="samples per case (1-5)")
    parser.add_argument("--workers", type=int, default=1, help="parallel reviews (1-8)")
    parser.add_argument("--output", type=Path, default=Path("evaluation/results/mock.json"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock-llm", action="store_true", help="canned responses (default)")
    group.add_argument("--provider", choices=["live"], help="call real providers (costs money)")
    parser.add_argument(
        "--models", default="", help="comma-separated LiteLLM ids for --provider live"
    )
    parser.add_argument(
        "--confirm-spend", action="store_true", help="required with --provider live"
    )
    parser.add_argument("--max-total-cost-usd", type=float, default=2.0)
    parser.add_argument(
        "--min-inline-severity",
        choices=[s.value for s in Severity],
        help="override policy.min_inline_severity (low = score every finding, as in v0.1)",
    )


def run_eval(args: argparse.Namespace) -> int:
    if not 1 <= args.repeats <= 5:
        raise ConfigError("--repeats must be between 1 and 5")
    if not 1 <= args.workers <= 8:
        raise ConfigError("--workers must be between 1 and 8")
    datasets = args.dataset or [Path("evaluation/cases")]
    cases, digest = load_cases(datasets, split=args.split)
    if args.provider == "live":
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        if not models:
            raise ConfigError("--provider live needs --models")
        if not args.confirm_spend:
            raise ConfigError(
                f"live evaluation calls {len(models)} model(s) on {len(cases)} cases "
                f"x {args.repeats} sample(s) and costs money; re-run with --confirm-spend"
            )
        if not 0 < args.max_total_cost_usd <= 20:
            raise ConfigError("--max-total-cost-usd must be between 0 and 20")
        factories = [_live_factory(m) for m in models]
        mode = "live"
    else:
        factories = [mock_factory]
        mode = "mock"
    config = parse_config(EVAL_CONFIG)
    if args.min_inline_severity:
        policy = config.policy.model_copy(
            update={"min_inline_severity": Severity(args.min_inline_severity)}
        )
        config = config.model_copy(update={"policy": policy})
    run = run_evaluation(
        cases,
        factories,
        config=config,
        dataset_sha256=digest,
        mode=mode,
        total_budget_usd=args.max_total_cost_usd,
        repeats=args.repeats,
        workers=args.workers,
        split=args.split,
    )
    output: Path = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical_json(run) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(to_markdown(run), encoding="utf-8")
    output.with_suffix(".csv").write_text(to_csv(run), encoding="utf-8")
    emit_json(
        {
            "mode": run.mode,
            "output": str(output),
            "total_spent_usd": run.total_spent_usd,
            "summaries": [s.model_dump(mode="json") for s in run.summaries],
        }
    )
    return 0
