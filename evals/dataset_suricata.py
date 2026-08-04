"""Concrete Dataset for the suricata-analyst model-comparison eval suite."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import yaml
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import MaxDuration

from .evaluators import (
    AvoidsBenignFalsePositive,
    CrashRateByCase,
    SurfacesPlantedFindings,
    VerdictConsistencyAcrossReps,
    VerdictLabel,
    WithinToolCallBudget,
)
from .fixtures import FixtureManifest
from .task import EvalCaseInputs, EvalCaseMetadata

SKILL_DIR = "skills/suricata-analyst"
PROMPT = (
    "Analyze this Suricata EVE JSON log for signs of C2 beaconing, anomalous egress, or "
    "protocol anomalies. Summarize your findings in markdown."
)

# The model roster itself lives in model_roster.yaml, not here, so it can be swapped without a
# code change -- pass a different file to load_model_roster()/--roster-file, or override
# entirely with build_dataset(models=...)/--models. See refs/pydantic-evals-plan.md.
DEFAULT_ROSTER_PATH = Path(__file__).resolve().parent / "model_roster.yaml"

MAX_TOOL_CALLS = 30
MAX_DURATION_SECONDS = 600


def load_model_roster(path: Path = DEFAULT_ROSTER_PATH) -> tuple[str, ...]:
    """Read a `models: [id, ...]` YAML file into an ordered tuple of model ids."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, Mapping):
        raise TypeError(f"{path}: top-level value must be a mapping with a 'models' key")
    models = loaded.get("models", [])
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise TypeError(f"{path}: 'models' must be a list of model id strings")
    if not models:
        raise ValueError(f"{path}: 'models' is empty -- at least one model id is required")
    return tuple(models)


def build_dataset(
    manifest: FixtureManifest,
    workspace_root: Path,
    *,
    models: tuple[str, ...] | None = None,
    roster_path: Path = DEFAULT_ROSTER_PATH,
) -> Dataset[EvalCaseInputs, str, EvalCaseMetadata]:
    """One `Case` per model, same prompt/fixture, sharing `workspace_root` so the fixture's
    Parquet cache converts once rather than once per case/rep.

    `models` takes precedence when given; otherwise the roster is read from `roster_path`
    (default `model_roster.yaml`, next to this module).
    """
    if models is None:
        models = load_model_roster(roster_path)
    metadata = EvalCaseMetadata(
        expected_findings=dict(manifest.expected_findings),
        benign_host_ip=manifest.benign_host_src,
        malicious_keywords=manifest.malicious_keywords,
    )
    cases = [
        Case(
            name=model,
            inputs=EvalCaseInputs(skill_dir=SKILL_DIR, prompt=PROMPT, model=model, workspace=workspace_root),
            metadata=metadata,
        )
        for model in models
    ]
    return Dataset(
        name="suricata-model-comparison",
        cases=cases,
        evaluators=[
            SurfacesPlantedFindings(),
            AvoidsBenignFalsePositive(),
            WithinToolCallBudget(max_calls=MAX_TOOL_CALLS),
            MaxDuration(seconds=MAX_DURATION_SECONDS),
            VerdictLabel(),
        ],
        report_evaluators=[CrashRateByCase(), VerdictConsistencyAcrossReps()],
    )
