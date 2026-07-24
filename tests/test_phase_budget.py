import asyncio
import unittest
from types import SimpleNamespace

from pydantic_ai.exceptions import SkipToolExecution
from pydantic_ai._agent_graph import CallToolsNode
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_graph import End

from skill_runner.phase_budget import (
    FOLLOWUP_INTERACTIVE_PHASE,
    INITIAL_INTERACTIVE_PHASE,
    INTERACTIVE_PHASE_METADATA_KEY,
    InteractivePhaseBudget,
)


class FakeAudit:
    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))


class InteractivePhaseBudgetTests(unittest.TestCase):
    def make_budget(self):
        self.audit = FakeAudit()
        self.statuses = []
        return InteractivePhaseBudget(audit=self.audit, status=self.statuses.append)

    def test_initial_phase_allows_two_run_code_calls_then_skips(self):
        budget = asyncio.run(
            self.make_budget().for_run(
                SimpleNamespace(metadata={INTERACTIVE_PHASE_METADATA_KEY: INITIAL_INTERACTIVE_PHASE})
            )
        )
        call = ToolCallPart(tool_name="run_code", args={"code": "pass"})

        self.assertEqual(asyncio.run(budget.before_tool_execute(None, call=call, tool_def=None, args={})), {})
        self.assertEqual(asyncio.run(budget.before_tool_execute(None, call=call, tool_def=None, args={})), {})
        with self.assertRaises(SkipToolExecution) as raised:
            asyncio.run(budget.before_tool_execute(None, call=call, tool_def=None, args={}))
        self.assertIn("budget reached", str(raised.exception.result))

        self.assertEqual(
            self.audit.events,
            [
                (
                    "phase_budget_reached",
                    {"phase": "initial", "run_code_calls": 2, "run_code_limit": 2},
                )
            ],
        )
        self.assertEqual(len(self.statuses), 1)

    def test_followup_phase_allows_four_run_code_calls_then_skips(self):
        budget = asyncio.run(
            self.make_budget().for_run(
                SimpleNamespace(metadata={INTERACTIVE_PHASE_METADATA_KEY: FOLLOWUP_INTERACTIVE_PHASE})
            )
        )
        call = ToolCallPart(tool_name="run_code", args={"code": "pass"})

        for _ in range(4):
            self.assertEqual(asyncio.run(budget.before_tool_execute(None, call=call, tool_def=None, args={})), {})
        with self.assertRaises(SkipToolExecution):
            asyncio.run(budget.before_tool_execute(None, call=call, tool_def=None, args={}))

        self.assertEqual(
            self.audit.events,
            [
                (
                    "phase_budget_reached",
                    {"phase": "followup", "run_code_calls": 4, "run_code_limit": 4},
                )
            ],
        )

    def test_non_sandbox_tool_does_not_consume_budget(self):
        budget = asyncio.run(
            self.make_budget().for_run(
                SimpleNamespace(metadata={INTERACTIVE_PHASE_METADATA_KEY: INITIAL_INTERACTIVE_PHASE})
            )
        )
        call = ToolCallPart(tool_name="list_directory", args={"path": "."})

        for _ in range(3):
            self.assertEqual(asyncio.run(budget.before_tool_execute(None, call=call, tool_def=None, args={})), {})

        self.assertEqual(self.audit.events, [])

    def test_repeated_post_budget_tool_call_becomes_automatic_checkpoint(self):
        budget = asyncio.run(
            self.make_budget().for_run(
                SimpleNamespace(metadata={INTERACTIVE_PHASE_METADATA_KEY: INITIAL_INTERACTIVE_PHASE})
            )
        )
        budget._budget_reached = True
        node = CallToolsNode(ModelResponse(parts=[ToolCallPart(tool_name="run_code", args={"code": "pass"})]))

        async def should_not_run(_node):
            self.fail("a post-budget tool call must not execute")

        result = asyncio.run(budget.wrap_node_run(None, node=node, handler=should_not_run))

        self.assertIsInstance(result, End)
        self.assertIn("Automatic checkpoint", result.data.output)
        self.assertEqual(
            self.audit.events,
            [
                (
                    "phase_checkpoint_forced",
                    {"phase": "initial", "run_code_calls": 0, "run_code_limit": 2},
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
