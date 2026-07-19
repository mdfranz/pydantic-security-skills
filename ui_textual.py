"""Textual TUI driver: a tool-calls table, a model output/thinking log, a live artifacts
tree, and a bottom bar that always accepts free-text follow-up prompts. Consumes the same
event stream and produces the same on-disk artifacts as console_ui.py -- this is a new
observer, not a new data producer. Imported lazily, only when --ui textual is selected, so
a console-only install never imports textual."""

import argparse
import asyncio
import json
import signal

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, DirectoryTree, Footer, Header, Input, RichLog

from audit import RunInterrupted, make_event_stream_handler
from run_core import ArtifactSession, RunSetup, prepare_run, run_turn_async, write_artifacts
from script_lint import lint_and_fix_scripts


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
            table.add_row("run_code", str(fields["code"]), "(pending)", key=str(fields["tool_call_id"]))
        elif kind == "tool_call":
            args = json.dumps(fields.get("args") or {})
            table.add_row(str(fields["tool_name"]), args, "(pending)", key=str(fields["tool_call_id"]))
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
            table.update_cell(str(tool_call_id), self.app.tool_result_column, str(content))
        except Exception:
            # No matching call row (shouldn't happen given the event stream's call/result
            # ordering) -- never let a rendering hiccup crash the run.
            table.add_row("?", "", str(content), key=str(tool_call_id))


class AnalystApp(App):
    """Multiple panels (tool calls, model output, artifacts) plus a bottom bar for
    prompting/follow-ups, for interactive investigation sessions."""

    CSS = """
    #left-col {
        width: 45%;
    }
    #tool-calls {
        height: 60%;
    }
    #artifacts {
        height: 40%;
        border-top: solid $accent;
    }
    #output {
        width: 55%;
    }
    """

    def __init__(self, setup: RunSetup, buffered_messages: list[str]):
        super().__init__()
        self.title = f"{setup.skill_name} -- {setup.task_id}"
        self.setup = setup
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
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left-col"):
                yield DataTable(id="tool-calls")
                yield DirectoryTree(str(self.setup.ws_path), id="artifacts")
            yield RichLog(id="output", markup=True, wrap=True)
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
        self.tool_table = self.query_one("#tool-calls", DataTable)
        _, _, self.tool_result_column = self.tool_table.add_columns("Tool", "Call", "Result")
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
