---
name: suricata-analyst
description: Analyzes Suricata EVE JSON logs to identify network threats, suspicious egress, and protocol anomalies. Use when a user provides eve.json logs, asks for network traffic analysis, or needs to hunt for C2 beaconing and data exfiltration.
metadata:
  author: "Security Engineering Team"
  version: "1.2.0"
  tags: ["suricata", "nsm", "threat-hunting", "network-security"]
---

# Suricata (EVE) Analyst

## Instructions

### Conventions
- **Input file**: The EVE log's filename is not fixed — it may be `eve.json` or a
  rotated/timestamped name like `eve-2026-01-21-01.json`.
- **Script prefix**: Reusable scripts use the `suricata_` prefix, e.g. `suricata_tls_sni.py`,
  `suricata_egress_volume.py`.

(The sandbox mechanics, script-reuse pattern, and workspace conventions that apply here are in
the runtime notes prepended to these instructions — see those before writing any code.)

### Step 1: Initial Discovery
1.  **Find the input file**: Before writing any analysis code, call `list_directory(path='.')`
    (the FileSystem tool, not `run_code`) to see what's actually in the workspace and use that
    exact filename in subsequent `run_code` calls as `/workspace/<filename>`. The same call also
    surfaces any existing `suricata_*.py` scripts — check for prior scripts and `analyst_log-*.md`
    reports per "Reuse Before Rewrite" in the runtime notes before writing anything new.
2.  **Sample the Data**: Always begin by sampling the logs to understand the schema and volume.
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
3.  **Identify Event Types**: Determine which protocols are present.
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
4.  **Consult References**: For detailed field mapping, read `/skill/references/eve_format.md`
    from inside `run_code` (e.g. `pathlib.Path("/skill/references/eve_format.md").read_text()`).

### Step 2: Targeted Analysis
1.  **Filter Noise**: Ignore `stats` events and focus on external traffic.
2.  **Persist and Report**: Save reusable scripts under the `suricata_` prefix and document
    findings in your final answer, per "Saving Artifacts" in the runtime notes.

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

### Error: "No alerts found"
**Cause**: The log might only contain metadata, or the signature engine wasn't triggered.
**Solution**: Pivot to protocol-based hunting (DNS/TLS) using `references/eve_format.md` for field guidance.

(For general log-parsing errors — invalid JSON, out of memory — see "Common Troubleshooting" in
the runtime notes.)
