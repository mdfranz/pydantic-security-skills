# Runtime & Workspace Conventions

These instructions apply to every skill run through this runner. They describe the execution
environment itself (the Monty sandbox, the workspace, and how this runner persists artifacts) —
not any particular log format. Skill-specific instructions follow after this section.

## Sandbox Notes

Generated code runs in the Monty sandbox, not host Python: no third-party imports and only
`sys`, `typing`, `asyncio`, `math`, `json`, `re`, `datetime`, `os`, `pathlib` are available (no
`collections`, no `orjson`/`polars`/`duckdb`). There is no `open()` builtin — use `pathlib.Path`
instead. The workspace is mounted read-write at `/workspace`, so any files written with
`write_file` are reachable at `/workspace/<name>` from sandboxed code — do not assume a fixed
input filename; always confirm it first (see the skill's own "Find the input file" step). The
skill's own directory is separately mounted **read-only** at `/skill`, so its reference material
lives at `/skill/references/<name>` — readable from `run_code` only, since the FileSystem tool's
root is the workspace, not the skill directory.

Three different path namespaces are in play — do not mix them up:
- **`run_code` (Monty sandbox), workspace mount**: paths are absolute against the mount, e.g.
  `/workspace/<filename>`.
- **`run_code` (Monty sandbox), skill mount**: read-only, e.g. `/skill/references/<name>`. Writes
  here fail — this mount exists for reading reference material, not saving anything.
- **`list_directory` / `read_file` / `write_file` (FileSystem tool)**: paths are relative to the
  workspace root itself, e.g. `list_directory(path='.')` or `write_file('notes.md', ...)`. Passing
  `/workspace` (or `/skill`) to these tools fails with "Path resolves outside the root directory" —
  those prefixes are only meaningful inside `run_code`.

File objects from `pathlib.Path(...).open()` do **not** support `for line in f:` — that raises
`TypeError: '_io.TextIOWrapper' object is not iterable`. Always read line-by-line with an explicit
loop instead:
```python
f = pathlib.Path("/workspace/<filename>").open()
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
- `socket`, `exec`, and `eval` are not available.
- `os.listdir`, `os.walk`, and `os.path` have no members in this sandbox — list a directory
  with `pathlib.Path(p).iterdir()` and join paths with `pathlib.Path(a) / b`, not `os.path.join`.
- `json.dumps(obj, default=str)` fails with `TypeError: JSONEncoder.__init__() got an
  unexpected keyword argument 'default'` — the `default=` kwarg isn't supported. Convert
  non-JSON-serializable values (e.g. cast to `str`/`int`) before calling `json.dumps`, not via
  a fallback hook.
- f-string format specs don't support the comma thousands-separator (`f"{n:,}"` raises a
  `SyntaxError`) — build the separators manually if needed, or just print the raw number.
- A `run_code` return value with a `dict` keyed by a non-string (e.g. a port number pulled
  straight from a log field, `results[dest_port] = ...`) fails tool-result validation with a
  `pydantic_core.ValidationError` like `Input should be a valid string [type=string_type,
  input_value=3478, input_type=int]`. Always `str()` the key when building a dict you intend to
  return or print as JSON: `results[str(dest_port)] = ...`.

## Running a Saved Script

Monty has no `exec`/`eval` and no way to `import` a file saved in `/workspace` — sandbox
restrictions block both. There is no "run this file" call. To reuse a saved script, `read_file`
its text and pass that text (edited as needed — e.g. a different input filename) as the `code`
argument of your next `run_code` call. That resubmission *is* how reuse works here; treat the
saved `.py` file as a code template you paste from, not a module you import.

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
