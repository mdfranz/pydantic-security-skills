"""Run preparation: secure workspace resolution, prompt assembly, and agent construction."""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic_ai import Agent
from pydantic_ai.capabilities import Thinking
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.profiles.anthropic import ANTHROPIC_THINKING_BUDGET_MAP
from pydantic_ai_harness import CodeMode, FileSystem
from pydantic_ai_harness.memory import FileStore, Memory
from pydantic_monty import MountDir, OSAccess

from .audit import AuditLog, install_sigterm_handler, restore_sigterm_handler
from .config import RunOptions, load_model_catalog
from .resilience import build_overflow_capability, build_provider_hooks
from .script_lint import lint_and_fix_scripts

SANDBOX_WORKSPACE_MOUNT = "/workspace"
SANDBOX_SKILL_MOUNT = "/skill"
SANDBOX_DATA_MOUNT = "/data"

# Task IDs are identifiers, not paths -- this rejects path separators, '.'/'..', absolute
# paths, and filename injection into audit paths. See refs/workspace-lifecycle.md.
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_TASK_NAMES = {"default", "data", "logs", "memory"}
DEFAULT_TASK_ID = "default"

# skill_runner/ is a package one level below the project root -- models.yaml and prompts/
# deliberately live at the root (config/content, not code), so both paths below need to
# climb out of the package directory, not just use __file__'s own parent.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SANDBOX_NOTES_PATH = PROJECT_ROOT / "prompts" / "sandbox_notes.md"


class TaskError(Exception):
    """Raised for invalid --task/--pristine combinations or unsafe task roots."""


class StatusSink(Protocol):
    def status(self, message: str) -> None: ...


class RunSink(StatusSink, Protocol):
    def emit(self, kind: str, **fields: object) -> None: ...


def resolve_task_id(options: RunOptions) -> tuple[str | None, str]:
    """Resolve the task id and mode ('accumulate' or 'pristine') from CLI args.

    Mirrors the table in refs/workspace-lifecycle.md: --task reuses a name (accumulate),
    --pristine defers id generation to create_pristine_task_root (returned task_id is None
    here -- the actual UUID4 is only minted once, at directory-creation time, so it can retry
    on collision), neither falls back to 'default', and both together is an error (already
    enforced by the argparse mutually-exclusive group, but checked again here in case this is
    ever called with a hand-built namespace).
    """
    if options.task and options.pristine:
        raise TaskError("--task and --pristine are mutually exclusive")
    if options.pristine:
        return None, "pristine"
    if options.task:
        if options.task in RESERVED_TASK_NAMES:
            raise TaskError(f"'{options.task}' is a reserved task name")
        if not TASK_ID_RE.match(options.task):
            raise TaskError(f"task names must match {TASK_ID_RE.pattern}")
        return options.task, "accumulate"
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


def load_sandbox_notes() -> str:
    """Runtime/workspace mechanics shared by every skill (Monty sandbox constraints, the
    script-reuse pattern, artifact conventions) -- prepended to each skill's own instructions
    so it only needs to be maintained in one place instead of duplicated per skill."""
    if SANDBOX_NOTES_PATH.exists():
        return SANDBOX_NOTES_PATH.read_text(encoding="utf-8")
    return ""


def load_skill(skill_dir: Path) -> dict:
    yaml_path = skill_dir / "skill.yaml"
    md_path = skill_dir / "SKILL.md"

    config: dict = {}
    if yaml_path.exists():
        loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise TaskError(f"skill metadata must be a mapping: {yaml_path}")
        config = loaded
    if md_path.exists():
        config["instructions"] = md_path.read_text(encoding="utf-8")

    return config


@dataclass(frozen=True)
class WorkspaceContext:
    """Internal workspace/security context assembled before agent construction."""

    task_id: str
    mode: str
    skill_path: Path
    ws_path: Path
    data_dir: Path
    logs_dir: Path
    memory_dir: Path
    memory_enabled: bool
    memory_scope: str | None


@dataclass(frozen=True)
class RunSetup:
    """Narrow handoff from setup into the application-level RunSession."""

    task_id: str
    skill_name: str
    ws_path: Path
    run_stamp: str
    model: str
    audit: AuditLog
    previous_sigterm_handler: Any
    agent: Agent
    run_metadata: dict[str, str]
    initial_prompt: str | None
    prompt_prefix: str
    options: RunOptions


def _prepare_workspace(skill_path: Path, options: RunOptions) -> WorkspaceContext:
    task_id, mode = resolve_task_id(options)
    base_path = options.workspace.resolve()
    base_path.mkdir(parents=True, exist_ok=True)

    data_dir = base_path / "data"
    data_dir.mkdir(exist_ok=True)

    logs_dir = base_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    logs_dir.chmod(0o700)

    memory_dir = base_path / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_dir.chmod(0o700)

    if mode == "pristine":
        task_id, ws_path = create_pristine_task_root(base_path)
    else:
        assert task_id is not None
        ws_path = create_task_root(base_path, task_id, exclusive=False)

    memory_enabled = mode != "pristine"
    memory_scope = f"{skill_path.name}/{task_id}" if memory_enabled else None
    return WorkspaceContext(
        task_id=task_id,
        mode=mode,
        skill_path=skill_path,
        ws_path=ws_path,
        data_dir=data_dir,
        logs_dir=logs_dir,
        memory_dir=memory_dir,
        memory_enabled=memory_enabled,
        memory_scope=memory_scope,
    )


def _configure_logfire(options: RunOptions, sink: StatusSink) -> None:
    if not options.logfire:
        return
    try:
        import logfire

        logfire.configure(console=False if options.ui == "textual" else None)
        logfire.instrument_pydantic_ai()
        sink.status("Logfire configured and instrumentation enabled.")
    except Exception as exc:
        sink.status(f"Logfire not available: {exc}")
        sink.status("Continuing without Logfire tracing.")


def _build_instructions(skill_path: Path, *, interactive: bool) -> str:
    config = load_skill(skill_path)
    skill_instructions = config.get("instructions", "You are a security analyst.")
    sandbox_notes = load_sandbox_notes()
    instructions = f"{sandbox_notes}\n\n{skill_instructions}" if sandbox_notes else skill_instructions
    if not interactive:
        return instructions
    return instructions + (
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


def _build_agent(
    context: WorkspaceContext,
    options: RunOptions,
    instructions: str,
    audit: AuditLog,
    sink: StatusSink,
) -> Agent:
    capabilities: list[Any] = [
        FileSystem(root_dir=str(context.ws_path)),
        CodeMode(
            tools=[],
            max_retries=options.max_retries,
            mount=[
                MountDir(SANDBOX_WORKSPACE_MOUNT, str(context.ws_path), mode="read-write"),
                MountDir(SANDBOX_SKILL_MOUNT, str(context.skill_path.resolve()), mode="read-only"),
                MountDir(SANDBOX_DATA_MOUNT, str(context.data_dir), mode="read-only"),
            ],
            os_access=OSAccess(environ={}),
        ),
        build_overflow_capability(context.logs_dir, context.task_id),
    ]
    if context.memory_enabled:
        capabilities.append(
            Memory(
                store=FileStore(str(context.memory_dir)),
                namespace=context.skill_path.name,
                agent_name=context.task_id,
            )
        )

    model_settings: dict[str, int] = {}
    if options.max_tokens is not None:
        model_settings["max_tokens"] = options.max_tokens
    if options.thinking:
        capabilities.append(Thinking(effort=options.thinking))
        effort_budget = ANTHROPIC_THINKING_BUDGET_MAP[options.thinking]
        min_max_tokens = effort_budget + 4096
        if model_settings.get("max_tokens", 0) < min_max_tokens:
            model_settings["max_tokens"] = min_max_tokens

    def on_rate_limit_retry(exc: ModelHTTPError, attempt: int, delay: float) -> None:
        audit.event(
            "model_request_retry",
            reason="rate_limit",
            status_code=exc.status_code,
            model=exc.model_name,
            retry_attempt=attempt,
            max_retries=1,
            delay_seconds=delay,
        )
        sink.status(
            f"Provider rate limit from {exc.model_name}; retrying in {delay:g}s "
            f"(attempt {attempt}/1)."
        )

    capabilities.append(build_provider_hooks(on_retry=on_rate_limit_retry))

    return Agent(
        options.model,
        name=context.skill_path.name,
        system_prompt=instructions,
        capabilities=capabilities,
        model_settings=model_settings or None,
    )


def _build_prompt_prefix(context: WorkspaceContext, sink: StatusSink) -> str:
    prompt_parts: list[str] = []
    data_files = sorted(path.name for path in context.data_dir.iterdir() if path.is_file())
    if data_files:
        inventory = "\n".join(f"- {SANDBOX_DATA_MOUNT}/{name}" for name in data_files)
        prompt_parts.append(
            f"Input files available read-only at {SANDBOX_DATA_MOUNT} (use these exact paths "
            f"in run_code):\n{inventory}\n\n"
        )
        sink.status(f"Found {len(data_files)} input file(s) in data: {', '.join(data_files)}")

    existing_scripts = sorted(path.name for path in context.ws_path.glob("*.py"))
    if existing_scripts:
        inventory = "\n".join(f"- {name}" for name in existing_scripts)
        prompt_parts.append(
            "Reusable scripts already saved in the workspace from earlier sessions (read one "
            "with the FileSystem tool and adapt it before writing new analysis code from "
            f"scratch, per the skill instructions):\n{inventory}\n\n"
        )
        sink.status(
            f"Found {len(existing_scripts)} existing script(s) in workspace: "
            f"{', '.join(existing_scripts)}"
        )
    return "".join(prompt_parts)


def prepare_run(
    skill_dir: str,
    initial_prompt: str | None,
    options: RunOptions,
    sink: StatusSink,
) -> RunSetup:
    """Assemble a run and guarantee cleanup if any post-audit setup step fails."""
    catalog = load_model_catalog(PROJECT_ROOT)
    resolved_model = catalog.resolve(options.model)
    if resolved_model != options.model:
        sink.status(f"Resolved model alias '{options.model}' to '{resolved_model}'")
        options = options.with_model(resolved_model)

    skill_path = Path(skill_dir)
    context = _prepare_workspace(skill_path, options)
    run_stamp = datetime.now().strftime("%y-%m-%d_%H-%M-%S")
    run_id = uuid.uuid4().hex
    audit = AuditLog(context.logs_dir, context.task_id, run_id)
    previous_sigterm_handler: Any = None
    sigterm_handler_installed = False

    try:
        previous_sigterm_handler = install_sigterm_handler()
        sigterm_handler_installed = True
        sink.status(
            f"Task: {context.task_id} (mode: {context.mode}) -- model: {options.model} -- "
            f"workspace: {context.ws_path} -- memory: "
            f"{context.memory_scope if context.memory_enabled else 'disabled (pristine)'}"
        )
        _configure_logfire(options, sink)
        instructions = _build_instructions(skill_path, interactive=options.interactive)
        agent = _build_agent(context, options, instructions, audit, sink)
        lint_and_fix_scripts(context.ws_path, on_message=sink.status)
        prompt_prefix = _build_prompt_prefix(context, sink)
        run_metadata = {"task_id": context.task_id, "run_id": run_id}
        audit.event(
            "run_start",
            task_id=context.task_id,
            run_id=run_id,
            mode=context.mode,
            skill=skill_path.name,
            model=options.model,
            workspace=str(context.ws_path),
            interactive=options.interactive,
            thinking=options.thinking,
            max_tokens=options.max_tokens,
            memory_enabled=context.memory_enabled,
            memory_scope=context.memory_scope,
        )
    except BaseException as exc:
        try:
            audit.event("setup_error", error=str(exc), error_type=type(exc).__name__)
            audit.event("run_end", status="failed")
        finally:
            audit.close()
            if sigterm_handler_installed:
                restore_sigterm_handler(previous_sigterm_handler)
        raise

    return RunSetup(
        task_id=context.task_id,
        skill_name=skill_path.name,
        ws_path=context.ws_path,
        run_stamp=run_stamp,
        model=options.model,
        audit=audit,
        previous_sigterm_handler=previous_sigterm_handler,
        agent=agent,
        run_metadata=run_metadata,
        initial_prompt=initial_prompt,
        prompt_prefix=prompt_prefix,
        options=options,
    )
