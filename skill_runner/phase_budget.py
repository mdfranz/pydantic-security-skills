"""Runner-enforced phase boundaries for interactive analysis sessions.

Prompt-only checkpoints are advisory: a model can keep emitting tool calls before an
``agent.run`` invocation returns control to either UI.  This capability makes the first
interactive discovery pass a real boundary by allowing only a small number of sandbox
executions, then returning a normal checkpoint instead of a usage-limit failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable

from pydantic_ai import _agent_graph
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.exceptions import SkipToolExecution
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import RunContext, ToolDefinition
from pydantic_graph import End

from .audit import AuditLog


INTERACTIVE_PHASE_METADATA_KEY = "interactive_phase"
INITIAL_INTERACTIVE_PHASE = "initial"
INITIAL_INTERACTIVE_RUN_CODE_LIMIT = 2


@dataclass
class InteractivePhaseBudget(AbstractCapability[Any]):
    """Cap sandbox calls in the first interactive turn and force a usable checkpoint.

    ``for_run`` returns a fresh instance so concurrent agent runs never share counters.
    Later interactive follow-ups deliberately remain uncapped in this first MVP: their
    scope is selected by the user at the preceding checkpoint.
    """

    audit: AuditLog
    status: Callable[[str], None]
    run_code_limit: int = INITIAL_INTERACTIVE_RUN_CODE_LIMIT
    _enabled: bool = field(default=False, init=False, repr=False)
    _run_code_calls: int = field(default=0, init=False, repr=False)
    _budget_reached: bool = field(default=False, init=False, repr=False)
    _checkpoint_forced: bool = field(default=False, init=False, repr=False)

    def get_ordering(self) -> CapabilityOrdering:
        """Run outside CodeMode so a denied call never reaches the sandbox."""
        return CapabilityOrdering(position="outermost")

    async def for_run(self, ctx: RunContext[Any]) -> InteractivePhaseBudget:
        phase = (ctx.metadata or {}).get(INTERACTIVE_PHASE_METADATA_KEY)
        per_run = replace(self)
        per_run._enabled = phase == INITIAL_INTERACTIVE_PHASE
        return per_run

    async def before_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._enabled or call.tool_name != "run_code":
            return args

        if self._run_code_calls < self.run_code_limit:
            self._run_code_calls += 1
            return args

        if not self._budget_reached:
            self._budget_reached = True
            self.audit.event(
                "phase_budget_reached",
                phase=INITIAL_INTERACTIVE_PHASE,
                run_code_calls=self._run_code_calls,
                run_code_limit=self.run_code_limit,
            )
            self.status(
                "Initial discovery budget reached after "
                f"{self.run_code_limit} run_code call(s); returning a checkpoint."
            )

        raise SkipToolExecution(self._tool_budget_message())

    async def wrap_node_run(
        self,
        ctx: RunContext[Any],
        *,
        node: _agent_graph.AgentNode[Any, Any],
        handler,
    ):
        """End only if the post-budget model response tries to call another tool.

        The skipped call normally causes the model to write its own concise checkpoint on
        the next response.  If it instead tries another tool, return a deterministic
        checkpoint so the UI can regain input without another loop iteration.
        """
        if (
            self._enabled
            and self._budget_reached
            and isinstance(node, _agent_graph.CallToolsNode)
            and any(isinstance(part, ToolCallPart) for part in node.model_response.parts)
        ):
            if not self._checkpoint_forced:
                self._checkpoint_forced = True
                self.audit.event(
                    "phase_checkpoint_forced",
                    phase=INITIAL_INTERACTIVE_PHASE,
                    run_code_calls=self._run_code_calls,
                    run_code_limit=self.run_code_limit,
                )
            return End(FinalResult(output=self._automatic_checkpoint()))
        return await handler(node)

    def _tool_budget_message(self) -> str:
        return (
            "Interactive initial-discovery budget reached. Do not call more tools in this "
            "turn. Return a concise checkpoint with completed findings, the evidence limits, "
            "and the next proposed question for the user."
        )

    def _automatic_checkpoint(self) -> str:
        return (
            "Automatic checkpoint: the interactive initial-discovery budget was reached "
            f"after {self.run_code_limit} run_code calls. Completed tool results are preserved "
            "above. Continue to begin the next phase, or focus the next phase on a specific question."
        )
