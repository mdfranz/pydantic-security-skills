import asyncio
import time
import unittest
from unittest.mock import MagicMock

from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent, ToolCallPart, ToolReturnPart
from skill_runner.audit import (
    RunInterrupted,
    install_sigterm_handler,
    make_event_stream_handler,
    restore_sigterm_handler,
    start_turn_watchdog,
)


class TurnWatchdogTests(unittest.TestCase):
    def test_no_budget_returns_no_timer(self):
        self.assertIsNone(start_turn_watchdog(None))

    def test_cancelled_before_it_fires_does_nothing(self):
        previous = install_sigterm_handler()
        try:
            timer = start_turn_watchdog(60)
            timer.cancel()
            time.sleep(0.05)  # long enough that a bug would have already fired
        finally:
            restore_sigterm_handler(previous)

    def test_uncancelled_watchdog_interrupts_a_stuck_turn(self):
        previous = install_sigterm_handler()
        try:
            start_turn_watchdog(0.05)
            with self.assertRaises(RunInterrupted):
                time.sleep(1)
        finally:
            restore_sigterm_handler(previous)


class EventStreamHandlerTests(unittest.TestCase):
    def test_duration_ms_calculated_for_tool_calls(self):
        audit = MagicMock()
        sink = MagicMock()

        call_part = ToolCallPart(tool_name="query_sql", args={"sql": "SELECT 1"}, tool_call_id="call-123")
        return_part = ToolReturnPart(tool_name="query_sql", content={"rows": []}, tool_call_id="call-123")

        call_event = FunctionToolCallEvent(part=call_part)
        result_event = FunctionToolResultEvent(part=return_part)

        async def events():
            yield call_event
            await asyncio.sleep(0.01)
            yield result_event

        handler = make_event_stream_handler(audit, sink)
        asyncio.run(handler(None, events()))

        self.assertEqual(audit.event.call_count, 2)
        call_kwargs = audit.event.call_args_list[1].kwargs
        self.assertEqual(call_kwargs["tool_call_id"], "call-123")
        self.assertEqual(call_kwargs["tool_name"], "query_sql")
        self.assertIsNotNone(call_kwargs["duration_ms"])
        self.assertGreaterEqual(call_kwargs["duration_ms"], 10.0)


if __name__ == "__main__":
    unittest.main()
