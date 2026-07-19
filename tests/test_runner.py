import unittest

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


if __name__ == "__main__":
    unittest.main()
