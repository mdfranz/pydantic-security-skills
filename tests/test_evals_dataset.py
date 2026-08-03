import tempfile
import unittest
from pathlib import Path

from pydantic_evals.evaluators import MaxDuration

from evals.dataset_suricata import MODEL_ROSTER, build_dataset
from evals.evaluators import (
    AvoidsBenignFalsePositive,
    CrashRateByCase,
    SurfacesPlantedFindings,
    VerdictConsistencyAcrossReps,
    VerdictLabel,
    WithinToolCallBudget,
)
from evals.fixtures import build_suricata_fixture


class BuildDatasetTests(unittest.TestCase):
    def test_one_case_per_model_in_the_default_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = Path(directory)
            (workspace_root / "data-source").mkdir()
            manifest = build_suricata_fixture(workspace_root / "data-source")
            dataset = build_dataset(manifest, workspace_root)

            self.assertEqual(len(dataset.cases), len(MODEL_ROSTER))
            self.assertEqual({case.name for case in dataset.cases}, set(MODEL_ROSTER))
            for case in dataset.cases:
                self.assertEqual(case.inputs.model, case.name)
                self.assertEqual(case.inputs.workspace, workspace_root)
                self.assertEqual(case.metadata.expected_findings, manifest.expected_findings)

    def test_custom_model_subset_overrides_default_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = Path(directory)
            (workspace_root / "data-source").mkdir()
            manifest = build_suricata_fixture(workspace_root / "data-source")
            dataset = build_dataset(manifest, workspace_root, models=("google:gemini-3-flash-preview",))

            self.assertEqual([case.name for case in dataset.cases], ["google:gemini-3-flash-preview"])

    def test_dataset_evaluators_are_wired(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = Path(directory)
            (workspace_root / "data-source").mkdir()
            manifest = build_suricata_fixture(workspace_root / "data-source")
            dataset = build_dataset(manifest, workspace_root)

            evaluator_types = {type(e) for e in dataset.evaluators}
            self.assertEqual(
                evaluator_types,
                {SurfacesPlantedFindings, AvoidsBenignFalsePositive, WithinToolCallBudget, MaxDuration, VerdictLabel},
            )
            report_evaluator_types = {type(e) for e in dataset.report_evaluators}
            self.assertEqual(report_evaluator_types, {CrashRateByCase, VerdictConsistencyAcrossReps})


if __name__ == "__main__":
    unittest.main()
