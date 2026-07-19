"""UI-neutral run behavior shared by every driver (console_ui.py, ui_textual.py): task/
workspace resolution, skill/prompt assembly, Agent construction, the RunSink protocol, the
per-turn helpers, and artifact persistence. Neither UI module should reach past this module
into agent execution or workspace mutation directly."""

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

import yaml
from pydantic_ai import Agent
from pydantic_ai.capabilities import Thinking
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.profiles.anthropic import ANTHROPIC_THINKING_BUDGET_MAP
from pydantic_ai_harness import CodeMode, FileSystem
from pydantic_ai_harness.memory import FileStore, Memory
from pydantic_monty import MountDir, OSAccess

from .audit import AuditLog, install_sigterm_handler
from .script_lint import lint_and_fix_scripts

SANDBOX_WORKSPACE_MOUNT = "/workspace"
SANDBOX_SKILL_MOUNT = "/skill"
SANDBOX_DATA_MOUNT = "/data"

# Task IDs are identifiers, not paths -- this rejects path separators, '.'/'..', absolute
# paths, and filename injection into audit paths. See refs/workspace-lifecycle.md.
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_TASK_NAMES = {"default", "data", "logs", "memory"}
DEFAULT_TASK_ID = "default"

SANDBOX_NOTES_PATH = Path(__file__).parent / "prompts" / "sandbox_notes.md"


class TaskError(Exception):
    """Raised for invalid --task/--pristine combinations or unsafe task roots."""


class RunSink(Protocol):
    def status(self, message: str) -> None: ...              # one-off lines: banner, "Saved report", etc.

    def emit(self, kind: str, **fields: object) -> None: ...  # live events, same vocabulary as AuditLog.event


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


def load_models_config() -> dict:
    """Load models configuration from models.yaml if it exists."""
    config_path = Path(__file__).parent / "models.yaml"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Warning: Failed to load models.yaml: {e}", file=sys.stderr)
    return {}


def resolve_model(model_name: str, models_config: dict) -> str:
    """Resolve model alias/short name to its full ID defined in models.yaml."""
    # Check hierarchical providers format
    providers = models_config.get("providers")
    if isinstance(providers, dict):
        for provider_info in providers.values():
            if isinstance(provider_info, dict):
                models_list = provider_info.get("models", [])
                for m in models_list:
                    if m.get("alias") == model_name or m.get("id") == model_name:
                        return m.get("id")

    # Fallback to flat models list
    models_list = models_config.get("models", [])
    if isinstance(models_list, list):
        for m in models_list:
            if m.get("alias") == model_name or m.get("id") == model_name:
                return m.get("id")
    return model_name


@dataclass
class ModelChoice:
    """One selectable entry in models.yaml, flattened out of whichever of the two supported
    shapes (hierarchical `providers`, or a flat `models` list) resolve_model() was given."""

    id: str
    alias: str
    description: str
    provider: str


def list_model_choices(models_config: dict) -> list[ModelChoice]:
    """Flatten models.yaml into a UI-agnostic list -- used by ui_textual.py's model picker.
    Mirrors resolve_model()'s two supported shapes (hierarchical providers, flat fallback)."""
    choices: list[ModelChoice] = []
    providers = models_config.get("providers")
    if isinstance(providers, dict):
        for provider_name, provider_info in providers.items():
            if not isinstance(provider_info, dict):
                continue
            for m in provider_info.get("models", []):
                model_id = m.get("id", "")
                choices.append(
                    ModelChoice(
                        id=model_id,
                        alias=m.get("alias", model_id),
                        description=m.get("description", ""),
                        provider=provider_name,
                    )
                )
        return choices

    for m in models_config.get("models", []):
        model_id = m.get("id", "")
        choices.append(
            ModelChoice(
                id=model_id,
                alias=m.get("alias", model_id),
                description=m.get("description", ""),
                provider=model_id.split(":", 1)[0] if ":" in model_id else "",
            )
        )
    return choices


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


def format_conversation_history(message_history: list[ModelMessage], prompt: str) -> str:
    """Format the full agent conversation including all tool calls and results."""
    lines = ["## Conversation History\n"]

    # User prompt
    lines.append("### User Prompt\n")
    lines.append(f"```\n{prompt}\n```\n")

    # Messages and tool interactions
    lines.append("### Agent Communication\n")

    for msg in message_history:
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


@dataclass
class ArtifactSession:
    """Everything write_artifacts needs to render a report, independent of how many turns
    were involved or which UI drove them."""

    initial_prompt: str | None
    prompts: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    message_history: list[ModelMessage] = field(default_factory=list)
    interactive: bool = False


def write_artifacts(
    *,
    ws_path: Path,
    run_stamp: str,
    skill_name: str,
    session: ArtifactSession,
    debug: bool,
    on_message: Callable[[str], None] = print,
) -> None:
    """Persist the analyst_log report and generated_code/*.py artifacts for a run.

    For a single-turn console session (interactive or not), this reproduces the original
    console report shape exactly: header uses session.initial_prompt (the raw CLI prompt,
    without inventory prefix), the conversation's "User Prompt" section uses
    session.prompts[0] (the prompt actually sent, inventory prefix included), and findings
    render as one "Final Findings" section or, for --interactive with multiple checkpoints,
    an "Analysis Checkpoints" section.

    A free-text multi-turn Textual session (not --interactive, more than one submitted
    prompt) gets its own header shape that lists every prompt and an "Analysis Turns"
    section -- it must not claim the latest follow-up was the sole original prompt.
    """
    report_path = ws_path / f"analyst_log-{run_stamp}.md"
    first_prompt = session.prompts[0] if session.prompts else (session.initial_prompt or "")
    conversation = format_conversation_history(session.message_history, first_prompt)

    if session.interactive and len(session.outputs) > 1:
        combined_findings = "\n\n---\n\n".join(
            f"### Checkpoint {i + 1}\n{output}" for i, output in enumerate(session.outputs)
        )
        findings_section = f"## Analysis Checkpoints\n\n{combined_findings}\n"
    elif not session.interactive and len(session.outputs) > 1:
        combined_findings = "\n\n---\n\n".join(
            f"### Turn {i + 1}\n{output}" for i, output in enumerate(session.outputs)
        )
        findings_section = f"## Analysis Turns\n\n{combined_findings}\n"
    else:
        findings_section = f"## Final Findings\n\n{session.outputs[-1] if session.outputs else ''}\n"

    if session.interactive or len(session.prompts) <= 1:
        mode_label = "Interactive (multiple checkpoints)" if session.interactive else "Standard"
        header = (
            f"# Analysis Report\n\n"
            f"- Skill: `{skill_name}`\n"
            f"- Prompt: {session.initial_prompt}\n"
            f"- Run: `{run_stamp}`\n"
            f"- Mode: {mode_label}\n\n"
        )
    else:
        prompts_list = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(session.prompts))
        header = (
            f"# Analysis Report\n\n"
            f"- Skill: `{skill_name}`\n"
            f"- Mode: Textual (multi-turn session)\n"
            f"- Run: `{run_stamp}`\n"
            f"- Prompts:\n{prompts_list}\n\n"
        )

    report_path.write_text(f"{header}{conversation}\n{findings_section}", encoding="utf-8")
    on_message(f"\nSaved analysis report: {report_path}")

    # Persist every generated run_code program. These are audit artifacts;
    # reusable analysis scripts should still be written by the agent with
    # FileSystem.write_file using meaningful names.
    generated_dir = ws_path / "generated_code"
    generated_dir.mkdir(exist_ok=True)
    code_index = 0
    for msg in session.message_history:
        for part in msg.parts:
            if isinstance(part, ToolCallPart) and part.tool_name == "run_code":
                code = part.args_as_dict().get("code") if part.args else None
                if code:
                    code_index += 1
                    code_path = generated_dir / f"{run_stamp}-{code_index:02d}.py"
                    code_path.write_text(code, encoding="utf-8")
                    if debug:
                        on_message(f"\n[call {part.tool_call_id}]\n{code}")
            elif isinstance(part, ToolReturnPart) and part.tool_name == "run_code" and debug:
                on_message(f"\n[return {part.tool_call_id}]\n{part.content}")
    if code_index:
        on_message(f"Saved {code_index} generated run_code artifact(s) in {generated_dir}")


def record_prompt(audit: AuditLog, prompt: str) -> None:
    audit.event("prompt", prompt=prompt)


async def run_turn_async(
    agent: Agent,
    prompt: str,
    message_history: list[ModelMessage] | None,
    *,
    stream_handler,
    run_metadata: dict,
    audit: AuditLog,
    model: str | None = None,
):
    """Shared async per-turn helper -- Textual's worker awaits this directly on the App's
    own asyncio loop. Always records the audit `prompt` event immediately before the call.
    `model`, if given, overrides the Agent's own default model for just this call --
    pydantic_ai supports switching models turn-to-turn without rebuilding the Agent, which is
    what ui_textual.py's command-palette model picker relies on."""
    record_prompt(audit, prompt)
    kwargs = {"event_stream_handler": stream_handler, "metadata": run_metadata}
    if message_history is not None:
        kwargs["message_history"] = message_history
    if model is not None:
        kwargs["model"] = model
    return await agent.run(prompt, **kwargs)


def run_turn_sync(
    agent: Agent,
    prompt: str,
    message_history: list[ModelMessage] | None,
    *,
    stream_handler,
    run_metadata: dict,
    audit: AuditLog,
):
    """Shared sync per-turn helper -- console mode's blocking control flow. Always records
    the audit `prompt` event immediately before the call."""
    record_prompt(audit, prompt)
    kwargs = {"event_stream_handler": stream_handler, "metadata": run_metadata}
    if message_history is not None:
        kwargs["message_history"] = message_history
    return agent.run_sync(prompt, **kwargs)


@dataclass
class RunSetup:
    """Result of prepare_run(): everything a driver needs to start issuing turns."""

    task_id: str
    mode: str
    skill_path: Path
    skill_name: str
    ws_path: Path
    base_path: Path
    data_dir: Path
    logs_dir: Path
    memory_dir: Path
    memory_enabled: bool
    memory_scope: str | None
    run_stamp: str
    run_id: str
    model: str
    audit: AuditLog
    agent: Agent
    run_metadata: dict
    initial_prompt: str | None
    prompt_prefix: str
    interactive: bool
    debug: bool


def prepare_run(
    skill_dir: str,
    initial_prompt: str | None,
    args: argparse.Namespace,
    sink: RunSink,
    *,
    ui_mode: str = "console",
) -> RunSetup:
    """Task/workspace resolution, skill/prompt assembly, and Agent construction -- shared by
    every driver. All status output goes through sink.status(...) rather than print(), so a
    Textual driver can buffer it until its App has mounted (see BufferedTextualSink).

    Raises TaskError for invalid --task/--pristine combinations or unsafe task roots; the
    caller is expected to map that to a CLI usage error.
    """
    models_config = load_models_config()
    resolved_model = resolve_model(args.model, models_config)
    if resolved_model != args.model:
        sink.status(f"Resolved model alias '{args.model}' to '{resolved_model}'")
        args.model = resolved_model

    task_id, mode = resolve_task_id(args)

    skill_path = Path(skill_dir)

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

    if mode == "pristine":
        task_id, ws_path = create_pristine_task_root(base_path)
    else:
        ws_path = create_task_root(base_path, task_id, exclusive=False)

    run_stamp = datetime.now().strftime("%y-%m-%d_%H-%M-%S")
    run_id = uuid.uuid4().hex

    audit = AuditLog(logs_dir, task_id, run_id)
    sink.status(
        f"Task: {task_id} (mode: {mode}) -- model: {args.model} -- workspace: {ws_path} -- "
        f"memory: {memory_scope if memory_enabled else 'disabled (pristine)'}"
    )

    # Registered only once audit exists, since there's nothing to flush before that point.
    install_sigterm_handler()

    if args.logfire:
        try:
            import logfire

            # Under --ui textual, Logfire's span-tree console printer would corrupt the
            # TUI's alternate screen -- suppress it there; console mode keeps today's default.
            logfire.configure(console=False if ui_mode == "textual" else None)
            logfire.instrument_pydantic_ai()
            sink.status("Logfire configured and instrumentation enabled.")
        except Exception as e:
            sink.status(f"Logfire not available: {e}")
            sink.status("Continuing without Logfire tracing.")

    config = load_skill(skill_path)
    skill_instructions = config.get("instructions", "You are a security analyst.")
    sandbox_notes = load_sandbox_notes()
    instructions = f"{sandbox_notes}\n\n{skill_instructions}" if sandbox_notes else skill_instructions

    # In interactive mode, add checkpoint instructions after major analysis phases. This is
    # purely a system-prompt toggle -- its *mechanism* (console's blocking input() loop)
    # stays console-only; under --ui textual it still shapes the model's behavior even
    # though the bottom bar always accepts free-text follow-ups regardless.
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

    lint_and_fix_scripts(ws_path, on_message=sink.status)

    # Input files can't be discovered with the FileSystem tool (its root is the task
    # workspace, not /data), so the runner surfaces the /data inventory directly in the
    # prompt of whichever turn is submitted first (the CLI-provided initial prompt, or the
    # first Textual bottom-bar message when there is none).
    prompt_prefix = ""
    data_files = sorted(p.name for p in data_dir.iterdir() if p.is_file())
    if data_files:
        inventory = "\n".join(f"- {SANDBOX_DATA_MOUNT}/{name}" for name in data_files)
        prompt_prefix += (
            f"Input files available read-only at {SANDBOX_DATA_MOUNT} (use these exact paths "
            f"in run_code):\n{inventory}\n\n"
        )
        sink.status(f"Found {len(data_files)} input file(s) in data: {', '.join(data_files)}")

    existing_scripts = sorted(p.name for p in ws_path.glob("*.py"))
    if existing_scripts:
        inventory = "\n".join(f"- {name}" for name in existing_scripts)
        prompt_prefix += (
            "Reusable scripts already saved in the workspace from earlier sessions (read one "
            "with the FileSystem tool and adapt it before writing new analysis code from "
            f"scratch, per the skill instructions):\n{inventory}\n\n"
        )
        sink.status(f"Found {len(existing_scripts)} existing script(s) in workspace: {', '.join(existing_scripts)}")

    run_metadata = {"task_id": task_id, "run_id": run_id}

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

    return RunSetup(
        task_id=task_id,
        mode=mode,
        skill_path=skill_path,
        skill_name=skill_path.name,
        ws_path=ws_path,
        base_path=base_path,
        data_dir=data_dir,
        logs_dir=logs_dir,
        memory_dir=memory_dir,
        memory_enabled=memory_enabled,
        memory_scope=memory_scope,
        run_stamp=run_stamp,
        run_id=run_id,
        model=args.model,
        audit=audit,
        agent=agent,
        run_metadata=run_metadata,
        initial_prompt=initial_prompt,
        prompt_prefix=prompt_prefix,
        interactive=args.interactive,
        debug=args.debug,
    )
