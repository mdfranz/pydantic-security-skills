# Pydantic Security Skills

A Pydantic AI-based execution environment for running security analyst skills safely, using
[Monty](https://github.com/pydantic/monty) (a sandboxed Python interpreter) and
[Pydantic AI Harness](https://github.com/pydantic/pydantic-ai-harness) for sandboxed code execution.

Currently ships one skill: **`suricata-analyst`** — analyzes Suricata EVE JSON logs (network
threats, suspicious egress, protocol anomalies).

## How it works

- `runner.py` loads a skill's `SKILL.md` as the agent's instructions.
- The agent gets a `FileSystem` capability scoped to `./workspace` (read/write/search files) and
  `CodeMode`, which wraps every tool — including `FileSystem`'s — into a single sandboxed
  `run_code` tool. The model writes Python that calls those tools; the code itself runs inside
  Monty, with no host filesystem/env/clock access beyond what the wrapped tools expose.
- Analysis is native Python + `json` only — Monty permits a fixed stdlib subset (`sys`, `typing`,
  `asyncio`, `math`, `json`, `re`, `datetime`, `os`, `pathlib`), no third-party imports and no class
  definitions, so DuckDB/Polars/pandas can never run inside the sandbox regardless of what a skill
  asks for.

## Prerequisites

- [uv](https://github.com/astral-sh/uv) installed.
- An API key for the model provider (e.g. `GEMINI_API_KEY` or `GOOGLE_API_KEY` for Gemini).

## Running

Drop your log data into `./workspace` (created automatically on first run), then:

```bash
export GEMINI_API_KEY="your-gemini-api-key"

# skill_dir defaults to skills/suricata-analyst
uv run runner.py "Identify the top 5 source IPs by event count in eve.json"

# Or target a different skill / workspace / model explicitly:
uv run runner.py skills/suricata-analyst "Analyze security events" --workspace ./workspace --model google:gemini-3-flash-preview
```

Flags:
- `--model` — model ID (default `google:gemini-3-flash-preview`).
- `--workspace` — directory the agent's `FileSystem` capability is scoped to (default `./workspace`).
- `--debug` — prints the Python code the model generated and ran inside the Monty sandbox for each `run_code` call, plus its return value.
- `--logfire` — traces the run with [Logfire](https://pydantic.dev/logfire). Prints a live span tree to the console with zero setup; also ships to the Logfire UI once authenticated (see below).

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
uv run runner.py skills/<name> "<prompt>"
```
