# Does the Agent Actually Review Prior `analyst_log-*.md` Reports? — qwen3.6-flash vs gemini-3.5-flash

**Date:** 2026-07-19
**Prompt:** "Summarize what you've learnd so far"
**Skill:** `skills/suricata-analyst`
**Task:** `default` (accumulate mode, same workspace/state for both runs, ~3 minutes apart)
**Command:** `uv run runner.py --model <model> "Summarize what you've learnd so far"`

Both runs hit the same accumulated `default` task workspace: an existing `MEMORY.md` notebook (per-task, auto-injected into the system prompt) and six `analyst_log-*.md` reports already on disk from prior sessions. Same prompt, same available reusable scripts, same input file (`eve-2026-01-06-01.json`). Not a controlled benchmark — one trial per model — but the two runs are close enough in time and state to be a fair like-for-like comparison of *tool-use behavior*, which is what this note is about.

## Executive summary

Given an identical "summarize what you've learned" prompt, **`openrouter:qwen/qwen3.6-flash` made zero tool calls** and answered entirely from the `Memory` capability's auto-injected `MEMORY.md` notebook content already sitting in its system prompt. **`google:gemini-3.5-flash` actually went and looked**: it called `list_directory`, attempted a sandboxed `glob()` over the workspace (which failed — see below), then explicitly `read_file`'d the most recent `analyst_log-*.md` report before answering.

Both answers were plausible-sounding and consistent with what's actually in `MEMORY.md`. But only gemini-3.5-flash did what `SKILL.md` actually instructs ("call `list_directory`... to check the task workspace itself for any existing... `analyst_log-*.md` reports... before writing anything new") — qwen3.6-flash's own thinking trace even said *"let me check if there are any separate files referenced"* and then never did.

## Evidence

### qwen3.6-flash — `workspace/logs/runner-default-11109616909c445298e185bb1af5bae5.jsonl`

```
run_start  (12:23:45 UTC, model: openrouter:qwen/qwen3.6-flash)
prompt
model_thinking   "...let me check if there are any separate files referenced."
model_text       <full summary, answered directly>
run_end    (12:23:51 UTC, status: completed)
```

No `tool_call`, `run_code_call`, `tool_result`, or `run_code_return` events at all — a 6-second run with the entire response generated from context already present in the system prompt (the `Memory` capability's bounded auto-injection of `workspace/memory/suricata-analyst/default/MEMORY.md`).

### gemini-3.5-flash — `workspace/logs/runner-default-19b185e0a43b4b07a64b2f9bb5007c53.jsonl`

```
run_start   (12:26:44 UTC, model: google:gemini-3.5-flash)
prompt
tool_call   list_directory
tool_result list_directory → analyst_log-26-07-18_22-31-09.md (6768 bytes), analyst_log-26-07-19_07-34-20.md (8581 bytes), ... (6 files)
run_code_call:
    import pathlib
    workspace_path = pathlib.Path("/workspace")
    md_files = sorted(workspace_path.glob("analyst_log-*.md"))
    for f in md_files:
        print(f"{f.name}: {f.stat().st_size} bytes")
run_code_return:
    Runtime error:
    AttributeError: 'PosixPath' object has no attribute 'glob'
tool_call   read_file   path='analyst_log-26-07-19_08-12-48.md', offset=0, limit=2000
tool_result read_file → full 827-line prior report
model_text  <summary, grounded in the file it just read>
run_end     (12:27:08 UTC, status: completed)
```

A 24-second run that started with the same instinct as qwen3.6-flash's thinking trace, but actually acted on it: `list_directory` first, then tried to `glob()` for `analyst_log-*.md` from inside the Monty sandbox — which failed, because **Monty's restricted `pathlib` subset doesn't implement `Path.glob()`** (a real environment limitation, not a model mistake). It recovered gracefully and fell back to the native `read_file` tool instead, reading the most recent report by name.

## Takeaways

- **`MEMORY.md` alone can produce a plausible-sounding "summary of prior work" without the model ever touching a file.** For this prompt shape, that's mostly fine since qwen3.6-flash's answer matched `MEMORY.md`'s actual content — but it means the summary is only as fresh/accurate as whatever was last written to memory, not the full analyst-log record.
- **`SKILL.md`'s instruction to check the workspace for prior `analyst_log-*.md` reports is followed inconsistently across models.** gemini-3.5-flash treated it as a real directive worth acting on for a recall question; qwen3.6-flash's thinking even acknowledged the idea and then dropped it.
- **`pathlib.Path.glob()` is unavailable inside the Monty sandbox.** Any skill instructions or future generated code that assume `glob()` works will hit this same `AttributeError`. Worth documenting as a known sandbox gap (see `pydantic_ai_harness`'s restricted stdlib subset) so it isn't rediscovered per-model as a silent failure.

## Limitations

- One trial per model — not a statistically reliable behavioral claim, just an observed difference worth tracking.
- Only two models compared; unknown how this generalizes across the rest of the `models.yaml` roster.
- The accumulated workspace state (6 prior `analyst_log-*.md` files, a populated `MEMORY.md`) is specific to this task's history and not a clean-room comparison — see `--pristine` for that in future runs.
