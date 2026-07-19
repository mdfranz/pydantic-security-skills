#!/usr/bin/env bash
# Run the same prompt against every model in $MODELS, each in its own fresh --pristine
# workspace (no prior agent state, isolated from every other model's run), with --logfire
# enabled. See refs/workspace-lifecycle.md for what --pristine guarantees.
#
# Usage:
#   MODELS="google:gemini-3-flash-preview openrouter:deepseek/deepseek-v4-pro" \
#     scripts/compare_models.sh "Summarize the ports and protocols seen in the EVE log."
#
# Optional env vars:
#   SKILL_DIR  - skill directory to run (default: skills/suricata-analyst, same as skill-runner)
#   WORKSPACE  - workspace base/case root (default: ./workspace, same as skill-runner)
#
# Any extra arguments after the prompt are passed through to skill-runner as-is, e.g.:
#   ./compare_models.sh "..." --thinking medium

set -uo pipefail

if [ -z "${MODELS:-}" ]; then
  echo "error: set MODELS to a space-separated list of model ids, e.g.:" >&2
  echo "  MODELS=\"google:gemini-3-flash-preview openrouter:deepseek/deepseek-v4-pro\" $0 \"<prompt>\"" >&2
  exit 1
fi

if [ "$#" -lt 1 ]; then
  echo "usage: MODELS=\"model1 model2 ...\" $0 \"<prompt>\" [extra skill-runner args...]" >&2
  exit 1
fi

PROMPT="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

SKILL_DIR="${SKILL_DIR:-skills/suricata-analyst}"
WORKSPACE="${WORKSPACE:-./workspace}"

manifest="$(mktemp)"
run_log="$(mktemp)"
trap 'rm -f "$manifest" "$run_log"' EXIT

model_count=$(wc -w <<<"$MODELS")
echo "Running against $model_count model(s), each in a fresh --pristine workspace under $WORKSPACE"

for model in $MODELS; do
  echo
  echo "=== $model ==="
  uv run skill-runner "$SKILL_DIR" "$PROMPT" \
    --model "$model" \
    --workspace "$WORKSPACE" \
    --pristine \
    --logfire \
    "$@" 2>&1 | tee "$run_log"
  status="${PIPESTATUS[0]}"

  task_id="$(sed -n 's/^Task: \(task-[^ ]*\).*/\1/p' "$run_log" | head -1)"
  if [ "$status" -eq 0 ] && [ -n "$task_id" ]; then
    echo "$model	$task_id	ok" >>"$manifest"
  else
    echo "$model	${task_id:-<none>}	failed (exit $status)" >>"$manifest"
  fi
done

echo
echo "=== Summary ==="
printf "%-40s %-45s %s\n" "MODEL" "TASK" "STATUS"
while IFS=$'\t' read -r model task_id result; do
  printf "%-40s %-45s %s\n" "$model" "$task_id" "$result"
done <"$manifest"
