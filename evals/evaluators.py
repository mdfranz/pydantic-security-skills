"""Case-level and report-level evaluators for the suricata-analyst eval suite.

Case-level `Evaluator`s check one run's output against `EvalCaseMetadata`'s ground truth
(`evals.fixtures.FixtureManifest`) or structural metrics emitted by `evals.task.EvalSink`.
Report-level `ReportEvaluator`s see every rep of every case together (via
`EvaluationReport.case_groups()`) and answer questions no single run can, e.g. whether a model
agrees with its own prior verdict.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic_ai import Agent
from pydantic_evals.evaluators import (
    Evaluator,
    EvaluationReason,
    EvaluatorContext,
    EvaluatorOutput,
    ReportEvaluator,
    ReportEvaluatorContext,
)
from pydantic_evals.reporting.analyses import ReportAnalysis, TableResult

from .task import EvalCaseInputs, EvalCaseMetadata

_EvalCtx = EvaluatorContext[EvalCaseInputs, str, EvalCaseMetadata]


@dataclass
class SurfacesPlantedFindings(Evaluator[EvalCaseInputs, str, EvalCaseMetadata]):
    """Per-finding substring check against `ctx.metadata.expected_findings`. Each finding
    becomes its own named assertion, so "only 1 of 9 runs caught this" (the headline of
    results/pristine-model-comparison-2026-07-19.md) is visible per-rep in the report instead
    of requiring a human re-read of every write-up."""

    def evaluate(self, ctx: _EvalCtx) -> Mapping[str, bool]:
        report = ctx.output.lower()
        metadata = ctx.metadata or EvalCaseMetadata(expected_findings={}, benign_host_ip="", malicious_keywords=())
        return {label: substring.lower() in report for label, substring in metadata.expected_findings.items()}


@dataclass
class AvoidsBenignFalsePositive(Evaluator[EvalCaseInputs, str, EvalCaseMetadata]):
    """Heuristic, not authoritative (see THREAT_MODEL.md's ground-truth caveat on the real
    184MB capture) -- but grounded in a fact that genuinely IS known here: the benign host is a
    deliberately planted false-positive trap, not an inferred label. Flags a report that
    mentions the planted benign host's IP in the same sentence as a malicious/urgent keyword."""

    def evaluate(self, ctx: _EvalCtx) -> EvaluatorOutput:
        metadata = ctx.metadata
        if metadata is None or not metadata.benign_host_ip:
            return EvaluationReason(value=True, reason="no benign host configured for this case")

        ip = metadata.benign_host_ip
        report = ctx.output.lower()
        if ip not in report:
            return EvaluationReason(value=True, reason=f"{ip} not mentioned in the report")

        # Split into lines first, then sentences within each line -- a plain `(?<=[.!?\n])\s+`
        # split requires whitespace immediately after the boundary, which markdown table rows
        # (`|...|\n|...|`, no leading space) never have, so it was sweeping an entire table
        # into one "sentence" and comparing across rows instead of within them. Caught live:
        # both deepseek-v4-flash and glm-5.2's real reports put the beacon finding and the
        # benign host in different rows of the same table, which this evaluator originally
        # misread as one sentence containing both.
        sentences = [s for line in report.split("\n") for s in re.split(r"(?<=[.!?])\s+", line)]
        offending = [s for s in sentences if ip in s and any(k in s for k in metadata.malicious_keywords)]
        if offending:
            return EvaluationReason(
                value=False,
                reason=f"benign host {ip} flagged alongside a malicious/urgent keyword: {offending[0].strip()[:200]!r}",
            )
        return EvaluationReason(value=True)


@dataclass
class WithinToolCallBudget(Evaluator[EvalCaseInputs, str, EvalCaseMetadata]):
    """Reads `ctx.metrics` populated by `evals.task.EvalSink.emit` -- no OTel/`--logfire`
    dependency. Counts both `run_code` calls and native tool calls, mirroring the call counts
    `results/*.md` tabulated by hand from audit logs."""

    max_calls: int

    def evaluate(self, ctx: _EvalCtx) -> EvaluatorOutput:
        calls = int(ctx.metrics.get("emit.tool_call", 0)) + int(ctx.metrics.get("emit.run_code_call", 0))
        within = calls <= self.max_calls
        reason = None if within else f"{calls} tool/run_code calls exceeds the budget of {self.max_calls}"
        return EvaluationReason(value=within, reason=reason)


_VERDICT_JUDGE_SYSTEM_PROMPT = (
    "You are classifying a security analyst's written report about network traffic. Read the "
    "report and classify its OVERALL verdict into exactly one label:\n"
    "- 'benign': no real security concern raised.\n"
    "- 'monitor': something worth watching, not urgent, no immediate action recommended.\n"
    "- 'high-risk': recommends immediate action (isolation, escalation, incident response), or "
    "explicitly characterizes a finding as C2/malicious with high confidence.\n"
    "Respond with only the label."
)


@dataclass
class VerdictLabel(Evaluator[EvalCaseInputs, str, EvalCaseMetadata]):
    """An independent judge model classifies the report's overall verdict. Deliberately not
    one of the models under comparison, so it isn't judging its own output. Surfaced as a
    per-case **label**, not pass/fail -- there is no ground truth for which verdict is
    correct (THREAT_MODEL.md); `VerdictConsistencyAcrossReps` is what reads this across reps."""

    model: str = "openai:gpt-5-mini"

    async def evaluate(self, ctx: _EvalCtx) -> EvaluatorOutput:
        judge = Agent(
            self.model,
            output_type=Literal["benign", "monitor", "high-risk"],
            system_prompt=_VERDICT_JUDGE_SYSTEM_PROMPT,
        )
        result = await judge.run(ctx.output)
        return EvaluationReason(value=result.output)


def _grouped_cases(report):
    """`{case_name: (runs, failures)}`, working whether or not the report used `repeat > 1`
    (`EvaluationReport.case_groups()` returns `None` for a single-run experiment)."""
    groups = report.case_groups()
    if groups is not None:
        return {g.name: (list(g.runs), list(g.failures)) for g in groups}
    grouped: dict[str, tuple[list, list]] = {}
    for case in report.cases:
        grouped.setdefault(case.name, ([], []))[0].append(case)
    for failure in report.failures:
        grouped.setdefault(failure.name, ([], []))[1].append(failure)
    return grouped


@dataclass
class CrashRateByCase(ReportEvaluator[EvalCaseInputs, str, EvalCaseMetadata]):
    """Walks every rep of every case and computes failures/total -- operationalizes the crash
    modes documented in ISSUES.md #5/#6/#7/#12/#18 as a per-model rate instead of something
    found by manually re-reading Logfire traces after the fact."""

    def evaluate(self, ctx: ReportEvaluatorContext) -> ReportAnalysis:
        rows: list[list] = []
        for name, (runs, failures) in sorted(_grouped_cases(ctx.report).items()):
            total = len(runs) + len(failures)
            rate = len(failures) / total if total else 0.0
            rows.append([name, total, len(failures), round(rate, 3)])
        return TableResult(
            title="Crash rate by case",
            description="Failed reps / total reps per case (model), across all repeats.",
            columns=["case", "runs", "failures", "crash_rate"],
            rows=rows,
        )


@dataclass
class VerdictConsistencyAcrossReps(ReportEvaluator[EvalCaseInputs, str, EvalCaseMetadata]):
    """Informational only: whether repeated runs of the SAME model agree with their OWN
    verdict label. This is self-consistency, not correctness -- there is no ground truth for
    which verdict is right (THREAT_MODEL.md), only whether the model is stable across
    identical reps. Directly operationalizes the GLM-5.2 flip-flop finding in
    results/pristine-model-comparison-2026-07-19.md ("low risk" in rep 1, "HIGH confidence,
    isolate host" in reps 2 and 3 on the identical beacon)."""

    label_name: str = "VerdictLabel"

    def evaluate(self, ctx: ReportEvaluatorContext) -> ReportAnalysis:
        rows: list[list] = []
        for name, (runs, _failures) in sorted(_grouped_cases(ctx.report).items()):
            labels = [r.labels[self.label_name].value for r in runs if self.label_name in r.labels]
            distinct = sorted(set(labels))
            rows.append([name, len(labels), ", ".join(distinct), len(distinct) <= 1])
        return TableResult(
            title="Verdict consistency across reps (informational, not correctness)",
            description=(
                "Whether repeated runs of the same model agree with their own prior verdict "
                "label. No claim is made about which verdict is correct."
            ),
            columns=["case", "reps_with_label", "distinct_labels", "self_consistent"],
            rows=rows,
        )
