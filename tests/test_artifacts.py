import unittest

from skill_runner.artifacts import Transcript, Turn, render_report


class ArtifactTests(unittest.TestCase):
    def test_multi_turn_report_uses_structured_turns(self):
        transcript = Transcript(initial_prompt="first")
        transcript.append(Turn("first", "inventory\n\nfirst", "one", [], "provider:model"))
        transcript.append(Turn("second", "second", "two", [], "provider:model"))

        report = render_report(run_stamp="stamp", skill_name="demo", transcript=transcript)

        self.assertIn("- Mode: Textual (multi-turn session)", report)
        self.assertIn("1. inventory\n\nfirst", report)
        self.assertIn("2. second", report)
        self.assertIn("### Turn 1\none", report)
        self.assertIn("### Turn 2\ntwo", report)


if __name__ == "__main__":
    unittest.main()
