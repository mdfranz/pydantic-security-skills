import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai_harness import CodeMode, FileSystem
from pydantic_monty import MountDir, OSAccess

SANDBOX_WORKSPACE_MOUNT = "/workspace"
SANDBOX_SKILL_MOUNT = "/skill"


class TeeWriter:
    """Write console output to both the terminal and the run transcript."""

    def __init__(self, console, transcript):
        self.console = console
        self.transcript = transcript

    def write(self, data):
        self.console.write(data)
        self.transcript.write(data)
        self.transcript.flush()

    def flush(self):
        self.console.flush()
        self.transcript.flush()

    def isatty(self):
        return self.console.isatty()

def load_skill(skill_dir: Path):
    yaml_path = skill_dir / "skill.yaml"
    md_path = skill_dir / "SKILL.md"

    config = {}
    if yaml_path.exists():
        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f)
    if md_path.exists():
        with open(md_path, "r") as f:
            config["instructions"] = f.read()

    return config

def format_conversation_history(result, prompt: str) -> str:
    """Format the full agent conversation including all tool calls and results."""
    lines = ["## Conversation History\n"]

    # User prompt
    lines.append("### User Prompt\n")
    lines.append(f"```\n{prompt}\n```\n")

    # Messages and tool interactions
    lines.append("### Agent Communication\n")

    for msg in result.all_messages():
        if isinstance(msg, ModelRequest):
            # Skip showing request context to keep logs concise
            pass
        elif isinstance(msg, ModelResponse):
            # Show model response and tool interactions
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    # Skip run_code calls since the generated code is saved to disk
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
                    # Skip run_code returns to keep logs concise
                    if part.tool_name != "run_code":
                        lines.append(f"#### Tool Result: `{part.tool_name}` (ID: {part.tool_call_id})\n")
                        # Truncate very long outputs
                        content = part.content
                        if len(str(content)) > 1000:
                            content = str(content)[:1000] + "\n... (truncated)"
                        lines.append(f"```\n{content}\n```\n")
                else:
                    # Text content from model
                    if hasattr(part, 'content') and str(part.content).strip():
                        lines.append(f"**Agent:** {part.content}\n\n")

    return "".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Pydantic AI Security Skill Runner")
    parser.add_argument(
        "skill_dir", nargs="?", default="skills/suricata-analyst", help="Path to skill directory"
    )
    parser.add_argument("prompt", help="Query prompt for the agent")
    parser.add_argument("--model", default="google:gemini-3-flash-preview", help="Model ID")
    parser.add_argument("--workspace", default="./workspace", help="Workspace path")
    parser.add_argument("--debug", action="store_true", help="Print generated sandbox code and its result")
    parser.add_argument(
        "--logfire",
        action="store_true",
        help="Trace the run with Logfire. Prints spans to the console with no setup; "
        "also ships to the Logfire UI if LOGFIRE_TOKEN is set (or `logfire auth` has been run).",
    )
    args = parser.parse_args()

    ws_path = Path(args.workspace).resolve()
    ws_path.mkdir(parents=True, exist_ok=True)

    run_stamp = datetime.now().strftime("%y-%m-%d_%H-%M-%S")
    transcript_path = ws_path / f"runner-{run_stamp}.log"
    transcript_file = transcript_path.open("a", encoding="utf-8")

    # Keep the interactive output while retaining the complete run for later
    # inspection. This includes model responses, tool calls, and tool results.
    sys.stdout = TeeWriter(sys.stdout, transcript_file)
    sys.stderr = TeeWriter(sys.stderr, transcript_file)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(transcript_path, mode="a", encoding="utf-8")],
    )

    if args.logfire:
        import logfire

        logfire.configure()
        logfire.instrument_pydantic_ai()

    skill_path = Path(args.skill_dir)
    config = load_skill(skill_path)
    instructions = config.get("instructions", "You are a security analyst.")

    agent = Agent(
        args.model,
        system_prompt=instructions,
        capabilities=[
            FileSystem(root_dir=str(ws_path)),
            CodeMode(
                tools=[],  # Keep FileSystem tools native so they're callable without run_code
                mount=[
                    MountDir(SANDBOX_WORKSPACE_MOUNT, str(ws_path), mode="read-write"),
                    # Read-only so generated code can consult a skill's reference material
                    # (e.g. references/*.md) without being able to modify the skill itself.
                    MountDir(SANDBOX_SKILL_MOUNT, str(skill_path.resolve()), mode="read-only"),
                ],
                # Empty environ keeps host env vars isolated; only the host clock is exposed,
                # so generated code can timestamp filenames per the skill's naming convention.
                os_access=OSAccess(environ={}),
            ),
        ],
    )

    existing_scripts = sorted(p.name for p in ws_path.glob("*.py"))
    run_prompt = args.prompt
    if existing_scripts:
        inventory = "\n".join(f"- {name}" for name in existing_scripts)
        run_prompt = (
            "Reusable scripts already saved in the workspace from earlier sessions (read one "
            "with the FileSystem tool and adapt it before writing new analysis code from "
            f"scratch, per the skill instructions):\n{inventory}\n\n{args.prompt}"
        )
        print(f"Found {len(existing_scripts)} existing script(s) in workspace: {', '.join(existing_scripts)}")

    try:
        print(f"Running Pydantic AI agent on skill: {skill_path.name}")
        result = agent.run_sync(run_prompt)
        print("\n--- Agent Response ---")
        print(result.output)

        # Save the full conversation and findings independently of the transcript
        report_path = ws_path / f"analyst_log-{run_stamp}.md"
        conversation = format_conversation_history(result, run_prompt)
        report_path.write_text(
            f"# Analysis Report\n\n"
            f"- Skill: `{skill_path.name}`\n"
            f"- Prompt: {args.prompt}\n"
            f"- Run: `{run_stamp}`\n\n"
            f"{conversation}\n"
            f"## Final Findings\n\n{result.output}\n",
            encoding="utf-8",
        )
        print(f"\nSaved analysis report: {report_path}")

        # Persist every generated run_code program. These are audit artifacts;
        # reusable analysis scripts should still be written by the agent with
        # FileSystem.write_file using meaningful names.
        generated_dir = ws_path / "generated_code"
        generated_dir.mkdir(exist_ok=True)
        code_index = 0
        for msg in result.all_messages():
            for part in msg.parts:
                if isinstance(part, ToolCallPart) and part.tool_name == "run_code":
                    code = part.args_as_dict().get("code") if part.args else None
                    if code:
                        code_index += 1
                        code_path = generated_dir / f"{run_stamp}-{code_index:02d}.py"
                        code_path.write_text(code, encoding="utf-8")
                        if args.debug:
                            print(f"\n[call {part.tool_call_id}]\n{code}")
                elif isinstance(part, ToolReturnPart) and part.tool_name == "run_code" and args.debug:
                    print(f"\n[return {part.tool_call_id}]\n{part.content}")
        if code_index:
            print(f"Saved {code_index} generated run_code artifact(s) in {generated_dir}")

    finally:
        transcript_file.flush()
        transcript_file.close()

if __name__ == "__main__":
    main()
