# Pydantic Security Skills

A Pydantic AI-based execution environment for running security analyst skills safely, using
[Monty](https://github.com/pydantic/monty) (a sandboxed Python interpreter) and
[Pydantic AI Harness](https://github.com/pydantic/pydantic-ai-harness) for sandboxed code execution.

Currently ships one skill: **`suricata-analyst`** — analyzes Suricata EVE JSON logs (network
threats, suspicious egress, protocol anomalies).

## Related projects

- [sec-skillz](https://github.com/mdfranz/sec-skillz)
- [agno-catbox](https://github.com/mdfranz/agno-catbox)

## Further reading

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — components, structure, and trust boundaries.
- [`PYDANTIC-STACK.md`](PYDANTIC-STACK.md) — how the pydantic-ai/harness/Monty stack is wired
  together, and why.
- [`PROJECT.md`](PROJECT.md) — development history and design decisions by phase.
- [`ISSUES.md`](ISSUES.md) — known issues and improvement backlog, evidenced from real runs.

## How it works

- `skill-runner` loads a skill's `SKILL.md` as the agent's instructions.
- `--workspace` (default `./workspace`) is the **case root**, not the agent's own directory. It
  has four subtrees: `data/` (canonical input evidence, read-only), one directory per *task*
  (the agent's writable workspace — see below), `memory/` (per-task notebook, tool-visible only —
  see below), and `logs/` (host-only audit records, never visible to the agent).
- The agent gets a `FileSystem` capability scoped to the current task's directory
  (`<workspace>/<task>/`, read/write/search files), called natively — `CodeMode` is configured
  with `tools=[]`, so it doesn't wrap any tool behind `run_code`; `run_code` is purely a sandboxed
  Python execution surface. Inside it, `pathlib` reaches three mounted directories:
  `/workspace` (read-write, the same task directory `FileSystem` is scoped to), `/data`
  (read-only, canonical input evidence shared across tasks), and `/skill` (read-only, the current
  skill's own directory, for reference material like `references/*.md`). No other host
  filesystem/env/clock access is available.
- In accumulate mode, the agent also gets a `Memory` capability: a persistent `MEMORY.md`
  notebook, auto-injected (bounded) into every request, plus `read_memory`/`write_memory`/
  `search_memory` tools for longer topic files — native, like `FileSystem`, never routed through
  `run_code` or mounted into the sandbox. Omitted entirely in `--pristine` mode.
- Analysis is native Python + `json` only — Monty permits a fixed stdlib subset (`sys`, `typing`,
  `asyncio`, `math`, `json`, `re`, `datetime`, `os`, `pathlib`), no third-party imports and no class
  definitions, so DuckDB/Polars/pandas can never run inside the sandbox regardless of what a skill
  asks for.

### Task-scoped workspaces

Every run selects a *task*, which decides whether the agent starts from a clean slate or builds
on prior runs. See [`refs/workspace-lifecycle.md`](refs/workspace-lifecycle.md) for the full
design.

- No flags → the shared `default` task (accumulates).
- `--task <name>` → a named task that accumulates across runs sharing that name. Names must
  match `[a-z0-9][a-z0-9_-]{0,63}`; `default`, `data`, `logs`, and `memory` are reserved.
- `--pristine` → a fresh, auto-generated (`task-<uuid4>`) task directory with no prior agent
  state — including no `Memory` capability, so a pristine run's tool surface and prompt are
  identical regardless of prior runs. The directory persists after the run for later inspection
  but is never reselected automatically.
- `--task` and `--pristine` are mutually exclusive.

## Prerequisites

- [uv](https://github.com/astral-sh/uv) installed.
- An API key for the model provider (e.g. `GEMINI_API_KEY` or `GOOGLE_API_KEY` for Gemini).

## Running

Drop your log data into `./workspace/data` (created automatically on first run), then:

```bash
export GEMINI_API_KEY="your-gemini-api-key"

# Run a model in interactive mode (prompts you for the query):
./run.sh google:gemini-3.5-flash

# Or specify a different skill directory:
./run.sh google:gemini-3.5-flash skills/osqueryd-analyst

# Run raw command: skill_dir defaults to skills/suricata-analyst; task defaults to "default"
uv run skill-runner "Identify the top 5 source IPs by event count in eve.json"

# Or target a different skill / task / model explicitly:
uv run skill-runner skills/suricata-analyst "Analyze security events" --task suricata-triage --model google:gemini-3-flash-preview

# Or start from a clean workspace, isolated from any prior run:
uv run skill-runner skills/suricata-analyst "Baseline analysis" --pristine

# Or open the Textual TUI instead of the plain console (requires the `tui` extra, see below):
uv run skill-runner skills/suricata-analyst "Baseline analysis" --ui textual

# The prompt itself is optional only with --ui textual -- an empty session opens and the
# first message is typed into the bottom bar. Console mode always requires a prompt.
uv run skill-runner --ui textual
```

Flags:
- `--model` — model ID (default `google:gemini-3-flash-preview`; see `models.yaml` for aliases).
- `--ui` — `console` (default) or `textual`. `console` is today's raw-print behavior, meant for
  scripts and quick one-shot runs. `textual` opens a multi-panel TUI (tool calls, model
  output/thinking, a live artifacts tree, and a bottom bar that always accepts free-text
  follow-up prompts) for interactive investigation sessions — see "TUI mode" below.
- `--workspace` — the workspace base/case root (default `./workspace`); see "Task-scoped workspaces" above.
- `--task` — reuse (or create) a named task workspace; accumulates across runs.
- `--pristine` — start a fresh, isolated task workspace with no prior agent state.
- `--debug` — prints the Python code the model generated and ran inside the Monty sandbox for each `run_code` call, plus its return value (console mode only; Textual's panels are always untruncated and scrollable).
- `--logfire` — traces the run with [Logfire](https://pydantic.dev/logfire). Prints a live span tree to the console with zero setup (suppressed under `--ui textual`, so it doesn't corrupt the TUI's alternate screen); also ships to the Logfire UI once authenticated (see below).
- `--interactive` — adds "pause and checkpoint" instructions to the system prompt. In console mode this also drives a blocking continue/stop/focus loop after each response; under `--ui textual` it only affects the system prompt, since the bottom bar already always accepts free-text follow-ups.
- `--thinking` — enables model thinking/reasoning with a specified effort level (`low`, `medium`, `high`, `xhigh`). Useful for complex reasoning tasks on supporting models (e.g. Gemini 3+ / Claude Opus 4.6+).
- `--max-tokens` — the maximum number of tokens to generate before stopping. Defaults to automatically scaling when thinking effort is set, preventing Anthropic API validation errors.
- `--max-retries` — max retries for a failing `run_code` call before the turn aborts (default `5`). Raised from `CodeMode`'s own default of 3 after a model got stuck retrying the same Monty-unsupported syntax repeatedly; see [`ISSUES.md`](ISSUES.md) #5.
- `--max-run-seconds` — a wall-clock budget per turn. A turn that runs longer is cleanly interrupted the same way Ctrl+C/SIGTERM is — partial progress kept, audit log records the interruption, process exits `143`. No limit by default. Added after a run hung silently for ~2 hours before failing; see [`ISSUES.md`](ISSUES.md) #12.
- `--max-turns` — max model round-trips per turn, passed through to `pydantic_ai`'s `UsageLimits.request_limit` (default: pydantic_ai's own default of 50).

### TUI mode

`--ui textual` requires the optional `tui` extra:

```bash
uv sync --extra tui
```

Console-only installs are unaffected — `textual` is only imported lazily, inside the `--ui
textual` branch. Both UIs consume the same event stream and write the same artifacts
(`analyst_log-*.md`, `generated_code/*.py`, the audit JSONL); the TUI is a different way of
*watching* a run, not a different run. A multi-turn Textual session checkpoints the same report
after every successful turn and lists every prompt submitted so far, rather than the single
`- Prompt:` line console mode's single-turn report uses.

Every run also retains artifacts. Inside the task directory (`<workspace>/<task>/`):
`generated_code/` contains each generated `run_code` program, and
`analyst_log-YY-MM-DD_HH-MM-SS.md` contains the final analysis response. The agent is additionally
instructed to save reusable scripts and intermediate findings with the filesystem tools. Outside
the task directory, in `<workspace>/logs/` (not agent-visible), `runner-<task>-<run-id>.jsonl`
holds a complete append-only audit trail of the run — configuration, prompts, every tool call and
result, `run_code` bodies/returns, errors, and completion — richer than a console transcript so
even a failed or interrupted run leaves a durable record. In accumulate mode, `<workspace>/memory/
<skill>/<task>/MEMORY.md` holds whatever the agent chose to persist via `write_memory` — reachable
only through the memory tools, never through `FileSystem` or `list_directory`.

## Optional: Logfire tracing

One-time setup, per machine:

```bash
uv run logfire auth
uv run logfire projects use --org '<your-org>' '<your-project>'
```

This writes a token to `~/.logfire/` and a project pointer to `.logfire/logfire_credentials.json`
(gitignored). After that, any run with `--logfire` sends full traces — nested `run_code` spans,
the tool calls issued from inside the sandbox, model requests, timing, and token/cost usage — to
your Logfire project's dashboard.

> **Don't name a `uv` project `pydantic`** — it shadows the real `pydantic` PyPI package during
> dependency resolution and breaks `uv add`/`uv lock`. This project is named
> `pydantic-security-skills` for that reason.

## Adding a new skill

Add a directory under `skills/<name>/` with a `SKILL.md` (and optionally `skill.yaml` for
structured config, plus a `references/` directory). Run it with:

```bash
uv run skill-runner skills/<name> "<prompt>"
```

## Comparing models

`scripts/compare_models.sh` runs the same prompt against every model in `$MODELS` (a
space-separated list), each in its own fresh `--pristine` workspace — so no model sees another's
scripts or reports — with `--logfire` enabled for every run:

```bash
MODELS="google:gemini-3-flash-preview openrouter:deepseek/deepseek-v4-pro" \
  scripts/compare_models.sh "Generate traffic statistics: breakdown of protocols and top talkers."
```

Any arguments after the prompt are passed through to `skill-runner` (e.g. `--thinking medium`).
Optional env vars: `SKILL_DIR` (default `skills/suricata-analyst`) and `WORKSPACE` (default
`./workspace`). One model's failure doesn't abort the others — a summary table at the end maps
each model to its pristine task id and status, so you can find each run's `analyst_log-*.md`
under `workspace/<task-id>/` afterward.
