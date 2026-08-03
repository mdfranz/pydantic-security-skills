"""CLI entrypoint for the suricata-analyst Pydantic Evals suite.

Run via `uv run python -m evals.runner [options]` -- not a `[project.scripts]` entry, since
`evals/` is deliberately excluded from the built wheel (see [tool.hatch.build.targets.wheel]
in pyproject.toml, which lists only skill_runner) and a console script installed into
site-packages can't import a package that was never packaged alongside it.

Complements, not replaces, `compare_models.sh`: that script drives ad hoc, human-read
narrative comparisons against the real capture (no ground truth, see THREAT_MODEL.md); this
runner drives repeatable, ground-truth-checked regression comparisons against the synthetic
fixture in `evals/fixtures.py`. See refs/pydantic-evals-plan.md.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from .dataset_suricata import MODEL_ROSTER, build_dataset
from .fixtures import build_suricata_fixture
from .task import run_skill_eval


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the suricata-analyst Pydantic Evals suite.")
    parser.add_argument("--repeat", type=int, default=1, help="Number of reps per model case (default: 1).")
    parser.add_argument("--max-concurrency", type=int, default=None, help="Max concurrent case executions.")
    parser.add_argument(
        "--logfire",
        action="store_true",
        help="Push spans to the configured Logfire project (opt-in, matching skill-runner's own --logfire).",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Eval run directory (default: evals/.runs/<timestamp>, gitignored).",
    )
    parser.add_argument(
        "--models",
        default=None,
        help="Comma-separated model ids to override the default roster.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if args.logfire:
        import logfire

        # Matches skill_runner.run_core._configure_logfire's pattern: pydantic_evals' own
        # spans forward through logfire_api automatically once configure() has run, no
        # separate instrumentation call is needed for the Dataset.evaluate side.
        logfire.configure()
        logfire.instrument_pydantic_ai()

    run_stamp = datetime.now().strftime("%y-%m-%d_%H-%M-%S")
    workspace_root = Path(args.workspace) if args.workspace else Path("evals/.runs") / run_stamp
    workspace_root.mkdir(parents=True, exist_ok=True)
    data_source = workspace_root / "data-source"
    data_source.mkdir(exist_ok=True)
    manifest = build_suricata_fixture(data_source)

    models = tuple(m.strip() for m in args.models.split(",")) if args.models else MODEL_ROSTER
    dataset = build_dataset(manifest, workspace_root, models=models)

    report = dataset.evaluate_sync(
        run_skill_eval,
        name=f"suricata-model-comparison-{run_stamp}",
        repeat=args.repeat,
        max_concurrency=args.max_concurrency,
    )
    report.print()


if __name__ == "__main__":
    main()
