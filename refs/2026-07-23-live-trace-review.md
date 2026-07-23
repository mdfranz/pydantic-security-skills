# Live-Trace Review: Query-Tool Fixes, Latency Diagnostics, and a Before/After Efficiency Audit

**Date:** 2026-07-23
**Method:** Live review of real `suricata-analyst` runs against `eve-2026-01-06-01.json`, each
cross-checked against **two independent sources** — the local `workspace/logs/*.jsonl` audit
trail and the `tomfoolery` Logfire project — rather than either alone. See [PROJECT.md](../PROJECT.md)'s Phase 22
for the summary version of this work; this document is the detailed evidence trail behind it.

This review touched three real, separately-committable pieces of work (a prompt/doc defect, a
diagnostics gap, and a retrospective efficiency question), plus one scoped-but-undecided finding.
They're presented in the order they were found.

---

## 1. `UNNEST` + `GROUP BY`: the project's own canonical example was broken

### Discovery

Reviewing trace `019f8fb8f5cb733f47672ece66c0f480` (root span `b8b3eb78a3f121f6`, task
`task-5635a679-4669-4512-b923-8128e344b840`, `suricata-analyst`/`deepseek-v4-pro`, 2026-07-23
~13:23-13:32 UTC) turned up:

```
DataToolError: sql query failed: Binder Error: UNNEST not supported here
```

on a `query_sql` call authoring:

```sql
SELECT unnest(dns.queries).rrtype AS rrtype, count(*) AS n
FROM events WHERE event_type = 'dns' GROUP BY 1 ORDER BY 2 DESC
```

That query is a near-verbatim copy of two places in the repo:
- The worked example in `prompts/sandbox_notes.md`'s `query_sql` section — prepended to *every*
  skill run.
- The "manual verification" target query named in `SQL_QUERY_PLAN.md` §3.

Both put `UNNEST(...)` and `GROUP BY` in the same `SELECT`. DuckDB's binder rejects that
combination: `UNNEST` is a set-returning expression and can't be reconciled with aggregation in
one scope. Checking `tests/test_data_tools.py`, the only test that actually exercises `UNNEST`
(`test_nested_unnest_query_returns_nested_shapes`) wraps it in a `WITH` CTE first — the design
doc's own canonical example had never been exercised by that test, so nothing caught the defect
before it started actively steering every model onto a query guaranteed to fail on first try.

### Fix

Rewrote both examples to the working CTE form:

```sql
WITH u AS (SELECT unnest(dns.queries) AS q FROM events WHERE event_type = 'dns')
SELECT q.rrtype AS rrtype, count(*) AS n FROM u GROUP BY 1 ORDER BY 2 DESC
```

- `prompts/sandbox_notes.md`: replaced the broken worked example, and added an explicit gotcha in
  two places — inline next to the `UNNEST` guidance, and in the compact stdlib-gotchas list —
  stating plainly that `UNNEST` cannot appear in the same `SELECT` as a `GROUP BY`.
- `SQL_QUERY_PLAN.md` §3: fixed the same broken query in the "manual verification" target,
  with a parenthetical explaining why the CTE split is necessary.
- No `skill_runner/data_tools.py` changes were needed — this was purely a prompt/doc defect, not
  a bug in the tool implementation.
- Filed and closed as `ISSUES.md` #14.

### Verification

1. Ran `tests.test_data_tools.QuerySqlTests.test_nested_unnest_query_returns_nested_shapes`
   directly — passes, confirming the CTE form is genuinely correct DuckDB syntax, not just
   plausible-looking.
2. Ran the full `QuerySqlTests` class (13 tests) — no regressions.
3. **Corroborating evidence from a later, independent trace**: reviewing a separate 4-checkpoint
   interactive session later the same day (§3 below), checkpoints 2 and 4 both ran nested
   `dns.queries` unnest+aggregate queries repeatedly via the corrected CTE pattern, with zero
   `UNNEST`/`GROUP BY` binder errors anywhere in the session — despite that being exactly the
   error class just fixed. This is one model, one session, so it isn't proof the fix is the
   cause, but it's consistent with it working.

---

## 2. Query-latency diagnostics: a wrong hypothesis, disproven with real evidence

### The anomaly

A project-wide Logfire query (`tomfoolery`, last 2 days) over `execute_tool` span durations:

| tool | n | avg | p50 | p95 | max |
|---|---|---|---|---|---|
| `query_sql` | 119 | 69.4ms | 65.6ms | 133.6ms | 203.7ms |
| `query_events` | 383 | 46.8ms | 28.9ms | 45.4ms | **2070.2ms** |
| `aggregate_events` | 45 | 17.8ms | 13.8ms | 42.6ms | 44.0ms |
| `describe_events` | 9 | 15.5ms | 9.1ms | 48.4ms | 48.4ms |

`query_events`'s max is a ~46x outlier over its own p95. The 5 slowest calls (909ms-2070ms) were
all small (`limit<=50`) queries, and all were the first data-tool call of their respective run.

### The hypothesis that turned out to be wrong

The obvious explanation: each new task gets a fresh, empty Parquet cache (`ensure_parquet_cache`
converts NDJSON → Parquet on first use, per `data_tools.py`), so the first query per task always
pays that one-time conversion cost.

This was disproven with three independent pieces of evidence, not just re-reading the code:

1. **Code**: `run_core.py:203-213` sets `parquet_cache_root` from `options.workspace.resolve()` —
   the **top-level** `./workspace` directory (the CLI default) — not the per-task
   `workspace/task-<uuid>/` directory. The per-task directory (`ws_path`) is a structurally
   separate thing, created later, only for the agent's own writable scratch space.
2. **Disk**: `workspace/data-sink/parquet/` held exactly one cache file
   (`f0385e1d....parquet`, 13.8MB), and none of the 56 `task-*/` directories on disk had their
   own `data-sink/parquet/` at all — confirming the cache is genuinely shared and persistent, not
   rebuilt per task. The cache key (`_cache_identity`: `sha256(source_root|name|st_dev|st_ino|
   st_size|st_mtime_ns)`) is also stable across runs, since the source file's inode and mtime
   never change (`stat` showed `Modify: 2026-07-17 22:17:13`, untouched since).
3. **`process_pid`**: querying Logfire's `process_pid` column ruled out "one-time Polars import
   cost per OS process" too — two runs sharing the same PID (`21282`), 3 minutes apart, both
   still paid a slow first `query_events` call despite the second run reusing the same warm
   process *and* the same already-built cache file.

So the cache is not being rebuilt, and it isn't a one-time process-level cost either — but
*something* about the first data-tool call of each run is still consistently slower, and nothing
in the existing telemetry could distinguish "still somehow a cache rebuild" from "lock
contention" from "genuine cold-disk-I/O on first touch." The local audit log's `duration_ms`
(added in Phase 20) made this worse, not better, for this specific question: it only times the
**outer** `run_code` call and never isolates `query_sql`/`aggregate_events`/`query_events`
individually, since they're plain function calls inside the sandbox, not separate `pydantic_ai`
tool events.

### Fix: DEBUG-level cache and query timing, wired into Logfire

`skill_runner/data_tools.py` (silent by default — plain stdlib `logging`, no new dependency):

- `ensure_parquet_cache`: now logs an explicit `parquet cache hit` or `parquet cache miss` line
  with `lock_wait_ms` (fcntl contention) and, on a miss, `convert_ms` — closing the "was this
  actually a rebuild?" question directly instead of by inference.
- `query_events` / `aggregate_events`: log `collect_ms` for the actual Polars `.collect()` call,
  separately from cache resolution.
- `query_sql`: logs `execute_ms` for the DuckDB `fetchall()` call.

Verified directly (not just by inspection):

```
DEBUG parquet cache miss name='a.json' key=da324ed5e59e lock_wait_ms=0.0 convert_ms=17.0
DEBUG query_events collect_ms=3.9 name='a.json' event_type='dns' rows=5
DEBUG parquet cache hit name='a.json' key=da324ed5e59e lock_wait_ms=0.1
DEBUG query_events collect_ms=2.0 name='a.json' event_type='dns' rows=5
```

`skill_runner/run_core.py:_configure_logfire` now attaches `logfire.LogfireLoggingHandler()` to
the `data_tools` logger at `DEBUG`, but **only when `--logfire` is passed** — so these lines ride
along as log entries in the same Logfire traces this project already queries, at zero cost to
non-`--logfire` runs. Verified end-to-end with a real `logfire.configure(...)` + console exporter
before wiring it into `run_core.py`. This design choice (wire to Logfire rather than leave as
local-only debugging) was confirmed with the user directly rather than assumed.

**Live confirmation, same session**: reviewing a later trace (root span `29d2c36864f8e4d8`,
§3 below) showed the new debug lines already appearing as real Logfire spans —
`parquet cache hit name='eve-2026-01-06-01.json' key=f0385e1dfbe4 lock_wait_ms=0.3` and
`aggregate_events collect_ms=41.7 name='eve-2026-01-06-01.json' ... groups=6` — confirming the
wiring works against a real run, not just the manual test.

### Test coverage

45/45 `test_data_tools.py` tests pass unchanged; 58/58 combined with `test_run_core.py`. This is
logging only — no behavior change, no return-value change, verified by the fact that no existing
test needed modification.

### Status

Filed as `ISSUES.md` #15, **left open**. The debug logs make cache state directly observable
going forward, but the root cause of the original 2070ms outlier (cold-disk-I/O? something else
entirely?) is still not conclusively identified — that's deliberate; the fix is diagnostic
capability, not a guessed-at performance fix.

---

## 3. Live-trace agent-performance review

### Session reviewed

A full 4-checkpoint **interactive** session, `task-25132bfe-4b07-4476-8a37-5c1beae1cdb6`,
`suricata-analyst`/`deepseek-v4-pro`, against `eve-2026-01-06-01.json`. Interactive mode gives
each checkpoint its own Logfire trace/root span, all sharing one audit-log file — confirmed by
matching the audit log's 4 `prompt` event timestamps (16:49:26, 16:50:04, 16:52:37, 16:54:18 UTC)
against 4 separate `invoke_agent suricata-analyst` root spans in Logfire.

| # | Trace | Root span | Duration | Topic |
|---|---|---|---|---|
| 1 | `019f8fe1c773ce3ba95e91b53537ae63` | `29d2c36864f8e4d8` | 18.5s | Initial discovery |
| 2 | `019f8fe25d57b5b1816d4f8c070b3fcb` | `774488d65be5771f` | 94.1s | Adware hunt (TLS SNI + DNS) |
| 3 | `019f8fe4b29f0f7e228a10f8c6478dfc` | `6e225264b4cc6968` | 75.2s | OS fingerprinting |
| 4 | `019f8fe63b109ce7529936aa2d6995f9` | `545368b303712267` | 57.6s | PlatinumAI deep-dive |

**Total: 245.4s across 4 turns, 25 `run_code` calls.**

### Finding 1: tool execution is not the bottleneck

Per-checkpoint tool time (`run_code_return.duration_ms` summed, from the audit log) vs. total
trace duration (from Logfire):

| Checkpoint | Trace duration | Tool time | % of total |
|---|---|---|---|
| CP1 initial discovery | 18,533ms | 255ms | 1.4% |
| CP2 adware hunt | 94,060ms | 2,115ms | 2.2% |
| CP3 OS ID | 75,162ms | 4,530ms | 6.0% |
| CP4 PlatinumAI deep-dive | 57,646ms | 1,253ms | 2.2% |

**94-99% of wall-clock time is model inference latency, not tool/data execution**, in every
checkpoint. This means further optimizing `data_tools.py` (§2 above included) has a genuinely low
ceiling for improving overall run time — even the worst single query duration ever observed
(2070ms, §2) is noise against a 60-95 second checkpoint.

### Finding 2: retries are the expensive recoverable cost

3 errors this session, each self-corrected in exactly 1 retry:

| Error | Checkpoint | Recovery turn cost |
|---|---|---|
| `Binder Error: Referenced table "q" not found!` (CTE-scoping — new, single occurrence) | CP2 | 9.8s |
| `Name 'json' used when not defined` (forgot import) | CP3 | 10.1s |
| `json.dumps(row, indent=2, default=str)` (already-documented Monty gotcha) | CP3 | 18.8s |

**~38.7s — 16% of this session's total 245s — was spent purely on error-recovery turns**, not
analysis, because each retry is a full extra model inference round-trip even though the
underlying tool-side error costs milliseconds. The `json.dumps(default=str)` case is the
important one: it's already fully documented in `prompts/sandbox_notes.md`'s gotcha list, and it
**still recurred in this session** — consistent with the established pattern across `ISSUES.md`
#3/#7/#8/#9 that doc-only guidance doesn't reliably stop models from reaching for
idiomatic-but-unsupported Python.

The new CTE-scoping error (`Referenced table "q" not found!`) is a different DuckDB gotcha from
the `UNNEST`+`GROUP BY` one fixed in §1 — the model tried an `EXISTS`-style correlated subquery
referencing a `WITH q AS (...)` CTE alias out of scope, and self-corrected by switching to a
direct `LIKE` filter. Only one occurrence so far; per the pattern the existing `ISSUES.md` entries
follow (#14 was filed because it traced to a design-doc defect; #9/#3 were filed after 2+
independent occurrences), this is a watch-item, not yet filed as its own issue.

### Finding 3: the proven auto-fix pattern doesn't reach this failure class

`ISSUES.md` #3 already shows a fix that works: `script_lint.py`'s `lint_and_fix_scripts()`
deterministically strips the `__name__` guard from saved scripts instead of relying on model
compliance. Checking its actual scope: **it only cleans saved `.py` files between runs**
(`lint_and_fix_scripts(context.ws_path, ...)`, called from `run_core.py:503`). Both errors in
this session happened on live, first-attempt `run_code` calls that were never saved to a script —
that mechanism never had a chance to catch them.

Extending it to live code would mean intercepting the `code` argument before Monty executes it.
Checked where that path lives: `CodeMode` is imported from `pydantic_ai_harness` (confirmed via
`uv pip show pydantic_ai_harness`: a third-party PyPI dependency, v0.7.0, required by but not
owned by this repo). A live pre-execution fix would mean patching or wrapping a third-party
library boundary — a materially bigger, riskier change than the in-repo `script_lint.py` fix.
**No action taken pending a scoping decision** — this is presented as an open design question,
not a shipped fix.

---

## 4. Before/after efficiency and quality audit

### Method: two-source verification, not trusting a summary document

The comparison uses two pre-existing 2026-07-19 reports (`results/pristine-model-
comparison-2026-07-19.md`, `results/logfire-console-eve-model-comparison-2026-07-19.md`) as the
"before" baseline — both against the identical `eve-2026-01-06-01.json` fixture used throughout
this review, from before the Polars/DuckDB tools existed (`data_tools.py` was added in Phase 18,
2026-07-22; `query_sql` in Phase 19, 2026-07-23).

Rather than taking those reports' numbers on faith, both were re-verified independently:

**Against the raw local audit logs** — re-opened all 4 cited `workspace/logs/runner-task-*.jsonl`
files and recomputed `run_start`→`run_end` durations plus `run_code_call`/`write_file` counts
directly from the raw events:

| Audit log (task) | Reported | Recomputed | `run_code` calls | `write_file` |
|---|---:|---:|---:|---:|
| `task-37baec46-...` (qwen crash 1) | 13s | 13.37s | 5 | 0 |
| `task-c3acb612-...` (qwen crash 2) | 136s | 135.83s | 20 | 0 |
| `task-bc559dfe-...` (deepseek-v4-flash) | 188s | 188.41s | 8 | 0 |
| `task-1a32ba46-...` (gemini-3.5-flash) | 168s | 167.68s | 17 | 3 |

All 4 matched within rounding.

**Against Logfire independently** — queried `tomfoolery` for `invoke_agent suricata-analyst`
spans across 2026-07-19 (well within Logfire's 14-day retention from 2026-07-23). This
reproduced, from a completely separate data source, the GLM-5.2 slow-run durations (738.8s,
494.6s, 811.9s — matching the report's 739s/495s/812s) and both qwen3.6-flash crash traces
(`UnexpectedModelBehavior` at 13.4s, `ModelHTTPError` at 135.8s). As a bonus, the same query
window incidentally corroborated `ISSUES.md` #12's separately-documented findings — the 7,737s
and 1,115s `httpx.ReadTimeout` hangs and three `UsageLimitExceeded` traces around 20:14-20:25 UTC
that same day.

### Efficiency: ~3-4x fewer model turns for comparable-or-greater depth

**Before** (hand-rolled Python scanning): completed runs took 105-812s. DeepSeek V4 Flash needed
a **20-request budget** to finish a single analysis pass — two earlier attempts, at 8 and 16
requests, both ran out before ever reaching synthesis. Gemini 3.5 Flash needed **30 requests**,
failing once at 20. Every new question the model wanted to ask ("what about DNS now?", "what
about egress volume?") cost a fresh round of writing a scan loop over the raw 184MB file.

**After** (§3's session): the adware-hunt checkpoint alone — comparably deep, with quantified,
cited findings (exact connection counts, exact SNI lists) — used only **7 model turns**. The full
4-checkpoint session covering discovery + adware + OS fingerprinting + a beaconing deep-dive used
**25 `run_code` calls total**.

Since §3 established tool execution is only 1-6% of wall time either way, this ~3-4x turn-count
reduction translates almost directly into a ~3-4x wall-clock reduction — and it eliminates the
"ran out of request budget before synthesizing" failure mode outright, which hit both models in
the old comparison.

### Quality: better recall of rare facts, no evidence of better judgment

The old comparison's own qualitative analysis (verified against the raw log with `grep`, not just
trusted from the models' reports) surfaced two real signals:

- **A verified 53-hour long-lived flow** (`192.168.2.197→192.200.0.106:80`, `flow.age: 192346`
  seconds, 1-in-227,732 flow records) was flagged as a likely finding in only **1 of 9** old-
  approach runs — including only 1 of the *same model's own* 3 identical reps. A hand-rolled
  `readline()`-loop scan only finds what its ad hoc filter/sort logic happens to check for;
  nothing guarantees a rare outlier gets caught. `aggregate_events`/`query_sql` run **exact,
  complete-dataset** computations by design (`aggregate_events`'s own docstring: "Runs over the
  full lazy frame regardless of `limit`") — a query for, say, the top-10 longest flows is
  deterministically guaranteed to surface that record every time, independent of ad hoc scanning
  luck. This is a structural improvement in recall, not just convenience.
- **A verified MQTT beacon** (`192.168.3.105→20.44.17.102:8883`, exactly 125 flows) was found by
  GLM-5.2 in all 3 of its reps using a consistent beacon-scan methodology — but its own severity
  verdict flipped between reps: "low risk" in rep 1, "HIGH confidence C2, isolate host" in reps 2
  and 3. This is a **judgment/calibration** issue, not a data-completeness one — GLM found the
  signal every time, it just didn't agree with itself about what it meant.

**The distinction matters for what to expect going forward**: the query-tool investment
(Phases 18-19, plus this session's §1 fix) plausibly explains an improvement in whether a rare
fact gets surfaced at all. It does not touch, and nothing in today's data suggests it has
improved, whether a model's own severity/risk judgment about a correctly-found signal is stable
across runs.

---

## 5. Summary and open items

| Item | Status |
|---|---|
| `UNNEST`+`GROUP BY` canonical example broken | **Fixed** — `ISSUES.md` #14, closed |
| Query-latency root cause unclear (cache-miss vs. cold I/O vs. contention) | **Diagnostics added, root cause still open** — `ISSUES.md` #15 |
| DuckDB CTE-scoping (`EXISTS`/correlated subquery referencing an out-of-scope alias) | **Watch-item** — single occurrence, not yet filed |
| Live `run_code` retry cost from recurring Monty gotchas (`json.dumps(default=str)` etc.) | **Not actioned** — `script_lint.py`'s proven fix pattern doesn't reach live code; a live fix would cross into the third-party `pydantic_ai_harness` dependency boundary |
| Polars/DuckDB tools vs. hand-rolled parsing: efficiency | **Confirmed better**, ~3-4x turn-count reduction, two-source verified |
| Polars/DuckDB tools vs. hand-rolled parsing: result recall | **Confirmed better**, structural (exact-aggregation) explanation, two-source verified |
| Polars/DuckDB tools vs. hand-rolled parsing: model judgment consistency | **Unaddressed** — separate, still-open problem |

**Files changed this session** (all uncommitted as of this writing): `prompts/sandbox_notes.md`,
`SQL_QUERY_PLAN.md`, `ISSUES.md` (#14, #15), `skill_runner/data_tools.py`,
`skill_runner/run_core.py`. See [PROJECT.md](../PROJECT.md) Phase 22 for the commit-ready summary.
