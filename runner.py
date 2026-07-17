import argparse
from pathlib import Path

import yaml
from pydantic_ai import Agent
from pydantic_ai.messages import ToolCallPart, ToolReturnPart
from pydantic_ai_harness import CodeMode, FileSystem

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

    if args.logfire:
        import logfire

        logfire.configure()
        logfire.instrument_pydantic_ai()

    skill_path = Path(args.skill_dir)
    config = load_skill(skill_path)
    instructions = config.get("instructions", "You are a security analyst.")

    ws_path = Path(args.workspace).resolve()
    ws_path.mkdir(exist_ok=True)

    agent = Agent(
        args.model,
        system_prompt=instructions,
        capabilities=[
            FileSystem(root_dir=str(ws_path)),
            CodeMode(),
        ],
    )

    print(f"Running Pydantic AI agent on skill: {skill_path.name}")
    result = agent.run_sync(args.prompt)
    print("\n--- Agent Response ---")
    print(result.output)

    if args.debug:
        print("\n--- Sandbox run_code calls ---")
        for msg in result.all_messages():
            for part in msg.parts:
                if isinstance(part, ToolCallPart) and part.tool_name == "run_code":
                    code = part.args_as_dict().get("code") if part.args else None
                    print(f"\n[call {part.tool_call_id}]\n{code}")
                elif isinstance(part, ToolReturnPart) and part.tool_name == "run_code":
                    print(f"\n[return {part.tool_call_id}]\n{part.content}")

if __name__ == "__main__":
    main()
