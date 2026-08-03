import unittest
from pathlib import Path

from pydantic_evals.evaluators import EvaluationResult, EvaluatorContext, ReportEvaluatorContext
from pydantic_evals.evaluators.spec import EvaluatorSpec
from pydantic_evals.otel._errors import SpanTreeRecordingError
from pydantic_evals.reporting import EvaluationReport, ReportCase, ReportCaseFailure

from evals.evaluators import (
    AvoidsBenignFalsePositive,
    CrashRateByCase,
    SurfacesPlantedFindings,
    VerdictConsistencyAcrossReps,
    VerdictLabel,
    WithinToolCallBudget,
)
from evals.task import EvalCaseInputs, EvalCaseMetadata

_NO_SPANS = SpanTreeRecordingError("no spans recorded in this test")


def _ctx(output: str, metadata: EvalCaseMetadata | None, metrics: dict | None = None) -> EvaluatorContext:
    return EvaluatorContext(
        name="case",
        inputs=EvalCaseInputs(skill_dir="skills/suricata-analyst", prompt="p", model="test", workspace=Path(".")),
        metadata=metadata,
        expected_output=None,
        output=output,
        duration=1.0,
        _span_tree=_NO_SPANS,
        attributes={},
        metrics=metrics or {},
    )


_METADATA = EvalCaseMetadata(
    expected_findings={"long_lived_flow": "203.0.113.50", "beacon": "203.0.113.99"},
    benign_host_ip="192.168.50.30",
    malicious_keywords=("c2", "malicious", "isolate"),
)


class SurfacesPlantedFindingsTests(unittest.TestCase):
    def test_flags_each_finding_independently(self):
        ctx = _ctx("Saw a long flow to 203.0.113.50, nothing else notable.", _METADATA)
        result = SurfacesPlantedFindings().evaluate_sync(ctx)
        self.assertEqual(result, {"long_lived_flow": True, "beacon": False})

    def test_case_insensitive(self):
        ctx = _ctx("beacon to 203.0.113.99 observed", _METADATA)
        result = SurfacesPlantedFindings().evaluate_sync(ctx)
        self.assertTrue(result["beacon"])

    def test_missing_metadata_yields_no_findings(self):
        ctx = _ctx("anything", None)
        result = SurfacesPlantedFindings().evaluate_sync(ctx)
        self.assertEqual(result, {})


class AvoidsBenignFalsePositiveTests(unittest.TestCase):
    def test_passes_when_host_not_mentioned(self):
        ctx = _ctx("nothing interesting here", _METADATA)
        result = AvoidsBenignFalsePositive().evaluate_sync(ctx)
        self.assertTrue(result.value)

    def test_passes_when_host_mentioned_without_alarming_language(self):
        ctx = _ctx("192.168.50.30 made routine NTP requests, benign.", _METADATA)
        result = AvoidsBenignFalsePositive().evaluate_sync(ctx)
        self.assertTrue(result.value)

    def test_fails_when_host_flagged_as_malicious(self):
        ctx = _ctx("192.168.50.30 shows C2 behavior; isolate the host immediately.", _METADATA)
        result = AvoidsBenignFalsePositive().evaluate_sync(ctx)
        self.assertFalse(result.value)
        self.assertIn("192.168.50.30", result.reason)

    def test_passes_when_benign_host_and_malicious_finding_are_different_table_rows(self):
        # Regression test: caught live against real deepseek-v4-flash/glm-5.2 reports on
        # 2026-08-03. A markdown table has no whitespace after each `\n` (rows start with
        # `|`), so a naive `(?<=[.!?\n])\s+` split swept the whole table into one "sentence"
        # and flagged the benign host merely for sharing a table with an unrelated beacon row.
        report = (
            "| src_ip | dest | port | note |\n"
            "|---|---|---|---|\n"
            "| 192.168.50.20 | 203.0.113.99 | 1883 | **mqtt beacon, likely c2** |\n"
            "| 192.168.50.30 | 198.51.100.5 | 123 | benign ntp |\n"
        )
        ctx = _ctx(report, _METADATA)
        result = AvoidsBenignFalsePositive().evaluate_sync(ctx)
        self.assertTrue(result.value)

    def test_fails_when_same_table_row_flags_the_benign_host(self):
        report = (
            "| src_ip | dest | port | note |\n"
            "|---|---|---|---|\n"
            "| 192.168.50.30 | 198.51.100.5 | 123 | possible c2, isolate |\n"
        )
        ctx = _ctx(report, _METADATA)
        result = AvoidsBenignFalsePositive().evaluate_sync(ctx)
        self.assertFalse(result.value)


class WithinToolCallBudgetTests(unittest.TestCase):
    def test_within_budget(self):
        ctx = _ctx("done", _METADATA, metrics={"emit.tool_call": 3, "emit.run_code_call": 2})
        result = WithinToolCallBudget(max_calls=10).evaluate_sync(ctx)
        self.assertTrue(result.value)

    def test_exceeds_budget(self):
        ctx = _ctx("done", _METADATA, metrics={"emit.tool_call": 20, "emit.run_code_call": 15})
        result = WithinToolCallBudget(max_calls=10).evaluate_sync(ctx)
        self.assertFalse(result.value)
        self.assertIn("35", result.reason)

    def test_no_metrics_counts_as_zero_calls(self):
        ctx = _ctx("done", _METADATA)
        result = WithinToolCallBudget(max_calls=1).evaluate_sync(ctx)
        self.assertTrue(result.value)


class VerdictLabelTests(unittest.TestCase):
    def test_default_name_matches_report_evaluator_default_label_name(self):
        # VerdictConsistencyAcrossReps's default `label_name` must keep matching this class's
        # serialization name, or the cross-reference silently breaks.
        self.assertEqual(VerdictLabel().get_default_evaluation_name(), "VerdictLabel")
        self.assertEqual(VerdictConsistencyAcrossReps().label_name, "VerdictLabel")


def _report_case(name: str, source_case_name: str | None, verdict: str | None) -> ReportCase:
    labels = {}
    if verdict is not None:
        labels["VerdictLabel"] = EvaluationResult(
            name="VerdictLabel", value=verdict, reason=None, source=EvaluatorSpec(name="VerdictLabel", arguments=None)
        )
    return ReportCase(
        name=name,
        inputs=None,
        metadata=None,
        expected_output=None,
        output="report text",
        metrics={},
        attributes={},
        scores={},
        labels=labels,
        assertions={},
        task_duration=1.0,
        total_duration=1.0,
        source_case_name=source_case_name,
    )


def _report_failure(name: str, source_case_name: str | None) -> ReportCaseFailure:
    return ReportCaseFailure(
        name=name,
        inputs=None,
        metadata=None,
        expected_output=None,
        error_message="boom",
        error_stacktrace="",
        source_case_name=source_case_name,
    )


class CrashRateByCaseTests(unittest.TestCase):
    def test_computes_failure_rate_per_case_with_repeats(self):
        report = EvaluationReport(
            name="exp",
            cases=[
                _report_case("model-a [1/3]", "model-a", "benign"),
                _report_case("model-a [2/3]", "model-a", "benign"),
                _report_case("model-b [1/2]", "model-b", "benign"),
                _report_case("model-b [2/2]", "model-b", "benign"),
            ],
            failures=[_report_failure("model-a [3/3]", "model-a")],
        )
        ctx = ReportEvaluatorContext(name="exp", report=report, experiment_metadata=None)
        analysis = CrashRateByCase().evaluate(ctx)
        rows = {row[0]: row[1:] for row in analysis.rows}
        self.assertEqual(rows["model-a"], [3, 1, round(1 / 3, 3)])
        self.assertEqual(rows["model-b"], [2, 0, 0.0])

    def test_falls_back_to_case_name_grouping_when_repeat_is_one(self):
        report = EvaluationReport(
            name="exp",
            cases=[_report_case("model-a", None, "benign"), _report_case("model-b", None, "benign")],
            failures=[],
        )
        ctx = ReportEvaluatorContext(name="exp", report=report, experiment_metadata=None)
        analysis = CrashRateByCase().evaluate(ctx)
        rows = {row[0]: row[1:] for row in analysis.rows}
        self.assertEqual(rows, {"model-a": [1, 0, 0.0], "model-b": [1, 0, 0.0]})


class VerdictConsistencyAcrossRepsTests(unittest.TestCase):
    def test_flags_inconsistent_verdicts_across_reps(self):
        report = EvaluationReport(
            name="exp",
            cases=[
                _report_case("model-a [1/2]", "model-a", "benign"),
                _report_case("model-a [2/2]", "model-a", "high-risk"),
                _report_case("model-b [1/2]", "model-b", "benign"),
                _report_case("model-b [2/2]", "model-b", "benign"),
            ],
        )
        ctx = ReportEvaluatorContext(name="exp", report=report, experiment_metadata=None)
        analysis = VerdictConsistencyAcrossReps().evaluate(ctx)
        rows = {row[0]: row[1:] for row in analysis.rows}
        self.assertEqual(rows["model-a"][2], False)
        self.assertEqual(rows["model-b"][2], True)


if __name__ == "__main__":
    unittest.main()
