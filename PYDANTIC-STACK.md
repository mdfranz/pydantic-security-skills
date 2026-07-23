# The Pydantic Stack in This Solution

This document explains how each piece of the Pydantic AI ecosystem is wired together in
the `skill_runner` package to run the `suricata-analyst` skill (and any future skill) safely against
untrusted, potentially huge log data. It's an implementation-level companion to `README.md`,
which covers usage; this covers *why the code is built the way it is*.

## Stack inventory

| Package | Version (`uv.lock`) | Role here |
| --- | --- | --- |
| [`pydantic-ai`](https://ai.pydantic.dev) | 2.11.0 | The `Agent` — model calling, tool-calling loop, message history |
| [`pydantic-ai-harness`](https://github.com/pydantic/pydantic-ai-harness) | 0.7.0 | `FileSystem` and `CodeMode` capabilities plugged into the `Agent` |
| [`pydantic-monty`](https://github.com/pydantic/monty) | 0.0.18 | `Monty` — the sandboxed Python interpreter that actually executes model-written code, plus `MountDir`/`OSAccess` |
| [`logfire`](https://pydantic.dev/logfire) | 4.37.0 | Optional OpenTelemetry tracing of the whole run (`--logfire`) |
| `pyyaml` | 6.0.3 | Parses `skill.yaml` (structured skill config, if present) |

None of these are used in isolation — the interesting part is how `Agent`, `CodeMode`, and
`Monty` compose in `skill_runner/run_core.py`.

---

## 1. `pydantic-ai`: the `Agent`

```python
agent = Agent(
    args.model,                       # e.g. "google:gemini-3-flash-preview"
    system_prompt=instructions,       # SKILL.md's full contents
    capabilities=[...],
    model_settings=model_settings or None,
)
```

`Agent` is the outermost object. It owns:
- the model connection (`args.model`, a `provider:model-id` string resolved by pydantic-ai's
  model registry — no separate client setup needed),
- the **system prompt**, which is literally the skill's `SKILL.md` file read verbatim
  (`load_skill()`, `skill_runner/run_core.py`) — the skill *is* the prompt, there's no templating layer,
- the **tool-calling loop** — `agent.run_sync(run_prompt)` (`skill_runner/run_core.py`) drives however many
  model⇄tool round trips are needed until the model produces a final answer,
- **`model_settings`**, a generic passthrough for provider-level request parameters (`max_tokens`,
  `temperature`, etc.), applied to every call for the life of the agent. This project only ever
  populates one key in it, `max_tokens`, and only when `--max-tokens` and/or `--thinking` is
  passed (`skill_runner/run_core.py`, see §2).

`capabilities` is pydantic-ai's extension point for bolting cross-cutting behavior onto an
agent (tool wrapping, filesystem access, deferred-tool handling, reasoning effort, etc.) without
touching the tool-calling loop itself. This project uses three: two from `pydantic-ai-harness`
(§3, §4), layered in a specific order that matters (see §5), and one from core `pydantic-ai`
itself, `Thinking`, which is conditional rather than always-on (§2).

---

## 2. `pydantic-ai`: the `Thinking` capability

```python
if args.thinking:
    capabilities.append(Thinking(effort=args.thinking))
    # Anthropic rejects requests where max_tokens <= thinking.budget_tokens. Floor
    # max_tokens to comfortably clear the budget even if the user passed an explicit
    # --max-tokens that's too low for this effort level.
    effort_budget = ANTHROPIC_THINKING_BUDGET_MAP[args.thinking]
    min_max_tokens = effort_budget + 4096
    if model_settings.get("max_tokens", 0) < min_max_tokens:
        model_settings["max_tokens"] = min_max_tokens
```

`Thinking` (imported from `pydantic_ai.capabilities` — core `pydantic-ai`, not the harness) is
appended to the capabilities list only when `--thinking <effort>` is passed on the CLI. It
requests extended reasoning from whichever model is in use at one of four effort levels
(`low`/`medium`/`high`/`xhigh`), giving a single unified knob across providers that expose
reasoning differently under the hood (Anthropic's `budget_tokens`, OpenAI's `reasoning.effort`,
etc.) — the same `--thinking high` flag works unchanged against `anthropic`, `openai`, and
`google` models.

Anthropic's API specifically rejects any request where `max_tokens` isn't strictly greater than
`thinking.budget_tokens` — the model can't be given fewer output tokens than it's allowed to
spend on reasoning alone. `ANTHROPIC_THINKING_BUDGET_MAP` (imported from
`pydantic_ai.profiles.anthropic`) is the same lookup table pydantic-ai's own Anthropic model
class uses internally to translate an `effort` level into `budget_tokens`; `skill_runner/run_core.py` reuses it
directly (rather than hand-maintaining a duplicate) so the two can't drift apart, and computes a
floor of `budget_tokens + 4096` for `model_settings["max_tokens"]`. An explicit `--max-tokens`
value is only left alone if it already clears that floor — an insufficient explicit value gets
raised, not trusted as-is, which is what closed a real `max_tokens must be greater than
thinking.budget_tokens` 400 error hit in testing. The constraint itself is Anthropic-specific, so
the floor is simply unused (harmless) against providers that don't enforce it.

---

## 3. `pydantic-ai-harness`: `FileSystem`

```python
FileSystem(root_dir=str(ws_path))
```

Adds four tools to the agent — `list_directory`, `read_file`, `write_file`, `grep_search`/`find_files`
— scoped to `ws_path` (the `--workspace` directory, default `./workspace`). Paths passed to these
tools are **relative to `root_dir`**, and the toolset actively rejects escapes: passing an
absolute path like `/workspace/eve.json` fails with "Path resolves outside the root directory."
This is the mechanism that keeps the agent from reading/writing anywhere on the host outside the
one directory you handed it — no code was written for this; it's enforced by the capability.

This is also why SKILL.md is explicit that `run_code` and `FileSystem` tools use **three different
path namespaces** (`skills/suricata-analyst/SKILL.md:22-31`): `run_code` sees the workspace mount
at `/workspace/...` and the read-only skill mount at `/skill/...`, while `FileSystem` tools see
paths relative to `.` and can't reach `/skill` at all. `/workspace` is the same directory on disk
under two different views; `/skill` only exists from inside `run_code`.

---

## 4. `pydantic-ai-harness`: `CodeMode`

```python
CodeMode(
    tools=["describe_events", "query_events", "aggregate_events", "query_sql"],
    mount=[
        MountDir(SANDBOX_WORKSPACE_MOUNT, str(ws_path), mode="read-write"),
        MountDir(SANDBOX_SKILL_MOUNT, str(skill_path.resolve()), mode="read-only"),
        MountDir(SANDBOX_DATA_SOURCE_MOUNT, str(source_root), mode="read-only"),
        MountDir(SANDBOX_DATA_MOUNT, str(source_root), mode="read-only"),
    ],
    os_access=OSAccess(environ={}),
)
```

This is the core of the design, and the piece most worth understanding in detail. `CodeMode`
replaces however many tools an agent has with a single `run_code` tool: instead of the model
picking a tool and filling in a JSON args schema, it **writes Python code** that calls the
capability's chosen tools as plain functions (`some_tool(arg=...)`), and that code runs
inside `Monty` rather than the host Python process.

### `tools=[...]` — which tools get sandboxed

`CodeMode.tools` (a.k.a. the *tool selector*) decides which of the agent's tools become
sandboxed callables inside `run_code`, versus staying as ordinary native tool calls the model
issues directly. It accepts:
- `'all'` (the default) — every tool the agent has,
- a list of tool names — only those,
- a predicate callable — computed per-tool.

This project sets it to the four-name list `["describe_events", "query_events",
"aggregate_events", "query_sql"]` — exactly those four are sandboxed. `FileSystem`'s tools
(`list_directory`, `read_file`, `write_file`) and `Memory`'s tools stay as native top-level tool
calls; `run_code` is otherwise a Python execution surface for the model to process log data, not
a general wrapper around other tools. The four sandboxed names are `data_tools.describe_events`/
`data_tools.query_events`/`data_tools.aggregate_events`/`data_tools.query_sql` (`skill_runner/
data_tools.py`), bound to this run's source/cache roots by small wrapper functions in
`_build_data_tools()` (`skill_runner/run_core.py`) and registered as `Tool(..., sequential=True)`
on the `Agent` itself — `sequential=True` is what makes `CodeMode` render them as plain `def`
callables (`result = query_events(...)`) rather than the `async def` signature every other
plain-function tool gets by default.

This selector wasn't always non-empty — it originally defaulted to `'all'`, which silently broke
an explicit instruction in `SKILL.md` ("call `list_directory(path='.')` ... the FileSystem tool,
not `run_code`"): with `tools='all'`, `list_directory` was *only* reachable from inside a
`run_code` call, so the model had to burn a full sandbox round-trip (write Python, `await
list_directory(...)`, get the result back) just to see what files existed. Confirmed via Logfire
traces before/after — pre-fix, `execute_tool list_directory` was a **child span of
`execute_tool run_code`**; post-fix, it's a sibling, directly under the agent-run span. It was
then set to `[]` (nothing sandboxed) until the Parquet-backed query tools were added, then grown
from two names to four when `describe_events`/`query_sql` were added — the principle held
throughout: the selector names exactly the tools that should pay the sandbox round-trip, and
defaults to none.

### `mount` — sharing directories with sandboxed code

```python
mount=[
    MountDir("/workspace", str(ws_path), mode="read-write"),
    MountDir("/skill", str(skill_path.resolve()), mode="read-only"),
    MountDir("/data-source", str(source_root), mode="read-only"),
    MountDir("/data", str(source_root), mode="read-only"),
]
```

`Monty`'s sandboxed interpreter has **no filesystem access by default** — `pathlib.Path("/etc/passwd")`
just fails inside `run_code`, full stop, regardless of what the host process can see. `CodeMode.mount`
accepts a single `MountDir` or a list of them; this project uses four entries over three distinct
host directories, each punching one specific, explicit hole:

- **`/workspace` → `ws_path`, `mode="read-write"`.** The same directory `FileSystem` is scoped
  to. Sandboxed code can create/modify files here, which is what lets generated code do
  ```python
  f = pathlib.Path("/workspace/eve.json").open()
  ```
  directly against the real 251MB EVE log on disk.
- **`/skill` → the current skill's own directory (`skills/suricata-analyst/`), `mode="read-only"`.**
  Exposes a skill's `references/` material (e.g. `/skill/references/eve_format.md`) to sandboxed
  `pathlib` reads, without ever letting generated code modify the skill's own source. This mount
  didn't exist originally — `SKILL.md` told the model to "refer to `references/eve_format.md`",
  but neither `FileSystem` (scoped to `ws_path`) nor the original single workspace mount could
  reach the skill directory at all, so that instruction was a dead reference; the model happened
  to already know Suricata's EVE schema from training data, which is why it went unnoticed. Adding
  this second mount makes the instruction actually work, and generalizes: any future skill's
  `references/` directory is reachable at `/skill/references/...` with no per-skill code changes.
- **`/data-source` and `/data` → `source_root`, both `mode="read-only"`.** The same host
  directory mounted twice under two virtual paths — `/data-source` is the current, canonical
  name; `/data` is kept only as an alias so skills/prompts written against the older single-`/data`
  layout keep working unchanged. `source_root` itself is resolved once at startup by
  `_select_source_root()` (`workspace/data-source/` if it already exists as a real directory,
  else `workspace/data/`) — never a union of both, so which files are visible doesn't depend on
  mount order.

Deliberately **not** mounted: `workspace/data-sink/parquet/`, the host-side Parquet cache all four
sandboxed data tools read/write (`query_sql` included — it registers a lazy DuckDB `VIEW` over
this same cache file rather than mounting or copying it). It stays host-only on purpose (see §4's
`tools=[...]` discussion) — the sandbox has no Polars or DuckDB and cannot read Parquet directly,
and there is no reason for model-written code to see cache-internal filenames (SHA-256
fingerprints of source identity) at all. `describe_events`/`query_events`/`aggregate_events`/
`query_sql` are the only path to that data, and they return plain dicts, never a path into the
cache — including `query_sql`, whose `sql` argument is free text but whose *result* is still just
a normalized dict, never a filesystem handle.

Nothing outside these mount points is reachable from sandboxed code, mount or no mount.

### `os_access` — environment variables and the clock, deliberately gutted

```python
OSAccess(environ={})
```

`mount` only ever grants filesystem access under the mounted path. Separately, `os.getenv`,
`os.environ`, `datetime.datetime.now()`, and `datetime.date.today()` inside `run_code` are routed
through an `os_access` handler if one is configured — and are hard errors if not. `OSAccess` is
Monty's built-in handler; passing `environ={}` means:
- `os.environ` / `os.getenv(...)` inside sandboxed code see an **empty** dict — no host secrets,
  API keys, or cloud credentials leak into model-written code, no matter what the model asks for,
- `datetime.now()` / `date.today()` still proxy to the real host clock (this is `OSAccess`'s
  default behavior, not something suppressed here) — this is intentional, since the skill's
  filename convention (`analyst_log-YY-MM-DD_HH-MM-SS.md`) depends on it.

`mount` and `os_access` are independent, additive grants — passing one doesn't imply the other.
Both are configured here because the skill genuinely needs a real, writable file mount *and* a
real (but env-scrubbed) clock; nothing else about the host is exposed.

### `dynamic_catalog` — not used, and correctly so

`CodeMode` also has a `dynamic_catalog` flag (default `False`) that moves the list of
sandbox-callable function signatures out of `run_code`'s tool description (which sits in the
prompt-cache-keyed tool-definitions block) and into a separately-cached system-prompt segment.
It only pays off when the sandboxed toolset changes mid-run — e.g. via `ToolSearch` discovering
new tools. This project's sandboxed toolset (`describe_events`, `query_events`,
`aggregate_events`, `query_sql`) is fixed for the life of a run — no `ToolSearch` capability is in
play — so there's no cache-instability problem to solve; the four signatures render once into
`run_code`'s description and stay there. Worth revisiting only if a `ToolSearch` capability or
dynamically-registered tools are added later.

---

## 5. Capability ordering

```python
capabilities = [
    FileSystem(root_dir=str(ws_path)),
    CodeMode(tools=["describe_events", "query_events", "aggregate_events", "query_sql"], ...),
]
if args.thinking:
    capabilities.append(Thinking(effort=args.thinking))
```

`FileSystem` is listed first so its tools exist on the agent before `CodeMode` decides how to
present them; the four data tools themselves are registered via `Agent(tools=[...])`
(not the `capabilities` list — they're ordinary `Tool` objects, not a capability). `CodeMode`
declares itself `position='outermost'` internally (it wraps around the whole assembled toolset,
including `ToolSearch` if present) — but *which* of those tools get folded into `run_code` is
entirely controlled by `CodeMode.tools`, independent of list order. With a fixed four-name
selector, order doesn't currently change behavior, but it's the natural place to add a selector
predicate later if a future skill adds more tools that *should* be sandboxed.

`Thinking` is appended last, and conditionally — it doesn't wrap or select tools the way
`FileSystem`/`CodeMode` do, so its position relative to them doesn't matter; it's ordered last
here simply because whether it's added at all depends on a CLI flag evaluated after the other
two are already in the list.

---

## 6. `pydantic-monty`: the actual sandbox

`Monty` is the interpreter `CodeMode` hands generated code to. It is **not** a subprocess or a
container — it's a from-scratch Python-subset interpreter with its own type checker, so:

- **No third-party imports, ever.** Only `sys`, `typing`, `asyncio`, `math`, `json`, `re`,
  `datetime`, `os`, `pathlib` are importable. `orjson`, `polars`, `duckdb`, `pandas` cannot run
  inside `run_code` no matter what a skill's instructions ask for — this is enforced at the
  interpreter level, not by convention (`README.md:17-20`).
- **No class definitions.** Analysis scripts are necessarily written as flat functions/loops.
- **No `open()` builtin as normally understood, no `for line in f:`.** `SKILL.md` documents the
  workaround (explicit `readline()` loop) because Monty's file objects don't support the
  iterator protocol the way host Python's do (`skills/suricata-analyst/SKILL.md:29-40`).
- **Static type checking on the first call of a session.** Before running the model's code,
  `CodeMode` type-checks it against stub signatures built from the sandboxed tool set — here,
  the four data tools' real parameter and return types, so e.g. passing a `str` for
  `limit` is caught before the sandbox ever runs (note: `query_sql`'s `sql` argument is still
  free-text — the type checker confirms it's a `str`, not that it's valid SQL). This is also why
  built-in exception names like
  `FileNotFoundError` don't resolve in `except` clauses under Monty's checker — the skill
  documents catching `Exception` and branching on `type(e).__name__` instead.
- **REPL-style state persistence.** Each `run_code` call in a run shares one `Monty` REPL session
  unless the model passes `restart=true` — variables from an earlier `run_code` call are still
  in scope in the next one, which is why the model doesn't need to re-read the log file from
  scratch every call.

---

## 7. Data flow, end to end

```
SKILL.md ──(read verbatim)──► Agent(system_prompt=...)
                                      │
prompt (+ existing-scripts note) ──► agent.run_sync(...)
                                      │
                          ┌───────────┴────────────┐
                          │                         │
                 native tool call            run_code(code=...)
                 (list_directory,                    │
                  read_file, write_file)      Monty interpreter
                          │                  (no host access except:
                          │                   - pathlib under /workspace, rw (MountDir)
                    ws_path on host           - pathlib under /skill, read-only (MountDir)
                    (relative paths)          - os.environ={}, clock (OSAccess)  )
                          │                            │
                          │                  ┌─────────┴─────────┐
                          │                  │                    │
                          │           same ws_path on host   skill_path on host
                          │           (/workspace/... paths) (/skill/... paths,
                          │                  │                read-only)
                          └──────────────────┤
                                             │
                    result.output ──► analyst_log-*.md
                    run_code calls ──► generated_code/*.py
                    full transcript ─► runner-*.log
```

Native `FileSystem` calls and sandboxed `pathlib` calls through the workspace `MountDir` resolve
to **the same directory on disk**, just addressed differently (relative path vs.
`/workspace/...`) — that's the detail SKILL.md calls out explicitly, and the reason the `tools`
selector matters: an over-broad selector routes calls that should be native through `run_code`
instead. The skill `MountDir` is a separate, read-only view onto a different directory
(`skill_path`, not `ws_path`) with no native equivalent at all — reference material is only
reachable via `run_code`.

The two sandboxed data tools sit alongside `run_code(code=...)` in the diagram above, not inside
it: `query_events(...)`/`aggregate_events(...)` calls made from *within* sandboxed code are
dispatched back out to the host process (never to `Monty` itself, which has no Polars), where
`ensure_parquet_cache()` resolves the model's logical filename against `source_root`, converts to
Parquet on first use, and returns plain dicts back into the sandbox. That round trip is invisible
to the model beyond the function call itself — no separate tool-call/tool-result pair shows up
the way it would for a *native* tool, because from `CodeMode`'s perspective this is still one
`run_code` execution.

---

## 8. Observability: Logfire

```python
if args.logfire:
    import logfire
    logfire.configure()
    logfire.instrument_pydantic_ai()
```

`instrument_pydantic_ai()` auto-instruments the `Agent`, emitting one OTel span per model call
(`chat <model>`) and one per tool execution (`execute_tool <name>`), all under a root
`invoke_agent agent` span, with `parent_span_id` reflecting actual nesting. This is what makes it
possible to *verify* the `CodeMode` tool-selector behavior empirically rather than just reading
the source: querying the Logfire project (`tomfoolery`) for a run's `trace_id` and grouping by
`parent_span_id` shows directly whether `list_directory` executed as a sibling of `run_code`
(native) or a child of it (sandboxed) — this is exactly how the `tools=[]` fix was confirmed
against a real run, not just a synthetic one.

Setup is one-time per machine (`uv run logfire auth` + `logfire projects use`); after that,
`--logfire` ships full traces (spans, tool calls, token/cost usage) with no code changes beyond
the two lines above.

---

## 9. Design rationale, summarized

| Decision | Why |
| --- | --- |
| `CodeMode(tools=["query_events", "aggregate_events"])` instead of default `'all'` | Keeps `FileSystem`/`Memory` tools callable natively, matching `SKILL.md`'s explicit instructions and avoiding a wasted sandbox round-trip for simple directory/file operations; only the two host-side Polars helpers pay the sandbox round-trip, since only they need it |
| `mount` = `[workspace rw, skill ro, data-source ro, data ro (alias)]`; `data-sink/` (Parquet cache) deliberately *not* mounted | Sandboxed code gets real file I/O for the (potentially 250MB+) log data, plus read access to a skill's own reference material — without ever seeing the rest of the host filesystem, letting generated code modify the skill's source, or seeing cache-internal Parquet filenames |
| `os_access=OSAccess(environ={})` | Sandboxed code gets a working clock for filename timestamps, but zero visibility into host secrets/env vars |
| `dynamic_catalog` left at default (`False`) | No `ToolSearch`/dynamic tool discovery in play yet — the cache-stability tradeoff it solves doesn't apply |
| `Thinking` capability only added when `--thinking` is passed, with `model_settings["max_tokens"]` floored via pydantic-ai's own `ANTHROPIC_THINKING_BUDGET_MAP` | Reasoning effort should be opt-in, not always paid for; the floor prevents Anthropic's `max_tokens must be greater than thinking.budget_tokens` 400 without hand-duplicating pydantic-ai's own budget table |
| Monty's stdlib subset + no-class restriction | Guarantees any skill's instructions (however phrased) can never pull in `pandas`/`duckdb`/etc. or execute anything outside the interpreter's control |
| Every `run_code` call persisted to `generated_code/` | Audit trail independent of what the model chooses to save via `write_file` |
| Full transcript (`runner-*.log`) + final answer (`analyst_log-*.md`) written separately | The report is consumable without parsing console output; the transcript is there for post-hoc debugging (including feeding into Logfire trace lookups by timestamp) |
