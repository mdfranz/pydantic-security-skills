"""Reusable Textual widgets and modal screens for the analyst TUI."""

import os
import subprocess
from pathlib import Path

from rich.syntax import Syntax
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Markdown, Static


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
