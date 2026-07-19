# skill_runner/ Implementation Guide

Implementation-level reference for the `skill_runner` package. Concrete details for a developer
modifying this code — types, functions, control flow, gotchas — not architecture/trust boundaries
(see `../ARCHITECTURE.md`), the pydantic-ai/harness/Monty API rationale (see
`../PYDANTIC-STACK.md`), or the CLI usage/flag reference (see `../README.md`).

## Cross-cutting: one three-stage pipeline, two front ends

Every invocation goes through the same three stages regardless of `--ui`:

1. **`config.py`** turns `argparse.Namespace` into an immutable `RunOptions`.
2. **`run_core.py`**'s `prepare_run()` turns `RunOptions` into a `RunSetup` — workspace resolved,
   `Agent` built, audit log opened, SIGTERM handler installed.
3. **`session.py`**'s `RunSession` wraps a `RunSetup` and owns everything from that point on: turn
   submission, transcript, state machine, stuck-run safeguards, artifact persistence, audit
   finalization.

`console_ui.py` and `ui_textual.py` are the only two callers of `prepare_run()`/`RunSession`, and
neither mutates the workspace or manages lifecycle directly — they collect input and render output
against the same `RunSink` protocol (`status(message)`, `emit(kind, **fields)`), so a fix or new
event kind added to the shared pipeline reaches both front ends automatically.

## `runner.py` — thin CLI entrypoint

Package-level `main()`, installed as the `skill-runner` console script
(`pyproject.toml`: `skill-runner = "skill_runner.runner:main"`).

### `build_parser() -> argparse.ArgumentParser`
Single positional `positionals: list[str]` (see `resolve_positionals` below), plus every flag
documented in `../README.md`. Three worth calling out here because their defaults matter for
behavior, not just help text:
- `--max-retries` (`type=int`, default `5`) — passed straight through to `run_core.py`'s
  `CodeMode(max_retries=...)`. `CodeMode`'s own hardcoded default is `3`; this raises the floor.
- `--max-run-seconds` (`type=int`, default `None`) — `None` means no watchdog is ever started
  (see `session.py`/`audit.py` below); any int enables a per-turn wall-clock budget.
- `--max-turns` (`type=int`, default `None`) — `None` means `session.py` never constructs a
  `UsageLimits`, so `pydantic_ai`'s own default (`request_limit=50`) applies silently.

### `resolve_positionals(positionals, ui, parser) -> tuple[str, str | None]`
Resolves the shared `[skill_dir] prompt` grammar explicitly rather than via argparse's own
optional-positional handling, because a single optional positional is ambiguous (argparse can't
tell a lone `skill_dir` from a lone `prompt`). Rules: 0 positionals is only valid under
`--ui textual` (returns `(DEFAULT_SKILL_DIR, None)` — an empty session, first message typed into
the bottom bar); 1 positional is treated as the prompt with the default skill dir; 2 is
`(skill_dir, prompt)`; 3+ is a `parser.error()`.

### `main()`
1. Parses args, builds `RunOptions`, resolves positionals.
2. Guards `--ui textual` against a missing `textual` install via `importlib.util.find_spec` —
   deliberately *before* any workspace/task directory gets created, since `--pristine` has real
   side effects (an actual directory gets created) that shouldn't happen just to then fail on a
   missing optional dependency.
3. Dispatches to `run_textual` (imported lazily, inside this branch only, so a console-only
   install never imports `textual`) or `run_console`.
4. Catches `TaskError` → `parser.error(str(e))` (invalid `--task`/`--pristine` combinations).
5. Catches `RunInterrupted` → `map_run_interrupted_exit_code()` (exit `143`).

**Gotcha (fixed 2026-07-19, `ISSUES.md` #13):** step 5 must live inside `main()` itself, not only
under `if __name__ == "__main__":` at module scope. The installed `skill-runner` console-script
shim (`.venv/bin/skill-runner`) does `from skill_runner.runner import main; sys.exit(main())`
directly — it never touches the `__main__` guard. A version of the `RunInterrupted` catch living
only under that guard silently never runs for the primary entry point, which is how real
Ctrl+C/SIGTERM crashed with a raw traceback instead of a clean exit for as long as the
console-script entry point existed. If you ever add a new top-level exception mapping here, put it
in `main()`, not the bottom guard.

## `config.py` — `RunOptions` / `ModelCatalog`

### `RunOptions` (frozen dataclass)
The single struct threaded through every layer below `runner.py`. All fields are plain values (no
`argparse.Namespace` leaks past `from_namespace`). `with_model(model) -> RunOptions` is the only
mutator, implemented via `dataclasses.replace` — used by `run_core.prepare_run()` after resolving
a model alias, and by nothing else. Adding a new CLI flag means: add the argparse arg in
`runner.py`, add the field here (with a default, so existing `RunOptions(...)` call sites and
tests keep working), and read it from `args` in `from_namespace`.

### `ModelCatalog` / `ModelChoice`
`ModelCatalog.from_mapping(config)` normalizes two `models.yaml` shapes into one flat
`tuple[ModelChoice, ...]`: the current hierarchical `providers: {name: {models: [...]}}` form, and
a legacy flat `models: [...]` form (both are accepted in the same file simultaneously — results
are concatenated, not exclusive). `resolve(model_name) -> str` matches against `alias` or `id`
and **falls back to returning the input unchanged** if nothing matches — deliberate, so a raw
`provider:model` string not yet in the catalog still works, but it also means a typo gets zero
local feedback and surfaces as an opaque provider-side 404/400 later (`ISSUES.md` #11, still open).

## `run_core.py` — workspace, agent, and prompt assembly

### Task/workspace resolution
- `resolve_task_id(options) -> (task_id | None, mode)`: `--pristine` returns `(None, "pristine")`
  — the actual `task-<uuid4>` is minted later, only at directory-creation time, so
  `create_pristine_task_root` can retry on the (vanishingly unlikely) collision without wasting an
  id. `--task <name>` validates against `TASK_ID_RE` (`^[a-z0-9][a-z0-9_-]{0,63}$`) and
  `RESERVED_TASK_NAMES` (`default`, `data`, `logs`, `memory`), returns `(name, "accumulate")`.
  Neither flag → `(DEFAULT_TASK_ID, "accumulate")`.
- `create_task_root(base, task_id, *, exclusive)`: resolves the candidate path and asserts
  `resolved.parent == base` — the isolation guarantee from `refs/workspace-lifecycle.md`, enforced
  by re-checking after `.resolve()` (which follows symlinks), not just string-matching the input.
- `create_pristine_task_root(base, *, max_attempts=5)`: loops `create_task_root(..., exclusive=True)`
  under a fresh `task-{uuid.uuid4()}`, catching `FileExistsError` up to `max_attempts` times.

### `_prepare_workspace(skill_path, options) -> WorkspaceContext`
Creates (or reuses) `data/`, `logs/` (`chmod 0o700`), `memory/` (`chmod 0o700`), and the resolved
task root, in that order. `memory_enabled = mode != "pristine"` — pristine tasks never get a
`Memory` capability at all (not merely an empty one), so their tool surface and prompt token count
stay identical run-to-run regardless of prior history — this is what makes `--pristine` comparisons
apples-to-apples (see `../results/*.md`).

### `_build_agent(context, options, instructions, audit, sink) -> Agent`
Builds the `capabilities` list, in this order — order matters for `Agent`'s own resolution, not
just readability:
1. `FileSystem(root_dir=str(context.ws_path))` — native tools (`read_file`/`write_file`/
   `list_directory`/etc.), scoped to the task root only. Cannot reach `/data` or `/skill` — see
   `ISSUES.md` #10 for the resulting recurring "path resolves outside root" confusion.
2. `CodeMode(tools=[], max_retries=options.max_retries, mount=[...], os_access=OSAccess(environ={}))`
   — `tools=[]` keeps `FileSystem`'s tools native rather than wrapped behind `run_code` (a
   deliberate Phase 2 fix — see `../PROJECT.md`). Three `MountDir`s: `/workspace` (rw, task root),
   `/skill` (ro, skill directory), `/data` (ro, canonical input). `max_retries` is the only
   CLI-tunable knob into `CodeMode` today.
3. `build_overflow_capability(context.logs_dir, context.task_id)` — see `resilience.py` below.
4. `Memory(...)`, conditionally, only if `context.memory_enabled`.
5. `Thinking(effort=options.thinking)`, conditionally. When present, floors
   `model_settings["max_tokens"]` to `ANTHROPIC_THINKING_BUDGET_MAP[effort] + 4096`
   **unconditionally** (`max(explicit_value, floor)`), not only when `max_tokens` was otherwise
   unset — an explicit-but-insufficient `--max-tokens` alongside `--thinking` used to reach the
   Anthropic API unguarded and 400 (Phase 5/6 history in `../PROJECT.md`).
6. `build_provider_hooks(on_retry=on_rate_limit_retry)` — see `resilience.py` below. Always
   appended last, unconditionally.

`on_rate_limit_retry(exc, attempt, delay)` (a closure capturing `audit`/`sink`) is the callback
passed to `build_provider_hooks`: writes a `model_request_retry` audit event and a status line.
Defined inline in `_build_agent` rather than as a module function specifically so it can close
over `audit`/`sink` without threading them through `resilience.py`'s otherwise-pure builder
functions.

### `_build_prompt_prefix(context, sink) -> str`
Only called for the *first* turn of a run (see `session.py`'s `_effective_prompt`). Lists `/data`
files (the model can't discover these itself — `FileSystem` doesn't reach `/data`) and any
existing `*.py` scripts already in the task workspace root (encouraging reuse over rewriting).
Both sections are conditionally omitted if empty; a pristine task always omits the second.

### `prepare_run(skill_dir, initial_prompt, options, sink) -> RunSetup`
The single function both UI drivers call. Order: resolve model alias → `_prepare_workspace` →
create `run_stamp`/`run_id`/`AuditLog` → `install_sigterm_handler()` → build instructions/agent →
`lint_and_fix_scripts` (pre-run pass, before the "existing scripts" inventory is shown to the
model) → `_build_prompt_prefix` → write `run_start` audit event. **Any `BaseException` raised
after the audit log opens** is caught, writes `setup_error`/failed `run_end`, closes the audit,
restores the previous SIGTERM handler if one was installed, then re-raises — so a setup-time crash
still leaves a complete, closed audit record instead of an unflushed file.

## `resilience.py` — provider retry and tool-output overflow protection

Two independent, composable `pydantic_ai` capabilities/hooks, both built once per run in
`_build_agent` above.

### Rate-limit retry
- `_rate_limit_delay_seconds(exc: ModelHTTPError) -> float`: reads
  `exc.body["metadata"]["retry_after_seconds"]` if present (OpenRouter's convention), else `1.0`,
  clamped to `[0.1, 30.0]` and coerced to a finite float (guards against a malformed/absurd
  provider-supplied value).
- `retry_rate_limited_request(request_context, handler, *, on_retry=None)`: calls `handler` up to
  2 times total. Only retries `ModelHTTPError` with `status_code == 429`; any other exception, or a
  second 429, re-raises immediately. Sleeps via `anyio.sleep(delay)` between attempts.
- `build_provider_hooks(*, on_retry=None) -> Hooks`: wraps the above as a `Hooks().on.model_request`
  handler named `retry_provider_429`.

**Motivation** (`ISSUES.md` #12): observed a kimi-k3 run get a `429` with
`retry_after_seconds: 1` — a signal a short retry would very likely succeed — but fail immediately
with no retry at all. This closes that gap with exactly one bounded retry, not unbounded backoff.

### Tool-output overflow
- `build_overflow_capability(logs_dir, task_id) -> OverflowingToolOutput`: one `Band` — any tool
  return over `TOOL_OUTPUT_OVERFLOW_CHARS` (`10_000`) chars gets `Spill`'d: a
  `TOOL_OUTPUT_PREVIEW_CHARS` (`1_000`)-char preview stays in model history, the full value is
  written to `LocalFileStore(base_dir=logs_dir / "overflow" / task_id)`, and the model gets an
  opaque read handle back. If the spill itself fails, `Truncate(max_chars=TOOL_OUTPUT_FALLBACK_CHARS)`
  (`4_000`) is the last-resort fallback so the original oversized value never reaches context
  either way.
- The audit event captures the overflow metadata via `audit.py`'s `_overflow_metadata()` (see
  below) — `overflow_handle`/`overflow_bytes` land on the `tool_result`/`run_code_return` audit
  event whenever present, alongside the bounded preview that's actually in history.

**Motivation** (`ISSUES.md` #2/#6): qwen3.6-flash reproducibly dumped every Suricata `stats` event
verbatim via `print(json.dumps(s))` in a loop, producing a 24.7M-char tool return that blew the
model's context window outright. This makes that class of mistake survivable at the tool-output
layer regardless of whether a skill's prompt guidance (`prompts/sandbox_notes.md`'s "~50 lines"
reminder) is followed.

**Gotcha:** the store path is `<workspace>/logs/overflow/<task_id>/` — inside the host-only,
`chmod 0o700` audit domain, not the task workspace (`/workspace` mount) and not readable by
`FileSystem` or `run_code`. Only the model's own `read_tool_result`-style handle (opaque, resolved
server-side by the harness) can retrieve a spilled value — there's no path a skill's own code can
use to reach it directly.

## `session.py` — `RunSession`, the application-lifecycle boundary

### `SessionState` (Enum)
`READY → RUNNING → ACTIVE → COMPLETED`, with `FAILED`/`INTERRUPTED` as terminal alternatives.
`_begin_turn` refuses to submit while `RUNNING`, `COMPLETED`, or `INTERRUPTED` (raises
`RuntimeError`) — `ACTIVE` (a completed turn, ready for the next one) and `READY` (nothing
submitted yet) are the only states a new turn can start from.

### `RunSession.__init__(setup, sink, *, checkpoint_each_turn)`
`checkpoint_each_turn=True` (Textual) persists `analyst_log-*.md`/`generated_code/` after *every*
successful turn; `False` (console) persists once, in `complete()`, at the very end. This is the
one behavioral fork between the two UIs baked into `RunSession` itself rather than left to the
caller.

### Turn submission — `submit_sync(prompt)` / `submit_async(prompt, *, model=None)`
Both follow the identical shape:
1. `_begin_turn(prompt)` → applies `_effective_prompt` (prepends `setup.prompt_prefix`, but only
   on the very first turn of the run — tracked by `_first_turn_sent`), writes a `prompt` audit
   event, sets state `RUNNING`.
2. `start_turn_watchdog(setup.options.max_run_seconds)` — starts (or, if `max_run_seconds is
   None`, returns `None` and starts nothing).
3. Calls `agent.run_sync`/`agent.run` with `_run_kwargs(...)`.
4. On success: `_record_success` (appends a `Turn`, sets state `ACTIVE`, checkpoints if
   `checkpoint_each_turn`). On `RunInterrupted`: `self.interrupt(str(exc))`, re-raise. On any other
   `Exception`: `self.fail(exc)`, re-raise.
5. `finally`: cancels the watchdog if one was started — **must** happen on every exit path,
   success or failure, or a completed turn's timer can fire late and interrupt a *later*,
   unrelated turn.

### `_run_kwargs(model=None) -> dict`
Builds the kwargs dict passed to `agent.run`/`run_sync`: always `event_stream_handler` +
`metadata`; `message_history` only if there's a prior turn; `model` only when overriding (Textual's
model-switch command); `usage_limits=UsageLimits(request_limit=options.max_turns)` only if
`max_turns is not None` — otherwise `pydantic_ai`'s own default (`request_limit=50`) applies
untouched.

### Lifecycle helpers
- `complete()`: no-op → `FAILED` if zero successful turns; else persists (if not already
  checkpointing per-turn) and sets `COMPLETED`.
- `fail(exc)` / `interrupt(reason)`: both idempotent (early-return if already in that terminal
  state), write one audit event (`error` / `interrupted`), set state.
- `close()`: idempotent (`_closed` guard). Writes `run_end` with `status = "completed" if state is
  COMPLETED else "failed"` — **note**: an `INTERRUPTED` session's `run_end` status is `"failed"`,
  not a third value; `INTERRUPTED` only distinguishes itself via the preceding `interrupted` audit
  event. Then closes the `AuditLog` and restores the pre-run SIGTERM handler
  (`restore_sigterm_handler`), always, in a `finally`.
- `__enter__`/`__exit__`: the `with RunSession(...) as session:` context-manager form
  (`console_ui.py`'s only usage) maps `(RunInterrupted, KeyboardInterrupt)` → `interrupt()`, any
  other exception → `fail()`, a clean `ACTIVE` exit → `complete()`, then always `close()`. Returns
  `False` (never suppresses the exception).

## `audit.py` — audit log, SIGTERM plumbing, and event translation

### `RunInterrupted(BaseException)`
Deliberately *not* an `Exception` subclass, so ordinary `except Exception` handlers elsewhere in
the stack (including third-party ones) don't accidentally swallow it — an interruption must
propagate all the way to `runner.py::main()`'s explicit catch.

### SIGTERM → `RunInterrupted`
- `install_sigterm_handler() -> Any` / `restore_sigterm_handler(previous)`: swap
  `signal.signal(signal.SIGTERM, _raise_on_sigterm)` in and back out, returning/accepting the
  prior handler so nesting (or restoring after a `--ui textual` session, which takes over SIGTERM
  itself — see `ui_textual.py` below) doesn't clobber something unrelated.
- `_raise_on_sigterm(signum, frame)`: raises `RunInterrupted(f"received signal {signum}")`. Both
  console mode's real SIGTERM handling and `start_turn_watchdog`'s synthetic self-signal go through
  this exact function — the audit trail can't distinguish an external kill from a
  `--max-run-seconds` timeout by message text alone (both read `"received signal 15"`).

### `start_turn_watchdog(seconds: int | None) -> threading.Timer | None`
`None` → returns `None`, nothing started. Otherwise: `threading.Timer(seconds, os.kill,
args=(os.getpid(), signal.SIGTERM))`, `daemon=True`, started immediately, returned for the caller
to `.cancel()`. Deliberately reuses the SIGTERM-to-`RunInterrupted` path above instead of a second
interruption mechanism (e.g. `asyncio.wait_for` for the async path only) — one code path handles
Ctrl+C, an external `kill -TERM`, *and* a wall-clock timeout identically, in both console and
Textual mode. Relies on a CPython guarantee: a signal delivered via `os.kill`/`raise_signal` from
any thread is still only ever *handled* on the main thread, so a background `Timer` thread can
reliably interrupt main-thread work (including a blocking `agent.run_sync()` call) this way.
**Gotcha**: the caller must cancel the timer on every exit path (see `session.py` above) — an
uncancelled timer fires regardless of whether the turn already finished.

### `AuditLog`
One instance per run. `__init__` opens `logs_dir / f"runner-{task_id}-{run_id}.jsonl"` in append
mode and immediately `chmod 0o600`s it (audit records can contain prompts/generated code/model
reasoning). `event(kind, **fields)` writes one JSON line (`ts`, `event`, `**fields`) and
`flush()`es immediately — not buffered — so a mid-run crash still leaves every event up to that
point on disk. `close()` just closes the file handle.

### `_overflow_metadata(part) -> dict[str, object]`
Pulls only `overflow_handle`/`overflow_bytes`/`overflow_content_handle` (`_OVERFLOW_METADATA_KEYS`)
out of a tool-result part's `metadata` mapping, if present — deliberately narrow, so unrelated
capability metadata doesn't leak into the audit log verbatim.

### `make_event_stream_handler(audit, sink)`
Returns an `async def handler(ctx, event_iter)` suitable for `agent.run`'s `event_stream_handler`
kwarg. For each streamed pydantic-ai event, calls **both** `audit.event(kind, **fields)` and
`sink.emit(kind, **fields)` with identical arguments — this single fact is what guarantees the
audit log and whichever UI is active never diverge. Event kind mapping:
`PartEndEvent`(`TextPart`) → `model_text`; `PartEndEvent`(`ThinkingPart`) → `model_thinking`;
`FunctionToolCallEvent` → `run_code_call` (if `tool_name == "run_code"`) else `tool_call`;
`FunctionToolResultEvent` → `run_code_return`/`tool_result` (same split), with
`_overflow_metadata(part)` merged into the fields whenever present.

## `artifacts.py` — `Turn`/`Transcript` and report rendering

### `Turn` (frozen dataclass) / `Transcript`
`Turn` is one successful turn's full record (both prompt forms, output, message history, model
used). `Transcript.append(turn)` is the only mutator; `.prompts`/`.outputs` are derived list
properties; `.message_history` returns the **last** turn's message history only (pydantic-ai
threads the full conversation through each turn's own `all_messages()`, so the latest turn's
history already is the complete conversation — earlier turns' histories are strict prefixes).

### `format_conversation_history(message_history, prompt) -> str`
Walks `ModelResponse` messages only (skips `ModelRequest` entirely — that's just the prompt being
echoed back, already shown separately). For each part: `ToolCallPart` with `tool_name == "run_code"`
renders as a placeholder (`"(Code saved to generated_code/ directory)"`) rather than the actual
code, since that's already persisted separately by `write_artifacts`; other tool calls render their
JSON args; `ToolReturnPart` (non-`run_code`) renders its content, truncated at 1000 chars;
`ThinkingPart` renders as a blockquote.

### `render_report(...) -> str` / `write_artifacts(...)`
`render_report` is pure (no I/O) — three different findings-section shapes depending on
`transcript.interactive` and turn count (single "Final Findings", multi-turn interactive
"Checkpoints", multi-turn non-interactive "Analysis Turns"), so the report structure this doc's
own results write-ups grep for (`## Final Findings`) only appears for the single-turn case.
`write_artifacts` does the actual disk I/O: writes the rendered report to
`analyst_log-<run_stamp>.md`, then walks `transcript.message_history` a second time (independently
of `format_conversation_history`) to extract every `run_code` call's code into
`generated_code/<run_stamp>-NN.py`, numbered sequentially. **This is the only place `run_code`
bodies get persisted** — a model that never calls `write_file` (see `ISSUES.md`'s original
write_file/generated_code finding) leaves behind only these numbered snapshots, never a named,
directly-reusable script.

## `script_lint.py` — deterministic post-hoc script fixes

`lint_and_fix_scripts(ws_path, on_message)`: globs `ws_path.glob("*.py")` — **non-recursive**, so
it only ever touches scripts sitting directly in the task root (i.e. ones a model saved via
`write_file`), never `generated_code/*.py` snapshots. Two checks per file:
1. `MAIN_GUARD_RE` — strips a trailing `if __name__ == "__main__":` guard via regex +
   `textwrap.dedent`, since Monty never defines `__name__` and the guard is a `NameError` landmine
   when the script is later pasted into a fresh `run_code` call. Auto-fixed, not just warned.
2. `SYS_ARGV_RE` — warns (does not auto-fix, since the right replacement is contextual) on any
   `sys.argv` usage, after a naive per-line `#`-comment strip so a *mention* of `sys.argv` in a
   comment doesn't false-positive.

Called twice per `prepare_run`/`RunSession` lifecycle: once in `prepare_run` before the "existing
scripts" prompt inventory is built (so the model sees already-fixed scripts), and again in
`RunSession._persist` after each checkpoint/completion (so a script the *current* turn just wrote
gets fixed before the next turn — or the next run — reads it back).

## `console_ui.py` — `ConsoleSink`, `run_console`

### `ConsoleSink`
`status()` = plain `print()`. `emit(kind, **fields)` reproduces the pre-refactor console output
exactly, dispatching on `kind` to `_echo(label, content, debug)` — a shared helper that truncates
to 400 chars unless `--debug`, always printing a `--- {label} ---` banner.

### `parse_checkpoint_input` / `build_continuation_prompt`
Pure functions backing `--interactive`'s blocking loop: `parse_checkpoint_input` maps free text to
a `CheckpointAction(kind, focus)` (`"stop"`/`"continue"`/anything else treated as a focus request,
stripping a leading `"focus on "` if present); `build_continuation_prompt` turns that back into the
actual follow-up prompt text sent to the model (`None` for `stop`, or for an empty/unparsed focus).

### `run_console(skill_dir, prompt, options)`
`prepare_run` → `with RunSession(..., checkpoint_each_turn=False) as session:` → one
`submit_sync(prompt)`, print the output, then (only under `--interactive`) loop: blocking
`input()`, parse, submit a continuation turn, print, repeat until `stop`. `session.complete()` is
the last line inside the `with` block — the context manager's own `__exit__` only calls
`complete()` automatically on a clean exit with state `ACTIVE`, but calling it explicitly here
means the exception paths (`RunInterrupted`/other) still get `__exit__`'s handling while the happy
path's completion is explicit and visible at the call site.

## `ui_textual.py` — Textual TUI driver

Imported lazily (only from inside `runner.py::main()`'s `--ui textual` branch) so a console-only
install never imports `textual`.

### `BufferedTextualSink` → `TextualSink`
Setup (`prepare_run`) happens *before* `AnalystApp` exists to receive live events, so
`BufferedTextualSink` just accumulates `status()` strings; `emit()` on it is unreachable
(`AssertionError` if ever called — setup never streams agent events). Once mounted, `AnalystApp`
drains the buffer into its `RichLog` and switches to a live `TextualSink`, which is only ever
driven from inside the `run_turn` worker `Task` — same asyncio loop as the App itself, so it
mutates widgets directly with no `call_from_thread` needed.

### `TextualSink.emit` tool-call/result pairing
`run_code_call`/`tool_call` add a `ToolTable` row keyed by `tool_call_id` with a `"(pending)"`
result cell; `run_code_return`/`tool_result` call `_update_result`, which `update_cell`s that same
row **in place** by key (`RichLog` has no such API, which is why tool events use `ToolTable`
instead). If no matching row exists (shouldn't happen given the event stream's guaranteed
call-then-result ordering), falls back to adding a new row rather than raising — a rendering
hiccup must never crash the run. `tool_result` events for `write_file` additionally trigger
`request_artifacts_reload()`, keeping the `DirectoryTree` panel live without any new
instrumentation (it's just watching for a tool name already flowing through the existing sink).

### `AnalystApp`
- `__init__` builds `self.session = RunSession(setup, self.sink, checkpoint_each_turn=True)`
  immediately — the App owns exactly one `RunSession` for its whole lifetime, even across many
  turns.
- `on_mount`: takes over `SIGTERM` via `asyncio.get_running_loop().add_signal_handler` — **not**
  the raw `signal.signal` handler `prepare_run` already installed. That raw handler can land
  inside asyncio's own internals (observed mid-`select()` during headless testing — Phase 12
  history in `../PROJECT.md`) rather than the running turn's own coroutine; `add_signal_handler`
  defers to a deterministic point on the loop instead, always resolving to `self.exit()`.
- `_handle_sigterm`: sets `interrupted_reason`, calls `session.interrupt(reason)`, `self.exit()`.
- `run_turn` (`@work(exclusive=True, group="agent-run")`): disables the prompt bar
  (`set_turn_active(True)`), calls `session.submit_async`, re-enables it in a `finally` regardless
  of outcome. The `except RunInterrupted` clause here is a **defensive fallback only** — real
  SIGTERM is normally caught by `_handle_sigterm` above; this only fires if the raw handler somehow
  still gets invoked before `on_mount`'s takeover runs.
- `action_quit` (`@work`, Ctrl+Q): shows `QuitConfirmScreen`, and on confirm just calls
  `self.exit()`. **Known gotcha, not yet fixed**: unlike `_handle_sigterm`, this path never sets
  `self.interrupted_reason` or calls `session.interrupt()` — so a user-confirmed Ctrl+Q quit
  doesn't get the same audit/exit-code treatment as a SIGTERM-driven one (`run_textual`'s
  `interrupted_reason is not None` check below never fires for it). The `@work` decorator plus the
  `isinstance(self.screen, QuitConfirmScreen)` guard exist specifically so a second Ctrl+Q press
  while the dialog is already open doesn't stack a second dialog.

### `run_textual(skill_dir, initial_prompt, options)`
`prepare_run` → construct `AnalystApp` (if construction itself throws, falls back to a bare
`RunSession` just to `fail()`/`close()` it so the audit log still closes cleanly, then re-raises)
→ `with app.session: app.run(); ...`. After `app.run()` returns (Textual's own event loop exited),
re-raises `RunInterrupted(app.interrupted_reason)` if that got set — this is what lets
`runner.py`'s single top-level `except RunInterrupted` handle both UI drivers identically.

## `tui_widgets.py` — reusable Textual widgets/screens

### `ToolTable(DataTable)`
Fixed-width `Time`(8)/`Tool`(14) columns, `Result` fills whatever's left — `DataTable` has no
built-in flex column, so `_resize_columns` computes it manually on every `on_mount`, `on_resize`,
`add_row`, and `update_cell`. **Gotcha**: a widget's `.size` is still `Size(0, 0)` inside
`on_mount()` (confirmed via headless `Pilot` testing), so the very first layout uses a
`max(available, 10)` floor rather than a possibly-negative computed width, corrected once the real
`Resize` event lands. `add_row`/`update_cell` both defer their resize via
`call_after_refresh` rather than resizing inline, so it runs after `DataTable`'s own bookkeeping
settles instead of stacking another synchronous layout pass on top.

### `QuitConfirmScreen(ModalScreen[bool])`
Trivial confirm dialog — `Escape` or the Cancel button dismiss with `False`, the Quit button
dismisses with `True`. See `ui_textual.py`'s `action_quit` above for how the result is (and isn't
fully) used.

### `FileViewerScreen(ModalScreen[None])`
Read-only preview for a file picked in the `DirectoryTree`. Caps preview at `_MAX_PREVIEW_BYTES`
(`300_000`) to avoid loading something pathologically large into a Rich renderable in memory.
Dispatches on suffix: `.md` → `Markdown(text)`, `.py` → `Syntax(text, "python", ...)`, everything
else → plain `Static(text, markup=False)` — the `markup=False` matters specifically because tool
output routinely contains literal `[...]` (e.g. `read_file`'s own `[path | N lines | hash:...]`
header) that `Static`'s default markup parsing would otherwise silently swallow as an unrecognized
style tag. `'e'` shells out to `$EDITOR` via `self.app.suspend()` (temporarily yields the terminal),
then closes the screen and triggers an artifacts-tree reload afterward, since the file may have
just changed on disk.

## Tests

- **`tests/test_config.py`** — `ModelCatalog` normalization across both hierarchical and flat
  `models.yaml` shapes.
- **`tests/test_audit.py`** — `start_turn_watchdog`: no-budget returns `None`; a cancelled timer
  never fires; an uncancelled one reliably interrupts real blocking work (`time.sleep`) via a real
  installed SIGTERM handler.
- **`tests/test_session.py`** — `RunSession` against a `FakeAgent`/`FakeAudit`/`FakeSink` triple:
  first-turn prompt-prefix behavior, a later-turn failure marking the whole session `FAILED`, and
  (via a `FakeAgent(sleep_seconds=...)` variant) `--max-run-seconds` actually interrupting a
  simulated stuck turn end-to-end through `submit_sync`.
- **`tests/test_run_core.py`** — a post-audit-creation setup failure still finalizes the audit log
  (`setup_error`/failed `run_end`/close/restore-handler) before re-raising.
- **`tests/test_artifacts.py`** — multi-turn report rendering uses the structured `Turn`/
  `Transcript` model rather than ad hoc string concatenation.
- **`tests/test_resilience.py`** — the 429 retry (bounded delay honored, a second 429 not
  retried, non-429 never retried) and the overflow capability (an oversized return gets replaced
  with a bounded preview + `overflow_handle` before it would reach model history; the spill store
  is task-scoped and `chmod 0o700`), plus one test confirming `overflow_handle`/`overflow_bytes`
  make it onto the audit event via `audit.py`'s `_overflow_metadata`.

None of these tests spin up a real `Agent`/model/network call — `FakeAgent`/`TestModel`/mocked
`anyio.sleep` throughout, so the suite runs in well under a second end-to-end
(`uv run --with pytest pytest tests/`).
