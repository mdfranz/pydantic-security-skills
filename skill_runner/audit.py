"""Append-only run audit log, SIGTERM-to-exception plumbing, and the UI-neutral event
stream handler that feeds both the audit log and whichever UI sink is active."""

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartEndEvent,
    TextPart,
    ThinkingPart,
)

if TYPE_CHECKING:
    from .run_core import RunSink


class RunInterrupted(BaseException):
    """Raised from the SIGTERM handler so termination unwinds through the normal
    try/except/finally in main() -- like KeyboardInterrupt already does -- instead of the
    process dying before the audit log's run_end event is written. A BaseException, not an
    Exception, so it isn't mistaken for an application error. Can't help against SIGKILL,
    which no process can catch; that's an OS-level limit, not something this handles."""


def _raise_on_sigterm(signum, frame):
    raise RunInterrupted(f"received signal {signum}")


def install_sigterm_handler() -> Any:
    """Converts SIGTERM into a normal Python exception so the finally block in the caller
    still runs and writes run_end -- without this, a hard kill (e.g. a process manager's
    timeout, as opposed to Ctrl+C's KeyboardInterrupt, which Python already turns into an
    exception) terminates before the audit log is closed out."""
    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    return previous_handler


def restore_sigterm_handler(previous_handler: Any) -> None:
    """Restore the handler that was active before this run took ownership of SIGTERM."""
    signal.signal(signal.SIGTERM, previous_handler)


def start_turn_watchdog(seconds: int | None) -> threading.Timer | None:
    """Enforce a wall-clock budget on the turn about to run by delivering this process its own
    SIGTERM after `seconds` -- reusing install_sigterm_handler's existing conversion to
    RunInterrupted rather than inventing a second interruption path, since a stuck turn (an agent.
    run/run_sync call that's genuinely hung -- e.g. a provider connection that never returns, as
    opposed to GLM-5.2 just being slow but making steady progress) needs the exact same clean
    shutdown as Ctrl+C: audit log closed, partial artifacts kept, exit 143. A background thread
    can signal the main thread this way because the interpreter always runs the registered Python
    signal handler on the main thread regardless of which thread called os.kill/raise_signal.
    Returns None (nothing to cancel) when no budget is set. Caller must .cancel() the timer once
    the turn finishes on its own, or this fires late and interrupts a future, unrelated turn."""
    if seconds is None:
        return None
    timer = threading.Timer(seconds, os.kill, args=(os.getpid(), signal.SIGTERM))
    timer.daemon = True
    timer.start()
    return timer


def map_run_interrupted_exit_code() -> None:
    """Matches the shell's own SIGTERM exit-code convention (128 + 15) instead of a raw
    traceback -- the run_end/interrupted events are already written by the caller's
    finally block before this runs."""
    sys.exit(143)


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


_OVERFLOW_METADATA_KEYS = (
    "overflow_handle",
    "overflow_bytes",
    "overflow_content_handle",
)


def _overflow_metadata(part: object) -> dict[str, object]:
    """Keep spill references in the audit without serializing unrelated tool metadata."""
    metadata = getattr(part, "metadata", None)
    if not isinstance(metadata, Mapping):
        return {}
    return {key: metadata[key] for key in _OVERFLOW_METADATA_KEYS if key in metadata}


def make_event_stream_handler(audit: AuditLog, sink: "RunSink"):
    """Build an event_stream_handler that mirrors model responses and tool calls/results to
    the audit log incrementally, as pydantic_ai emits them during agent.run/run_sync -- not
    just reconstructed afterward from the finished conversation. Calls both audit.event(kind,
    **fields) and sink.emit(kind, **fields) with the identical (kind, fields) for every event,
    so the active UI always sees exactly what the audit log persists. Per-kind label/formatting
    lives in each sink's own emit -- this handler stays UI-agnostic."""

    call_start_times: dict[str, float] = {}

    async def handler(ctx, event_iter):
        async for event in event_iter:
            if isinstance(event, PartEndEvent):
                part = event.part
                if isinstance(part, TextPart) and part.content.strip():
                    audit.event("model_text", content=part.content)
                    sink.emit("model_text", content=part.content)
                elif isinstance(part, ThinkingPart) and part.content.strip():
                    audit.event("model_thinking", content=part.content)
                    sink.emit("model_thinking", content=part.content)
            elif isinstance(event, FunctionToolCallEvent):
                part = event.part
                call_start_times[part.tool_call_id] = time.monotonic()
                args = part.args_as_dict() if part.args else None
                if part.tool_name == "run_code":
                    code = (args or {}).get("code") or ""
                    audit.event("run_code_call", tool_call_id=part.tool_call_id, code=code)
                    sink.emit("run_code_call", tool_call_id=part.tool_call_id, code=code)
                else:
                    audit.event(
                        "tool_call", tool_call_id=part.tool_call_id, tool_name=part.tool_name, args=args
                    )
                    sink.emit(
                        "tool_call", tool_call_id=part.tool_call_id, tool_name=part.tool_name, args=args
                    )
            elif isinstance(event, FunctionToolResultEvent):
                part = event.part
                tool_name = getattr(part, "tool_name", None)
                content = getattr(part, "content", None)
                kind = "run_code_return" if tool_name == "run_code" else "tool_result"
                start_time = call_start_times.pop(part.tool_call_id, None)
                duration_ms = (
                    round((time.monotonic() - start_time) * 1000, 2)
                    if start_time is not None
                    else None
                )
                fields = {
                    "tool_call_id": part.tool_call_id,
                    "tool_name": tool_name,
                    "duration_ms": duration_ms,
                    "content": content,
                    **_overflow_metadata(part),
                }
                audit.event(kind, **fields)
                sink.emit(kind, **fields)

    return handler
