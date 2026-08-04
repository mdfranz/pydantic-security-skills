import tempfile
import unittest
from pathlib import Path

from pydantic_evals.evaluators import MaxDuration

from evals.dataset_suricata import DEFAULT_ROSTER_PATH, build_dataset, load_model_roster
from evals.evaluators import (
    AvoidsBenignFalsePositive,
    CrashRateByCase,
    SurfacesPlantedFindings,
    VerdictConsistencyAcrossReps,
    VerdictLabel,
    WithinToolCallBudget,
)
from evals.fixtures import build_suricata_fixture


class LoadModelRosterTests(unittest.TestCase):
    def test_default_roster_file_loads_a_nonempty_tuple(self):
        roster = load_model_roster()
        self.assertIsInstance(roster, tuple)
        self.assertGreater(len(roster), 0)
        self.assertTrue(all(isinstance(m, str) for m in roster))

    def test_loads_a_custom_roster_file(self):
        with tempfile.TemporaryDirectory() as directory:
            roster_path = Path(directory) / "custom_roster.yaml"
            roster_path.write_text("models:\n  - provider:model-a\n  - provider:model-b\n")

            roster = load_model_roster(roster_path)

            self.assertEqual(roster, ("provider:model-a", "provider:model-b"))

    def test_rejects_empty_models_list(self):
        with tempfile.TemporaryDirectory() as directory:
            roster_path = Path(directory) / "empty_roster.yaml"
            roster_path.write_text("models: []\n")

            with self.assertRaises(ValueError):
                load_model_roster(roster_path)

    def test_rejects_non_string_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            roster_path = Path(directory) / "bad_roster.yaml"
            roster_path.write_text("models:\n  - 123\n")

            with self.assertRaises(TypeError):
                load_model_roster(roster_path)


class BuildDatasetTests(unittest.TestCase):
    def test_one_case_per_model_in_the_default_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = Path(directory)
            (workspace_root / "data-source").mkdir()
            manifest = build_suricata_fixture(workspace_root / "data-source")
            dataset = build_dataset(manifest, workspace_root)

            default_roster = load_model_roster(DEFAULT_ROSTER_PATH)
            self.assertEqual(len(dataset.cases), len(default_roster))
            self.assertEqual({case.name for case in dataset.cases}, set(default_roster))
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

    def test_roster_path_overrides_default_file(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = Path(directory)
            (workspace_root / "data-source").mkdir()
            manifest = build_suricata_fixture(workspace_root / "data-source")
            roster_path = workspace_root / "roster.yaml"
            roster_path.write_text("models:\n  - provider:model-a\n  - provider:model-b\n")

            dataset = build_dataset(manifest, workspace_root, roster_path=roster_path)

            self.assertEqual([case.name for case in dataset.cases], ["provider:model-a", "provider:model-b"])

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
