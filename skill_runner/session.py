"""Application-level run session shared by console and Textual drivers."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import UsageLimits

from .artifacts import Transcript, Turn, write_artifacts
from .audit import RunInterrupted, make_event_stream_handler, restore_sigterm_handler, start_turn_watchdog
from .run_core import RunSetup, RunSink
from .script_lint import lint_and_fix_scripts


class SessionState(Enum):
    READY = "ready"
    RUNNING = "running"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class RunSession:
    """Own one run's turns, transcript, post-turn work, audit, and finalization."""

    def __init__(self, setup: RunSetup, sink: RunSink, *, checkpoint_each_turn: bool):
        self.setup = setup
        self.sink = sink
        self.checkpoint_each_turn = checkpoint_each_turn
        self.stream_handler = make_event_stream_handler(setup.audit, sink)
        self.transcript = Transcript(
            initial_prompt=setup.initial_prompt,
            interactive=setup.options.interactive,
        )
        self.state = SessionState.READY
        self._first_turn_sent = False
        self._closed = False

    @property
    def message_history(self) -> list[ModelMessage] | None:
        history = self.transcript.message_history
        return history or None

    @property
    def has_successful_turns(self) -> bool:
        return bool(self.transcript.turns)

    def __enter__(self) -> RunSession:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if isinstance(exc, (RunInterrupted, KeyboardInterrupt)):
            self.interrupt(str(exc))
        elif exc is not None:
            self.fail(exc)
        elif self.state is SessionState.ACTIVE:
            self.complete()
        self.close()
        return False

    def _effective_prompt(self, prompt: str) -> str:
        if self._first_turn_sent:
            return prompt
        self._first_turn_sent = True
        return self.setup.prompt_prefix + prompt

    def _begin_turn(self, prompt: str) -> str:
        if self._closed or self.state in {
            SessionState.RUNNING,
            SessionState.COMPLETED,
            SessionState.INTERRUPTED,
        }:
            raise RuntimeError(f"cannot submit a turn while session is {self.state.value}")
        effective_prompt = self._effective_prompt(prompt)
        self.setup.audit.event("prompt", prompt=effective_prompt)
        self.state = SessionState.RUNNING
        return effective_prompt

    def _run_kwargs(self, model: str | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "event_stream_handler": self.stream_handler,
            "metadata": self.setup.run_metadata,
        }
        if self.message_history is not None:
            kwargs["message_history"] = self.message_history
        if model is not None:
            kwargs["model"] = model
        if self.setup.options.max_turns is not None:
            kwargs["usage_limits"] = UsageLimits(request_limit=self.setup.options.max_turns)
        return kwargs

    def submit_sync(self, prompt: str):
        effective_prompt = self._begin_turn(prompt)
        watchdog = start_turn_watchdog(self.setup.options.max_run_seconds)
        try:
            result = self.setup.agent.run_sync(effective_prompt, **self._run_kwargs())
            self._record_success(prompt, effective_prompt, result, self.setup.model)
        except RunInterrupted as exc:
            self.interrupt(str(exc))
            raise
        except Exception as exc:
            self.fail(exc)
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
        return result

    async def submit_async(self, prompt: str, *, model: str | None = None):
        effective_prompt = self._begin_turn(prompt)
        selected_model = model or self.setup.model
        watchdog = start_turn_watchdog(self.setup.options.max_run_seconds)
        try:
            result = await self.setup.agent.run(
                effective_prompt,
                **self._run_kwargs(model=selected_model),
            )
            self._record_success(prompt, effective_prompt, result, selected_model)
        except RunInterrupted as exc:
            self.interrupt(str(exc))
            raise
        except Exception as exc:
            self.fail(exc)
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
        return result

    def _record_success(self, submitted_prompt: str, effective_prompt: str, result, model: str) -> None:
        self.transcript.append(
            Turn(
                submitted_prompt=submitted_prompt,
                effective_prompt=effective_prompt,
                output=result.output,
                message_history=result.all_messages(),
                model=model,
            )
        )
        self.state = SessionState.ACTIVE
        if self.checkpoint_each_turn:
            self._persist(lint_first=True)

    def _persist(self, *, lint_first: bool) -> None:
        if lint_first:
            lint_and_fix_scripts(self.setup.ws_path, on_message=self.sink.status)
        write_artifacts(
            ws_path=self.setup.ws_path,
            run_stamp=self.setup.run_stamp,
            skill_name=self.setup.skill_name,
            transcript=self.transcript,
            debug=self.setup.options.debug,
            on_message=self.sink.status,
        )
        if not lint_first:
            lint_and_fix_scripts(self.setup.ws_path, on_message=self.sink.status)

    def complete(self) -> None:
        if not self.has_successful_turns:
            self.state = SessionState.FAILED
            return
        if not self.checkpoint_each_turn:
            self._persist(lint_first=False)
        self.state = SessionState.COMPLETED

    def fail(self, exc: BaseException) -> None:
        if self.state is SessionState.FAILED:
            return
        self.setup.audit.event("error", error=str(exc), error_type=type(exc).__name__)
        self.state = SessionState.FAILED

    def interrupt(self, reason: str) -> None:
        if self.state is SessionState.INTERRUPTED:
            return
        self.setup.audit.event("interrupted", reason=reason)
        self.state = SessionState.INTERRUPTED

    def close(self) -> None:
        if self._closed:
            return
        status = "completed" if self.state is SessionState.COMPLETED else "failed"
        try:
            self.setup.audit.event("run_end", status=status)
        finally:
            try:
                self.setup.audit.close()
            finally:
                self._closed = True
                restore_sigterm_handler(self.setup.previous_sigterm_handler)
