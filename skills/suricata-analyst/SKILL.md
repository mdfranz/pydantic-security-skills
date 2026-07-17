---
name: suricata-analyst
description: Analyzes Suricata EVE JSON logs to identify network threats, suspicious egress, and protocol anomalies. Use when a user provides eve.json logs, asks for network traffic analysis, or needs to hunt for C2 beaconing and data exfiltration.
metadata:
  author: "Security Engineering Team"
  version: "1.1.0"
  tags: ["suricata", "nsm", "threat-hunting", "network-security"]
---

# Suricata (EVE) Analyst

## Instructions

### Sandbox Notes
Generated code runs in the Monty sandbox, not host Python: no third-party imports and only
`sys`, `typing`, `asyncio`, `math`, `json`, `re`, `datetime`, `os`, `pathlib` are available (no
`collections`, no `orjson`/`polars`/`duckdb`). There is no `open()` builtin — use `pathlib.Path`
instead. The workspace is mounted read-write at `/workspace`, so any files written with
`write_file` are reachable at `/workspace/<name>` from sandboxed code — but do not assume the
*input* EVE log is named `eve.json`; see "Find the input file" below.

Two different path namespaces are in play — do not mix them up:
- **`run_code` (Monty sandbox)**: paths are absolute against the mount, e.g. `/workspace/eve.json`.
- **`list_directory` / `read_file` / `write_file` (FileSystem tool)**: paths are relative to the
  workspace root itself, e.g. `list_directory(path='.')` or `write_file('notes.md', ...)`. Passing
  `/workspace` to these tools fails with "Path '/workspace' resolves outside the root directory" —
  the `/workspace` prefix is only meaningful inside `run_code`.

File objects from `pathlib.Path(...).open()` do **not** support `for line in f:` — that raises
`TypeError: '_io.TextIOWrapper' object is not iterable`. Always read line-by-line with an explicit
loop instead:
```python
f = pathlib.Path("/workspace/eve.json").open()
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

### Step 1: Initial Discovery
1.  **Find the input file**: The EVE log's filename is not fixed — it may be `eve.json` or a
    rotated/timestamped name like `eve-2026-01-21-01.json`. Before writing any analysis code, call
    `list_directory(path='.')` (the FileSystem tool, not `run_code`) to see what's actually in the
    workspace and use that exact filename in subsequent `run_code` calls as `/workspace/<filename>`.
2.  **Check for existing scripts**: The same `list_directory(path='.')` call also shows any
    `suricata_*.py` scripts left over from earlier sessions. If one matches the current task (same
    kind of analysis — e.g. `suricata_tls_sni.py` for a TLS/SNI question), `read_file` it and inspect
    its logic before writing anything new. If it fits, reuse its logic — see "Running a saved
    script" below — instead of re-deriving the analysis from scratch. If it's close but not quite
    right (wrong input filename, needs an extra filter), adapt it rather than starting over. Only
    write new logic when no existing script covers the task.
3.  **Sample the Data**: Always begin by sampling the logs to understand the schema and volume.
    ```python
    import json
    import pathlib

    sampled = 0
    f = pathlib.Path("/workspace/eve.json").open()  # use the filename list_directory returned
    while True:
        line = f.readline()
        if not line:
            break
        if not line.strip():
            continue
        print(json.dumps(json.loads(line), indent=2))
        sampled += 1
        if sampled == 5:
            break
    f.close()
    ```
4.  **Identify Event Types**: Determine which protocols are present.
    ```python
    import json
    import pathlib

    event_types: dict[str, int] = {}
    f = pathlib.Path("/workspace/eve.json").open()  # use the filename list_directory returned
    while True:
        line = f.readline()
        if not line:
            break
        if line.strip():
            event_type = json.loads(line).get("event_type", "unknown")
            event_types[event_type] = event_types.get(event_type, 0) + 1
    f.close()

    for event_type, count in sorted(event_types.items(), key=lambda kv: -kv[1]):
        print(count, event_type)
    ```
5.  **Consult References**: For detailed field mapping, refer to `references/eve_format.md`.

**Running a saved script**: Monty has no `exec`/`eval` and no way to `import` a file saved in
`/workspace` — sandbox restrictions block both. There is no "run this file" call. To reuse a saved
script, `read_file` its text and pass that text (edited as needed — e.g. a different input
filename) as the `code` argument of your next `run_code` call. That resubmission *is* how reuse
works here; treat the saved `.py` file as a code template you paste from, not a module you import.

### Step 2: Targeted Analysis
1.  **Filter Noise**: Ignore `stats` events and focus on external traffic.
2.  **Create Persistence**: Save every reusable analysis script to `/workspace` with a short, descriptive `suricata_<purpose>.py` name (e.g. `pathlib.Path("/workspace/suricata_tls_sni.py").write_text(...)`, or the `write_file` tool — either works, but the filename rule applies regardless of which one you use). Do not add timestamps to code filenames. Save intermediate normalized datasets or summaries when they will be reused. Before writing a new script, check it doesn't already exist under a different name (see Step 1) — update or reuse the existing one instead of creating a near-duplicate.
3.  **Document Findings**: Include the complete final findings, evidence, and file references in your final answer to the user — the runner automatically saves that answer as `analyst_log-YY-MM-DD_HH-MM-SS.md` in the workspace. Do not also write your own `analyst_log-*` file; that would duplicate the runner's report under a different (and likely inaccurate) timestamp.

## Working Agreements
- **No Deletions**: Do not delete scripts or intermediate output files.
- **Artifact Persistence**: Do not leave analysis code or important results only in `run_code` output. Write scripts, summaries, and reports to `/workspace` so they survive the run, using `pathlib.Path(...).write_text(...)` or the `write_file` tool.
- **Output Naming**: This is a naming rule, not a tool choice — it applies no matter how the file gets written. Keep reusable code filenames stable and descriptive, such as `suricata_tls_sni.py` or `suricata_egress_volume.py`, never timestamped. Use timestamps only for session reports (`analyst_log-*`) and throwaway outputs.
- **Python Style**: The sandbox only has stdlib `json` and `pathlib` available — there is no `orjson`, `polars`, or `duckdb`. Parse EVE lines with `json.loads` and stream over the file with `pathlib.Path(...).open()` and `f.readline()` in a `while` loop (see Sandbox Notes — `for line in f:` is not supported) rather than loading it all into memory.
- **Script Naming**: All scripts should start with `suricata_` and use a short purpose-based name (for example, `suricata_tls_sni.py`), without timestamps.
- **Reuse Before Rewrite**: Always check `/workspace` for an existing `suricata_*.py` script before writing new analysis logic (Step 1). Read and adapt it rather than re-deriving the same analysis — see "Running a saved script" above for how to actually execute a saved script's contents.

## Examples

### Example 1: Hunting for Rare SNIs
**User says**: "Check for suspicious TLS connections."
**Action**:
1. Filter for `event_type: "tls"`.
2. Extract `tls.sni` and count occurrences.
3. Highlight SNIs that appear fewer than 3 times across the dataset.

### Example 2: Volume-based Exfiltration
**User says**: "Find any hosts sending large amounts of data to the internet."
**Action**:
1. Query `event_type: "flow"`.
2. Sum `bytes_toserver` by `src_ip` where `dest_ip` is external.
3. Calculate Producer-Consumer Ratio (PCR).

## Troubleshooting

### Error: "Invalid JSON" or "Line Truncated"
**Cause**: The EVE log might have been cut off during a copy or crash.
**Solution**: Use a Python script that catches `json.JSONDecodeError`, reports the affected line, and skips malformed records while validating the rest of the file.

### Error: "Out of Memory"
**Cause**: Ingesting extremely large JSON files without streaming.
**Solution**: Process the EVE log incrementally with `pathlib.Path("/workspace/eve.json").open()` and `f.readline()` in a `while` loop, retaining only the fields and aggregates needed for the analysis.

### Error: "No alerts found"
**Cause**: The log might only contain metadata, or the signature engine wasn't triggered.
**Solution**: Pivot to protocol-based hunting (DNS/TLS) using `references/eve_format.md` for field guidance.
