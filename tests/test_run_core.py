import json
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from skill_runner import data_tools
from skill_runner.audit import AuditLog
from skill_runner.config import RunOptions
from skill_runner.run_core import (
    TaskError,
    _build_agent,
    _prepare_workspace,
    _select_source_root,
    prepare_run,
)


class StatusSink:
    def status(self, message):
        pass


class SelectSourceRootTests(unittest.TestCase):
    def test_prefers_data_source_when_it_already_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            (base / "data-source").mkdir()
            (base / "data").mkdir()
            (base / "data-source" / "only-in-source.json").touch()
            (base / "data" / "only-in-data.json").touch()

            selected = _select_source_root(base)

            self.assertEqual(selected, base / "data-source")
            self.assertTrue((selected / "only-in-source.json").exists())
            self.assertFalse((selected / "only-in-data.json").exists())

    def test_falls_back_to_data_when_data_source_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()

            selected = _select_source_root(base)

            self.assertEqual(selected, base / "data")
            self.assertTrue(selected.is_dir())

    def test_rejects_symlinked_data_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            real_dir = base.parent / f"{base.name}-real-source"
            real_dir.mkdir()
            self.addCleanup(real_dir.rmdir)
            (base / "data-source").symlink_to(real_dir)

            with self.assertRaises(TaskError):
                _select_source_root(base)

    def test_rejects_data_source_as_a_plain_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            (base / "data-source").touch()

            with self.assertRaises(TaskError):
                _select_source_root(base)


class PrepareRunTests(unittest.TestCase):
    def test_post_audit_setup_failure_is_finalized(self):
        previous_handler = signal.getsignal(signal.SIGTERM)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)

            with patch("skill_runner.run_core._build_agent", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    prepare_run("missing-skill", "prompt", options, StatusSink())

            audit_paths = list((workspace / "logs").glob("*.jsonl"))
            self.assertEqual(len(audit_paths), 1)
            records = [json.loads(line) for line in audit_paths[0].read_text().splitlines()]
            self.assertEqual(records[-2]["event"], "setup_error")
            self.assertEqual(records[-1]["event"], "run_end")
            self.assertEqual(records[-1]["status"], "failed")
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)


class DataToolsWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_events_reachable_from_run_code_and_caches_parquet(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)
            skill_dir = workspace / "skill"
            skill_dir.mkdir()

            context = _prepare_workspace(skill_dir, options)
            (context.source_root / "events.json").write_text(
                '{"event_type": "alert"}\n{"event_type": "flow"}\n'
            )

            calls = {"n": 0}

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                calls["n"] += 1
                if calls["n"] in (1, 2):
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="run_code",
                                args={"code": 'query_events(name="events.json", limit=10)'},
                            )
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="done")])

            audit = AuditLog(context.logs_dir, context.task_id, "run-id")
            try:
                agent = _build_agent(context, options, "instructions", audit, StatusSink())
                with patch.object(data_tools.pl, "scan_ndjson", wraps=data_tools.pl.scan_ndjson) as spy:
                    result = await agent.run("go", model=FunctionModel(respond))
            finally:
                audit.close()

            self.assertEqual(result.output, "done")
            self.assertEqual(calls["n"], 3, "expected two run_code calls plus the final text response")
            cached = list(context.parquet_cache_root.glob("*.parquet"))
            self.assertEqual(len(cached), 1, "both queries must share one fingerprint-keyed cache file")
            spy.assert_called_once()  # second call scans the Parquet cache, not the NDJSON source again

    async def test_data_source_and_data_mounts_expose_the_same_files(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)
            skill_dir = workspace / "skill"
            skill_dir.mkdir()

            context = _prepare_workspace(skill_dir, options)
            (context.source_root / "events.json").write_text('{"event_type": "alert"}\n')

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                if len(messages) == 1:
                    code = (
                        "import pathlib\n"
                        "a = pathlib.Path('/data-source/events.json').read_text()\n"
                        "b = pathlib.Path('/data/events.json').read_text()\n"
                        "a == b"
                    )
                    return ModelResponse(parts=[ToolCallPart(tool_name="run_code", args={"code": code})])
                return ModelResponse(parts=[TextPart(content="done")])

            audit = AuditLog(context.logs_dir, context.task_id, "run-id")
            try:
                agent = _build_agent(context, options, "instructions", audit, StatusSink())
                result = await agent.run("go", model=FunctionModel(respond))
            finally:
                audit.close()

            self.assertEqual(result.output, "done")

    async def test_aggregate_events_reachable_from_run_code(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)
            skill_dir = workspace / "skill"
            skill_dir.mkdir()

            context = _prepare_workspace(skill_dir, options)
            (context.source_root / "events.json").write_text(
                '{"event_type": "alert"}\n{"event_type": "flow"}\n{"event_type": "alert"}\n'
            )

            captured: dict[str, object] = {}

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                if len(messages) == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="run_code",
                                args={"code": 'aggregate_events(name="events.json", group_by=["event_type"])'},
                            )
                        ]
                    )
                for part in messages[-1].parts:
                    if isinstance(part, ToolReturnPart):
                        captured["result"] = part.content
                return ModelResponse(parts=[TextPart(content="done")])

            audit = AuditLog(context.logs_dir, context.task_id, "run-id")
            try:
                agent = _build_agent(context, options, "instructions", audit, StatusSink())
                result = await agent.run("go", model=FunctionModel(respond))
            finally:
                audit.close()

            self.assertEqual(result.output, "done")
            self.assertIn("groups", captured.get("result", {}))
            groups = {g["event_type"]: g["count"] for g in captured["result"]["groups"]}
            self.assertEqual(groups, {"alert": 2, "flow": 1})

    async def test_describe_events_reachable_from_run_code(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)
            skill_dir = workspace / "skill"
            skill_dir.mkdir()

            context = _prepare_workspace(skill_dir, options)
            (context.source_root / "events.json").write_text('{"event_type": "alert"}\n')

            captured: dict[str, object] = {}

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                if len(messages) == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="run_code",
                                args={"code": 'describe_events(name="events.json")'},
                            )
                        ]
                    )
                for part in messages[-1].parts:
                    if isinstance(part, ToolReturnPart):
                        captured["result"] = part.content
                return ModelResponse(parts=[TextPart(content="done")])

            audit = AuditLog(context.logs_dir, context.task_id, "run-id")
            try:
                agent = _build_agent(context, options, "instructions", audit, StatusSink())
                result = await agent.run("go", model=FunctionModel(respond))
            finally:
                audit.close()

            self.assertEqual(result.output, "done")
            names = {col["name"] for col in captured["result"]["columns"]}
            self.assertIn("event_type", names)

    async def test_query_events_equals_filter_reachable_from_run_code(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)
            skill_dir = workspace / "skill"
            skill_dir.mkdir()

            context = _prepare_workspace(skill_dir, options)
            (context.source_root / "events.json").write_text(
                '{"event_type": "alert", "proto": "TCP"}\n{"event_type": "alert", "proto": "UDP"}\n'
            )

            captured: dict[str, object] = {}

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                if len(messages) == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="run_code",
                                args={
                                    "code": (
                                        'query_events(name="events.json", '
                                        'equals={"proto": "UDP"})'
                                    )
                                },
                            )
                        ]
                    )
                for part in messages[-1].parts:
                    if isinstance(part, ToolReturnPart):
                        captured["result"] = part.content
                return ModelResponse(parts=[TextPart(content="done")])

            audit = AuditLog(context.logs_dir, context.task_id, "run-id")
            try:
                agent = _build_agent(context, options, "instructions", audit, StatusSink())
                result = await agent.run("go", model=FunctionModel(respond))
            finally:
                audit.close()

            self.assertEqual(result.output, "done")
            self.assertEqual(captured["result"]["returned"], 1)
            self.assertEqual(captured["result"]["rows"][0]["proto"], "UDP")

    async def test_query_sql_reachable_from_run_code_and_shares_parquet_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)
            skill_dir = workspace / "skill"
            skill_dir.mkdir()

            context = _prepare_workspace(skill_dir, options)
            (context.source_root / "events.json").write_text(
                '{"event_type": "alert"}\n{"event_type": "flow"}\n{"event_type": "alert"}\n'
            )

            calls = {"n": 0}
            captured: dict[str, object] = {}

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                calls["n"] += 1
                if calls["n"] == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="run_code",
                                args={"code": 'query_events(name="events.json", limit=1)'},
                            )
                        ]
                    )
                if calls["n"] == 2:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="run_code",
                                args={
                                    "code": (
                                        'query_sql(name="events.json", '
                                        'sql="SELECT event_type, count(*) AS n FROM events '
                                        'GROUP BY event_type ORDER BY n DESC")'
                                    )
                                },
                            )
                        ]
                    )
                for part in messages[-1].parts:
                    if isinstance(part, ToolReturnPart):
                        captured["result"] = part.content
                return ModelResponse(parts=[TextPart(content="done")])

            audit = AuditLog(context.logs_dir, context.task_id, "run-id")
            try:
                agent = _build_agent(context, options, "instructions", audit, StatusSink())
                result = await agent.run("go", model=FunctionModel(respond))
            finally:
                audit.close()

            self.assertEqual(result.output, "done")
            cached = list(context.parquet_cache_root.glob("*.parquet"))
            self.assertEqual(len(cached), 1, "query_events and query_sql must share one cache file")
            rows = {row["event_type"]: row["n"] for row in captured["result"]["rows"]}
            self.assertEqual(rows, {"alert": 2, "flow": 1})

    async def test_query_sql_traversal_attempt_is_retried_not_crashed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)
            skill_dir = workspace / "skill"
            skill_dir.mkdir()

            context = _prepare_workspace(skill_dir, options)
            (context.source_root / "events.json").write_text('{"event_type": "alert"}\n')

            calls = {"n": 0}

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                calls["n"] += 1
                if calls["n"] == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="run_code",
                                args={
                                    "code": (
                                        'query_sql(name="events.json", '
                                        "sql=\"SELECT * FROM read_parquet('/etc/passwd')\")"
                                    )
                                },
                            )
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="recovered")])

            audit = AuditLog(context.logs_dir, context.task_id, "run-id")
            try:
                agent = _build_agent(context, options, "instructions", audit, StatusSink())
                result = await agent.run("go", model=FunctionModel(respond))
            finally:
                audit.close()

            self.assertEqual(result.output, "recovered")
            self.assertEqual(calls["n"], 2, "the model should get retry feedback, not a crash")

    async def test_query_events_traversal_attempt_is_retried_not_crashed(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            options = RunOptions(model="test", workspace=workspace)
            skill_dir = workspace / "skill"
            skill_dir.mkdir()

            context = _prepare_workspace(skill_dir, options)
            (context.source_root / "events.json").write_text('{"event_type": "alert"}\n')

            calls = {"n": 0}

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                calls["n"] += 1
                if calls["n"] == 1:
                    return ModelResponse(
                        parts=[
                            ToolCallPart(
                                tool_name="run_code",
                                args={"code": 'query_events(name="../etc/passwd")'},
                            )
                        ]
                    )
                return ModelResponse(parts=[TextPart(content="recovered")])

            audit = AuditLog(context.logs_dir, context.task_id, "run-id")
            try:
                agent = _build_agent(context, options, "instructions", audit, StatusSink())
                result = await agent.run("go", model=FunctionModel(respond))
            finally:
                audit.close()

            self.assertEqual(result.output, "recovered")
            self.assertEqual(calls["n"], 2, "the model should get retry feedback, not a crash")


if __name__ == "__main__":
    unittest.main()
