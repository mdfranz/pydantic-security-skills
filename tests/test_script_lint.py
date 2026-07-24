import tempfile
import unittest
from pathlib import Path

from skill_runner.script_lint import lint_and_fix_scripts


class ScriptLintTests(unittest.TestCase):
    def test_warns_for_monty_unsupported_comma_format_specifier(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "suricata_summary.py").write_text(
                'print(f"{count:>12,}")\n', encoding="utf-8"
            )
            messages: list[str] = []

            lint_and_fix_scripts(workspace, on_message=messages.append)

        self.assertEqual(len(messages), 1)
        self.assertIn("suricata_summary.py:1", messages[0])
        self.assertIn("comma format specifier", messages[0])

    def test_ignores_commas_outside_f_string_format_specifiers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            (workspace / "suricata_summary.py").write_text(
                'print("alpha,beta")\nprint(f"{count:12d}")\n', encoding="utf-8"
            )
            messages: list[str] = []

            lint_and_fix_scripts(workspace, on_message=messages.append)

        self.assertEqual(messages, [])
