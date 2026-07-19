"""Console UX: today's behavior, kept exactly as-is -- raw prints to a flat terminal, plus
a blocking input()-based checkpoint loop under --interactive. "Dumb and primitive" by
design; this is what scripts and quick one-shot runs use."""

import json
from typing import Literal, NamedTuple

from .config import RunOptions
from .run_core import prepare_run
from .session import RunSession


def _echo(label: str, content: str, debug: bool, limit: int = 400) -> None:
    """Print a live progress line to stdout. Full content under --debug; otherwise
    truncated so a large run_code body or tool result doesn't flood the console."""
    content = content.strip()
    if not debug and len(content) > limit:
        content = content[:limit] + f"... ({len(content) - limit} more chars, rerun with --debug to see all)"
    print(f"\n--- {label} ---\n{content}", flush=True)


class ConsoleSink:
    """status() = plain print; emit() reproduces today's exact _echo(label, content)
    output per event kind, verbatim."""

    def __init__(self, debug: bool = False):
        self.debug = debug

    def status(self, message: str) -> None:
        print(message, flush=True)

    def emit(self, kind: str, **fields: object) -> None:
        if kind == "model_text":
            _echo("Agent", fields["content"], self.debug)
        elif kind == "model_thinking":
            _echo("Agent Thinking", fields["content"], self.debug)
        elif kind == "run_code_call":
            _echo(f"run_code call ({fields['tool_call_id']})", fields["code"], self.debug)
        elif kind == "tool_call":
            _echo(
                f"tool call: {fields['tool_name']} ({fields['tool_call_id']})",
                json.dumps(fields.get("args") or {}),
                self.debug,
            )
        elif kind == "run_code_return":
            _echo(f"run_code result ({fields['tool_call_id']})", str(fields["content"]), self.debug)
        elif kind == "tool_result":
            _echo(
                f"tool result: {fields['tool_name']} ({fields['tool_call_id']})",
                str(fields["content"]),
                self.debug,
            )


class CheckpointAction(NamedTuple):
    kind: Literal["continue", "stop", "focus"]
    focus: str | None = None


def parse_checkpoint_input(user_input: str) -> CheckpointAction:
    stripped = user_input.strip()
    if stripped.lower() == "stop":
        return CheckpointAction(kind="stop")
    if stripped.lower() == "continue":
        return CheckpointAction(kind="continue")
    focus = stripped.replace("focus on ", "").strip()
    return CheckpointAction(kind="focus", focus=focus or None)


def build_continuation_prompt(action: CheckpointAction) -> str | None:
    if action.kind == "continue":
        return (
            "The user wants to continue. Proceed with the next phase of analysis you outlined. "
            "After completing this phase, summarize what you found and ask if they want to continue further."
        )
    if action.kind == "focus" and action.focus:
        return (
            f"The user wants to shift focus to: {action.focus}\n\n"
            "Adjust your analysis to focus on this area specifically. After this analysis phase, "
            "ask if they want to continue investigating other angles."
        )
    return None


def run_console(skill_dir: str, prompt: str, options: RunOptions) -> None:
    """Today's console behavior, preserved byte-for-byte: agent.run_sync, a blocking
    input() checkpoint loop under --interactive, and the same artifact/report shape."""
    sink = ConsoleSink(debug=options.debug)
    setup = prepare_run(skill_dir, prompt, options, sink)

    with RunSession(setup, sink, checkpoint_each_turn=False) as session:
        print(f"Running Pydantic AI agent on skill: {setup.skill_name}")
        result = session.submit_sync(prompt)
        print("\n--- Agent Response ---")
        print(result.output)

        # In interactive mode, handle checkpoints until user says stop
        if options.interactive:
            checkpoint_count = 1
            while True:
                print("\n" + "=" * 60)
                user_input = input(
                    f"\n[Checkpoint {checkpoint_count}] Continue, stop, or adjust focus? (continue/stop/focus on X): "
                )
                action = parse_checkpoint_input(user_input)

                if action.kind == "stop":
                    print("Wrapping up analysis.")
                    break

                continuation_prompt = build_continuation_prompt(action)

                if action.kind == "continue":
                    print("Continuing analysis...\n")
                elif action.kind == "focus":
                    if continuation_prompt is None:
                        print("Could not parse focus. Use 'continue', 'stop', or 'focus on <topic>'.")
                        continue
                    print(f"Pivoting to focus on: {action.focus}\n")

                result = session.submit_sync(continuation_prompt)
                print(f"\n--- {'Analysis Continued' if action.kind == 'continue' else 'Focused Analysis'} ---")
                print(result.output)
                checkpoint_count += 1

        session.complete()
