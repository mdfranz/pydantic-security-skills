"""Structured run transcripts and durable report/code artifact persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)


@dataclass(frozen=True)
class Turn:
    """One successful agent turn, including both user and effective prompt forms."""

    submitted_prompt: str
    effective_prompt: str
    output: str
    message_history: list[ModelMessage]
    model: str


@dataclass
class Transcript:
    """Valid-by-construction sequence of successful turns for artifact rendering."""

    initial_prompt: str | None
    interactive: bool = False
    turns: list[Turn] = field(default_factory=list)

    def append(self, turn: Turn) -> None:
        self.turns.append(turn)

    @property
    def prompts(self) -> list[str]:
        return [turn.effective_prompt for turn in self.turns]

    @property
    def outputs(self) -> list[str]:
        return [turn.output for turn in self.turns]

    @property
    def message_history(self) -> list[ModelMessage]:
        return self.turns[-1].message_history if self.turns else []


def format_conversation_history(message_history: list[ModelMessage], prompt: str) -> str:
    """Format the full agent conversation including all tool calls and results."""
    lines = ["## Conversation History\n", "### User Prompt\n", f"```\n{prompt}\n```\n"]
    lines.append("### Agent Communication\n")

    for message in message_history:
        if isinstance(message, ModelRequest):
            continue
        if not isinstance(message, ModelResponse):
            continue
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                if part.tool_name == "run_code":
                    lines.append(f"#### Tool Call: `run_code` (ID: {part.tool_call_id})\n")
                    lines.append("(Code saved to generated_code/ directory)\n\n")
                else:
                    lines.append(f"#### Tool Call: `{part.tool_name}` (ID: {part.tool_call_id})\n")
                    if part.args:
                        try:
                            args = part.args_as_dict()
                            lines.append(f"```json\n{json.dumps(args, indent=2)}\n```\n")
                        except Exception:
                            lines.append(f"```\n{part.args}\n```\n")
            elif isinstance(part, ToolReturnPart):
                if part.tool_name != "run_code":
                    lines.append(f"#### Tool Result: `{part.tool_name}` (ID: {part.tool_call_id})\n")
                    content = part.content
                    if len(str(content)) > 1000:
                        content = str(content)[:1000] + "\n... (truncated)"
                    lines.append(f"```\n{content}\n```\n")
            elif isinstance(part, ThinkingPart):
                if part.content.strip():
                    quoted = part.content.strip().replace("\n", "\n> ")
                    lines.append(f"**Agent (Thinking):**\n> {quoted}\n\n")
            elif hasattr(part, "content") and str(part.content).strip():
                lines.append(f"**Agent:** {part.content}\n\n")

    return "".join(lines)


def render_report(*, run_stamp: str, skill_name: str, transcript: Transcript) -> str:
    """Render an analyst report without performing filesystem I/O."""
    prompts = transcript.prompts
    outputs = transcript.outputs
    first_prompt = prompts[0] if prompts else (transcript.initial_prompt or "")
    conversation = format_conversation_history(transcript.message_history, first_prompt)

    if transcript.interactive and len(outputs) > 1:
        combined_findings = "\n\n---\n\n".join(
            f"### Checkpoint {index + 1}\n{output}" for index, output in enumerate(outputs)
        )
        findings_section = f"## Analysis Checkpoints\n\n{combined_findings}\n"
    elif not transcript.interactive and len(outputs) > 1:
        combined_findings = "\n\n---\n\n".join(
            f"### Turn {index + 1}\n{output}" for index, output in enumerate(outputs)
        )
        findings_section = f"## Analysis Turns\n\n{combined_findings}\n"
    else:
        findings_section = f"## Final Findings\n\n{outputs[-1] if outputs else ''}\n"

    if transcript.interactive or len(prompts) <= 1:
        mode_label = "Interactive (multiple checkpoints)" if transcript.interactive else "Standard"
        header = (
            "# Analysis Report\n\n"
            f"- Skill: `{skill_name}`\n"
            f"- Prompt: {transcript.initial_prompt}\n"
            f"- Run: `{run_stamp}`\n"
            f"- Mode: {mode_label}\n\n"
        )
    else:
        prompts_list = "\n".join(f"{index + 1}. {prompt}" for index, prompt in enumerate(prompts))
        header = (
            "# Analysis Report\n\n"
            f"- Skill: `{skill_name}`\n"
            "- Mode: Textual (multi-turn session)\n"
            f"- Run: `{run_stamp}`\n"
            f"- Prompts:\n{prompts_list}\n\n"
        )

    return f"{header}{conversation}\n{findings_section}"


def write_artifacts(
    *,
    ws_path: Path,
    run_stamp: str,
    skill_name: str,
    transcript: Transcript,
    debug: bool,
    on_message: Callable[[str], None] = print,
) -> None:
    """Persist the current transcript report and generated run_code programs."""
    report_path = ws_path / f"analyst_log-{run_stamp}.md"
    report_path.write_text(
        render_report(run_stamp=run_stamp, skill_name=skill_name, transcript=transcript),
        encoding="utf-8",
    )
    on_message(f"\nSaved analysis report: {report_path}")

    generated_dir = ws_path / "generated_code"
    generated_dir.mkdir(exist_ok=True)
    code_index = 0
    for message in transcript.message_history:
        for part in message.parts:
            if isinstance(part, ToolCallPart) and part.tool_name == "run_code":
                code = part.args_as_dict().get("code") if part.args else None
                if code:
                    code_index += 1
                    code_path = generated_dir / f"{run_stamp}-{code_index:02d}.py"
                    code_path.write_text(code, encoding="utf-8")
                    if debug:
                        on_message(f"\n[call {part.tool_call_id}]\n{code}")
            elif isinstance(part, ToolReturnPart) and part.tool_name == "run_code" and debug:
                on_message(f"\n[return {part.tool_call_id}]\n{part.content}")
    if code_index:
        on_message(f"Saved {code_index} generated run_code artifact(s) in {generated_dir}")
