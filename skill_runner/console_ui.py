"""Console UX: today's behavior, kept exactly as-is -- raw prints to a flat terminal, plus
a blocking input()-based checkpoint loop under --interactive. "Dumb and primitive" by
design; this is what scripts and quick one-shot runs use."""

import argparse
import json
from typing import Literal, NamedTuple

from .audit import RunInterrupted, make_event_stream_handler
from .run_core import ArtifactSession, prepare_run, run_turn_sync, write_artifacts
from .script_lint import lint_and_fix_scripts


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


def run_console(skill_dir: str, prompt: str, args: argparse.Namespace) -> None:
    """Today's console behavior, preserved byte-for-byte: agent.run_sync, a blocking
    input() checkpoint loop under --interactive, and the same artifact/report shape."""
    sink = ConsoleSink(debug=args.debug)
    setup = prepare_run(skill_dir, prompt, args, sink, ui_mode="console")
    stream_handler = make_event_stream_handler(setup.audit, sink)

    completed = False
    run_prompt = setup.prompt_prefix + setup.initial_prompt

    try:
        print(f"Running Pydantic AI agent on skill: {setup.skill_name}")
        result = run_turn_sync(
            setup.agent,
            run_prompt,
            None,
            stream_handler=stream_handler,
            run_metadata=setup.run_metadata,
            audit=setup.audit,
        )
        print("\n--- Agent Response ---")
        print(result.output)

        # Collect all outputs for analyst_log (especially important in interactive mode)
        all_prompts = [run_prompt]
        all_outputs = [result.output]

        # In interactive mode, handle checkpoints until user says stop
        if args.interactive:
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

                result = run_turn_sync(
                    setup.agent,
                    continuation_prompt,
                    result.all_messages(),
                    stream_handler=stream_handler,
                    run_metadata=setup.run_metadata,
                    audit=setup.audit,
                )
                print(f"\n--- {'Analysis Continued' if action.kind == 'continue' else 'Focused Analysis'} ---")
                print(result.output)
                all_prompts.append(continuation_prompt)
                all_outputs.append(result.output)
                checkpoint_count += 1

        # Save the full conversation and findings independently of the audit log
        session = ArtifactSession(
            initial_prompt=setup.initial_prompt,
            prompts=all_prompts,
            outputs=all_outputs,
            message_history=result.all_messages(),
            interactive=args.interactive,
        )
        write_artifacts(
            ws_path=setup.ws_path,
            run_stamp=setup.run_stamp,
            skill_name=setup.skill_name,
            session=session,
            debug=args.debug,
            on_message=sink.status,
        )

        # Fix up any scripts saved this run before the process exits, so a same-run reuse
        # later in this session (or the next session's inventory) never sees a broken guard.
        lint_and_fix_scripts(setup.ws_path, on_message=sink.status)

        completed = True

    except RunInterrupted as e:
        setup.audit.event("interrupted", reason=str(e))
        raise
    except Exception as e:
        setup.audit.event("error", error=str(e), error_type=type(e).__name__)
        raise
    finally:
        setup.audit.event("run_end", status="completed" if completed else "failed")
        setup.audit.close()
