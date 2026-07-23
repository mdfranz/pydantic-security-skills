# 3rd Party Dependencies

This document lists and classifies the 3rd party dependencies used in the
`pydantic-security-skills` project. See `pyproject.toml` for exact version constraints and
`uv.lock` for the fully resolved dependency graph.

## Core Agent Framework

*   **[pydantic-ai](https://ai.pydantic.dev/)** (`>=2.9.1`): the LLM agent framework the whole
    runner is built on. `skill_runner/run_core.py` constructs the `Agent` itself and pulls in
    `capabilities.Thinking` (optional reasoning-effort support) and
    `profiles.anthropic.ANTHROPIC_THINKING_BUDGET_MAP` (the `max_tokens` floor that keeps
    `--thinking` from 400ing against Anthropic). `skill_runner/audit.py` and
    `skill_runner/artifacts.py` consume `messages.*` types (`ModelMessage`, `ToolCallPart`,
    `ThinkingPart`, etc.) to translate the streamed event feed into the audit log and the
    rendered report. `skill_runner/session.py` uses `usage.UsageLimits` for the `--max-turns`
    flag. `skill_runner/resilience.py` and `run_core.py` both catch
    `exceptions.ModelHTTPError` for provider-error handling (429 retry, and surfacing a clean
    message instead of a raw traceback). See `skill_runner/IMPL.md` for the full call-site
    breakdown.
*   **[pydantic-ai-harness](https://github.com/pydantic/pydantic-ai-harness)**
    (`[codemode]`, `>=0.7.0`): the sandboxed-execution and native-tool layer on top of
    pydantic-ai. `run_core.py` uses `CodeMode` (the `run_code` sandbox tool, configured with
    `tools=["describe_events", "query_events", "aggregate_events", "query_sql"]` so only those
    four host-side data-query helpers are routed through it and `FileSystem`'s tools stay native
    — see `PYDANTIC-STACK.md` for why) and `FileSystem` (task-scoped native read/write/search-file
    tools), plus
    `memory.FileStore`/`Memory` for the accumulate-mode notebook capability. `skill_runner/
    resilience.py` uses
    `overflowing_tool_output.{Band,LocalFileStore,OverflowingToolOutput,Spill,Truncate}` to
    spill oversized tool returns outside model history. The `[codemode]` extra is what pulls in
    `pydantic-monty` (see "Indirect, but directly imported" below) — without it, `CodeMode`
    itself wouldn't be installable.

## Sandbox

*   **[pydantic-monty](https://github.com/pydantic/monty)** (indirect — see below): the actual
    sandboxed Python interpreter `run_code` executes inside. Not a direct dependency in
    `pyproject.toml`; pulled in transitively by `pydantic-ai-harness[codemode]`'s own `codemode`
    extra. `run_core.py` imports `MountDir` (the sandbox mounts: `/workspace` read-write,
    `/skill`, `/data-source`, and its `/data` alias, all read-only) and `OSAccess` (constructed
    with `environ={}`, so sandboxed code gets no environment variables at all) directly, to
    configure `CodeMode`.

## Fast Data Queries

*   **[Polars](https://pola.rs/)** (`>=1.0.0`): a **host-side-only** dependency — never available
    inside the Monty sandbox itself, which has no third-party imports at all. `skill_runner/
    data_tools.py` uses `pl.scan_ndjson(...).sink_parquet(...)` to lazily convert an input log to
    a compressed Parquet cache on first query, and `pl.scan_parquet(...)` plus `.filter()`/
    `.select()`/`.group_by()`/`.agg(pl.len())`/`.collect_schema()` to serve `query_events`/
    `aggregate_events`/`describe_events`'s bounded pages, exact aggregates, and schema pages.
    These functions run on the host process and are exposed to the model only as ordinary
    sandboxed function calls (see `CodeMode` above) — the model never gets `import polars` inside
    `run_code`, only typed arguments and plain-dict results.
*   **[duckdb](https://duckdb.org/)** (`>=1.5.5`): a **host-side-only** dependency backing
    `query_sql`, the one data tool that lets the model author its own SQL. Unlike `polars-runtime`
    packages, `duckdb` ships CPython-version-specific wheels (no `abi3` forward compatibility), so
    its availability needs re-checking on every Python version bump — see `SQL_QUERY_PLAN.md` §2.
    `data_tools.py`'s `query_sql` opens a fresh in-memory connection per call, sets
    `allowed_paths`/`memory_limit`/`temp_directory` before disabling `enable_external_access`,
    registers a lazy `VIEW` over the same Parquet cache the other three tools use, and runs the
    model's `SELECT` against it with a wall-clock timeout via `con.interrupt()`. Also uses
    `duckdb.extract_statements`/`duckdb.StatementType` to reject anything but exactly one
    `SELECT`/`WITH ... SELECT` statement before it ever reaches the connection.

## Configuration & Skill Loading

*   **[PyYAML](https://pyyaml.org/)** (`>=6.0.3`): the only config-file parser in the project.
    `skill_runner/config.py`'s `load_model_catalog` parses `models.yaml`;
    `skill_runner/run_core.py`'s `load_skill` parses each skill's optional `skill.yaml`
    metadata. Always `yaml.safe_load`, never the unsafe loader.

## Provider Retry & Async

*   **[anyio](https://anyio.readthedocs.io/)** (`>=4.0`): used directly in
    `skill_runner/resilience.py`'s `retry_rate_limited_request` (`await anyio.sleep(delay)`
    between a provider's `429` response and the single bounded retry). Worth noting: `anyio` was
    already present as a *transitive* dependency (pulled in underneath `pydantic-ai`'s
    OpenAI/Anthropic provider SDKs, which depend on it for their own async HTTP handling) before
    first-party code started importing it directly — promoting it to an explicit
    `pyproject.toml` dependency once `resilience.py` needed it is the correct move, not
    redundant: pinning a version floor for something you import directly shouldn't depend on
    some other dependency continuing to need it too.

## Observability (optional)

*   **[logfire](https://pydantic.dev/logfire)** (`[system-metrics]`, `>=4.37.0`): OpenTelemetry
    tracing for pydantic-ai, gated entirely behind the `--logfire` CLI flag.
    `run_core.py`'s `_configure_logfire` calls `logfire.configure(...)` and
    `logfire.instrument_pydantic_ai()`, wrapped in a `try`/`except` so a missing/unauthenticated
    Logfire install degrades to a warning rather than aborting the run. The `system-metrics`
    extra additionally reports host CPU/memory alongside the trace spans.

## Terminal UI (optional, `tui` extra)

*   **[Textual](https://textual.textualize.io/)** (`>=8.2.8`): the entire `--ui textual` driver
    — `skill_runner/ui_textual.py` (`App`, `work`, `DataTable`/`RichLog`/`DirectoryTree`/`Input`
    widgets, the command palette) and `skill_runner/tui_widgets.py` (`ModalScreen`,
    `DataTable`/`Button`/`Label`/`Markdown`/`Static`). Declared as an optional extra
    (`uv sync --extra tui`) and imported lazily — only from inside `runner.py::main()`'s
    `--ui textual` branch — specifically so a console-only install never imports it at all.
*   **[Rich](https://rich.readthedocs.io/)** (indirect — see below): Textual is itself built on
    Rich, which is how it ends up available; `ui_textual.py` (`rich.text.Text`, used to render
    `DataTable` cells as plain, non-markup-parsed text — tool output routinely contains literal
    `[...]` that Rich's markup parser would otherwise silently eat) and `tui_widgets.py`
    (`rich.syntax.Syntax`, for the `.py` file-viewer's syntax highlighting) both import it
    directly.

## Indirect, but directly imported

Both entries below are absent from `pyproject.toml`'s `dependencies`/`optional-dependencies` —
they arrive transitively through something else — yet are imported directly by first-party code,
not just used incidentally underneath another library's API:

*   **pydantic-monty** — via `pydantic-ai-harness[codemode]`'s `codemode` extra (see "Sandbox"
    above).
*   **rich** — via `textual` (and present elsewhere in the dependency graph too) (see
    "Terminal UI" above).

If either of these ever stops being a transitive dependency of the package that currently
provides it, the corresponding direct `import` would break with no warning from `pyproject.toml`
alone — worth promoting to an explicit dependency the same way `anyio` was, if that ever looks
likely.

## Purely indirect (not imported directly anywhere in `skill_runner/`)

*   **[httpx](https://www.python-httpx.org/)**: the actual HTTP client underneath
    `pydantic-ai-slim`'s provider calls. Never imported directly, but its exceptions surface in
    practice — `httpx.ReadTimeout` shows up in real crash tracebacks (see `ISSUES.md` #12, a
    ~2-hour silent hang from one) even though nothing in this repo names `httpx` itself.
*   **[pydantic](https://docs.pydantic.dev/)**: pydantic-ai's own foundation. `skill_runner`
    never defines a `BaseModel` itself — every internal data shape (`RunOptions`, `RunSetup`,
    `Turn`, `Transcript`, `WorkspaceContext`) is a plain `@dataclass`, deliberately, to keep the
    runner's own types independent of pydantic-ai's validation machinery.

## Build & dev tooling

*   **[Hatchling](https://hatch.pypa.io/)** (`build-system.requires`): the build backend that
    packages `skill_runner/` and registers the `skill-runner` console-script entry point
    (`pyproject.toml`'s `[project.scripts]`) — not a runtime dependency, never imported by
    application code.
*   **pytest**: deliberately *not* a declared dependency anywhere in `pyproject.toml` — the test
    suite (`tests/`) is run via `uv run --with pytest pytest tests/`, an ephemeral overlay rather
    than a permanent dev dependency, so a plain `uv sync` (no test tooling) stays the fast path
    for anyone just running the tool.
