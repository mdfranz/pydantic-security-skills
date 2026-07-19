"""Textual TUI driver: a tool-calls table, a model output/thinking log, a live artifacts
tree, and a bottom bar that always accepts free-text follow-up prompts. Consumes the same
event stream and produces the same on-disk artifacts as console_ui.py -- this is a new
observer, not a new data producer. Imported lazily, only when --ui textual is selected, so
a console-only install never imports textual."""

import argparse
import asyncio
import os
import signal
import subprocess
from datetime import datetime
from functools import partial
from pathlib import Path

from rich.syntax import Syntax
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RichLog,
    Static,
)

from .audit import RunInterrupted, make_event_stream_handler
from .run_core import (
    ArtifactSession,
    RunSetup,
    list_model_choices,
    load_models_config,
    prepare_run,
    run_turn_async,
    write_artifacts,
)
from .script_lint import lint_and_fix_scripts

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
            self.app.output_log.write(f"[bold]Agent:[/bold] {fields['content']}")
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


class ToolTable(DataTable):
    """DataTable with fixed-width Time/Tool columns and a Result column that fills whatever
    horizontal space is left. DataTable has no built-in flex/stretch column, so this
    recomputes explicit column widths on every resize -- including the initial layout pass,
    since a widget's size is still Size(0, 0) inside on_mount() (confirmed via a headless
    Pilot test), before Textual has actually arranged it."""

    TIME_COLUMN_WIDTH = 8  # "HH:MM:SS"
    # Covers every tool name this harness actually exposes except the rarely-called
    # "create_directory" (16 chars, clipped by 2) -- narrower than the worst case on purpose,
    # since every fixed cell spent here is one taken from Result.
    TOOL_COLUMN_WIDTH = 14

    def on_mount(self) -> None:
        self._time_column, self._tool_column, self.result_column = self.add_columns(
            "Time", "Tool", "Result"
        )
        self._resize_columns()

    def on_resize(self, event: events.Resize) -> None:
        self._resize_columns()

    def add_row(self, *cells, **kwargs):
        row_key = super().add_row(*cells, **kwargs)
        # Real terminals can report an initial placeholder size (e.g. an SSH session before
        # the true geometry is negotiated) before the first authoritative Resize event lands
        # -- headless tests never hit this since run_test(size=...) reports the final size
        # immediately. Re-syncing on every row keeps Result from getting stuck narrow until
        # some *unrelated* later resize happens to fix it. Deferred via call_after_refresh
        # rather than called inline, so it runs after DataTable's own add_row bookkeeping has
        # finished settling instead of layering another synchronous layout pass on top of it.
        self.call_after_refresh(self._resize_columns)
        return row_key

    def update_cell(self, *args, **kwargs):
        result = super().update_cell(*args, **kwargs)
        self.call_after_refresh(self._resize_columns)
        return result

    def _resize_columns(self) -> None:
        if not self.columns:
            return
        # Render width per column includes left+right padding -- account for all three.
        total_padding = 2 * self.cell_padding * len(self.columns)
        fixed = self.TIME_COLUMN_WIDTH + self.TOOL_COLUMN_WIDTH
        available = self.size.width - fixed - total_padding
        self.columns[self._time_column].width = self.TIME_COLUMN_WIDTH
        self.columns[self._tool_column].width = self.TOOL_COLUMN_WIDTH
        # Falls back to a small positive width before the first real resize event lands
        # (size is still 0 at that point) -- harmless, immediately corrected once Textual
        # arranges the widget and fires Resize.
        self.columns[self.result_column].width = max(available, 10)
        self._require_update_dimensions = True
        self.refresh(layout=True)


class QuitConfirmScreen(ModalScreen[bool]):
    """Ctrl+Q's own default binding exits immediately -- fine for a one-shot console run,
    but here it can throw away an in-progress multi-turn investigation session, so quitting
    always asks first."""

    CSS = """
    QuitConfirmScreen {
        align: center middle;
    }
    #quit-dialog {
        width: auto;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $panel;
    }
    #quit-message {
        padding-bottom: 1;
    }
    #quit-buttons {
        width: auto;
        height: auto;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Label("Quit the analyst session?", id="quit-message")
            with Horizontal(id="quit-buttons"):
                yield Button("Quit", variant="error", id="confirm-quit")
                yield Button("Cancel", variant="primary", id="cancel-quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-quit")

    def action_cancel(self) -> None:
        self.dismiss(False)


class FileViewerScreen(ModalScreen[None]):
    """Read-only preview of a file picked in the artifacts tree -- Markdown renders
    formatted (headers, tables, bold), .py gets syntax highlighting, everything else is
    plain text. 'e' shells out to $EDITOR if set, since the tree is otherwise just a passive
    listing with no way to act on what it shows."""

    CSS = """
    FileViewerScreen {
        align: center middle;
    }
    #viewer-dialog {
        width: 90%;
        height: 90%;
        border: thick $accent;
        background: $panel;
    }
    #viewer-title {
        dock: top;
        background: $panel-lighten-1;
        color: $text;
        padding: 0 1;
        height: 1;
    }
    #viewer-body {
        padding: 1 2;
    }
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("q", "close", "Close"),
        ("e", "open_editor", "Edit ($EDITOR)"),
    ]

    # Guards against loading something pathologically large (e.g. a misplaced data file)
    # into a Rich renderable in memory -- generated_code/*.py and analyst_log-*.md are all
    # comfortably under this in practice.
    _MAX_PREVIEW_BYTES = 300_000

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical(id="viewer-dialog"):
            yield Static(self._title_text(), id="viewer-title", markup=False)
            with VerticalScroll(id="viewer-body"):
                yield from self._render_content()

    def _title_text(self) -> str:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        editor_hint = " -- e: edit" if os.environ.get("EDITOR") else ""
        return f"{self.path.name}  ({size:,} bytes) -- Esc/q: close{editor_hint}"

    def _render_content(self):
        try:
            raw = self.path.read_bytes()
        except OSError as e:
            yield Static(f"Could not read file: {e}", markup=False)
            return

        truncated = len(raw) > self._MAX_PREVIEW_BYTES
        if truncated:
            raw = raw[: self._MAX_PREVIEW_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            yield Static(f"[binary file, {len(raw):,} bytes -- not previewable]", markup=False)
            return
        if truncated:
            text += "\n\n... [truncated -- file exceeds the preview limit] ..."

        # Markdown/Syntax are already Rich renderables, not plain str, so they never go
        # through Static's markup parsing -- only the plain-text fallback needs markup=False
        # to avoid the same "literal '[...]' silently eaten as a style tag" bug _cell_text()
        # works around for the DataTable cells (e.g. read_file's own '[path | N lines | ...]'
        # header would otherwise vanish here too).
        suffix = self.path.suffix.lower()
        if suffix == ".md":
            yield Markdown(text)
        elif suffix == ".py":
            yield Static(Syntax(text, "python", line_numbers=True, word_wrap=True))
        else:
            yield Static(text, markup=False)

    def action_close(self) -> None:
        self.dismiss()

    def action_open_editor(self) -> None:
        editor = os.environ.get("EDITOR")
        if not editor:
            self.app.bell()
            self.notify("No $EDITOR set -- can't open an external editor.", severity="warning")
            return
        with self.app.suspend():
            subprocess.run([editor, str(self.path)])
        # Content on disk may have just changed -- close rather than show a stale preview;
        # reopening from the tree picks up the edit, and the artifacts panel needs a reload
        # too in case the edit changed something write_file would otherwise have caught.
        self.app.request_artifacts_reload()
        self.dismiss()


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
        self.available_models = list_model_choices(load_models_config())
        self.title = f"{setup.skill_name} -- {setup.task_id} -- {self.current_model}"
        self.buffered_messages = buffered_messages
        self.sink = TextualSink(self)
        self.stream_handler = make_event_stream_handler(setup.audit, self.sink)
        self.message_history = None
        self.session = ArtifactSession(
            initial_prompt=setup.initial_prompt,
            interactive=setup.interactive,
        )
        self.turn_active = False
        self.completed = False
        self.interrupted_reason: str | None = None
        self._first_turn_sent = False

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
            self.exit()

    def _handle_sigterm(self) -> None:
        reason = "received signal SIGTERM"
        self.interrupted_reason = reason
        self.setup.audit.event("interrupted", reason=reason)
        self.exit()

    @work(exclusive=True, group="agent-run")
    async def run_turn(self, prompt: str) -> None:
        self.set_turn_active(True)  # disable prompt bar; do not cancel/queue active work

        # Inventory (input files / reusable scripts) is folded into whichever turn is
        # submitted first -- the CLI-provided initial prompt, or the first bottom-bar
        # message when the session started empty.
        full_prompt = prompt
        if not self._first_turn_sent:
            full_prompt = self.setup.prompt_prefix + prompt
            self._first_turn_sent = True

        try:
            result = await run_turn_async(
                self.setup.agent,
                full_prompt,
                self.message_history,
                stream_handler=self.stream_handler,
                run_metadata=self.setup.run_metadata,
                audit=self.setup.audit,
                model=self.current_model,
            )
            self.message_history = result.all_messages()
            self.session.prompts.append(full_prompt)
            self.session.outputs.append(result.output)
            self.session.message_history = self.message_history

            lint_and_fix_scripts(self.setup.ws_path, on_message=self.sink.status)
            write_artifacts(
                ws_path=self.setup.ws_path,
                run_stamp=self.setup.run_stamp,
                skill_name=self.setup.skill_name,
                session=self.session,
                debug=self.setup.debug,
                on_message=self.sink.status,
            )
            self.completed = True
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
            self.setup.audit.event("interrupted", reason=str(e))
            self.exit()
        except Exception as e:
            self.setup.audit.event("error", error=str(e), error_type=type(e).__name__)
            self.sink.status(f"Error: {e}")
        finally:
            self.set_turn_active(False)


def run_textual(skill_dir: str, initial_prompt: str | None, args: argparse.Namespace) -> None:
    """Entrypoint for --ui textual. Shared setup runs before the App exists (so a
    BufferedTextualSink can catch its status output); AnalystApp then drains that buffer and
    drives turns through the same run_core helpers console_ui.py uses."""
    buffered = BufferedTextualSink()
    setup = prepare_run(skill_dir, initial_prompt, args, buffered, ui_mode="textual")
    app = AnalystApp(setup, buffered.messages)
    try:
        app.run()
    finally:
        setup.audit.event("run_end", status="completed" if app.completed else "failed")
        setup.audit.close()
    if app.interrupted_reason is not None:
        raise RunInterrupted(app.interrupted_reason)
