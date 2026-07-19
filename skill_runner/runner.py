"""Thin CLI entrypoint: argparse, positional resolution, UI selection/dispatch, and
SIGTERM exit-code mapping. No agent execution, workspace mutation, artifact formatting, or
UI rendering lives here -- see run_core.py (shared run behavior) and console_ui.py /
ui_textual.py (the two UI drivers)."""

import argparse
import importlib.util

from .audit import RunInterrupted, map_run_interrupted_exit_code
from .console_ui import run_console
from .run_core import TASK_ID_RE, TaskError, load_models_config

DEFAULT_SKILL_DIR = "skills/suricata-analyst"


def build_parser() -> argparse.ArgumentParser:
    models_config = load_models_config()
    default_model = models_config.get("default_model", "google:gemini-3-flash-preview")

    parser = argparse.ArgumentParser(description="Pydantic AI Security Skill Runner")
    parser.add_argument(
        "positionals",
        nargs="*",
        metavar="[skill_dir] prompt",
        help="Path to skill directory (optional, defaults to "
        f"{DEFAULT_SKILL_DIR}) and the query prompt. The prompt itself is optional only "
        "with --ui textual (an empty session opens and the first message is typed into "
        "the bottom bar); console mode always requires one.",
    )
    parser.add_argument(
        "--ui",
        default="console",
        choices=["console", "textual"],
        help="UI mode. 'console' (default): today's raw-print behavior, scripts/one-shot "
        "runs. 'textual': multi-panel TUI for interactive investigation sessions -- "
        "requires the 'textual' extra (uv sync --extra tui).",
    )
    parser.add_argument("--model", default=default_model, help="Model ID or alias")
    parser.add_argument(
        "--workspace",
        default="./workspace",
        help="Workspace base/case root. Contains data/ (read-only input), logs/ (host audit, "
        "not agent-visible), memory/ (per-task notebook, reachable only via the memory tools, "
        "never through FileSystem), and one subdirectory per task (the agent's writable root).",
    )
    task_group = parser.add_mutually_exclusive_group()
    task_group.add_argument(
        "--task",
        default=None,
        help="Reuse (or create) a named task workspace -- accumulates across runs. Must match "
        f"{TASK_ID_RE.pattern}; 'default', 'data', 'logs', and 'memory' are reserved. Defaults "
        "to the shared 'default' task if neither --task nor --pristine is given.",
    )
    task_group.add_argument(
        "--pristine",
        action="store_true",
        help="Start a fresh, isolated task workspace (auto-generated id) with no prior agent "
        "state. The directory persists after the run but is never selected automatically again.",
    )
    parser.add_argument("--debug", action="store_true", help="Print generated sandbox code and its result")
    parser.add_argument(
        "--logfire",
        action="store_true",
        help="Trace the run with Logfire. Prints spans to the console with no setup; "
        "also ships to the Logfire UI if LOGFIRE_TOKEN is set (or `logfire auth` has been run).",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactive mode: agent explains its investigation plan and asks for confirmation "
        "before deep analysis. Under --ui textual this only affects that system-prompt "
        "addendum -- the bottom bar always accepts free-text follow-ups either way.",
    )
    parser.add_argument(
        "--thinking",
        default=None,
        choices=["low", "medium", "high", "xhigh"],
        help="Enable model thinking/reasoning with the specified effort level.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="The maximum number of tokens to generate before stopping.",
    )
    return parser


def resolve_positionals(
    positionals: list[str], ui: str, parser: argparse.ArgumentParser
) -> tuple[str, str | None]:
    """Resolve the shared [skill_dir] prompt positional grammar. A single optional
    positional would be ambiguous with argparse (it can't tell a lone skill_dir from a lone
    prompt), so both stay one positional list, resolved explicitly here instead."""
    if len(positionals) == 0:
        if ui != "textual":
            parser.error("prompt is required unless --ui textual is used with no arguments")
        return DEFAULT_SKILL_DIR, None
    if len(positionals) == 1:
        return DEFAULT_SKILL_DIR, positionals[0]
    if len(positionals) == 2:
        return positionals[0], positionals[1]
    parser.error("too many positional arguments (expected: [skill_dir] prompt)")


def main():
    parser = build_parser()
    args = parser.parse_args()

    skill_dir, initial_prompt = resolve_positionals(args.positionals, args.ui, parser)

    # Before any workspace/task directory is created, since --pristine has side effects.
    if args.ui == "textual" and importlib.util.find_spec("textual") is None:
        parser.error("--ui textual requires the 'textual' extra: uv sync --extra tui")

    try:
        if args.ui == "textual":
            from .ui_textual import run_textual

            run_textual(skill_dir, initial_prompt, args)
        else:
            run_console(skill_dir, initial_prompt, args)
    except TaskError as e:
        parser.error(str(e))


if __name__ == "__main__":
    try:
        main()
    except RunInterrupted:
        map_run_interrupted_exit_code()
