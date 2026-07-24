"""Deterministic cleanup for workspace scripts saved across runs (see run_core.py's
sandbox-notes docstring for why this exists instead of relying on model compliance)."""

import ast
import re
import textwrap
from pathlib import Path
from typing import Callable

# Saved scripts are never executed as `python script.py` -- they're reused by pasting their
# text into a run_code call, where they always run as a top-level snippet. __name__ is never
# defined in that sandbox, so a trailing `if __name__ == "__main__":` guard (idiomatic in host
# Python, but a NameError landmine here) keeps showing up despite skill instructions warning
# against it. Lint and auto-fix it deterministically instead of relying on the model to comply.
MAIN_GUARD_RE = re.compile(r'^if __name__ == [\'"]__main__[\'"]\s*:[ \t]*(?:#.*)?\n', re.MULTILINE)
SYS_ARGV_RE = re.compile(r'\bsys\.argv\b')


def _comma_format_lines(text: str) -> tuple[int, ...]:
    """Find f-string format specifications Monty cannot reuse.

    Monty rejects Python's comma thousands separator (for example, ``{count:,d}`` or
    ``{count:>12,}``). Parsing first keeps ordinary commas in strings and expressions from
    producing a warning.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()

    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FormattedValue) or not isinstance(node.format_spec, ast.JoinedStr):
            continue
        literals = (
            value.value
            for value in node.format_spec.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
        if any("," in literal for literal in literals):
            lines.add(node.lineno)
    return tuple(sorted(lines))


def lint_and_fix_scripts(ws_path: Path, on_message: Callable[[str], None] = print) -> None:
    """Strip unsupported `if __name__ == "__main__":` guards from saved workspace scripts,
    and warn about `sys.argv` usage (not auto-fixed -- the right replacement is contextual)."""
    for script_path in sorted(ws_path.glob("*.py")):
        text = script_path.read_text(encoding="utf-8")

        match = MAIN_GUARD_RE.search(text)
        if match:
            body = textwrap.dedent(text[match.end():])
            text = text[: match.start()] + body
            script_path.write_text(text, encoding="utf-8")
            on_message(
                f"Auto-fixed {script_path.name}: removed unsupported "
                f'`if __name__ == "__main__":` guard (Monty has no __name__).'
            )

        # Naive comment stripping (good enough for this heuristic, ignores '#' in string
        # literals) so mentioning sys.argv in a comment doesn't trigger a false warning.
        code_only = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        if SYS_ARGV_RE.search(code_only):
            on_message(
                f"Warning: {script_path.name} uses sys.argv, which is not settable in the "
                "Monty sandbox and will fail when reused. Fix manually -- replace with a plain "
                "variable assigned near the top of the file."
            )

        for line_number in _comma_format_lines(text):
            on_message(
                f"Warning: {script_path.name}:{line_number} uses an f-string comma format "
                "specifier, which Monty does not support. Remove the thousands separator "
                "before reusing this script."
            )
