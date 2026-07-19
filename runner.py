"""Thin CLI entrypoint: argparse, UI dispatch, and SIGTERM exit-code mapping. No agent
execution, workspace mutation, artifact formatting, or UI rendering lives here -- see
run_core.py (shared run behavior) and console_ui.py / ui_textual.py (the two UI drivers)."""

import argparse

from audit import RunInterrupted, map_run_interrupted_exit_code
from console_ui import run_console
from run_core import TASK_ID_RE, TaskError, load_models_config


def build_parser() -> argparse.ArgumentParser:
    models_config = load_models_config()
    default_model = models_config.get("default_model", "google:gemini-3-flash-preview")

    parser = argparse.ArgumentParser(description="Pydantic AI Security Skill Runner")
    parser.add_argument(
        "skill_dir", nargs="?", default="skills/suricata-analyst", help="Path to skill directory"
    )
    parser.add_argument("prompt", help="Query prompt for the agent")
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
        help="Interactive mode: agent explains its investigation plan and asks for confirmation before deep analysis.",
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


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        run_console(args.skill_dir, args.prompt, args)
    except TaskError as e:
        parser.error(str(e))


if __name__ == "__main__":
    try:
        main()
    except RunInterrupted:
        map_run_interrupted_exit_code()
