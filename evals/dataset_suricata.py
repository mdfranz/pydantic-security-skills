"""Concrete Dataset for the suricata-analyst model-comparison eval suite."""

from __future__ import annotations

from pathlib import Path

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

# One model per provider tier -- agreed with the user as the v1 roster: small enough to run
# with repeat=3-5 routinely, spanning provider/cost tiers, no deliberately crash-prone model
# (see refs/pydantic-evals-plan.md).
MODEL_ROSTER: tuple[str, ...] = (
    "openrouter:qwen/qwen3.7-flash",
    "openrouter:deepseek/deepseek-v4-flash",
    "openrouter:z-ai/glm-5.2",
    "openrouter:deepseek/deepseek-v4-pro"
)

MAX_TOOL_CALLS = 30
MAX_DURATION_SECONDS = 600


def build_dataset(
    manifest: FixtureManifest,
    workspace_root: Path,
    *,
    models: tuple[str, ...] = MODEL_ROSTER,
) -> Dataset[EvalCaseInputs, str, EvalCaseMetadata]:
    """One `Case` per model, same prompt/fixture, sharing `workspace_root` so the fixture's
    Parquet cache converts once rather than once per case/rep."""
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
