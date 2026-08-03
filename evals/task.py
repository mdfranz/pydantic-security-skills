"""Pydantic Evals task function wrapping the real `skill_runner` execution path.

Reuses `skill_runner.run_core.prepare_run` / `skill_runner.session.RunSession` exactly as
`tests/test_run_core.py`'s `FunctionModel`-driven integration tests do -- no parallel run
implementation, no divergence from what `skill-runner` itself executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_evals import increment_eval_metric, set_eval_attribute

from skill_runner.config import RunOptions, ThinkingEffort
from skill_runner.run_core import RunSink, prepare_run
from skill_runner.session import RunSession


@dataclass(frozen=True)
class EvalCaseInputs:
    """Inputs for one Pydantic Evals case: what to run and against which model."""

    skill_dir: str
    prompt: str
    model: str
    workspace: Path
    max_turns: int | None = 40
    max_run_seconds: int | None = 900
    thinking: ThinkingEffort | None = None
    model_override: Any | None = None
    """Test-only escape hatch: substitutes a `FunctionModel`/`TestModel` for `model` via
    `RunSession.submit_async(prompt, model=...)`, the one override point the session already
    exposes. Production `Dataset`s leave this `None` and let `model` resolve through the real
    model catalog."""


@dataclass(frozen=True)
class EvalCaseMetadata:
    """Ground truth carried alongside each case, sourced from `evals.fixtures.FixtureManifest`
    so evaluators never duplicate the planted facts as separate magic strings."""

    expected_findings: dict[str, str]
    benign_host_ip: str
    malicious_keywords: tuple[str, ...]


class EvalSink:
    """Minimal StatusSink/RunSink: routes emitted run events into Pydantic Evals metrics
    (`EvaluatorContext.metrics`) instead of a console/Textual UI, with zero OTel/Logfire
    dependency -- `WithinToolCallBudget` and similar evaluators read these directly."""

    def status(self, message: str) -> None:
        pass

    def emit(self, kind: str, **fields: object) -> None:
        increment_eval_metric(f"emit.{kind}", 1)
        if kind == "tool_call" and fields.get("tool_name") == "write_file":
            increment_eval_metric("write_file_calls", 1)


async def run_skill_eval(inputs: EvalCaseInputs) -> str:
    """The Pydantic Evals task function: run one skill_runner session and return its final
    report text.

    Exceptions propagate uncaught so `Dataset.evaluate` records a native case failure -- the
    same crash modes documented in ISSUES.md #5/#6/#7/#12/#18 show up as failed reps instead
    of being silently swallowed.
    """
    options = RunOptions(
        model=inputs.model,
        workspace=inputs.workspace,
        pristine=True,
        logfire=False,
        thinking=inputs.thinking,
        max_turns=inputs.max_turns,
        max_run_seconds=inputs.max_run_seconds,
    )
    sink: RunSink = EvalSink()
    setup = prepare_run(inputs.skill_dir, inputs.prompt, options, sink)
    with RunSession(setup, sink, checkpoint_each_turn=True) as session:
        result = await session.submit_async(inputs.prompt, model=inputs.model_override)
    set_eval_attribute("task_id", setup.task_id)
    set_eval_attribute("ws_path", str(setup.ws_path))
    return str(result.output)
