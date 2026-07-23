import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import FunctionToolResultEvent, ToolCallPart, ToolReturn, ToolReturnPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_ai.usage import RunUsage

from skill_runner.audit import make_event_stream_handler
from skill_runner.resilience import (
    TOOL_OUTPUT_OVERFLOW_CHARS,
    build_overflow_capability,
    retry_rate_limited_request,
)


class ProviderRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_one_429_using_bounded_provider_delay(self):
        error = ModelHTTPError(
            429,
            "openrouter:moonshotai/kimi-k3",
            {"metadata": {"retry_after_seconds": 45}},
        )
        responses = [error, "ok"]

        async def handler(request_context):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        on_retry = Mock()
        with patch("skill_runner.resilience.anyio.sleep") as sleep:
            result = await retry_rate_limited_request("request", handler, on_retry=on_retry)

        self.assertEqual(result, "ok")
        sleep.assert_awaited_once_with(30.0)
        on_retry.assert_called_once_with(error, 1, 30.0)

    async def test_second_429_is_not_retried(self):
        error = ModelHTTPError(429, "openrouter:moonshotai/kimi-k3", {})
        handler = Mock(side_effect=[error, error])

        with patch("skill_runner.resilience.anyio.sleep") as sleep:
            with self.assertRaises(ModelHTTPError):
                await retry_rate_limited_request("request", handler)

        self.assertEqual(handler.call_count, 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_non_429_is_not_retried(self):
        error = ModelHTTPError(500, "provider:model", {})
        handler = Mock(side_effect=error)

        with patch("skill_runner.resilience.anyio.sleep") as sleep:
            with self.assertRaises(ModelHTTPError):
                await retry_rate_limited_request("request", handler)

        handler.assert_called_once_with("request")
        sleep.assert_not_awaited()


class OverflowCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_return_is_replaced_before_model_history(self):
        with tempfile.TemporaryDirectory() as directory:
            capability = build_overflow_capability(Path(directory) / "logs", "task-a")
            context = RunContext(
                deps=None,
                model=TestModel(),
                usage=RunUsage(),
                run_id="run-1",
            )
            result = await capability.after_tool_execute(
                context,
                call=ToolCallPart("run_code", {}, tool_call_id="call-1"),
                tool_def=ToolDefinition(name="run_code"),
                args={},
                result=ToolReturn(return_value={"output": "x" * 20_000}),
            )

            self.assertIsInstance(result, ToolReturn)
            self.assertLess(len(result.return_value), 2_000)
            self.assertIn("Tool output too large", result.return_value)
            self.assertEqual(result.metadata["overflow_handle"], "run-1/call-1.0")
            spilled = await capability.store.read(result.metadata["overflow_handle"])
            self.assertGreater(len(spilled), 20_000)

    async def test_data_tools_are_exempt_from_overflow_reduction(self):
        with tempfile.TemporaryDirectory() as directory:
            capability = build_overflow_capability(Path(directory) / "logs", "task-a")
            context = RunContext(
                deps=None,
                model=TestModel(),
                usage=RunUsage(),
                run_id="run-1",
            )
            oversized = {"rows": [{"sni": "x" * 20_000}], "offset": 0, "returned": 1, "has_more": False}

            for tool_name in ("query_events", "aggregate_events"):
                result = await capability.after_tool_execute(
                    context,
                    call=ToolCallPart(tool_name, {}, tool_call_id="call-1"),
                    tool_def=ToolDefinition(name=tool_name),
                    args={},
                    result=oversized,
                )
                # Untouched: still the original dict, not a spill-preview string --
                # sandboxed code calling query_events(...)["rows"] must never see a str here.
                self.assertIs(result, oversized)

    async def test_spill_store_is_task_scoped_and_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            logs_dir = Path(directory) / "logs"
            capability = build_overflow_capability(logs_dir, "task-a")

            self.assertEqual(capability.bands[0].over, TOOL_OUTPUT_OVERFLOW_CHARS)
            self.assertEqual(capability.store.base_dir, logs_dir / "overflow" / "task-a")

            handle = await capability.store.write("run/call.0", b"complete output")
            self.assertEqual(handle, "run/call.0")
            self.assertEqual(
                stat.S_IMODE((logs_dir / "overflow" / "task-a").stat().st_mode),
                0o700,
            )
            self.assertEqual(
                (logs_dir / "overflow" / "task-a" / "run" / "call.0").read_bytes(),
                b"complete output",
            )


class OverflowAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_spill_handle_and_size_are_forwarded_to_audit_and_sink(self):
        audit = Mock()
        sink = Mock()
        event = FunctionToolResultEvent(
            ToolReturnPart(
                tool_name="run_code",
                tool_call_id="call-1",
                content="bounded preview",
                metadata={
                    "overflow_handle": "run/call-1.0",
                    "overflow_bytes": 24_000_000,
                    "unrelated": object(),
                },
            )
        )

        async def events():
            yield event

        await make_event_stream_handler(audit, sink)(None, events())

        expected = {
            "tool_call_id": "call-1",
            "tool_name": "run_code",
            "duration_ms": None,
            "content": "bounded preview",
            "overflow_handle": "run/call-1.0",
            "overflow_bytes": 24_000_000,
        }
        audit.event.assert_called_once_with("run_code_return", **expected)
        sink.emit.assert_called_once_with("run_code_return", **expected)


if __name__ == "__main__":
    unittest.main()
