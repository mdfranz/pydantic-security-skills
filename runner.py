import argparse
import json
import re
import signal
import sys
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic_ai import Agent
from pydantic_ai.capabilities import Thinking
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartEndEvent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.profiles.anthropic import ANTHROPIC_THINKING_BUDGET_MAP
from pydantic_ai_harness import CodeMode, FileSystem
from pydantic_ai_harness.memory import FileStore, Memory
from pydantic_monty import MountDir, OSAccess

SANDBOX_WORKSPACE_MOUNT = "/workspace"
SANDBOX_SKILL_MOUNT = "/skill"
SANDBOX_DATA_MOUNT = "/data"

# Task IDs are identifiers, not paths -- this rejects path separators, '.'/'..', absolute
# paths, and filename injection into audit paths. See refs/workspace-lifecycle.md.
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_TASK_NAMES = {"default", "data", "logs", "memory"}
DEFAULT_TASK_ID = "default"

# Saved scripts are never executed as `python script.py` -- they're reused by pasting their
# text into a run_code call, where they always run as a top-level snippet. __name__ is never
# defined in that sandbox, so a trailing `if __name__ == "__main__":` guard (idiomatic in host
# Python, but a NameError landmine here) keeps showing up despite skill instructions warning
# against it. Lint and auto-fix it deterministically instead of relying on the model to comply.
MAIN_GUARD_RE = re.compile(r'^if __name__ == [\'"]__main__[\'"]\s*:[ \t]*(?:#.*)?\n', re.MULTILINE)
SYS_ARGV_RE = re.compile(r'\bsys\.argv\b')


def lint_and_fix_scripts(ws_path: Path) -> None:
    """Strip unsupported `if __name__ == \"__main__\":` guards from saved workspace scripts,
    and warn about `sys.argv` usage (not auto-fixed -- the right replacement is contextual)."""
    for script_path in sorted(ws_path.glob("*.py")):
        text = script_path.read_text(encoding="utf-8")

        match = MAIN_GUARD_RE.search(text)
        if match:
            body = textwrap.dedent(text[match.end():])
            text = text[: match.start()] + body
            script_path.write_text(text, encoding="utf-8")
            print(
                f"Auto-fixed {script_path.name}: removed unsupported "
                f'`if __name__ == "__main__":` guard (Monty has no __name__).'
            )

        # Naive comment stripping (good enough for this heuristic, ignores '#' in string
        # literals) so mentioning sys.argv in a comment doesn't trigger a false warning.
        code_only = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        if SYS_ARGV_RE.search(code_only):
            print(
                f"Warning: {script_path.name} uses sys.argv, which is not settable in the "
                "Monty sandbox and will fail when reused. Fix manually -- replace with a plain "
                "variable assigned near the top of the file."
            )


class TaskError(Exception):
    """Raised for invalid --task/--pristine combinations or unsafe task roots."""


class RunInterrupted(BaseException):
    """Raised from the SIGTERM handler so termination unwinds through the normal
    try/except/finally in main() -- like KeyboardInterrupt already does -- instead of the
    process dying before the audit log's run_end event is written. A BaseException, not an
    Exception, so it isn't mistaken for an application error. Can't help against SIGKILL,
    which no process can catch; that's an OS-level limit, not something this handles."""


def _raise_on_sigterm(signum, frame):
    raise RunInterrupted(f"received signal {signum}")


def resolve_task_id(args: argparse.Namespace) -> tuple[str | None, str]:
    """Resolve the task id and mode ('accumulate' or 'pristine') from CLI args.

    Mirrors the table in refs/workspace-lifecycle.md: --task reuses a name (accumulate),
    --pristine defers id generation to create_pristine_task_root (returned task_id is None
    here -- the actual UUID4 is only minted once, at directory-creation time, so it can retry
    on collision), neither falls back to 'default', and both together is an error (already
    enforced by the argparse mutually-exclusive group, but checked again here in case this is
    ever called with a hand-built namespace).
    """
    if args.task and args.pristine:
        raise TaskError("--task and --pristine are mutually exclusive")
    if args.pristine:
        return None, "pristine"
    if args.task:
        if args.task in RESERVED_TASK_NAMES:
            raise TaskError(f"'{args.task}' is a reserved task name")
        if not TASK_ID_RE.match(args.task):
            raise TaskError(f"task names must match {TASK_ID_RE.pattern}")
        return args.task, "accumulate"
    return DEFAULT_TASK_ID, "accumulate"


def create_task_root(base: Path, task_id: str, *, exclusive: bool) -> Path:
    """Resolve and create the task's workspace subdirectory, enforcing the isolation
    guarantee in refs/workspace-lifecycle.md: the task root must be a real, direct child of
    the (already-resolved) workspace base -- never a symlink, never a traversal target.

    `exclusive=True` (pristine mode) requires the directory not already exist, retrying on
    the vanishingly unlikely UUID4 collision. `exclusive=False` (accumulate) creates it if
    missing and reuses it otherwise.
    """
    candidate = base / task_id
    if candidate.exists() and (candidate.is_symlink() or not candidate.is_dir()):
        raise TaskError(f"task root {candidate} exists and is not a plain directory")

    if exclusive:
        candidate.mkdir(parents=False)
    else:
        candidate.mkdir(parents=False, exist_ok=True)

    resolved = candidate.resolve()
    if resolved.parent != base:
        raise TaskError(f"task root {resolved} is not a direct child of workspace base {base}")
    return resolved


def create_pristine_task_root(base: Path, *, max_attempts: int = 5) -> tuple[str, Path]:
    """Generate a fresh task-<uuid4> id and create its directory exclusively, retrying on
    the vanishingly unlikely name collision."""
    last_error: Exception | None = None
    for _ in range(max_attempts):
        task_id = f"task-{uuid.uuid4()}"
        try:
            return task_id, create_task_root(base, task_id, exclusive=True)
        except FileExistsError as e:
            last_error = e
            continue
    raise TaskError(f"could not allocate a pristine task directory after {max_attempts} attempts") from last_error


class AuditLog:
    """Append-only JSON Lines event stream for a single run, written to workspace/logs/ --
    a flat sibling of the task directories, outside the agent's reach. Richer than a console
    transcript by design: every event is flushed as it happens, so a failed or interrupted
    run still leaves a complete audit record. See refs/workspace-lifecycle.md."""

    def __init__(self, logs_dir: Path, task_id: str, run_id: str):
        self.path = logs_dir / f"runner-{task_id}-{run_id}.jsonl"
        self._file = self.path.open("a", encoding="utf-8")
        # Audit records can contain prompts, tool data, generated code, model responses, and
        # reasoning -- restrict to the owner regardless of umask. See "Retention and
        # permissions" in refs/workspace-lifecycle.md.
        self.path.chmod(0o600)

    def event(self, kind: str, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": kind,
            **fields,
        }
        self._file.write(json.dumps(record, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def make_event_stream_handler(audit: AuditLog):
    """Build an event_stream_handler that mirrors model responses and tool calls/results to
    the audit log incrementally, as pydantic_ai emits them during agent.run_sync -- not just
    reconstructed afterward from the finished conversation."""

    async def handler(ctx, event_iter):
        async for event in event_iter:
            if isinstance(event, PartEndEvent):
                part = event.part
                if isinstance(part, TextPart) and part.content.strip():
                    audit.event("model_text", content=part.content)
                elif isinstance(part, ThinkingPart) and part.content.strip():
                    audit.event("model_thinking", content=part.content)
            elif isinstance(event, FunctionToolCallEvent):
                part = event.part
                args = part.args_as_dict() if part.args else None
                if part.tool_name == "run_code":
                    audit.event("run_code_call", tool_call_id=part.tool_call_id, code=(args or {}).get("code"))
                else:
                    audit.event(
                        "tool_call", tool_call_id=part.tool_call_id, tool_name=part.tool_name, args=args
                    )
            elif isinstance(event, FunctionToolResultEvent):
                part = event.part
                tool_name = getattr(part, "tool_name", None)
                content = getattr(part, "content", None)
                kind = "run_code_return" if tool_name == "run_code" else "tool_result"
                audit.event(kind, tool_call_id=part.tool_call_id, tool_name=tool_name, content=content)

    return handler


SANDBOX_NOTES_PATH = Path(__file__).parent / "prompts" / "sandbox_notes.md"


def load_sandbox_notes() -> str:
    """Runtime/workspace mechanics shared by every skill (Monty sandbox constraints, the
    script-reuse pattern, artifact conventions) -- prepended to each skill's own instructions
    so it only needs to be maintained in one place instead of duplicated per skill."""
    if SANDBOX_NOTES_PATH.exists():
        return SANDBOX_NOTES_PATH.read_text(encoding="utf-8")
    return ""


def load_skill(skill_dir: Path):
    yaml_path = skill_dir / "skill.yaml"
    md_path = skill_dir / "SKILL.md"

    config = {}
    if yaml_path.exists():
        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f)
    if md_path.exists():
        with open(md_path, "r") as f:
            config["instructions"] = f.read()

    return config

def format_conversation_history(result, prompt: str) -> str:
    """Format the full agent conversation including all tool calls and results."""
    lines = ["## Conversation History\n"]

    # User prompt
    lines.append("### User Prompt\n")
    lines.append(f"```\n{prompt}\n```\n")

    # Messages and tool interactions
    lines.append("### Agent Communication\n")

    for msg in result.all_messages():
        if isinstance(msg, ModelRequest):
            # Skip showing request context to keep logs concise
            pass
        elif isinstance(msg, ModelResponse):
            # Show model response and tool interactions
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    # Skip run_code calls since the generated code is saved to disk
                    if part.tool_name == "run_code":
                        lines.append(f"#### Tool Call: `run_code` (ID: {part.tool_call_id})\n")
                        lines.append("(Code saved to generated_code/ directory)\n\n")
                    else:
                        lines.append(f"#### Tool Call: `{part.tool_name}` (ID: {part.tool_call_id})\n")
                        if part.args:
                            try:
                                args = part.args_as_dict()
                                lines.append(f"```json\n{json.dumps(args, indent=2)}\n```\n")
                            except Exception:
                                lines.append(f"```\n{part.args}\n```\n")
                elif isinstance(part, ToolReturnPart):
                    # Skip run_code returns to keep logs concise
                    if part.tool_name != "run_code":
                        lines.append(f"#### Tool Result: `{part.tool_name}` (ID: {part.tool_call_id})\n")
                        # Truncate very long outputs
                        content = part.content
                        if len(str(content)) > 1000:
                            content = str(content)[:1000] + "\n... (truncated)"
                        lines.append(f"```\n{content}\n```\n")
                elif isinstance(part, ThinkingPart):
                    if part.content.strip():
                        lines.append(f"**Agent (Thinking):**\n> {part.content.strip().replace('\n', '\n> ')}\n\n")
                else:
                    # Text content from model
                    if hasattr(part, 'content') and str(part.content).strip():
                        lines.append(f"**Agent:** {part.content}\n\n")

    return "".join(lines)


def print_thinking(result) -> None:
    """Print any thinking/reasoning parts generated in the latest run step."""
    for msg in result.new_messages():
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ThinkingPart) and part.content.strip():
                    print("\n--- Agent Thinking ---")
                    print(part.content.strip())

def main():
    parser = argparse.ArgumentParser(description="Pydantic AI Security Skill Runner")
    parser.add_argument(
        "skill_dir", nargs="?", default="skills/suricata-analyst", help="Path to skill directory"
    )
    parser.add_argument("prompt", help="Query prompt for the agent")
    parser.add_argument("--model", default="google:gemini-3-flash-preview", help="Model ID")
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
    args = parser.parse_args()

    try:
        task_id, mode = resolve_task_id(args)
    except TaskError as e:
        parser.error(str(e))

    # Hoisted here (rather than loaded alongside the rest of the skill config, further down)
    # because the memory scope, and the startup print below, both need it before that point.
    skill_path = Path(args.skill_dir)

    # Memory persists a per-task notebook exactly like scripts/analyst_logs already do, so it
    # follows task_id/mode precisely: accumulate tasks get a growing notebook; a freshly minted
    # pristine task_id gives an empty scope as a structural consequence, no special-casing
    # needed for *emptiness*. But the capability itself is omitted entirely in pristine mode
    # (not just pointed at an empty scope) -- see refs/workspace-lifecycle.md and
    # compare_models.sh, which needs identical tool surface/prompt tokens across pristine runs
    # of different models for a fair comparison.
    memory_enabled = mode != "pristine"
    memory_scope = f"{skill_path.name}/{task_id}" if memory_enabled else None

    base_path = Path(args.workspace).resolve()
    base_path.mkdir(parents=True, exist_ok=True)

    data_dir = base_path / "data"
    data_dir.mkdir(exist_ok=True)
    logs_dir = base_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    # Audit records under here can contain prompts, tool data, and model output -- restrict
    # to the owner regardless of umask, on every run (not just first creation), so an
    # externally loosened directory gets re-tightened rather than silently trusted.
    logs_dir.chmod(0o700)

    memory_dir = base_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    # Memory notes can contain security-analysis findings -- same rationale as logs_dir above.
    # Created and chmod'd here, not left to FileStore's own lazy mkdir (which never chmods), so
    # the directory is owner-only before Memory/FileStore ever touch disk.
    memory_dir.chmod(0o700)

    try:
        if mode == "pristine":
            task_id, ws_path = create_pristine_task_root(base_path)
        else:
            ws_path = create_task_root(base_path, task_id, exclusive=False)
    except TaskError as e:
        parser.error(str(e))

    run_stamp = datetime.now().strftime("%y-%m-%d_%H-%M-%S")
    run_id = uuid.uuid4().hex

    audit = AuditLog(logs_dir, task_id, run_id)
    print(
        f"Task: {task_id} (mode: {mode}) -- workspace: {ws_path} -- "
        f"memory: {memory_scope if memory_enabled else 'disabled (pristine)'}"
    )

    # Converts SIGTERM into a normal Python exception so the finally block below still runs
    # and writes run_end -- without this, a hard kill (e.g. a process manager's timeout, as
    # opposed to Ctrl+C's KeyboardInterrupt, which Python already turns into an exception)
    # terminates before the audit log is closed out. Registered only once audit exists, since
    # there's nothing to flush before that point anyway.
    signal.signal(signal.SIGTERM, _raise_on_sigterm)

    if args.logfire:
        try:
            import logfire

            logfire.configure()
            logfire.instrument_pydantic_ai()
            print("Logfire configured and instrumentation enabled.")
        except Exception as e:
            print(f"Logfire not available: {e}")
            print("Continuing without Logfire tracing.")

    config = load_skill(skill_path)
    skill_instructions = config.get("instructions", "You are a security analyst.")
    sandbox_notes = load_sandbox_notes()
    instructions = f"{sandbox_notes}\n\n{skill_instructions}" if sandbox_notes else skill_instructions

    # In interactive mode, add checkpoint instructions after major analysis phases
    if args.interactive:
        instructions += (
            "\n\n## INTERACTIVE MODE: Frequent Checkpoints\n\n"
            "After each major analysis phase (discovery, initial findings, threat hunting, etc.), "
            "STOP and ask the user:\n"
            "- What you've found so far\n"
            "- What you plan to investigate next (if anything)\n"
            "- Ask: 'Continue with [next analysis]?' (yes/no/focus on X instead)\n\n"
            "Do NOT assume the user wants exhaustive analysis. Keep analysis scope under user control. "
            "If user says 'no', wrap up with what you have. If they say 'focus on X', pivot to that. "
            "Multiple short checkpoints are better than one long silent analysis."
        )

    capabilities = [
        FileSystem(root_dir=str(ws_path)),
        CodeMode(
            tools=[],  # Keep FileSystem tools native so they're callable without run_code
            mount=[
                MountDir(SANDBOX_WORKSPACE_MOUNT, str(ws_path), mode="read-write"),
                # Read-only so generated code can consult a skill's reference material
                # (e.g. references/*.md) without being able to modify the skill itself.
                MountDir(SANDBOX_SKILL_MOUNT, str(skill_path.resolve()), mode="read-only"),
                # Read-only canonical evidence, shared across tasks (including pristine ones).
                # Not agent state -- never copied into a task workspace or writable by the agent.
                MountDir(SANDBOX_DATA_MOUNT, str(data_dir), mode="read-only"),
            ],
            # Empty environ keeps host env vars isolated; only the host clock is exposed,
            # so generated code can timestamp filenames per the skill's naming convention.
            os_access=OSAccess(environ={}),
        ),
    ]

    if memory_enabled:
        capabilities.append(
            Memory(
                store=FileStore(str(memory_dir)),
                # namespace/agent_name become a scope path segment (validated against
                # [A-Za-z0-9_.-]) -- safe today since skill_path.name and task_id are both
                # drawn from filesystem-safe names, but keep any future skill directory name
                # within that charset or scope resolution raises unconditionally, before
                # Memory's own injection_errors handling can apply.
                namespace=skill_path.name,
                agent_name=task_id,
            )
        )

    model_settings = {}
    if args.max_tokens is not None:
        model_settings["max_tokens"] = args.max_tokens

    if args.thinking:
        capabilities.append(Thinking(effort=args.thinking))
        # Anthropic rejects requests where max_tokens <= thinking.budget_tokens. Floor
        # max_tokens to comfortably clear the budget even if the user passed an explicit
        # --max-tokens that's too low for this effort level -- an explicit but insufficient
        # value should still be raised, not trusted as-is.
        effort_budget = ANTHROPIC_THINKING_BUDGET_MAP[args.thinking]
        min_max_tokens = effort_budget + 4096
        if model_settings.get("max_tokens", 0) < min_max_tokens:
            model_settings["max_tokens"] = min_max_tokens

    agent = Agent(
        args.model,
        # Stable logging identity independent of task id/mode -- a task-specific name would
        # create unhelpful high-cardinality telemetry. task_id/run_id are attached per-run below.
        name=skill_path.name,
        system_prompt=instructions,
        capabilities=capabilities,
        model_settings=model_settings or None,
    )

    lint_and_fix_scripts(ws_path)

    run_prompt = args.prompt

    # Input files can't be discovered with the FileSystem tool (its root is the task
    # workspace, not /data), so the runner surfaces the /data inventory directly in the prompt.
    data_files = sorted(p.name for p in data_dir.iterdir() if p.is_file())
    if data_files:
        inventory = "\n".join(f"- {SANDBOX_DATA_MOUNT}/{name}" for name in data_files)
        run_prompt = (
            f"Input files available read-only at {SANDBOX_DATA_MOUNT} (use these exact paths "
            f"in run_code):\n{inventory}\n\n{run_prompt}"
        )
        print(f"Found {len(data_files)} input file(s) in data: {', '.join(data_files)}")

    existing_scripts = sorted(p.name for p in ws_path.glob("*.py"))
    if existing_scripts:
        inventory = "\n".join(f"- {name}" for name in existing_scripts)
        run_prompt = (
            "Reusable scripts already saved in the workspace from earlier sessions (read one "
            "with the FileSystem tool and adapt it before writing new analysis code from "
            f"scratch, per the skill instructions):\n{inventory}\n\n{run_prompt}"
        )
        print(f"Found {len(existing_scripts)} existing script(s) in workspace: {', '.join(existing_scripts)}")

    run_metadata = {"task_id": task_id, "run_id": run_id}
    stream_handler = make_event_stream_handler(audit)
    completed = False

    audit.event(
        "run_start",
        task_id=task_id,
        run_id=run_id,
        mode=mode,
        skill=skill_path.name,
        model=args.model,
        workspace=str(ws_path),
        interactive=args.interactive,
        thinking=args.thinking,
        max_tokens=args.max_tokens,
        memory_enabled=memory_enabled,
        memory_scope=memory_scope,
    )

    try:
        print(f"Running Pydantic AI agent on skill: {skill_path.name}")
        audit.event("prompt", prompt=run_prompt)
        result = agent.run_sync(run_prompt, event_stream_handler=stream_handler, metadata=run_metadata)
        print_thinking(result)
        print("\n--- Agent Response ---")
        print(result.output)

        # Collect all outputs for analyst_log (especially important in interactive mode)
        all_outputs = [result.output]

        # In interactive mode, handle checkpoints until user says stop
        if args.interactive:
            checkpoint_count = 1
            while True:
                print("\n" + "=" * 60)
                user_input = input(
                    f"\n[Checkpoint {checkpoint_count}] Continue, stop, or adjust focus? (continue/stop/focus on X): "
                ).strip()

                if user_input.lower() == "stop":
                    print("Wrapping up analysis.")
                    break
                elif user_input.lower() == "continue":
                    print("Continuing analysis...\n")
                    continuation_prompt = (
                        "The user wants to continue. Proceed with the next phase of analysis you outlined. "
                        "After completing this phase, summarize what you found and ask if they want to continue further."
                    )
                    audit.event("prompt", prompt=continuation_prompt)
                    result = agent.run_sync(
                        continuation_prompt,
                        message_history=result.all_messages(),
                        event_stream_handler=stream_handler,
                        metadata=run_metadata,
                    )
                    print_thinking(result)
                    print("\n--- Analysis Continued ---")
                    print(result.output)
                    all_outputs.append(result.output)
                    checkpoint_count += 1
                else:
                    # User specified a different focus
                    focus = user_input.replace("focus on ", "").strip()
                    if focus:
                        print(f"Pivoting to focus on: {focus}\n")
                        continuation_prompt = (
                            f"The user wants to shift focus to: {focus}\n\n"
                            "Adjust your analysis to focus on this area specifically. After this analysis phase, "
                            "ask if they want to continue investigating other angles."
                        )
                        audit.event("prompt", prompt=continuation_prompt)
                        result = agent.run_sync(
                            continuation_prompt,
                            message_history=result.all_messages(),
                            event_stream_handler=stream_handler,
                            metadata=run_metadata,
                        )
                        print_thinking(result)
                        print("\n--- Focused Analysis ---")
                        print(result.output)
                        all_outputs.append(result.output)
                        checkpoint_count += 1
                    else:
                        print("Could not parse focus. Use 'continue', 'stop', or 'focus on <topic>'.")

        # Save the full conversation and findings independently of the audit log
        report_path = ws_path / f"analyst_log-{run_stamp}.md"
        conversation = format_conversation_history(result, run_prompt)

        # In interactive mode, combine all checkpoint outputs
        if args.interactive and len(all_outputs) > 1:
            combined_findings = "\n\n---\n\n".join(
                f"### Checkpoint {i + 1}\n{output}"
                for i, output in enumerate(all_outputs)
            )
            findings_section = f"## Analysis Checkpoints\n\n{combined_findings}\n"
        else:
            findings_section = f"## Final Findings\n\n{result.output}\n"

        report_path.write_text(
            f"# Analysis Report\n\n"
            f"- Skill: `{skill_path.name}`\n"
            f"- Prompt: {args.prompt}\n"
            f"- Run: `{run_stamp}`\n"
            f"- Mode: {'Interactive (multiple checkpoints)' if args.interactive else 'Standard'}\n\n"
            f"{conversation}\n"
            f"{findings_section}",
            encoding="utf-8",
        )
        print(f"\nSaved analysis report: {report_path}")

        # Persist every generated run_code program. These are audit artifacts;
        # reusable analysis scripts should still be written by the agent with
        # FileSystem.write_file using meaningful names.
        generated_dir = ws_path / "generated_code"
        generated_dir.mkdir(exist_ok=True)
        code_index = 0
        for msg in result.all_messages():
            for part in msg.parts:
                if isinstance(part, ToolCallPart) and part.tool_name == "run_code":
                    code = part.args_as_dict().get("code") if part.args else None
                    if code:
                        code_index += 1
                        code_path = generated_dir / f"{run_stamp}-{code_index:02d}.py"
                        code_path.write_text(code, encoding="utf-8")
                        if args.debug:
                            print(f"\n[call {part.tool_call_id}]\n{code}")
                elif isinstance(part, ToolReturnPart) and part.tool_name == "run_code" and args.debug:
                    print(f"\n[return {part.tool_call_id}]\n{part.content}")
        if code_index:
            print(f"Saved {code_index} generated run_code artifact(s) in {generated_dir}")

        # Fix up any scripts saved this run before the process exits, so a same-run reuse
        # later in this session (or the next session's inventory) never sees a broken guard.
        lint_and_fix_scripts(ws_path)

        completed = True

    except RunInterrupted as e:
        audit.event("interrupted", reason=str(e))
        raise
    except Exception as e:
        audit.event("error", error=str(e), error_type=type(e).__name__)
        raise
    finally:
        audit.event("run_end", status="completed" if completed else "failed")
        audit.close()

if __name__ == "__main__":
    try:
        main()
    except RunInterrupted:
        # Matches the shell's own SIGTERM exit-code convention (128 + 15) instead of a raw
        # traceback -- the run_end/interrupted events are already written by main()'s finally.
        sys.exit(143)
