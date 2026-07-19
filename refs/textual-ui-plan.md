# Add a Textual TUI mode alongside the existing console UX

## Context

`runner.py` currently has exactly one UX: raw `print()` to a flat terminal, plus a blocking
`input()`-based checkpoint loop under `--interactive`. This was fine for scripted/batch use
(`compare_models.sh`) but is a poor fit for actually watching an analysis unfold — the
console mode was just fixed to at least stream events live (`_echo`), but it's still one
scrolling wall of text with no structure.

The user wants two UXs to coexist:
1. **Console mode** — today's behavior, kept exactly as-is ("dumb and primitive" by design —
   this is what scripts and quick one-shot runs use).
2. **A new Textual TUI mode** — multiple panels (tool calls, model output, artifacts) plus a
   bottom bar for prompting/follow-ups, for interactive investigation sessions.

Both modes must consume the *same* underlying event stream and produce the *same* on-disk
artifacts (`analyst_log-*.md`, `generated_code/*.py`, the audit JSONL) — the UI is a new
observer, not a new data producer.

**Confirmed with the user:**
- Textual mode's bottom bar always accepts free-text follow-up prompts (not the old
  continue/stop/focus keyword parsing). `--interactive` becomes purely a system-prompt toggle
  (still adds the "pause and checkpoint" instructions) — its *mechanism* (blocking `input()`
  loop) stays console-only.
- The `prompt` CLI argument becomes optional. If omitted, `--ui textual` opens an empty
  session where the first message is typed into the bottom bar; console mode still requires
  it (errors without one, matching today).
- `textual` is added as an optional extra (`[project.optional-dependencies] tui = [...]`),
  not a hard dependency — console-only installs are unaffected.

## Verified technical facts (don't re-derive)

- `Agent.run(...)` (pydantic-ai) is natively async; `Agent.run_sync(...)` wraps it. Textual
  apps own their own asyncio loop, so the Textual driver should `await agent.run(...)`
  directly inside a Textual `@work` coroutine — never call the blocking `run_sync` from
  inside Textual, never spawn a separate thread (unnecessary).
- `logfire.configure(..., console: ConsoleOptions | Literal[False] | None = None)` — confirmed
  in this venv. Under `--ui textual`, pass `console=False` so Logfire's span-tree printer
  doesn't corrupt the TUI's alternate screen. Console mode keeps today's default (`None`).
- `textual` is not currently installed or a dependency anywhere in this repo.
- `pydantic_ai_harness.filesystem._toolset.FileSystemToolset` registers its write tool as
  `write_file` (`.venv/.../pydantic_ai_harness/filesystem/_toolset.py:109`) — this already
  flows through the existing tool-call event path, so the artifacts panel can detect live
  writes with zero new instrumentation.
- `compare_models.sh` scrapes runner.py's stdout for the literal
  `f"Task: {task_id} (mode: {mode}) -- workspace: ..."` line (today at runner.py:412-415) to
  discover each pristine run's task id. This line must keep printing to real stdout,
  byte-for-byte, in console mode.
- `run.sh` always appends `--interactive` and forwards extra args verbatim — unaffected by
  this change as long as `--ui` defaults to `console`.

## Approach

### 1. File structure

Perform the module split as the first implementation phase, after the contracts below are agreed
but before either UI flow is moved. The Textual UI must import a narrow, UI-neutral core rather
than reach into a large `runner.py`.

- **`runner.py`** (thin entrypoint): argparse/positional resolution, optional-extra guard, UI
  selection, lazy `ui_textual` import, and top-level `RunInterrupted` → exit `143` mapping. It
  must contain no agent execution, workspace mutation, artifact formatting, or UI rendering.
- **`run_core.py`**: task/workspace resolution and setup, skill/prompt assembly, capability and
  `Agent` construction, run metadata, the `RunSink` protocol, buffered preflight sink, shared
  sync/async per-turn helpers, and artifact session/checkpoint persistence. This is the sole
  module both UI drivers depend on for run behavior.
- **`audit.py`**: `AuditLog`, `RunInterrupted`, SIGTERM helper, and the UI-neutral
  `make_event_stream_handler(audit, sink)`. Keeping event persistence beside its adapter makes
  the audit contract easy to inspect without coupling it to either UI.
- **`script_lint.py`**: `lint_and_fix_scripts` and its regex constants. It accepts the
  `on_message` callback so neither UI leaks raw output.
- **`console_ui.py`**: `ConsoleSink`, console event formatting, checkpoint parsing/prompt
  helpers, and `run_console(...)`. Its behavior remains the existing `agent.run_sync` plus
  blocking `input()` loop, including byte-for-byte console output.
- **`ui_textual.py`** (new): `TextualSink`, `AnalystApp`, and `run_textual(...)`. Imported
  lazily only in the `--ui textual` branch, so a console-only install never imports `textual`.

Do not create further micro-modules for individual loaders, report formatters, or task helpers
unless `run_core.py` remains difficult to navigate after this split. Those helpers belong together
while they are only used to prepare and persist one analyst run.

### 2. CLI grammar and preflight lifecycle

Do **not** make both positional arguments independently optional: argparse would interpret a
single argument as `skill_dir`, so the existing convenient default-skill invocation would become
ambiguous. Keep the existing positional grammar for all non-empty runs:

```text
runner.py [skill_dir] prompt [flags]
```

For an empty Textual session, use `runner.py --ui textual` with no positionals. This is the only
new zero-positional form. In parsing, accept zero, one, or two positional values as a single
`positionals` list, then resolve them explicitly:

- zero values: valid only with `--ui textual`; use the default skill and `initial_prompt=None`;
- one value: the prompt for the default skill (preserves today's behavior);
- two values: `skill_dir`, then prompt (preserves today's behavior);
- more than two values: parser error.

This avoids adding a second prompt spelling while preserving every existing invocation. The
resolved `skill_dir` and `initial_prompt` are then passed to the shared setup code rather than
reading `args.skill_dir`/`args.prompt` directly.

The Textual app does not exist until after shared setup has created the task workspace, so status
messages produced during setup cannot be written directly to widgets. Create a small
`BufferedTextualSink` that implements `RunSink` and accumulates `status()` messages. Build the
agent and do pre-run linting/inventory through that sink; when `AnalystApp` mounts, drain those
messages into its visible status/output log and replace it with the normal `TextualSink`. This
keeps all output inside the alternate screen without moving workspace setup into UI code.

### 3. Shared sink interface

```python
# run_core.py
class RunSink(Protocol):
    def status(self, message: str) -> None: ...              # one-off lines: banner, "Saved report", etc.
    def emit(self, kind: str, **fields: object) -> None: ...  # live events, same vocabulary as AuditLog.event
```

`make_event_stream_handler(audit, sink)` in `audit.py` calls **both** `audit.event(kind, **fields)` and
`sink.emit(kind, **fields)` with the identical `(kind, fields)` for every event — the
label-formatting logic currently inline in the handler (runner.py:199-226) moves into each
sink's own `emit`. This guarantees the sink always sees exactly what the audit log persists,
and keeps the handler itself UI-agnostic.

`ConsoleSink.status` = `print(message, flush=True)`; `ConsoleSink.emit` reproduces today's
exact `_echo(label, content)` output per kind, verbatim. All other raw `print()`s currently in
`main()` (the `Task:` banner, logfire status lines, data/script inventory lines, the
`"Running Pydantic AI agent..."` line, `lint_and_fix_scripts`'s messages) route through
`sink.status(...)` instead — required, not cosmetic, since any leftover raw `print()` would
corrupt Textual's alternate-screen rendering exactly like `--logfire`'s console printer would.
`lint_and_fix_scripts` in `script_lint.py` gains an `on_message: Callable[[str], None] = print`
param for this.

Create one shared per-turn helper rather than letting either driver call the agent directly:

```python
async def run_turn_async(..., prompt: str, message_history): ...
def run_turn_sync(..., prompt: str, message_history): ...
```

Each helper must emit `audit.event("prompt", prompt=prompt)` immediately before its respective
`agent.run(...)`/`agent.run_sync(...)` call, pass the shared stream handler and metadata, and
return the result. This preserves the console audit sequence and ensures initial and free-text
Textual follow-ups are persisted identically. The helpers may share a small `record_prompt(...)`
function, but must retain async/sync execution appropriate to each driver.

**Checkpoint parsing extracted** (console-only mechanism, but the vocabulary functions are
pure and testable independent of `input()`):
```python
class CheckpointAction(NamedTuple):
    kind: Literal["continue", "stop", "focus"]
    focus: str | None = None

def parse_checkpoint_input(user_input: str) -> CheckpointAction: ...   # today's logic, lines 575-617
def build_continuation_prompt(action: CheckpointAction) -> str | None: ...  # exact text, lines 580-583/600-603
```

**Artifact writing extracted with an explicit session model** so console output remains byte-for-byte
compatible while a multi-turn Textual session has defined persistence semantics:
```python
@dataclass
class ArtifactSession:
    initial_prompt: str | None
    prompts: list[str]
    outputs: list[str]
    message_history: list[ModelMessage]
    interactive: bool

def write_artifacts(*, ws_path: Path, run_stamp: str, skill_name: str,
                     session: ArtifactSession, debug: bool,
                     on_message: Callable[[str], None] = print) -> None: ...
```

For console mode, construct the session so this helper preserves the existing report header,
conversation formatting, findings gate, generated-code naming, and debug dumps exactly. Keep
`args.interactive and len(outputs) > 1`; it is clearer as an expression of the console report
contract even if current control flow makes the first term redundant.

For Textual mode, persist a session checkpoint after every successfully completed turn, overwriting
the same `analyst_log-{run_stamp}.md` with the complete transcript to date. The Textual report
must identify itself as a multi-turn Textual session and list every submitted user prompt; it must
not claim that the latest follow-up was the sole original prompt. Generated-code artifacts are
derived from the complete `message_history` each time and use stable ordering/names, so rewriting
the same paths is intentional and cannot create duplicates. A failed/interrupted turn does not
replace the last successful checkpoint. This is equivalent artifact coverage, while recognizing
that a free-form TUI conversation cannot have the exact single-prompt console report content.

`run_console(...)` keeps today's control flow: still `agent.run_sync(...)`, still blocking
`input()`. Do not switch console mode to `await agent.run(...)` — zero reason to touch a
working sync path.

### 4. New CLI surface

- `--ui {console,textual}`, default `console` (same idiom as existing `--thinking` choice flag).
- Resolve the positional list as described above. A zero-positional invocation is rejected in
  console mode and accepted only for an empty Textual session.
- Early guard, before any workspace/task directory is created (since `--pristine` has side
  effects): `if args.ui == "textual" and importlib.util.find_spec("textual") is None:
  parser.error("--ui textual requires the 'textual' extra: uv sync --extra tui")`.
- `--logfire` + `--ui textual`: `logfire.configure(console=False, ...)`; console mode keeps
  `console=None` (today's default).
- `--debug`: console mode unchanged (400-char truncation in `ConsoleSink.emit`). Textual
  panels are scrollable, so `TextualSink.emit` never truncates regardless of `--debug`;
  `--debug` keeps its other meaning (gates the `generated_code` call/return dumps in
  `write_artifacts`).
- `--interactive` under `--ui textual`: only affects the system-prompt addendum
  (runner.py:441-452, must stay in shared setup code, not duplicated per-driver). The bottom
  bar always accepts free-text follow-ups regardless of this flag, per the user's decision.

### 5. Textual app shape (`ui_textual.py`)

```python
class TextualSink:
    """Only ever driven from inside the agent.run() worker Task -- shares the App's asyncio
    loop, so it mutates widgets directly with no call_from_thread needed."""
    def __init__(self, app: "AnalystApp"): ...
    def status(self, message: str) -> None: ...
    def emit(self, kind: str, **fields: object) -> None: ...  # routes into tool-calls or output panel by kind

class AnalystApp(App):
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-col"):
                yield RichLog(id="tool-calls", markup=True)   # call/result pairs, paired by tool_call_id
                yield DirectoryTree(str(self.ws_path), id="artifacts")
            yield RichLog(id="output", markup=True)           # thinking + text
        yield Input(placeholder="Ask the analyst…", id="prompt-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.drain_preflight_status()
        if self.initial_prompt:
            self.run_turn(self.initial_prompt)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt or self.turn_active:
            return
        event.input.value = ""
        self.run_turn(prompt)   # always free-text; message_history carries prior turns

    @work(exclusive=True, group="agent-run")
    async def run_turn(self, prompt: str) -> None:
        self.set_turn_active(True)  # disable prompt bar; do not cancel/queue active work
        try:
            result = await run_turn_async(..., prompt=prompt,
                                          message_history=self.message_history)
            self.message_history = result.all_messages()
            self.session.prompts.append(prompt)
            self.session.outputs.append(result.output)
            self.session.message_history = self.message_history
            lint_and_fix_scripts(self.ws_path, on_message=self.sink.status)
            write_artifacts(..., session=self.session, on_message=self.sink.status)
            self.query_one("#artifacts", DirectoryTree).reload()
        finally:
            self.set_turn_active(False)
```

- **Driving the agent without blocking the UI**: Textual's `@work` decorator turns the async
  method into a managed asyncio `Task` on the App's own loop; `await agent.run(...)` yields
  naturally, keeping the UI responsive while the model streams. This is the correct fit given
  `agent.run` is natively async.
- **Tool-call/result pairing**: `TextualSink` keeps `dict[tool_call_id, entry]`; a
  `tool_call`/`run_code_call` event inserts a pending entry, the matching
  `tool_result`/`run_code_return` updates it in place rather than appending a new line.
- **Artifacts panel**: `DirectoryTree(str(ws_path))`, reloaded (a) whenever a `write_file` tool
  event is observed (already flowing through the existing sink — no new instrumentation) and
  (b) after every `run_turn` completes (catches files a `run_code` script wrote directly via
  the `/workspace` mount without going through `write_file`).
- Exact widget/method names (`RichLog`, `DirectoryTree.reload()`) should be checked against
  whichever Textual version `uv add` resolves at implementation time.
- **One turn at a time**: trim and reject empty input, clear accepted input, and disable the
  prompt bar until the worker completes. Do not rely on `exclusive=True` as the UX policy:
  whether it cancels or suppresses a second worker must never determine whether an in-flight
  analysis survives.

### 6. Dependency changes

```toml
# pyproject.toml
[project.optional-dependencies]
tui = ["textual>=0.60"]
```
Installed via `uv sync --extra tui`. Use `uv add textual --optional tui` so `pyproject.toml`
and `uv.lock` update together — never hand-edit `uv.lock`.

### 7. Backward-compatibility / risk points

**Must not change**: every existing flag/default in console mode, the exact `Task: ...`
startup string, `run.sh`'s hardcoded `--interactive`, `AuditLog` file naming/location/`0o600`
permissions, `analyst_log-*.md`/`generated_code/*.py` shape in console mode, SIGTERM →
`RunInterrupted` → exit 143.

**Divergence risks to watch during implementation**:
- Extraction bugs in `write_artifacts`/checkpoint parsing silently reformatting console's
  report — diff report *structure* pre/post-refactor (not model content, which isn't
  deterministic).
- The `--interactive` system-prompt addendum must live in shared setup code, not duplicated
  per-driver.
- `lint_and_fix_scripts` runs twice today (before the run, and after) — both drivers must
  preserve both call sites.
- Textual status output has a pre-mount phase — buffer it until `AnalystApp.on_mount()` rather
  than leaking it to stdout or attempting widget mutation before the app exists.
- Textual follow-ups must produce an audit `prompt` event immediately before every agent call;
  this is part of the shared turn contract, not a UI responsibility.
- In Textual mode, the post-run `lint_and_fix_scripts` call occurs after each successful turn
  and before checkpointing artifacts, which preserves the current per-run cleanup guarantee for
  scripts that a later free-text turn may reuse.
- SIGTERM delivery while Textual owns the event loop/terminal is unverified — Textual may
  install its own signal handling (e.g. SIGTSTP/SIGCONT); confirm it doesn't clobber the
  `signal.signal(signal.SIGTERM, _raise_on_sigterm)` registration, and that the terminal is
  restored cleanly on interrupt. Treat as a required manual test, not an assumption.

### 8. Delivery order

0. **Set the contracts first**: document the unambiguous positional resolver,
   `ArtifactSession` checkpoint semantics, buffered Textual preflight sink, and sync/async
   per-turn helpers. This fixes shared-core boundaries before code moves.
1. **Modularize with console only**: move the stable responsibilities into `audit.py`,
   `script_lint.py`, `run_core.py`, and `console_ui.py`; reduce `runner.py` to CLI/dispatch;
   route all output through `ConsoleSink.status`/`emit`. Preserve the sync console execution
   and blocking checkpoint loop. Verify console output and artifacts are byte-for-byte unchanged.
2. **Add Textual mode**: `pyproject.toml` extra + `uv sync --extra tui`, new `ui_textual.py`,
   `--ui {console,textual}` flag + `find_spec` guard + branch in `main()`, forced
   `console=False` for `--logfire` under textual.
3. **Docs**: update `ARCHITECTURE.md` (mermaid + Runner section — mention the `RunSink` split
   and two UI backends), `README.md` (Flags/Running section — document `--ui`, optional
   prompt, the `tui` extra), `PROJECT.md` (new dated phase entry) — all three exist in this
   repo per its `CLAUDE.md` doc-sync convention.

### Verification (against `workspace/data/eve-2026-01-06-01.json`)

1. Console regression: run the same prompt pre/post-refactor, diff stdout format and
   `analyst_log`/`generated_code`/audit-jsonl shape.
2. Console `--interactive` walkthrough (continue/focus/stop) — confirm unchanged UX.
3. `compare_models.sh` regression — confirm its `Task:` sed-scrape and summary table still work.
4. Textual smoke test: `--ui textual --pristine` with no prompt (empty session, type first
   message in the bar) and with a prompt (auto-runs first turn) — confirm clean alt-screen
   render, streamed tool-call/output panels, artifacts panel populating live, a free-text
   follow-up in the bottom bar producing a new turn with carried-over history.
5. `--ui textual --logfire` together — confirm no span-tree text leaks into the TUI.
6. SIGTERM mid-run under `--ui textual` — confirm clean exit, restored terminal,
   `interrupted`/`run_end` audit events present.
7. Audit-log parity — same prompt run under both modes, confirm the expected ordered sequence
   (`run_start`, each `prompt`, streamed events, `run_end`), equivalent tool-call/result IDs,
   and one persisted prompt event per submitted Textual turn. Event-kind comparison alone is
   insufficient.
8. Textual session persistence — submit multiple prompts, confirm the one report checkpoint
   contains all prompts/conversation to date, generated-code paths remain stable rather than
   duplicating, and an interrupted/failed turn leaves the prior successful checkpoint intact.

### Critical files

- `/home/mdfranz/github/pydantic-security-skills/runner.py`
- `/home/mdfranz/github/pydantic-security-skills/run_core.py` (new)
- `/home/mdfranz/github/pydantic-security-skills/audit.py` (new)
- `/home/mdfranz/github/pydantic-security-skills/script_lint.py` (new)
- `/home/mdfranz/github/pydantic-security-skills/console_ui.py` (new)
- `/home/mdfranz/github/pydantic-security-skills/ui_textual.py` (new)
- `/home/mdfranz/github/pydantic-security-skills/pyproject.toml`
- `/home/mdfranz/github/pydantic-security-skills/run.sh` (verify unaffected, no edits expected)
- `/home/mdfranz/github/pydantic-security-skills/compare_models.sh` (verify unaffected, no edits expected)
- `/home/mdfranz/github/pydantic-security-skills/ARCHITECTURE.md`, `README.md`, `PROJECT.md` (docs)
