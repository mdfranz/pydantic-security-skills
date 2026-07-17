# The Pydantic Stack in This Solution

This document explains how each piece of the Pydantic AI ecosystem is wired together in
`runner.py` to run the `suricata-analyst` skill (and any future skill) safely against
untrusted, potentially huge log data. It's an implementation-level companion to `README.md`,
which covers usage; this covers *why the code is built the way it is*.

## Stack inventory

| Package | Version (`uv.lock`) | Role here |
| --- | --- | --- |
| [`pydantic-ai`](https://ai.pydantic.dev) | 2.9.1 | The `Agent` — model calling, tool-calling loop, message history |
| [`pydantic-ai-harness`](https://github.com/pydantic/pydantic-ai-harness) | 0.7.0 | `FileSystem` and `CodeMode` capabilities plugged into the `Agent` |
| [`pydantic-monty`](https://github.com/pydantic/monty) | 0.0.18 | `Monty` — the sandboxed Python interpreter that actually executes model-written code, plus `MountDir`/`OSAccess` |
| [`logfire`](https://pydantic.dev/logfire) | 4.37.0 | Optional OpenTelemetry tracing of the whole run (`--logfire`) |
| `pyyaml` | 6.0.3 | Parses `skill.yaml` (structured skill config, if present) |

None of these are used in isolation — the interesting part is how `Agent`, `CodeMode`, and
`Monty` compose in `runner.py:93-106`.

---

## 1. `pydantic-ai`: the `Agent`

```python
agent = Agent(
    args.model,                 # e.g. "google:gemini-3-flash-preview"
    system_prompt=instructions, # SKILL.md's full contents
    capabilities=[...],
)
```

`Agent` is the outermost object. It owns:
- the model connection (`args.model`, a `provider:model-id` string resolved by pydantic-ai's
  model registry — no separate client setup needed),
- the **system prompt**, which is literally the skill's `SKILL.md` file read verbatim
  (`load_skill()`, `runner.py:35-47`) — the skill *is* the prompt, there's no templating layer,
- the **tool-calling loop** — `agent.run_sync(run_prompt)` (`runner.py:121`) drives however many
  model⇄tool round trips are needed until the model produces a final answer.

`capabilities` is pydantic-ai's extension point for bolting cross-cutting behavior onto an
agent (tool wrapping, filesystem access, deferred-tool handling, etc.) without touching the
tool-calling loop itself. This project uses two, both from `pydantic-ai-harness`, layered in a
specific order that matters (see §4).

---

## 2. `pydantic-ai-harness`: `FileSystem`

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

## 3. `pydantic-ai-harness`: `CodeMode`

```python
CodeMode(
    tools=[],
    mount=[
        MountDir(SANDBOX_WORKSPACE_MOUNT, str(ws_path), mode="read-write"),
        MountDir(SANDBOX_SKILL_MOUNT, str(skill_path.resolve()), mode="read-only"),
    ],
    os_access=OSAccess(environ={}),
)
```

This is the core of the design, and the piece most worth understanding in detail. `CodeMode`
replaces however many tools an agent has with a single `run_code` tool: instead of the model
picking a tool and filling in a JSON args schema, it **writes Python code** that calls the
capability's chosen tools as plain functions (`await some_tool(arg=...)`), and that code runs
inside `Monty` rather than the host Python process.

### `tools=[]` — which tools get sandboxed

`CodeMode.tools` (a.k.a. the *tool selector*) decides which of the agent's tools become
sandboxed callables inside `run_code`, versus staying as ordinary native tool calls the model
issues directly. It accepts:
- `'all'` (the default) — every tool the agent has,
- a list of tool names — only those,
- a predicate callable — computed per-tool.

This project sets it to `[]` (an empty list) — **nothing** is sandboxed. Both `FileSystem`'s
tools (`list_directory`, `read_file`, `write_file`) and any future non-`FileSystem` tools stay
as native top-level tool calls; `run_code` exists purely as a Python execution surface for the
model to process log data, not as a wrapper around other tools.

This wasn't the original configuration — it defaulted to `'all'`, which silently broke an
explicit instruction in `SKILL.md` ("call `list_directory(path='.')` ... the FileSystem tool, not
`run_code`"): with `tools='all'`, `list_directory` was *only* reachable from inside a `run_code`
call, so the model had to burn a full sandbox round-trip (write Python, `await
list_directory(...)`, get the result back) just to see what files existed. Confirmed via Logfire
traces before/after — pre-fix, `execute_tool list_directory` was a **child span of
`execute_tool run_code`**; post-fix, it's a sibling, directly under the agent-run span. See the
commit that introduced `tools=[]` for the full before/after.

### `mount` — sharing directories with sandboxed code

```python
mount=[
    MountDir("/workspace", str(ws_path), mode="read-write"),
    MountDir("/skill", str(skill_path.resolve()), mode="read-only"),
]
```

`Monty`'s sandboxed interpreter has **no filesystem access by default** — `pathlib.Path("/etc/passwd")`
just fails inside `run_code`, full stop, regardless of what the host process can see. `CodeMode.mount`
accepts a single `MountDir` or a list of them; this project uses two, each punching one specific,
explicit hole:

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

Nothing outside these two mount points is reachable from sandboxed code, mount or no mount.

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
new tools. This project's toolset is static (and, post-fix, empty — `tools=[]` means `run_code`
has zero sandbox-callable functions to render in the first place), so there's no cache-instability
problem to solve. Worth revisiting only if a `ToolSearch` capability or dynamically-registered
tools are added later.

---

## 4. Capability ordering

```python
capabilities=[
    FileSystem(root_dir=str(ws_path)),
    CodeMode(tools=[], ...),
]
```

`FileSystem` is listed first so its tools exist on the agent before `CodeMode` decides how to
present them. `CodeMode` declares itself `position='outermost'` internally (it wraps around the
whole assembled toolset, including `ToolSearch` if present) — but *which* of those tools get
folded into `run_code` is entirely controlled by `CodeMode.tools`, independent of list order.
With `tools=[]`, order doesn't currently change behavior, but it's the natural place to add a
selector predicate later if a future skill adds tools that *should* be sandboxed (see §7).

---

## 5. `pydantic-monty`: the actual sandbox

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
  `CodeMode` type-checks it against stub signatures built from the sandboxed tool set
  (irrelevant here since that set is empty, but this is why built-in exception names like
  `FileNotFoundError` don't resolve in `except` clauses under Monty's checker — the skill
  documents catching `Exception` and branching on `type(e).__name__` instead).
- **REPL-style state persistence.** Each `run_code` call in a run shares one `Monty` REPL session
  unless the model passes `restart=true` — variables from an earlier `run_code` call are still
  in scope in the next one, which is why the model doesn't need to re-read the log file from
  scratch every call.

---

## 6. Data flow, end to end

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
`/workspace/...`) — that's the detail SKILL.md calls out explicitly, and the reason `tools=[]`
matters: without it, the "native" path never actually got used. The skill `MountDir` is a
separate, read-only view onto a different directory (`skill_path`, not `ws_path`) with no native
equivalent at all — reference material is only reachable via `run_code`.

---

## 7. Observability: Logfire

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

## 8. Design rationale, summarized

| Decision | Why |
| --- | --- |
| `CodeMode(tools=[])` instead of default `'all'` | Keeps `FileSystem` tools callable natively, matching `SKILL.md`'s explicit instructions and avoiding a wasted sandbox round-trip for simple directory/file operations |
| `mount` = `[workspace rw, skill read-only]` | Sandboxed code gets real file I/O for the (potentially 250MB+) log data, plus read access to a skill's own reference material — without ever seeing the rest of the host filesystem, and without letting generated code modify the skill's source |
| `os_access=OSAccess(environ={})` | Sandboxed code gets a working clock for filename timestamps, but zero visibility into host secrets/env vars |
| `dynamic_catalog` left at default (`False`) | No `ToolSearch`/dynamic tool discovery in play yet — the cache-stability tradeoff it solves doesn't apply |
| Monty's stdlib subset + no-class restriction | Guarantees any skill's instructions (however phrased) can never pull in `pandas`/`duckdb`/etc. or execute anything outside the interpreter's control |
| Every `run_code` call persisted to `generated_code/` | Audit trail independent of what the model chooses to save via `write_file` |
| Full transcript (`runner-*.log`) + final answer (`analyst_log-*.md`) written separately | The report is consumable without parsing console output; the transcript is there for post-hoc debugging (including feeding into Logfire trace lookups by timestamp) |
