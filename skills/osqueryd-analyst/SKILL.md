---
name: osqueryd-analyst
description: Analyzes osqueryd results logs to identify suspicious process execution, anomalous network connections, package installations, modified environment variables, and interactive shell history. Use when a user provides osqueryd results logs (typically JSON/JSONL format), asks for host telemetry analysis, or needs to hunt for host-based compromises.
metadata:
  author: "Security Engineering Team"
  version: "1.2.0"
  tags: ["osquery", "host-security", "threat-hunting", "endpoint-detection"]
---

# Osqueryd Results Analyst

## Instructions

### Conventions
- **Input file**: The osqueryd results log's filename is not fixed — it may be
  `osqueryd.results.log` or a rotated/timestamped name like `osqueryd-results-2026-04-14.log`.
- **Script prefix**: Reusable scripts use the `osqueryd_` prefix, e.g.
  `osqueryd_network_connections.py`, `osqueryd_process_lineage.py`.

(The sandbox mechanics, script-reuse pattern, and workspace conventions that apply here are in
the runtime notes prepended to these instructions — see those before writing any code.)

### Step 1: Initial Discovery
1.  **Find the input file**: Before writing any analysis code, call `list_directory(path='.')`
    (the FileSystem tool, not `run_code`) to see what's actually in the workspace and use that
    exact filename in subsequent `run_code` calls as `/workspace/<filename>`. The same call also
    surfaces any existing `osqueryd_*.py` scripts — check for prior scripts and `analyst_log-*.md`
    reports per "Reuse Before Rewrite" in the runtime notes before writing anything new.
2.  **Sample the Data**: Always begin by sampling the logs to understand the schema and volume.
    ```python
    import json
    import pathlib

    sampled = 0
    f = pathlib.Path("/workspace/osqueryd.results.log").open()  # use the filename list_directory returned
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
3.  **Identify Event Types**: Determine which osqueryd queries are present.
    ```python
    import json
    import pathlib

    query_names: dict[str, int] = {}
    f = pathlib.Path("/workspace/osqueryd.results.log").open()  # use the filename list_directory returned
    while True:
        line = f.readline()
        if not line:
            break
        if line.strip():
            name = json.loads(line).get("name", "unknown")
            query_names[name] = query_names.get(name, 0) + 1
    f.close()

    for name, count in sorted(query_names.items(), key=lambda kv: -kv[1]):
        print(count, name)
    ```
4.  **Consult References**: For detailed field mapping, read `/skill/references/osqueryd_format.md`
    from inside `run_code` (e.g. `pathlib.Path("/skill/references/osqueryd_format.md").read_text()`).

### Step 2: Targeted Analysis
1.  **Filter Noise**: Ignore expected system processes (e.g., standard `systemd` or idle system workers) and focus on anomalies or non-standard paths.
2.  **Persist and Report**: Save reusable scripts under the `osqueryd_` prefix and document
    findings in your final answer, per "Saving Artifacts" in the runtime notes.

## Examples

### Example 1: Correlating Processes with Sockets
**User says**: "Correlate active network connections to their parent processes."
**Action**:
1. Filter for `name: "net_processes"` to collect connection sockets (PID, local/remote IPs, and ports).
2. Filter for `name: "all_processes"` to build a process directory mapping `pid` to `cmdline`, `path`, and `parent`.
3. Join them by `pid` to identify the command lines establishing external connections.

### Example 2: Auditing Shell History for Malicious Activity
**User says**: "Look for suspicious commands executed in interactive shells."
**Action**:
1. Filter for `name: "shell_history"`.
2. Extract the `command`, `history_file`, and `uid` columns.
3. Highlight commands modifying `/etc/hosts`, downloading remote scripts (e.g. `curl`, `wget`), or executing encoded shells.

## Troubleshooting

### Error: "No records found for query X"
**Cause**: The query might not have been scheduled in the osqueryd configuration, or had no differential changes.
**Solution**: Check for other process-related queries (like `active_processes` instead of `all_processes`) or pivot your threat hunt to look at general system activity logs.

(For general log-parsing errors — invalid JSON, out of memory — see "Common Troubleshooting" in
the runtime notes.)
