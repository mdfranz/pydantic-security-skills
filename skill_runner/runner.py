"""Thin CLI entrypoint: argparse, positional resolution, UI selection/dispatch, and
SIGTERM exit-code mapping. No agent execution, workspace mutation, artifact formatting, or
UI rendering lives here -- see run_core.py (shared run behavior) and console_ui.py /
ui_textual.py (the two UI drivers)."""

import argparse
import importlib.util

from .audit import RunInterrupted, map_run_interrupted_exit_code
from .config import RunOptions, load_model_catalog
from .console_ui import run_console
from .run_core import PROJECT_ROOT, RESERVED_TASK_NAMES, TASK_ID_RE, TaskError

DEFAULT_SKILL_DIR = "skills/suricata-analyst"


def build_parser() -> argparse.ArgumentParser:
    default_model = load_model_catalog(PROJECT_ROOT).default_model

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
        "--skill",
        dest="explicit_skill_dir",
        default=None,
        help="Explicit skill directory. In particular, use with --ui textual to open an empty "
        "session for a non-default skill without supplying a prompt.",
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
        help="Workspace base/case root. Contains data-source/ (read-only input; data/ is used "
        "instead as a legacy fallback if data-source/ doesn't exist), data-sink/parquet/ (host-only "
        "Parquet cache for query_events/aggregate_events, never agent-visible), logs/ (host audit, "
        "not agent-visible), memory/ (per-task notebook, reachable only via the memory tools, "
        "never through FileSystem), and one subdirectory per task (the agent's writable root).",
    )
    task_group = parser.add_mutually_exclusive_group()
    reserved_names = ", ".join(f"'{name}'" for name in sorted(RESERVED_TASK_NAMES))
    task_group.add_argument(
        "--task",
        default=None,
        help="Reuse (or create) a named task workspace -- accumulates across runs. Must match "
        f"{TASK_ID_RE.pattern}; {reserved_names} are reserved. Defaults "
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
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Max retries for a failing run_code call (syntax/runtime errors count as retries) "
        "before the turn aborts with UnexpectedModelBehavior. Default: 5.",
    )
    parser.add_argument(
        "--max-run-seconds",
        type=int,
        default=None,
        help="Wall-clock budget per turn. If a single turn (one submitted prompt, including all "
        "its tool calls) runs longer than this, it's cleanly interrupted the same way Ctrl+C/"
        "SIGTERM is -- partial progress is preserved, the audit log records the interruption, and "
        "the process exits 143. Default: no limit.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Max model round-trips (requests) per turn, passed through to pydantic_ai's "
        "UsageLimits.request_limit. Default: pydantic_ai's own default (50).",
    )
    return parser


def resolve_positionals(
    positionals: list[str], ui: str, parser: argparse.ArgumentParser, explicit_skill_dir: str | None = None
) -> tuple[str, str | None]:
    """Resolve the shared [skill_dir] prompt positional grammar. A single optional
    positional would be ambiguous with argparse (it can't tell a lone skill_dir from a lone
    prompt), so both stay one positional list, resolved explicitly here instead."""
    if explicit_skill_dir is not None:
        if len(positionals) == 0:
            if ui != "textual":
                parser.error("prompt is required unless --ui textual is used with no arguments")
            return explicit_skill_dir, None
        if len(positionals) == 1:
            return explicit_skill_dir, positionals[0]
        parser.error("--skill accepts at most one positional prompt")

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
    options = RunOptions.from_namespace(args)

    skill_dir, initial_prompt = resolve_positionals(
        args.positionals, args.ui, parser, args.explicit_skill_dir
    )

    # Before any workspace/task directory is created, since --pristine has side effects.
    if args.ui == "textual" and importlib.util.find_spec("textual") is None:
        parser.error("--ui textual requires the 'textual' extra: uv sync --extra tui")

    try:
        if args.ui == "textual":
            from .ui_textual import run_textual

            run_textual(skill_dir, initial_prompt, options)
        else:
            run_console(skill_dir, initial_prompt, options)
    except TaskError as e:
        parser.error(str(e))
    except RunInterrupted:
        # Caught here, not just under `if __name__ == "__main__"` below -- the installed
        # `skill-runner` console script (pyproject.toml's `skill-runner = "skill_runner.runner:
        # main"`, what `uv run skill-runner` actually invokes) calls main() directly and never
        # goes through that guard, so a version living only there never ran in practice for the
        # primary entry point. Ctrl+C, SIGTERM, and --max-run-seconds all raise RunInterrupted by
        # design (see audit.py) and rely on this mapping for a clean exit instead of a traceback.
        map_run_interrupted_exit_code()


if __name__ == "__main__":
    main()
