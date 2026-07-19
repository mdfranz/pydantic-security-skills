#!/usr/bin/env bash
#
# Wrapper script to run a model in interactive mode and prompt the user for the query.
#
# Usage:
#   ./run.sh <model_id> [skill_dir] [additional_args...]
#
# Example:
#   ./run.sh google:gemini-3.5-flash
#

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <model_id> [skill_dir] [additional_args...]" >&2
  exit 1
fi

MODEL="$1"
SKILL_DIR="skills/suricata-analyst"

# Check if the second argument is a directory (indicating it's the skill_dir)
if [ "$#" -ge 2 ] && [ -d "$2" ]; then
  SKILL_DIR="$2"
  shift 2
else
  shift 1
fi

# Resolve repository root and change directory to it
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Read the prompt from stdin/user
if [ -t 0 ]; then
  # Interactive input
  echo -n "Enter prompt for the agent: "
  read -r prompt
else
  # Piped/redirected input
  prompt=$(cat)
fi

if [ -z "${prompt:-}" ]; then
  echo "Error: Prompt cannot be empty." >&2
  exit 1
fi

# Run the runner in interactive mode using uv, passing any additional arguments along
uv run skill-runner "$SKILL_DIR" "$prompt" --model "$MODEL" --interactive "$@"
