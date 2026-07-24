"""Textual TUI driver: a tool-calls table, a model output/thinking log, a live artifacts
tree, and a bottom bar that always accepts free-text follow-up prompts. Consumes the same
event stream and produces the same on-disk artifacts as console_ui.py -- this is a new
observer, not a new data producer. Imported lazily, only when --ui textual is selected, so
a console-only install never imports textual."""

import asyncio
import signal
from datetime import datetime
from functools import partial

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DirectoryTree,
    Footer,
    Header,
    Input,
    RichLog,
)

from .audit import RunInterrupted
from .config import RunOptions, load_model_catalog
from .run_core import (
    PROJECT_ROOT,
    RunSetup,
    build_resume_hint,
    prepare_run,
)
from .session import RunSession, SessionState
from .tui_widgets import FileViewerScreen, QuitConfirmScreen, ToolTable

_CELL_TEXT_LIMIT = 300


def _cell_text(value: object, limit: int = _CELL_TEXT_LIMIT) -> Text:
    """Plain (non-markup) Rich Text for DataTable cells. Tool output routinely contains
    literal '[...]' -- e.g. read_file's '[path | N lines | hash:...]' header -- which
    DataTable's default markup parsing (triggered for any plain str cell) would swallow as
    an unrecognized style tag, rendering the cell blank instead of raising. Wrapping in Text
    up front skips that parsing entirely. Also truncates with a visible ellipsis, since
    update_cell doesn't grow the column to fit new content (update_width defaults to False)
    -- without this, long results were silently hard-clipped with no sign anything was cut."""
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return Text(text)


class BufferedTextualSink:
    """Accumulates status() messages produced during shared setup (task/workspace
    resolution, Agent construction, pre-run linting/inventory) before AnalystApp exists to
    receive them. Setup produces no streamed events -- only status lines -- so emit() is
    never expected to be called on this sink. AnalystApp drains the buffered messages into
    its visible output log on mount, then the run hands off to a live TextualSink."""

    def __init__(self):
        self.messages: list[str] = []

    def status(self, message: str) -> None:
        self.messages.append(message)

    def emit(self, kind: str, **fields: object) -> None:
        raise AssertionError("BufferedTextualSink.emit should never be called during setup")


class TextualSink:
    """Only ever driven from inside the agent.run() worker Task -- shares the App's asyncio
    loop, so it mutates widgets directly with no call_from_thread needed."""

    def __init__(self, app: "AnalystApp"):
        self.app = app

    def status(self, message: str) -> None:
        self.app.output_log.write(message)

    def emit(self, kind: str, **fields: object) -> None:
        table = self.app.tool_table
        if kind == "model_text":
            label = "Agent (draft)" if self.app.setup.options.interactive else "Agent"
            self.app.output_log.write(f"[bold]{label}:[/bold] {fields['content']}")
        elif kind == "model_thinking":
            self.app.output_log.write(f"[dim]Agent (thinking): {fields['content']}[/dim]")
        elif kind == "run_code_call":
            table.add_row(
                _cell_text(datetime.now().strftime("%H:%M:%S")),
                _cell_text("run_code"),
                _cell_text("(pending)"),
                key=str(fields["tool_call_id"]),
            )
        elif kind == "tool_call":
            table.add_row(
                _cell_text(datetime.now().strftime("%H:%M:%S")),
                _cell_text(fields["tool_name"]),
                _cell_text("(pending)"),
                key=str(fields["tool_call_id"]),
            )
        elif kind == "run_code_return":
            self._update_result(fields["tool_call_id"], fields["content"])
        elif kind == "tool_result":
            self._update_result(fields["tool_call_id"], fields["content"])
            # Artifacts panel: reload whenever a write_file tool event is observed --
            # already flowing through the existing sink, no new instrumentation needed.
            if fields.get("tool_name") == "write_file":
                self.app.request_artifacts_reload()

    def _update_result(self, tool_call_id: object, content: object) -> None:
        table = self.app.tool_table
        try:
            table.update_cell(str(tool_call_id), self.app.tool_result_column, _cell_text(content))
        except Exception:
            # No matching call row (shouldn't happen given the event stream's call/result
            # ordering) -- never let a rendering hiccup crash the run.
            table.add_row(
                _cell_text(datetime.now().strftime("%H:%M:%S")),
                _cell_text("?"),
                _cell_text(content),
                key=str(tool_call_id),
            )


class ModelCommands(Provider):
    """Command-palette source listing every model in models.yaml. Ctrl+P already opens the
    palette (see the Footer's own '^p palette' hint) so this reuses that existing, always-
    visible affordance rather than adding another keybinding or a permanent widget."""

    def _label(self, app: "AnalystApp", choice) -> str:
        current = " [current]" if choice.id == app.current_model else ""
        return f"{choice.alias} -- {choice.description} ({choice.provider}){current}"

    async def discover(self) -> Hits:
        app = self.app
        assert isinstance(app, AnalystApp)
        for choice in app.available_models:
            yield Hit(1.0, self._label(app, choice), partial(app.set_model, choice.id), help=choice.id)

    async def search(self, query: str) -> Hits:
        app = self.app
        assert isinstance(app, AnalystApp)
        matcher = self.matcher(query)
        for choice in app.available_models:
            label = self._label(app, choice)
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), partial(app.set_model, choice.id), help=choice.id)


class AnalystApp(App):
    """Multiple panels (tool calls, model output, artifacts) plus a bottom bar for
    prompting/follow-ups, for interactive investigation sessions."""

    COMMANDS = App.COMMANDS | {ModelCommands}

    CSS = """
    #output {
        width: 55%;
    }
    #side-col {
        width: 45%;
    }
    #tool-calls {
        height: 60%;
    }
    #tool-calls > .datatable--header {
        background: $panel-lighten-1;
        color: $text;
    }
    #artifacts {
        height: 40%;
        border-top: solid $accent;
    }
    """

    def __init__(self, setup: RunSetup, buffered_messages: list[str]):
        super().__init__()
        self.setup = setup
        self.current_model = setup.model
        self.available_models = load_model_catalog(PROJECT_ROOT).choices
        self.title = f"{setup.skill_name} -- {setup.task_id} -- {self.current_model}"
        self.buffered_messages = buffered_messages
        self.sink = TextualSink(self)
        self.session = RunSession(setup, self.sink, checkpoint_each_turn=True)
        self.turn_active = False
        self.interrupted_reason: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            yield RichLog(id="output", markup=True, wrap=True)
            with Vertical(id="side-col"):
                yield ToolTable(id="tool-calls")
                yield DirectoryTree(str(self.setup.ws_path), id="artifacts")
        yield Input(placeholder="Ask the analyst...", id="prompt-bar")
        yield Footer()

    def on_mount(self) -> None:
        # prepare_run() already installed audit.py's raw signal.signal(SIGTERM, ...) handler
        # for console mode's synchronous blocking calls, but that handler can interrupt
        # arbitrary asyncio-internal C code (e.g. the selector poll deep inside the event
        # loop) rather than our own coroutine, which is exactly the "clobbered/unverified"
        # risk refs/textual-ui-plan.md calls out for Textual. Take over SIGTERM here with
        # asyncio's own signal handling instead: it defers the callback to a normal,
        # deterministic point on this loop, so the shutdown is always a clean self.exit()
        # regardless of whether a turn is active when the signal arrives.
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, self._handle_sigterm)

        self.output_log = self.query_one("#output", RichLog)
        self.tool_table = self.query_one("#tool-calls", ToolTable)
        self.tool_result_column = self.tool_table.result_column
        self.artifacts_tree = self.query_one("#artifacts", DirectoryTree)
        self.prompt_bar = self.query_one("#prompt-bar", Input)
        self.prompt_bar.focus()

        for message in self.buffered_messages:
            self.output_log.write(message)

        if self.setup.initial_prompt:
            self.run_turn(self.setup.initial_prompt)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt or self.turn_active:
            return
        event.input.value = ""
        self.run_turn(prompt)  # always free-text; message_history carries prior turns

    def set_turn_active(self, active: bool) -> None:
        self.turn_active = active
        self.prompt_bar.disabled = active

    def request_artifacts_reload(self) -> None:
        self.artifacts_tree.reload()

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.push_screen(FileViewerScreen(event.path))

    def set_model(self, model_id: str) -> None:
        """Called from the command palette (Ctrl+P -> pick a model from models.yaml). Takes
        effect on the next submitted prompt -- pydantic_ai lets a turn override the Agent's
        model without rebuilding it, so message_history carries over across the switch."""
        if model_id == self.current_model:
            return
        self.current_model = model_id
        self.title = f"{self.setup.skill_name} -- {self.setup.task_id} -- {self.current_model}"
        self.setup.audit.event("model_changed", model=model_id)
        self.sink.status(f"Model switched to {model_id} (takes effect on the next prompt).")

    @work
    async def action_quit(self) -> None:
        # Overrides App's default action_quit (bound to Ctrl+Q), which exits immediately --
        # push_screen_wait requires an active worker context, hence @work here. Each Ctrl+Q
        # press spawns its own worker (no exclusive= group), so without this guard a second
        # press while the dialog is already open stacks a *second* QuitConfirmScreen -- it
        # only becomes visible after cancelling the first, looking like the app is asking
        # twice.
        if isinstance(self.screen, QuitConfirmScreen):
            return
        if await self.push_screen_wait(QuitConfirmScreen()):
            self._cancel_active_turn("user requested quit")
            self.exit()

    def _cancel_active_turn(self, reason: str) -> None:
        """Record and cancel the sole agent worker before leaving the app.

        Textual exits its UI immediately, but an agent turn is an independent worker. Marking
        the session first makes the audit truthful even if cancellation reaches the provider at
        a later await point; cancelling the group prevents a worker from completing against a
        closed session after the alternate screen has been restored.
        """
        if not self.turn_active:
            return
        self.session.interrupt(reason)
        self.workers.cancel_group(self, "agent-run")

    def _handle_sigterm(self) -> None:
        reason = "received signal SIGTERM"
        self.interrupted_reason = reason
        self._cancel_active_turn(reason)
        if self.session.state is not SessionState.INTERRUPTED:
            self.session.interrupt(reason)
        self.exit()

    @work(exclusive=True, group="agent-run")
    async def run_turn(self, prompt: str) -> None:
        self.set_turn_active(True)  # disable prompt bar; do not cancel/queue active work

        try:
            await self.session.submit_async(prompt, model=self.current_model)
            self.request_artifacts_reload()
        except RunInterrupted as e:
            # Defensive fallback: SIGTERM is normally handled by _handle_sigterm via
            # asyncio's own signal handling (registered in on_mount), not by this except
            # clause. This only fires if audit.py's raw signal.signal(SIGTERM, ...)
            # handler (installed earlier, in prepare_run) somehow still gets invoked --
            # e.g. before on_mount's asyncio handler takes over. Same clean-shutdown
            # path either way: self.exit() is Textual's own, guaranteeing terminal
            # restoration, and run_textual() re-raises RunInterrupted once app.run()
            # returns so the top-level exit-143 mapping still applies.
            self.interrupted_reason = str(e)
            self.exit()
        except asyncio.CancelledError:
            # Normal during a confirmed quit or app shutdown. The explicit transition keeps
            # `RunSession.__exit__` from closing a RUNNING turn without an interruption event.
            if self.session.state is not SessionState.INTERRUPTED:
                self.session.interrupt("agent turn cancelled")
            raise
        except Exception as e:
            self.sink.status(f"Error: {e}")
        finally:
            self.set_turn_active(False)


def run_textual(skill_dir: str, initial_prompt: str | None, options: RunOptions) -> None:
    """Entrypoint for --ui textual. Shared setup runs before the App exists (so a
    BufferedTextualSink can catch its status output); AnalystApp then drains that buffer and
    drives turns through the same RunSession boundary console_ui.py uses."""
    buffered = BufferedTextualSink()
    setup = prepare_run(skill_dir, initial_prompt, options, buffered)
    try:
        app = AnalystApp(setup, buffered.messages)
    except BaseException as exc:
        fallback_session = RunSession(setup, buffered, checkpoint_each_turn=True)
        fallback_session.fail(exc)
        fallback_session.close()
        raise

    with app.session:
        try:
            app.run()
        finally:
            # Printed after app.run() returns, not through the TUI's own output log -- by this
            # point Textual has already exited its alt-screen and restored the normal terminal,
            # which is exactly when a copy-pasteable resume command becomes visible/useful.
            print(f"\nTo resume this task: {build_resume_hint(skill_dir, setup.task_id, options, ui='textual')}")
        if app.interrupted_reason is not None:
            raise RunInterrupted(app.interrupted_reason)
