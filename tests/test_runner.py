import shlex
import unittest
from pathlib import Path

from skill_runner.config import RunOptions
from skill_runner.run_core import build_resume_hint
from skill_runner.runner import DEFAULT_SKILL_DIR, build_parser, resolve_positionals


class PositionalResolutionTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_explicit_skill_allows_blank_textual_session(self):
        args = self.parser.parse_args(["--ui", "textual", "--skill", "skills/osqueryd-analyst"])

        skill_dir, prompt = resolve_positionals(
            args.positionals, args.ui, self.parser, args.explicit_skill_dir
        )

        self.assertEqual(skill_dir, "skills/osqueryd-analyst")
        self.assertIsNone(prompt)

    def test_explicit_skill_uses_one_positional_as_prompt(self):
        args = self.parser.parse_args(["--skill", "skills/osqueryd-analyst", "Find persistence"])

        skill_dir, prompt = resolve_positionals(
            args.positionals, args.ui, self.parser, args.explicit_skill_dir
        )

        self.assertEqual(skill_dir, "skills/osqueryd-analyst")
        self.assertEqual(prompt, "Find persistence")

    def test_existing_single_positional_behavior_is_unchanged(self):
        args = self.parser.parse_args(["Find persistence"])

        skill_dir, prompt = resolve_positionals(
            args.positionals, args.ui, self.parser, args.explicit_skill_dir
        )

        self.assertEqual(skill_dir, DEFAULT_SKILL_DIR)
        self.assertEqual(prompt, "Find persistence")


class ResumeHintTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_console_hint_round_trips_skill_prompt_and_run_settings(self):
        options = RunOptions(
            model="google:gemini-3.6-flash",
            workspace=Path("./case workspace"),
            interactive=True,
            thinking="high",
            logfire=True,
            max_turns=30,
        )

        command = build_resume_hint(
            "skills/osqueryd-analyst",
            "task-123",
            options,
        )
        args = self.parser.parse_args(shlex.split(command)[3:])
        skill_dir, prompt = resolve_positionals(
            args.positionals,
            args.ui,
            self.parser,
            args.explicit_skill_dir,
        )

        self.assertEqual(skill_dir, "skills/osqueryd-analyst")
        self.assertIn("Resume this task", prompt)
        self.assertEqual(args.task, "task-123")
        self.assertEqual(args.workspace, "case workspace")
        self.assertEqual(args.model, "google:gemini-3.6-flash")
        self.assertTrue(args.interactive)
        self.assertEqual(args.thinking, "high")
        self.assertTrue(args.logfire)
        self.assertEqual(args.max_turns, 30)

    def test_textual_hint_round_trips_without_a_synthetic_prompt(self):
        options = RunOptions(
            model="openrouter:deepseek/deepseek-v4-pro",
            workspace=Path("./workspace"),
            ui="textual",
        )

        command = build_resume_hint(
            "skills/suricata-analyst",
            "task-456",
            options,
            ui="textual",
        )
        args = self.parser.parse_args(shlex.split(command)[3:])
        skill_dir, prompt = resolve_positionals(
            args.positionals,
            args.ui,
            self.parser,
            args.explicit_skill_dir,
        )

        self.assertEqual(skill_dir, "skills/suricata-analyst")
        self.assertIsNone(prompt)
        self.assertEqual(args.ui, "textual")
        self.assertEqual(args.model, "openrouter:deepseek/deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()
