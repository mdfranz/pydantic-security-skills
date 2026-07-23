# Technical Implementation Plan: Model-Authored SQL via `query_sql`

This document details adding a third host-side query tool, `query_sql`, alongside the existing
`query_events`/`aggregate_events`, plus a bounded `describe_events` schema helper and exact-match
filters for the two existing Polars tools (see `DATA_SOURCE_SINK_PLAN.md`). Unlike the typed
tools, `query_sql` lets the model author the query itself, as SQL text, executed host-side by
DuckDB against the same Parquet cache. This directly reverses `DATA_SOURCE_SINK_PLAN.md`'s
original "no SQL, no model-authored query language" principle — that call was made before the
typed tools had been exercised against real nested Suricata JSON (`tls.sni`,
`dns.queries[].rrtype`), and in practice those nested fields are exactly what the flat,
top-level-only typed tools cannot reach without pulling raw pages and iterating client-side in
the sandbox — the friction that motivated this plan.

The security posture changes accordingly: instead of forbidding a query language, this plan
constrains the *engine connection* the query language runs against, using DuckDB's own documented
mechanism for untrusted SQL. Every claim about DuckDB's behavior below was verified against the
project's actual pinned version (1.5.5) before being written down here, not assumed from general
DuckDB knowledge — see §2 for what was tested and how.

---

## 1. When to Use `query_events`/`aggregate_events` vs. `query_sql`

Both stay available; this is additive, not a replacement (same principle as
`DATA_SOURCE_SINK_PLAN.md` §1). An early draft of this plan justified keeping the typed tools
mainly on auditability grounds, plus a since-fixed ~130x performance gap (see §2's benchmark
table) from a first-draft `query_sql` design that eagerly materialized the entire source file
before running any query — the fixed design (§2) closes that gap, so the case below rests on
what's actually still true after the fix, not a stale number.

**Reach for `query_events`/`aggregate_events` first** — the flat, typed tools — when the question
is a simple filter, a single-field browse, or a group-by over a top-level column
(`event_type`, `src_ip`, `dest_port`, `proto`, ...):
- **Cannot express an expensive query.** The parameter surface is a small, enumerable set
  (`name`, `event_type`, bounded top-level `equals`, `columns`, `offset`/`limit`, `group_by`) —
  there is no way to accidentally write a slow cross join or an unbounded aggregation through
  it, unlike `query_sql` where a `timeout`/`memory_limit` backstop is needed precisely because the
  query shape is unconstrained (§2).
- **Trivially auditable.** `aggregate_events(group_by=["event_type"])` says exactly what it
  touched in one audit-log line. `query_sql(sql="...")` requires reading the actual query text to
  know its blast radius — still safe (§2), just not a one-glance read the way a fixed parameter
  list is.
- **Statically type-checked before execution.** Monty's stub-based type check (built into
  `CodeMode`) catches a wrong argument type on `query_events(limit="ten")` before the tool ever
  runs. `query_sql`'s `sql` argument is free text with no static check — a syntax mistake always
  costs a retry round-trip.
- **No materialization/connection setup per call.** `query_events`/`aggregate_events` reuse the
  Parquet cache directly via a lazy Polars scan; `query_sql` opens and configures a fresh DuckDB
  connection per call (§2). Cheap either way after the §2 fix, but the typed tools have strictly
  less moving state per call.

None of this is about raw query speed anymore (§2 fixed that gap) — **reach for `query_sql` when
the typed tools are structurally unable to express the question**, not merely when SQL would be
more convenient:
- **Nested fields** — `tls.sni`, `dns.queries[].rrtype`, anything under a Parquet `STRUCT`/`LIST`
  column. `UNNEST` and struct dot-access solve this directly; the typed tools have no path to it
  at all short of pulling raw rows and writing a manual tally loop in the sandbox (exactly what
  happened investigating DNS/TLS behavior — see `PROJECT.md`'s account of that run).
- **Correlation across event types** — e.g. join `dns` and the `tls` connection that followed it
  by `src_ip` and a timestamp window, to spot DNS-then-connect beaconing patterns. There is no
  typed-tool equivalent of a `JOIN`.
- **Statistical/window functions** — `stddev`/`percentile_cont` over inter-arrival times for
  beacon-interval detection, `ROW_NUMBER() OVER (PARTITION BY ...)` for per-host sessionization,
  `HAVING` clauses, multi-level `GROUP BY` combinations that would otherwise mean extending
  `aggregate_events`'s parameter surface indefinitely for every combination a skill might want.

In short: typed tools are the fast, foolproof common path; `describe_events` makes their schema
discoverable; `query_sql` is the escape hatch for exploratory or structurally nested/correlated
analysis that would otherwise force the model back into slow, error-prone client-side Python
over raw pages.

### Companion improvements to the typed path

Two narrow additions close common-path gaps without recreating SQL through an expanding parameter
language:

1. **`describe_events(name, offset=0, limit=100)`** returns bounded Parquet schema metadata as
   `{"columns": [{"name": ..., "dtype": ...}, ...], "offset": ..., "returned": ...,
   "has_more": ...}`. It uses `pl.scan_parquet(path).collect_schema()` and never scans data rows.
   Schema discovery is important before authoring SQL because a sampled row may contain a null
   `dns`/`tls` struct and therefore conceal its nested fields. `offset` must be non-negative and
   `limit` is capped at `MAX_SCHEMA_COLUMNS`; names and dtype strings are host-derived, never
   model-authored.
2. **Top-level exact-match filters on both existing tools.** Add
   `equals: dict[str, str | int | float | bool | None] | None = None` to `query_events` and
   `aggregate_events`. Entries are combined with AND and applied before projection/grouping;
   `None` means `is_null()`. Cap the mapping at `MAX_EQUAL_FILTERS`, validate every key with the
   existing top-level field-name rules, and accept only the declared scalar values. Preserve the
   dedicated `event_type` argument for compatibility and reject `event_type` inside `equals` so
   there is no ambiguous double specification. This covers routine IP, port, protocol, flow-ID,
   and boolean lookups while remaining cheap, typed, and one-line auditable.

Deliberately do **not** add typed wrappers or parameters for sorting, range predicates, `IN`,
histograms, time buckets, nested paths, arbitrary expressions, joins, or statistics in this
change. `aggregate_events(group_by=[...])` already supplies distinct/top-frequency values; the
remaining operations belong in `query_sql` unless repeated real investigations establish another
narrow common path.

---

## 2. Security Model: Constraining the Connection, Not the Query Text

### Design history: why this isn't "materialize into a TABLE, then lock down"

The first draft of this plan registered the cached Parquet file by fully materializing it —
`CREATE TABLE events AS SELECT * FROM read_parquet(path)` — before disabling external access,
reasoning that a `VIEW` re-reads the file lazily on every query and would break once access was
locked down (true — see the table below). That draft had a real, measured bug: `CREATE TABLE ...
AS SELECT *` pulls in *every* column, including this schema's several large `STRUCT` columns
(`flow`, `tcp`, `quic`, `tls`, `dns`, `stats`), before running even a single-column query. Measured
against the project's real 176MB fixture (13.8MB compressed Parquet cache, 312,161 rows):

| Path | Time | Memory |
| --- | --- | --- |
| Polars lazy scan + `group_by("event_type")` (what `aggregate_events` already does) | ~15ms | negligible |
| DuckDB `CREATE TABLE events AS SELECT * FROM read_parquet(...)`, then the same query | ~2,000ms | **OOM'd at 512MB, 768MB, and 1GB**; needed >1.5GB |

That's not a synthetic worst case — this is the project's one real test fixture, and it OOM'd at
memory limits that sounded generous on paper. (This matches prior experience with DuckDB against
large JSON-derived data more generally, not something specific to this schema.) Isolating it
further: materializing only `event_type` took 24ms under 256MB; materializing 6 flat columns took
70ms; even the full nested `tls` struct alone took 63ms under 512MB. The blowup is entirely
`SELECT *` eagerly pulling every wide struct column regardless of what the model's query actually
needs — exactly the kind of self-inflicted cost the typed tools never pay, because Polars' lazy
scan pushes column/predicate selection down into the Parquet reader instead of materializing
first and filtering after.

**Fix: `allowed_paths`, not `CREATE TABLE`.** DuckDB has a second, more granular access-control
setting — `allowed_paths` (a list of exact file paths *always* queryable even with
`enable_external_access=false`) — that was missed in the first pass. With the cache file
allowlisted, a lazy `VIEW` over `read_parquet(...)` keeps working *after* lockdown (the earlier
"views break" finding was specifically about an un-allowlisted file), and DuckDB's own
Parquet-native pushdown does the same column/predicate pruning Polars does — no eager
materialization, no OOM risk proportional to total file width. Re-measured with the fix: **13.6ms**
for the identical `group_by("event_type")` query (matching Polars), **23.7ms** for a nested
`tls.sni` query with no materialization step at all.

### Verified DuckDB 1.5.5 behavior (not assumed)

| Claim | Verified how | Result |
| --- | --- | --- |
| `SET enable_external_access=false` blocks `read_csv`/`read_parquet` on arbitrary paths, `COPY TO`, `ATTACH` to a file path | Ran each against `/etc/passwd`, `/tmp/pwn.db`, `/tmp/pwn.csv` with the flag set | All raised `PermissionException` |
| The flag cannot be re-enabled once set false, in the same connection | Called `SET enable_external_access=true` after setting it false | `InvalidInputException: Cannot enable external access while database is running` |
| `ATTACH ':memory:' AS other` (no path) still works with the flag set | Ran it directly | Succeeds — harmless, no filesystem/network reach, not a bypass |
| `allowed_paths=['<exact file>']` keeps that one file queryable even after lockdown, via a lazy `VIEW` — no materialization needed | Set it, locked down, created a `VIEW` over `read_parquet(<that file>)`, queried it | Worked, 13.6ms for a flat group-by matching Polars' own time |
| `allowed_paths` matches **exact paths, not prefixes** | Copied the allowed file to a sibling path sharing its filename as a prefix (`<hash>-evil.parquet`), queried it | Blocked with `PermissionException` — confirms no prefix-match bypass |
| `allowed_paths` (like `temp_directory`) can only be changed **before** lockdown | Tried `SET allowed_paths=[...]` after lockdown | `InvalidInputException: Cannot change allowed_paths when enable_external_access is disabled` |
| A `VIEW` over `read_parquet(...)` for a **non**-allowlisted file still needs filesystem access at query time (lazy) and fails post-lockdown | Created the view before lockdown for a file not in `allowed_paths`, queried it after | `PermissionException` — confirms `allowed_paths` (not view-vs-table) was the actual missing piece |
| `temp_directory` can only be changed **before** the flag is set false | Tried `SET temp_directory=...` after lockdown | `PermissionException: Modifying the temp_directory has been disabled` |
| `memory_limit` bounds the in-memory buffer pool but does **not** hard-fail an over-limit query | Set `memory_limit='64MB'`, ran a 20M-row materialization | Succeeded — spilled to `temp_directory` instead of erroring |
| `con.interrupt()` from a separate thread reliably cancels a running query | Started a cross-join expected to run for minutes, called `interrupt()` after 1s from a watchdog thread | `InterruptException` after ~1.0s |
| `duckdb.extract_statements(sql)` parses without executing and reports statement count + `StatementType` per statement, with **no extra dependency** | Ran it against multi-statement input, `ATTACH; SELECT`, `DROP TABLE`, `WITH ... SELECT`, `EXPLAIN SELECT` | Correctly distinguishes count and type in every case; `WITH ... SELECT` reports as `StatementType.SELECT` |
| A cp314 wheel exists (`manylinux_2_26_{aarch64,x86_64}`) | Checked PyPI JSON for `duckdb` 1.5.5 | Present — no dependency blocker, unlike polars this isn't an abi3 wheel so future duckdb releases must be re-checked per Python version bump |

### Required connection sequence

Order matters — several of the findings above are one-way locks, so setup must happen in this
exact sequence or the later steps silently fail or can't be undone:

```python
con = duckdb.connect(":memory:")

# DuckDB does not accept a prepared parameter in CREATE VIEW ... read_parquet(?), so every
# host-controlled path used in SQL must be encoded as a SQL string literal. Doubling single
# quotes is DuckDB's standard string-literal escaping; never interpolate a raw Path directly.
def sql_string_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"

# 1. Host-controlled resource limits and the allowlist, ALL BEFORE lockdown -- temp_directory
#    and allowed_paths can't be changed after enable_external_access=false, so both must be
#    set first. `validated_cache_path` comes only from ensure_parquet_cache -- the model
#    never supplies it.
scratch_dir = <per-call, per-task scratch directory>          # created here, removed in finally
scratch_literal = sql_string_literal(scratch_dir)
cache_literal = sql_string_literal(validated_cache_path)
con.execute(f"SET temp_directory={scratch_literal}")
con.execute(f"SET memory_limit='{SQL_MEMORY_LIMIT}'")
con.execute(f"SET allowed_paths=[{cache_literal}]")

# 2. Register a lazy VIEW (not a materialized TABLE) over the one allowlisted file. No
#    eager read here -- DuckDB's Parquet reader pushes column/predicate selection down to
#    whatever the model's query below actually asks for, the same way Polars' lazy scan does.
con.execute(f"CREATE VIEW events AS SELECT * FROM read_parquet({cache_literal})")

# 3. Lock down. From this point, no filesystem or network access is reachable from ANY SQL
#    text executed on this connection, including the model's, EXCEPT the one allowlisted
#    path from step 1 -- this is the actual security boundary, not the statement-shape
#    validation in step 4.
con.execute("SET enable_external_access=false")

# 4. Defense in depth, not the primary control: reject anything but exactly one SELECT
#    (or WITH ... SELECT) statement, using DuckDB's own parser via extract_statements --
#    catches accidental multi-statement input and non-SELECT statement types with a clear
#    ModelRetry message instead of a confusing downstream engine error. ATTACH/DROP/COPY/etc.
#    are already unreachable via step 3 regardless of this check.
statements = duckdb.extract_statements(sql)
if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
    raise DataToolError("sql must be exactly one SELECT (or WITH ... SELECT) statement")

# 5. Apply a host-controlled relational LIMIT so `has_more` is accurate regardless of whether
#    the model's own query had a LIMIT -- same limit+1 trick query_events already uses. Use
#    DuckDB's relation API instead of textually wrapping the SQL: the parser accepts a terminal
#    semicolon followed by a comment, which cannot be made safe to embed with `rstrip(";")`.
limited_result = con.sql(sql).limit(limit + 1)

# 6. Wall-clock timeout via a watchdog thread calling interrupt() -- there is no
#    statement_timeout config in this DuckDB version; interrupt() is the only cancellation
#    primitive that was confirmed to actually stop a running query.
# 7. Fetch `limited_result`, shape it into {"columns", "rows", "returned", "has_more"}, close the
#    connection, remove scratch_dir -- in a finally, so a timeout/error never leaks the
#    temp directory or an open connection.
```

### What this does and does not bound

- **Does bound**: filesystem reads/writes anywhere except the one allowlisted, exact-matched
  `events` source file (§ table above — confirmed not prefix-matchable); network access (blocks
  extension loading, e.g. `httpfs`); wall-clock query duration (interrupt-based timeout);
  returned row count (`has_more`-style outer `LIMIT`); statement shape (single `SELECT`/`WITH
  ... SELECT`).
- **Does not bound**: temporary disk usage from spilling on a genuinely expensive query (e.g. a
  self cross-join or a wide `GROUP BY` over high-cardinality columns) before the timeout fires
  (bounded in *time* by the timeout, not in bytes — `memory_limit` only decides *when* spilling
  starts, not a hard cap) and CPU/memory for the duration up to the timeout. This is a
  resource-usage concern proportional to what the model's *own query* actually does, not a
  self-inflicted materialization tax (that's what the `allowed_paths` fix removed) and not a
  confidentiality/traversal one — the spill goes to a host-controlled `scratch_dir` cleaned up in
  `finally`, never a path the model chose. Listed as an open item in §5, same posture as the
  "Retention and permissions" open item in `refs/workspace-lifecycle.md`.
- **No materialization cost, no column-pruning hack needed.** Because registration is a lazy
  `VIEW` over an allowlisted path rather than an eager `TABLE`, `query_sql` doesn't need any
  logic to guess which columns a query references ahead of time (an idea considered and dropped
  once `allowed_paths` was found — it would have meant parsing the model's SQL for column
  references ourselves, essentially reimplementing what DuckDB's own pushdown already does for
  free once the lazy `VIEW` path is available).

---

## 2A. Result Serialization

DuckDB's Python API returns nested `STRUCT`/`LIST` columns as native Python `dict`/`list` already
(confirmed via `fetchall()` against the real `tls`/`dns` columns), consistent with what
`query_events`/`aggregate_events` already hand back from Polars. Three things still need explicit
handling before a row reaches the sandbox, mirroring gotchas already documented in
`sandbox_notes.md` for the model's own code:

- **Recursive JSON normalization**: non-JSON-native scalars (`datetime.datetime`,
  `decimal.Decimal`, UUIDs, bytes, etc.) can occur at the top level *or inside* a returned
  `STRUCT`/`LIST`. Walk every result value recursively: preserve `None`/`bool`/`int`/`float`/`str`,
  convert tuples/lists to lists of normalized values, convert dict values recursively and dict
  keys to strings, and use `str(value)` for every other scalar. Normalizing only the top-level
  tuple is insufficient — the real schema can return a `dict` containing a `datetime` or
  `Decimal`.
- **Column names as dict keys**: a `SELECT count(*) AS "weird key"` or an unaliased expression
  (`count(*)` with no `AS`) needs a stable, always-string column name. Use
  `limited_result.columns` rather than trusting the model to alias every projected column.
- **Duplicate projected names are rejected**: DuckDB preserves duplicates in result metadata
  (`SELECT 1 AS x, 2 AS x` reports `["x", "x"]`). Zipping those columns into a row dict would
  silently discard a value, so detect duplicates before fetching and raise `DataToolError`
  listing the repeated names. The model can retry with unique aliases.

---

## 3. Code Changes Breakdown

### A. Dependencies (`pyproject.toml`)

Add **only** `duckdb`:
```toml
dependencies = [
    ...
    "duckdb>=1.5.5",
    "polars>=1.0.0",
    ...
]
```
> Unlike `polars-runtime-32` (abi3, forward-compatible), `duckdb` ships CPython-version-specific
> wheels. A cp314 wheel exists today (verified in §2's table) — re-verify on every Python version
> bump, since there is no abi3 safety net here.

### B. `skill_runner/data_tools.py` — typed-tool enhancements and new functions

Add the following bounds alongside the existing constants:

```python
MAX_SCHEMA_COLUMNS = 500
MAX_EQUAL_FILTERS = 10
MAX_SQL_ROWS = 500                   # mirrors MAX_PAGE_ROWS
SQL_QUERY_TIMEOUT_SECONDS = 10
SQL_MEMORY_LIMIT = "512MB"
```

Add `equals` to both existing tools and route it through one shared validator/expression builder
so their filtering semantics cannot drift:

```python
def query_events(
    ...,
    event_type: str | None = None,
    equals: dict[str, str | int | float | bool | None] | None = None,
    ...
) -> dict[str, object]: ...

def aggregate_events(
    ...,
    event_type: str | None = None,
    equals: dict[str, str | int | float | bool | None] | None = None,
    ...
) -> dict[str, object]: ...
```

Add the bounded schema helper, reusing `ensure_parquet_cache` and returning string dtype
representations so the result remains a plain serializable dict:

```python
def describe_events(
    name: str,
    source_root: Path,
    cache_root: Path,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, object]:
    """Return a bounded page of top-level Parquet column names and dtype descriptions."""
```

Finally, add `query_sql` alongside all three typed helpers, reusing `ensure_parquet_cache`
unchanged (same name validation, same containment defense, same fingerprinted cache —
`query_sql` never adds a second cache or a second path-resolution path):

```python
def query_sql(
    name: str,
    source_root: Path,
    cache_root: Path,
    sql: str,
    limit: int = 100,
) -> dict[str, object]:
    """Run one read-only SELECT (or WITH ... SELECT) against `name`'s cached Parquet file,
    exposed as a lazy `events` view over an allowlisted path (never a materialized table --
    see §2's design history for why that first draft OOM'd). See §2 for the full
    connection-lockdown sequence this wraps; every step there runs inside this function, in
    order, in a try/finally that always closes the connection and removes the scratch
    temp_directory."""
```

Bounds: `1 <= limit <= MAX_SQL_ROWS`; `sql` must be non-empty and parse (via
`duckdb.extract_statements`) to exactly one `SELECT`/`WITH ... SELECT` statement. All validation
failures raise `DataToolError` (the existing `ModelRetry` subclass), matching
`query_events`/`aggregate_events`'s existing error-handling contract — invalid SQL should read as
retry feedback the model can act on, not a crashed run.

Return shape: `{"columns": [...], "rows": [...], "returned": N, "has_more": bool}` — an explicit
`columns` list (unlike `query_events`, whose columns are implied by the caller's own `columns=`
argument) since `query_sql`'s projected columns are whatever the model's `SELECT` list produced.
Every row is a dict keyed by those columns after recursive JSON normalization. Duplicate column
names are a validation error rather than a lossy dict conversion.

### C. `skill_runner/run_core.py` — wiring

1. The existing bound `query_events`/`aggregate_events` closures expose their new `equals`
   argument. `_build_data_tools()` also gains bound `Tool(describe_events, sequential=True)` and
   `Tool(query_sql, sequential=True)` objects, added to both the `Agent(tools=[...])` list and
   `CodeMode(tools=["describe_events", "query_events", "aggregate_events", "query_sql"])`.
   **Reuse `sequential=True`** — this project already hit the bug where a plain function tool
   renders as `async def` (requiring `await`) by default inside `run_code`; the fix for
   `query_events`/`aggregate_events` was `Tool(..., sequential=True)`, and skipping it for either
   new helper would reintroduce the exact same failure mode.
2. No new mounts, no new reserved names, no change to source-root selection — `query_sql` reuses
   `context.source_root`/`context.parquet_cache_root` exactly as the other two tools do.

### D. `skill_runner/resilience.py`

Add `"describe_events"` and `"query_sql"` to `DATA_TOOL_NAMES` (the `OverflowingToolOutput`
exemption set added for `query_events`/`aggregate_events` — see `PROJECT.md`'s account of that
fix). Both have bounded return contracts and the same nested-tool-call exposure to
`after_tool_execute` interception; without the exemption an oversized dict could be replaced by
a spill-path string and silently break sandbox code expecting the documented result shape.

### E. `prompts/sandbox_notes.md`

Extend "Fast Input Queries" with `describe_events`, `equals` syntax/AND/null semantics, and the
typed-vs-SQL guidance from §1. Add a SQL subsection documenting: the fixed `events` view name per
`name=`; that only one `SELECT`/`WITH ... SELECT` statement is accepted (no DDL, no multiple
statements, no `ATTACH`/`COPY`/`PRAGMA`); that aggregates are exact over the complete file (the
query runs against the full `events` view regardless of `limit` — `query_sql`'s boundedness is
only on *returned* rows via `has_more`, unlike `query_events`'s page, which is explicitly not a
complete sample); the `limit`/`MAX_SQL_ROWS` cap; and when to prefer it over the typed tools.
Per the sequencing constraint already established for `sandbox_notes.md`
(`DATA_SOURCE_SINK_PLAN.md` §4D), this text must land in the same change as §3B/§3C, never ahead
of them.

---

## 4. Non-Goals (v1)

- **No multi-file joins.** `query_sql`'s `name` targets exactly one cached file, same as
  `query_events`/`aggregate_events`. Joining two source files in one query is a real, plausible
  future ask (e.g. correlating two log types) but is out of scope here — it would mean
  registering multiple allowlisted views per call and deciding how `name` generalizes to a list,
  which deserves its own design pass rather than being folded in speculatively.
- **No cross-call connection/view caching.** Every call opens a fresh connection and re-registers
  the `events` view. This costs little now that registration is a lazy view rather than an eager
  materialization (§2) — connection setup, not data loading, is the only remaining per-call
  overhead — so there's nothing to cache yet; revisit only if that setup cost is shown to matter
  at a scale not yet observed.
- **No configurable timeout/memory limit per call.** `SQL_QUERY_TIMEOUT_SECONDS`/
  `SQL_MEMORY_LIMIT` are fixed module constants, not model-controlled arguments — letting the
  model raise its own resource ceiling defeats the point of having one.

---

## 5. Verification Plan

Per project convention, tests assert **behavior**, not implementation details of DuckDB itself.

1. **Unit Tests (`tests/test_data_tools.py` additions):**
   - `describe_events` returns bounded, paginated column names and dtype strings from a fixture
     containing flat and nested columns without collecting rows; invalid offsets/limits are
     rejected.
   - `query_events` and `aggregate_events` apply multiple top-level equality filters with AND
     semantics over strings, numbers, booleans, and nulls. Both reject unknown/invalid fields,
     unsupported values, more than `MAX_EQUAL_FILTERS`, and `event_type` duplicated through
     `equals`; aggregate counts remain exact over the complete filtered input.
   - Single-`SELECT` queries against a small fixture Parquet-backed `events` view return
     correctly shaped `{"columns", "rows", "returned", "has_more"}`.
   - A valid query ending in `; -- trailing comment` is accepted and bounded correctly, guarding
     the relation-based limit against regression to fragile textual SQL wrapping.
   - `WITH ... SELECT` (CTE) is accepted; multi-statement input (`SELECT 1; SELECT 2`),
     non-`SELECT` statements (`DROP TABLE events`, `ATTACH ':memory:' AS x; SELECT 1`, `EXPLAIN
     SELECT 1`), and empty/whitespace-only `sql` are all rejected with `DataToolError`.
   - Source/cache/scratch directories containing a single quote still work, proving every
     host-controlled path is encoded as a DuckDB string literal rather than interpolated raw.
   - **Traversal/exfiltration attempts inside the SQL text itself are inert, not merely
     rejected**: `SELECT * FROM read_parquet('/etc/passwd')`, `COPY events TO '/tmp/x.csv'`,
     `ATTACH '/tmp/x.db' AS x`, and a query against a *sibling* cache file (same directory,
     prefix-sharing filename, not the one passed as `name`) all fail with the engine's own
     `PermissionException` surfaced as a `DataToolError` — asserting the *connection* blocks
     these (via `enable_external_access`/`allowed_paths`), not a string blocklist that could be
     bypassed by a syntax variant the tests didn't anticipate.
   - A query engineered to run past `SQL_QUERY_TIMEOUT_SECONDS` (e.g. a large self cross join)
     is interrupted and raises `DataToolError`, and the scratch `temp_directory` is removed
     afterward regardless.
   - `limit`/`has_more` pagination bounds: zero, negative, and over-`MAX_SQL_ROWS` limits
     rejected; a query returning more than `limit` rows reports `has_more=True` and exactly
     `limit` rows.
   - A `GROUP BY` aggregate over more rows than `MAX_SQL_ROWS` still reports the *exact* leading
     groups (demonstrating the query scans the complete `events` view, only the returned row
     count is capped) — the same "aggregates aren't limited by page caps" property
     `aggregate_events` already guarantees, verified here for SQL aggregates too.
   - A nested-field query (`UNNEST`/struct dot-access against a fixture with a `STRUCT`/`LIST`
     column) returns correctly shaped nested dicts/lists — the core motivating capability.
   - Timestamp/decimal values are converted to strings at the top level and when nested inside
     structs/lists; the complete result is JSON-serializable. A query projecting duplicate names
     (`SELECT 1 AS x, 2 AS x`) is rejected with `DataToolError` instead of losing one value.
   - **Regression guard for the materialization bug this design replaced**: a `SELECT
     event_type, count(*) FROM events GROUP BY event_type`-shaped query against a fixture with
     several wide, mostly-null `STRUCT` columns alongside the queried flat column completes
     quickly and without approaching `SQL_MEMORY_LIMIT` — asserting the fix (lazy view +
     `allowed_paths`) rather than re-introducing eager `SELECT *` materialization.
2. **Integration Test (`tests/test_run_core.py` additions):**
   - Extend the existing `FunctionModel`-driven `DataToolsWiringTests` with `describe_events`,
     filtered `query_events`/`aggregate_events`, and `query_sql` cases. Drive `run_code` with
     synchronous calls (no `await` — catching the same sequential-signature regression class
     caught for the original tools) and assert correctly shaped results reach the model.
   - A `query_sql` call containing a traversal/exfiltration attempt (per the unit test above)
     surfaces as a retry the run recovers from, not a crash — mirroring
     `test_query_events_traversal_attempt_is_retried_not_crashed`.
3. **Manual verification against the real fixture** (176MB EVE log, per the DNS/TLS
   characterization run already exercised in this project): confirm a `query_sql` call answering
   the same DNS/TLS questions that previously required a manual client-side tally loop — a
   `SELECT unnest(dns.queries).rrtype AS rrtype, count(*) FROM events WHERE event_type='dns' GROUP
   BY 1 ORDER BY 2 DESC` — returns the exact same distribution in one call, with no pagination
   loop and no client-side counting.
