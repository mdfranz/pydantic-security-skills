# Runtime & Workspace Conventions

These instructions apply to every skill run through this runner. They describe the execution
environment itself (the Monty sandbox, the workspace, and how this runner persists artifacts) —
not any particular log format. Skill-specific instructions follow after this section.

## Sandbox Notes

Generated code runs in the Monty sandbox, not host Python: no third-party imports and only
`sys`, `typing`, `asyncio`, `math`, `json`, `re`, `datetime`, `os`, `pathlib` are available (no
`collections`, no `orjson`/`polars`/`duckdb`). There is no `open()` builtin — use `pathlib.Path`
instead. This run has its own task workspace, mounted read-write at `/workspace`, so any files
written with `write_file` are reachable at `/workspace/<name>` from sandboxed code — this is
where you save reusable scripts, reports, and other output; it accumulates or resets per the
runner's task mode, but it never contains the case's input evidence. Canonical input evidence
(the logs you're asked to analyze) is separately mounted **read-only** at `/data-source` (also
aliased at `/data`, for backwards compatibility — both point at the same directory) — the runner
lists what's there at the start of your prompt (as `/data-source/<filename>`); use those exact
paths in `run_code`, and do not assume a fixed input filename. The skill's own directory is also
mounted **read-only**, at `/skill`, so its reference material lives at `/skill/references/<name>`
— readable from `run_code` only, since the FileSystem tool's root is the task workspace, not the
skill directory or `/data-source`.

Four different path namespaces are in play — do not mix them up:
- **`run_code` (Monty sandbox), workspace mount**: read-write, absolute against the mount, e.g.
  `/workspace/<filename>`. This is where you write scripts and other output — never input data.
- **`run_code` (Monty sandbox), data mount**: read-only, e.g. `/data-source/<filename>` (also
  reachable at `/data/<filename>`). This is where case input evidence lives; the runner tells you
  what's there at the start of your prompt. Writes here fail.
- **`run_code` (Monty sandbox), skill mount**: read-only, e.g. `/skill/references/<name>`. Writes
  here fail — this mount exists for reading reference material, not saving anything.
- **`list_directory` / `read_file` / `write_file` (FileSystem tool)**: paths are relative to the
  task workspace root itself, e.g. `list_directory(path='.')` or `write_file('notes.md', ...)`.
  This tool can only reach the task workspace — it cannot see `/data-source` or `/skill` at all,
  so use `run_code` (not this tool) to discover or read input files. Passing `/workspace`,
  `/data-source`, `/data`, or `/skill` to this tool fails with "Path resolves outside the root
  directory" — those prefixes are only meaningful inside `run_code`.

## Fast Input Queries: `query_events(...)`, `aggregate_events(...)`, `describe_events(...)`

For large input logs, host-side helpers are callable directly from `run_code` — call them
synchronously, without `await`:

```python
page = query_events(name="eve-2026-01-06-01.json", event_type="alert",
                     equals={"proto": "TCP", "dest_port": 443},
                     columns=["timestamp", "src_ip", "dest_ip"], limit=100)
# page = {"rows": [...], "offset": 0, "returned": 100, "has_more": True}

totals = aggregate_events(name="eve-2026-01-06-01.json", group_by=["event_type"])
# totals = {"groups": [{"event_type": "alert", "count": 4213}, ...], "truncated": False}

schema = describe_events(name="eve-2026-01-06-01.json", limit=100)
# schema = {"columns": [{"name": "tls", "dtype": "Struct[...]"}, ...], "offset": 0,
#           "returned": 42, "has_more": False}
```

All take `name`, the **bare input filename** exactly as listed under `/data-source` at the start
of your prompt (e.g. `eve-2026-01-06-01.json`) — **not** a path. Do not pass `/data-source/...`,
`/data/...`, `/workspace`, `..`, or an absolute path; those are rejected. `query_events`/
`aggregate_events`/`describe_events` run Polars **on the host** — you still cannot
`import polars`/`duckdb` inside the sandbox; the sandbox stdlib limits below still apply to your
own code. The first call for a given file converts it to a compressed Parquet cache (one-time
cost); later calls (from any of these tools, including `query_sql`) reuse that cache.

**`query_events`** returns a bounded **page** for inspection, never a statistically complete
sample:
- `event_type` (optional) filters to one event type; `equals` (optional) adds exact-match filters
  over other top-level fields (max 10 entries, combined with AND, `None` means "is null" —
  put `event_type` in the dedicated argument, not inside `equals`); `columns` (optional) selects
  fields (max 50, no duplicates); `offset`/`limit` paginate (`limit` defaults to 100, capped at
  500).
- The result is a dict, not a plain row list: `rows` (at most `limit` rows), `offset`, `returned`,
  and `has_more` — check `has_more` and re-call with a larger `offset` to page through the rest.
  Never infer a whole-file total or distribution from one page.

**`aggregate_events`** computes an **exact** count over the *complete* filtered dataset, ignoring
`limit` for the input it scans — use this instead of counting rows out of a `query_events` page:
- Accepts the same `event_type`/`equals` filters as `query_events`, applied before grouping.
- With no `group_by`, returns `{"count": <exact total>}`.
- With `group_by` (max 10 field names, no duplicates), returns `{"groups": [...], "truncated":
  bool}` — groups are sorted by count descending and capped at `limit` (default 100, max 500);
  `truncated` is true when more groups existed than were returned. The count *per group* is
  always exact regardless of truncation — only which groups are returned is capped.

**`describe_events`** returns a bounded page of top-level column names and dtype strings without
scanning any data rows (`offset`/`limit`, `limit` capped at 500) — call this before authoring a
`query_sql` query, since a sampled row's `dns`/`tls` struct may be null and conceal nested fields
that are present elsewhere in the file.

Prefer the typed tools over hand-rolled line-by-line NDJSON parsing for large files:
`query_events` for a filtered slice to inspect, `aggregate_events` for exact totals or
breakdowns, `describe_events` to discover schema. Reach for `query_sql` (below) only when the
question is structurally beyond what these three can express — nested fields, joins across
event types, or window/statistical functions — not merely when SQL would be more convenient.

## Model-Authored SQL: `query_sql(...)`

```python
result = query_sql(
    name="eve-2026-01-06-01.json",
    sql="WITH u AS (SELECT unnest(dns.queries) AS q FROM events WHERE event_type = 'dns') "
        "SELECT q.rrtype AS rrtype, count(*) AS n FROM u GROUP BY 1 ORDER BY 2 DESC",
    limit=100,
)
# result = {"columns": ["rrtype", "n"], "rows": [{"rrtype": "A", "n": 1832}, ...],
#           "returned": 6, "has_more": False}
```

`query_sql` runs one read-only SQL query, authored by you, host-side via DuckDB against the same
Parquet cache the typed tools use — the file's rows are always queryable as a fixed view named
`events`, regardless of the `name` you pass. This is the escape hatch for what the typed tools
structurally cannot express:
- **Nested fields** — `tls.sni`, `dns.queries[].rrtype`, anything under a `STRUCT`/`LIST` column.
  Use `UNNEST(...)` and struct dot-access (`tls.sni`), as in the example above. Call
  `describe_events` first if you're not sure a field is present or what it's called.
  **`UNNEST` cannot appear in the same `SELECT` as a `GROUP BY`** — DuckDB's binder rejects it
  (`Binder Error: UNNEST not supported here`) because `UNNEST` is a set-returning expression and
  can't be reconciled with aggregation in one scope. Always split it into a `WITH` CTE that does
  the `UNNEST`, then `GROUP BY` in the outer `SELECT` over the CTE, exactly as in the example
  above — never write `SELECT unnest(col).field, count(*) FROM events ... GROUP BY 1` directly.
- **Correlation across event types** — `JOIN events a ON ... JOIN events a2 ON ...`-style
  self-joins, e.g. matching a `dns` row to the `tls` connection that followed it.
- **Statistical/window functions** — `stddev`, `percentile_cont`, `ROW_NUMBER() OVER (...)`,
  `HAVING`, multi-level `GROUP BY` — anything beyond `aggregate_events`'s single-level grouping.

Rules and limits:
- `sql` must be exactly one `SELECT` (or `WITH ... SELECT`) statement — no DDL (`CREATE`,
  `DROP`, `ATTACH`), no `COPY`, no `PRAGMA`, no multiple statements separated by `;`. Anything
  else is rejected before it runs, with a message you can act on.
- `limit` (default 100, capped at 500) bounds only how many **rows are returned** — an
  aggregate like the `GROUP BY` example above always computes over the *complete* `events` view
  regardless of `limit`, exactly like `aggregate_events`. Check `has_more` the same way you would
  for `query_events`.
- Every projected column must have a unique name — alias duplicate expressions
  (`count(*) AS n`, not two unaliased `count(*)`) or the call is rejected.
- There is no filesystem or network access reachable from your SQL text beyond the one
  `events` view — attempts to read another file, `ATTACH` a database, or `COPY` out fail with a
  clear error, not a silent no-op, so don't spend a retry probing for it.
- Queries are bounded by a wall-clock timeout; a query that runs too long is cancelled and
  reported as an error rather than left to hang.

File objects from `pathlib.Path(...).open()` do **not** support `for line in f:` — that raises
`TypeError: '_io.TextIOWrapper' object is not iterable`. Always read line-by-line with an explicit
loop instead:
```python
f = pathlib.Path("/data-source/<filename>").open()
while True:
    line = f.readline()
    if not line:
        break
    ...
f.close()
```

The sandbox's static type checker does not resolve builtin exception names like
`FileNotFoundError` in `except` clauses (it errors with "used when not defined"). Catch
`except Exception as e:` instead and inspect `type(e).__name__` if you need to branch on the
error type.

**Do not write scripts in host-CLI shape.** A saved `.py` file is never executed with
`python script.py <args>` — it is reused by reading its text and pasting it as the `code`
argument of a `run_code` call (see "Running a Saved Script" below), so it always runs as a
top-level script, never as an imported module. This means:
- Never gate logic behind `if __name__ == "__main__":` — `__name__` is not defined in the
  sandbox, so this raises `NameError: name '__name__' is not defined`. Put the entry-point
  code directly at the top level of the file (still fine to define helper functions above it).
- Never assign to `sys.argv` — the sandbox's `sys` module does not allow attribute
  assignment (`AttributeError: 'module' object has no attribute 'argv' and no __dict__ for
  setting new attributes`). If a script needs a parameter, set it as a plain variable near the
  top of the file and edit that value when adapting the script, rather than reading it from
  `sys.argv`.

A few other stdlib/builtin gaps that are easy to reach for out of habit and will fail:
- `collections` (including `Counter`) is not importable — use a plain `dict` with
  `d[key] = d.get(key, 0) + 1` instead of `Counter`.
- `ipaddress` is not importable. For the limited address checks an analysis needs, work with the
  string fields already present in the record (for example, compare an exact address or split an
  IPv4 address on `.`); do not add an `ipaddress` import.
- `socket`, `exec`, and `eval` are not available.
- `os.listdir`, `os.walk`, and `os.path` have no members in this sandbox — list a directory
  with `pathlib.Path(p).iterdir()` and join paths with `pathlib.Path(a) / b`, not `os.path.join`.
- `str.format()` is not available. Use simple f-strings without advanced format specifications,
  or concatenate strings. In particular, do not use `"...".format(...)` as a fallback for an
  unsupported f-string format.
- `json.dumps(obj, default=str)` fails with `TypeError: JSONEncoder.__init__() got an
  unexpected keyword argument 'default'` — the `default=` kwarg isn't supported. Convert
  non-JSON-serializable values (e.g. cast to `str`/`int`) before calling `json.dumps`, not via
  a fallback hook.
- f-string format specs don't support the comma thousands-separator (`f"{n:,}"` raises a
  `SyntaxError`) — do not retry with forms such as `f"{n:>8,d}"`; build the separators manually
  if needed, or just print the raw number.
- A `run_code` return value with a `dict` keyed by a non-string (e.g. a port number pulled
  straight from a log field, `results[dest_port] = ...`) fails tool-result validation with a
  `pydantic_core.ValidationError` like `Input should be a valid string [type=string_type,
  input_value=3478, input_type=int]`. Always `str()` the key when building a dict you intend to
  return or print as JSON: `results[str(dest_port)] = ...`.
- In `query_sql`, `UNNEST(...)` combined with `GROUP BY` in the same `SELECT` fails with
  `Binder Error: UNNEST not supported here` — wrap the `UNNEST` in a `WITH` CTE and `GROUP BY` in
  the outer query instead (see the `query_sql` section above for the working form).

## Python Style & Working Agreements

To ensure stability, efficiency, and to prevent token limits from being exceeded, adhere to the following coding rules:
- **Avoid Token Overflow**: Printing full records or raw dumps of large files directly into the execution output can exceed context token limits. Always aggregate, filter, and summarize data programmatically in your Python code first.
- **Output Limits**: Limit printed detail (such as individual log lines, event details, or lists of records) to at most ~50 lines of detail per `run_code` call, unless the user explicitly requests a full listing. If more data exists, print a summary (e.g., total count, top 10 elements) instead.

## Running a Saved Script

Monty has no `exec`/`eval` and no way to `import` a file saved in `/workspace` — sandbox
restrictions block both. There is no "run this file" call. To reuse a saved script, `read_file`
its text and pass that text (edited as needed — e.g. a different input filename) as the `code`
argument of your next `run_code` call. That resubmission *is* how reuse works here; treat the
saved `.py` file as a code template you paste from, not a module you import.

Do not try to bridge the FileSystem workspace with a relative `pathlib.Path("script.py")` inside
`run_code`: that relative path is not the task workspace and commonly raises `FileNotFoundError`.
Use the native `read_file(path="script.py")` tool to obtain code for resubmission. Use
`pathlib.Path("/workspace/script.py")` only when sandboxed code genuinely needs to read or write
an artifact by its absolute sandbox-mount path; it still cannot execute or import that file.

## Reuse Before Rewrite

Before writing new analysis code, always check the workspace for prior work relevant to the
current task — reusing or adapting beats re-deriving from scratch:

- **Existing scripts**: `list_directory(path='.')` shows every reusable script left over from
  earlier sessions, named with this skill's own prefix (see the skill's instructions below for
  the exact prefix and naming pattern). If one matches the current task, `read_file` it and
  inspect its logic before writing anything new — see "Running a Saved Script" above for how to
  actually execute it. If it's close but not quite right (wrong input filename, needs an extra
  filter), adapt it rather than starting over. Only write new logic when no existing script
  covers the task.
- **Prior findings**: There may be many `analyst_log-*.md` reports from earlier sessions
  (potentially hundreds), so don't rely on `list_directory` for this — it returns every entry
  with no limit and will flood context. Instead:
  - Run `find_files('analyst_log-*.md')` to see how many reports exist and their timestamps.
  - If there are more than a handful, use `search_files` with `include_glob='analyst_log-*.md'`
    and a regex for terms relevant to the current task (an IP, hostname, PID, or topic keyword)
    to find which specific reports already cover it — this is bounded even when the listing
    itself is not.
  - `read_file` only the reports that actually matched, not the whole set, so you build on what
    was already found instead of rediscovering it from scratch. Reference prior findings in your
    new report where they inform the current analysis, and note anything that has changed since.

## Saving Artifacts

- **No Deletions**: Do not delete scripts or intermediate output files.
- **Artifact Persistence**: Do not leave analysis code or important results only in `run_code`
  output. Write scripts, summaries, and reports to `/workspace` so they survive the run, using
  `pathlib.Path(...).write_text(...)` or the `write_file` tool — either works, but the naming
  rule below applies regardless of which one you use.
- **Output Naming**: Save every reusable analysis script to `/workspace` using this skill's own
  short, descriptive `<prefix>_<purpose>.py` naming convention (see the skill's instructions
  below for the exact prefix), never timestamped. Use timestamps only for session reports
  (`analyst_log-*`) and throwaway outputs. Save intermediate normalized datasets or summaries
  when they will be reused. Before writing a new script, check it doesn't already exist under a
  different name (see "Reuse Before Rewrite" above) — update or reuse the existing one instead
  of creating a near-duplicate.
- **Document Findings**: Include the complete final findings, evidence, and file references in
  your final answer to the user — the runner automatically saves that answer as
  `analyst_log-YY-MM-DD_HH-MM-SS.md` in the workspace. Do not also write your own `analyst_log-*`
  file; that would duplicate the runner's report under a different (and likely inaccurate)
  timestamp.

## Common Troubleshooting

### Error: "Invalid JSON" or "Line Truncated"
**Cause**: The log might have been cut off during a copy, transfer, or crash.
**Solution**: Use a Python script that catches `json.JSONDecodeError`, reports the affected
line, and skips malformed records while validating the rest of the file.

### Error: "Out of Memory"
**Cause**: Ingesting an extremely large log file completely into memory at once.
**Solution**: Process the log incrementally with `pathlib.Path(...).open()` and `f.readline()`
in a `while` loop, retaining only the fields and aggregates needed for the analysis rather than
holding every raw record.
