import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_runner.config import RunOptions
from skill_runner.run_core import prepare_run


class StatusSink:
    def status(self, message):
        pass


class PrepareRunTests(unittest.TestCase):
    def test_post_audit_setup_failure_is_finalized(self):
        previous_handler = signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)

            with patch("skill_runner.run_core._build_agent", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    prepare_run("missing-skill", "prompt", options, StatusSink())

            audit_paths = list((workspace / "logs").glob("*.jsonl"))
            self.assertEqual(len(audit_paths), 1)
            records = [json.loads(line) for line in audit_paths[0].read_text().splitlines()]
            self.assertEqual(records[-2]["event"], "setup_error")
            self.assertEqual(records[-1]["event"], "run_end")
            self.assertEqual(records[-1]["status"], "failed")
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)


if __name__ == "__main__":
    unittest.main()
