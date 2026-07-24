---
name: suricata-analyst
description: Analyzes Suricata EVE JSON logs to identify network threats, suspicious egress, and protocol anomalies. Use when a user provides Suricata logs, asks for network traffic analysis, or needs to hunt for C2 beaconing and data exfiltration.
metadata:
  author: "Security Engineering Team"
  version: "1.4.0"
  tags: ["suricata", "nsm", "threat-hunting", "network-security"]
---

# Suricata (EVE) Analyst

## Instructions

### Conventions
- **Input file**: Treat the bare filename listed by the runner as authoritative. Never assume a
  filename or derive one from a naming convention.
- **Script prefix**: Reusable scripts use the `suricata_` prefix, e.g. `suricata_tls_sni.py`,
  `suricata_egress_volume.py`.

(The sandbox mechanics, script-reuse pattern, and workspace conventions that apply here are in
the runtime notes prepended to these instructions — see those before writing any code.)

### Step 1: Initial Discovery — Host-Side Query Tools First
1.  **Find the input and reusable state**: The runner lists the input's bare filename at the
    start of the prompt. Use that exact value as `name` for the query tools; do not pass a path.
    Call `list_directory(path='.')` to check the task workspace for existing `suricata_*.py`
    scripts and `analyst_log-*.md` reports before writing anything new.
2.  **Batch initial discovery**: In one `run_code` call, call `describe_events` and
    `aggregate_events(group_by=["event_type"])`, then return a compact summary. Retain the
    schema inside that call, but return dtype details only for fields relevant to the next query;
    do not emit the complete schema. Bundle independent baseline queries in that same call.
    Use at most two `run_code` calls before the first user-facing checkpoint.
3.  **Establish exact baseline counts**: Use `aggregate_events` with appropriate filters for
    complete-dataset totals. Do not count records in sandbox Python.
4.  **Inspect bounded examples only when needed**: Use `query_events` with a small `limit` and
    explicit top-level `columns` when representative rows are needed. A page is not a
    complete-dataset result. Query only event types present in the baseline aggregation.
5.  **Use the query interface correctly**: `query_events(columns=...)` accepts top-level column
    names only (for example, `"tls"`, not `"tls.sni"`). Use `query_sql` for nested fields,
    correlations, or window/statistical analysis. Use typed tools for simple counts and exact
    groupings.
6.  **Keep tool use compact**: Do not narrate each sample or query while discovering the data.
    Gather independent evidence in the same `run_code` call, then provide one concise checkpoint
    with findings and the next investigative choice.
7.  **Consult references as needed**: For detailed field mapping, read
    `/skill/references/eve_format.md` from inside `run_code`.

**Do not open or iterate the raw NDJSON log with `pathlib`/`json` for discovery, counts,
filtering, aggregation, or repeated protocol hunts.** The host-side query tools run against the
complete cached dataset and are the required default. Only use direct parsing for a narrowly
scoped operation that the tools demonstrably cannot express; state that limitation and read the
smallest possible subset.

### Step 2: Targeted Analysis
1.  **Filter Noise**: Ignore `stats` events and focus on external traffic.
2.  **Persist and Report**: Save reusable scripts under the `suricata_` prefix and document
    findings in your final answer, per "Saving Artifacts" in the runtime notes.

## Examples

### Example 1: Hunting for Rare SNIs
**User says**: "Check for suspicious TLS connections."
**Action**:
1. In the initial discovery call, retain `describe_events` output and extract the TLS field shape
   needed for the query.
2. Use `query_sql` to extract `tls.sni` and count it over the complete dataset.
3. Highlight SNIs that appear fewer than 3 times, then use `query_events` with top-level columns
   such as `"tls"` to inspect a bounded set of the corresponding connections.

### Example 2: Volume-based Exfiltration
**User says**: "Find any hosts sending large amounts of data to the internet."
**Action**:
1. Call `describe_events` to confirm the flow-byte field names.
2. Use `query_sql` to sum outbound bytes by source where the destination is external.
3. Calculate Producer-Consumer Ratio (PCR) in SQL or from the returned aggregate rows.

## Troubleshooting

### Error: "No alerts found"
**Cause**: The log might only contain metadata, or the signature engine wasn't triggered.
**Solution**: Pivot to protocol-based hunting (DNS/TLS) using `references/eve_format.md` for field guidance.

(For general log-parsing errors — invalid JSON, out of memory — see "Common Troubleshooting" in
the runtime notes.)
