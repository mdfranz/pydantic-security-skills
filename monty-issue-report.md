## Regression: valid multi-line `StackFrame` is rejected for an unhandled external-call exception

`pydantic-monty` 0.0.19 rejects a valid multi-line traceback frame when decoding an unhandled exception injected at an external-function call.

Instead of raising `MontyRuntimeError` for the original exception, the parent reports a worker protocol error:

```
RuntimeError: monty worker protocol error: invalid exception payload: invalid value for StackFrame.end.column: 2 is before start column 9
```

This is a regression from 0.0.18.

## Scope and impact

The error occurs when the parent decodes the traceback for an **unhandled** exception returned by an external function invoked through a multi-line call. A sandboxed `try/except RuntimeError` around the call catches the exception normally; the failure is specifically in the unhandled-exception path that crosses the worker protocol.

This affects integrations such as `pydantic-ai-harness` CodeMode: a host tool error at a multi-line call site becomes an unexpected host-side protocol `RuntimeError`, rather than the normal Monty runtime error that the integration can render or recover from.

The observed failing call sites from the integration used `query_sql(...)`, but the standalone reproducer shows that neither the query implementation, tool-output limiting, an LLM, nor a dataset is required.

Any exception the host returns for the suspended call triggers this, regardless of where it originates. Observed triggers so far include host-side argument validation that raises *before* the tool body runs (an unregistered `offset` kwarg) and an error raised from *inside* the tool body (a DuckDB binder error). Only the call site's shape matters — a multi-line call — not the kind of exception.

## Original project and trigger

This was found during normal use of the public [Pydantic Security Skills](https://github.com/mdfranz/pydantic-security-skills) project. Its `skill-runner` uses `pydantic-ai-harness` CodeMode to expose four bounded, host-side log-query tools inside Monty: `describe_events`, `query_events`, `aggregate_events`, and `query_sql`. The relevant [`query_sql` wrapper and CodeMode configuration](https://github.com/mdfranz/pydantic-security-skills/blob/reign-smart-models/skill_runner/run_core.py#L359-L400) show that `query_sql` accepts `name`, `sql`, and `limit`, but no `offset` argument.

The original run asked an LLM-backed Suricata analyst to:

> Check for suspicious TLS connections in the EVE log — hunt for rare SNIs.

After retrieving the first 500 single-occurrence SNIs, the model generated this multi-line call to request the remainder:

```python
rare_snis2 = query_sql(
    name="eve-2026-01-21-01.json",
    sql="SELECT tls.sni AS sni, count(*) AS n "
        "FROM events WHERE event_type = 'tls' AND tls.sni IS NOT NULL "
        "GROUP BY 1 HAVING count(*) = 1 ORDER BY 1",
    limit=500,
    offset=500,
)
```

In this first captured failure, `offset` is not part of the registered `query_sql` signature, so host-side tool argument validation raises before the tool function executes. CodeMode passes that external exception back into Monty at the suspended call site. 

Because the sandboxed script does not catch it, Monty sends an exception traceback whose frame spans from the `query_sql` name to the later closing parenthesis. The parent then rejects that valid multi-line frame and reports the protocol error, masking the useful underlying tool-validation error.

A second independent run failed through the same traceback-decoding path at another multi-line `query_sql` call. Its underlying tool exception is unavailable because the protocol error replaced it, but the different reported start column matches the different assignment prefix in that run.

One preceding result in the first agent session was large enough to be spilled by the project's tool-output limiter. That was incidental: the standalone reproduction below injects a small synthetic exception directly and demonstrates that output spilling is not part of the failure.

## Root cause

In `crates/monty-proto/src/convert/exception.rs`, `StackFrame::try_from` validates every frame with a preview as though its range were confined to one line. The comment above the check states the assumption explicitly:

```rust
// Monty only attaches a preview for same-line spans with in-bounds
// columns, so rejecting anything else loses no real frames.
if let Some(preview) = &frame.preview_line {
    if end.column < start.column {
        return Err(ProtoConvertError::InvalidValue {
            field: "StackFrame.end.column",
            reason: format!("{} is before start column {}", end.column, start.column),
        });
    }
    // ... followed by the preview-width check ...
}
```

The check does not first compare `start.line` and `end.line`. It therefore rejects valid multi-line ranges such as `(line 1, column 9)` through `(line 3, column 2)`.

The quoted assumption no longer holds. `SourceMap::resolve_range` returns `Some(preview)` on both of its branches — the cached single line when `start_line_idx == end_line_idx`, and a pre-rendered `multiline_preview(...)` block otherwise. There is no path that produces a frame without a preview, so the preview check does not select for same-line frames.

This conflicts with the existing multi-line-frame support:

- `SourceMap::resolve_range` creates a multi-line preview when a range spans different lines.
- `StackFrame::fmt` detects `start.line != end.line`, renders the multi-line preview without caret markers, and returns before reaching the caret-width subtraction.

The malformed-payload check is appropriate for same-line frames because their renderer derives caret width from `end.column - start.column`. It should not be applied across different lines.

The check also runs for frames with `hide_caret: true`, which never render carets at all — further indication that its scope is broader than the underflow it guards.

### Why this is 0.0.19-only

The two changes landed one day apart, both between the 0.0.18 and 0.0.19 releases:

| Commit | Date | Change |
| --- | --- | --- |
| `b89c78f` (#575) | 2026-07-20 | Added the `StackFrame::try_from` column validation and the comment above |
| `048dc2d` (#592) | 2026-07-21 | Added `SourceMap::multiline_preview` and multi-line preview resolution |

`git tag --contains b89c78f` lists only `v0.0.19`, `v0.0.19-beta.5`, and `v0.0.19-beta.6` — the validation is absent from 0.0.18, which matches the observed version behaviour.

The comment's assumption was accurate when it was written and went stale the next day, when multi-line previews began being attached to frames the validator still treats as single-line. This reads as an ordering accident rather than a deliberate protocol restriction on multi-line frames.


## Environment

- `pydantic-monty` 0.0.19 (reproduces)
- `pydantic-monty` 0.0.18 (does not reproduce)
- Python 3.14.3 on Debian 12 x86_64

## Minimal reproduction for 0.0.19

```python
from pydantic_monty import FunctionSnapshot, Monty, MontyComplete

code = '''\
value = query_sql(
    name="eve.json",
)
value
'''

with Monty() as monty:
    with monty.checkout(type_check=False) as session:
        state = session.feed_start(code, skip_type_check=True)
        while not isinstance(state, MontyComplete):
            if isinstance(state, FunctionSnapshot):
                # Models a host-dispatched external tool raising an exception.
                state = state.resume({"exception": RuntimeError("synthetic host error")})
            else:
                state = state.resume_auto()
```

### Expected result

`state.resume(...)` raises `MontyRuntimeError` whose rendered error is the original exception, for example:

```
RuntimeError: synthetic host error
```

### Actual result on 0.0.19

```text
RuntimeError: monty worker protocol error: invalid exception payload: invalid value for StackFrame.end.column: 2 is before start column 9
```

## Observed host-side run failures

The bug appeared in three separate `pydantic-ai-harness` CodeMode runs, across two different models and three different underlying exceptions. The first two are abridged excerpts from the host runner's JSONL audit logs; in both cases the `run_code_call` is followed by the protocol `RuntimeError` and a failed `run_end`, with no `run_code_return`:

```json
{"ts":"2026-08-01T00:59:31.644079+00:00","event":"run_code_call","tool_call_id":"call_5175469c31884ba7a6f133ab","code":"rare_snis2 = query_sql(\n    ...\n)\n..."}
{"ts":"2026-08-01T00:59:31.648661+00:00","event":"error","error":"monty worker protocol error: invalid exception payload: invalid value for StackFrame.end.column: 2 is before start column 14","error_type":"RuntimeError"}
{"ts":"2026-08-01T00:59:31.649285+00:00","event":"run_end","status":"failed"}
```

The second run failed independently with a different start column matching its different assignment prefix:

```json
{"ts":"2026-08-01T00:54:47.568330+00:00","event":"run_code_call","tool_call_id":"call_4a9f2b3a1ec0402c8d9916f1","code":"host133_details = query_sql(\n    ...\n)\n..."}
{"ts":"2026-08-01T00:54:47.661735+00:00","event":"error","error":"monty worker protocol error: invalid exception payload: invalid value for StackFrame.end.column: 2 is before start column 18","error_type":"RuntimeError"}
{"ts":"2026-08-01T00:54:47.661848+00:00","event":"run_end","status":"failed"}
```

These are host-runner failure records rather than worker stderr: the externally visible symptom is that the parent rejects the worker's exception payload, aborts the active `run_code` call, and marks the agent run failed.

### Third run, traced with Logfire

A later run on a different model (`openrouter:deepseek/deepseek-v4-pro`, the earlier two were a different provider) reproduced the same failure and exited 1. This run was traced, so the underlying exception the protocol error masks is recoverable.

The failing call was the fifth `query_sql` in a batched `run_code` block:

```python
rare_ja4 = query_sql(
    name=fn,
    sql="""
    SELECT tls.ja4 AS ja4, count(*) AS n, tls.sni AS sample_sni
    FROM events
    WHERE event_type = 'tls' AND tls.ja4 IS NOT NULL
    GROUP BY 1
    HAVING count(*) <= 5
    ORDER BY 2 ASC
    LIMIT 30
    """,
    limit=30
)
```

`rare_ja4 = ` is 11 characters, so `query_sql` begins at column 12 — matching the reported `start column 12` — and the closing parenthesis sits on its own line, giving the exclusive `end.column: 2`:

```
RuntimeError: monty worker protocol error: invalid exception payload: invalid value for StackFrame.end.column: 2 is before start column 12
```

The trigger here was neither an unregistered argument nor a tool-output spill. The tool body itself raised, because `GROUP BY 1` groups only `ja4` while `tls.sni` is also selected:

```
DataToolError: sql query failed: Binder Error: column "tls" must appear in the GROUP BY clause
or must be part of an aggregate function.
```

The trace preserves the causal chain that the protocol error otherwise destroys — the real exception stays on the inner tool span, while every ancestor carries only the protocol error:

| Span | Level | Exception |
| --- | --- | --- |
| `execute_tool query_sql` | error | `DataToolError: sql query failed: Binder Error: column "tls" must appear in the GROUP BY clause ...` |
| `execute_tool run_code` | error | `RuntimeError: monty worker protocol error: ... 2 is before start column 12` |
| `invoke_agent suricata-analyst` | error | `RuntimeError: monty worker protocol error: ... 2 is before start column 12` |

This is a useful diagnostic workaround, not a fix: the agent run still fails, and the model never sees the binder error it would otherwise have corrected on the next turn.

### Fourth run: single-line and multi-line calls failing side by side

A fourth run (`google:gemini-3.6-flash`, same prompt and dataset) reproduced the failure and also produced a within-session control. Two `query_sql` calls raised `DataToolError` two seconds apart, in the same session, from the same tool. Only the call shape differed.

The first was written on one line, and behaved correctly:

```python
ip_snis = query_sql(name=filename, sql=sql_ip_sni, limit=100)
```

Monty rendered a normal traceback with caret markers, which the harness surfaced as a `ToolRetryError`:

```
  File "<python-input-3>", line 40, in <module>
    ip_snis = query_sql(name=filename, sql=sql_ip_sni, limit=100)
              ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Exception: sql failed to parse: Parser Error: syntax error at or near "REGEXP"
```

The agent read that error and continued. Two seconds later the same tool raised again, this time at a multi-line call site, and the run died:

```python
alerts = query_sql(
    name=filename,
    sql="SELECT alert.signature AS signature, alert.category AS category, count(*) AS count "
        "FROM events WHERE event_type = 'alert' GROUP BY alert.signature, alert.category ORDER BY count DESC",
    limit=50
)
```

`alerts = ` is 9 characters, so `query_sql` begins at column 10:

```
RuntimeError: monty worker protocol error: invalid exception payload: invalid value for StackFrame.end.column: 2 is before start column 10
```

The masked exception, recovered from the trace, was `DataToolError: sql query failed: Binder Error: Referenced table "alert" not found! Candidate tables: "events"` — an error the model had just demonstrated it could recover from when it arrived through the single-line path.

This is the clearest evidence that the call site's shape is the whole trigger. Same model, same session, same tool, same exception class, seconds apart:

| Call shape | Frame | Outcome |
| --- | --- | --- |
| Single line | `start.line == end.line` | Traceback renders, `ToolRetryError`, agent recovers |
| Multi-line | `start.line != end.line` | Payload rejected, protocol `RuntimeError`, run fails |

### Summary of observed runs

| Run | Model | Underlying exception | Start column |
| --- | --- | --- | --- |
| 1 | (provider A) | Unregistered `offset` kwarg, host-side argument validation | 14 |
| 2 | (provider A) | Unavailable — replaced by the protocol error | 18 |
| 3 | `deepseek/deepseek-v4-pro` | `Binder Error: column "tls" must appear in the GROUP BY clause` | 12 |
| 4 | `google:gemini-3.6-flash` | `Binder Error: Referenced table "alert" not found!` | 10 |

Four runs, two providers, four distinct underlying exceptions, four different start columns each matching its own assignment prefix. In every case the end column is the closing parenthesis on a later line.

Changing the call to one line avoids the bug:

```python
value = query_sql(name="eve.json")
```

The indentation of the closing `)` changes the rejected end column but does not prevent the failure. For example, an unindented closing parenthesis yields `end.column: 2`; indenting it by four spaces yields `end.column: 6`.

Those coordinates are valid because they refer to different lines: the expression starts at the `query_sql` name on the first line and ends after the closing parenthesis on a later line. An end column smaller than the start column is invalid only when both locations are on the same line.
    
## 0.0.18 control

The equivalent 0.0.18 snapshot API reproducer completes its exception path correctly:

```python
state = Monty(code, type_check=False).start()
while not isinstance(state, MontyComplete):
    if isinstance(state, FunctionSnapshot):
        state = state.resume({"exception": RuntimeError("synthetic host error")})
    else:
        state = state.resume_auto()
```

It raises `MontyRuntimeError: RuntimeError: synthetic host error` for both the one-line and multi-line call forms.

### End-to-end control on the same integration

The failing gemini-3.6-flash run above was repeated with the same prompt, dataset, and model against a 0.0.18 environment. It completed successfully: exit 0, 14 `run_code` calls, no protocol error, and a finished analysis.

Caveat on the comparison: `pydantic-monty` cannot be downgraded on its own. `pydantic-ai-harness` 0.11.0 raised its `codemode` floor to `pydantic-monty>=0.0.19`, and the 0.0.19 `Monty` class is a worker pool (`Monty()` then `.checkout()`) whereas 0.0.18's is a single interpreter constructed with the code. The control therefore ran on harness 0.10.0 + monty 0.0.18, so the harness version moved too.

During that run a host tool raised at a multi-line call site — the exact shape that is fatal on 0.0.19:

```python
dns_queries = query_sql(
    name="eve-2026-01-21-01.json",
    sql="""
    SELECT timestamp, src_ip, dns.rrname AS query_domain, ...
    FROM events
    WHERE event_type = 'dns' AND dns.rrname IS NOT NULL AND (...)
    """,
)
```

The exception propagated into the sandbox as a normal traceback and was handed to the model:

```
Runtime error:
Traceback (most recent call last):
  File "<python-input-6>", line 2, in <module>
Exception: sql query failed: Binder Error: Could not find key "rrname" in struct

Candidate Entries: "rcode", "rd", "queries"
```

The agent then called `describe_events`, discovered that `dns` stores queries as `List(Struct({'rrname': String, 'rrtype': String}))`, adjusted its SQL, and continued to completion. On 0.0.19 this same class of error at this same call shape ends the run instead.

Note also what the 0.0.18 frame does *not* contain: any source preview line or caret markers, just the `File ... line 2, in <module>` header. That is consistent with multi-line ranges carrying no preview on 0.0.18, which is what made the validator's same-line assumption safe when it was written.


## Suggested fix

Scope the column-*order* comparison to same-line frames, and keep the preview-width bound unconditional:

```rust
if end.line < start.line {
    return Err(ProtoConvertError::InvalidValue {
        field: "StackFrame.end.line",
        reason: format!("{} is before start line {}", end.line, start.line),
    });
}

if let Some(preview) = &frame.preview_line {
    // Columns are only ordered within a single line. A multi-line span
    // legitimately ends at a smaller column than it starts, and `fmt`
    // returns before the caret subtraction for those frames.
    if start.line == end.line && end.column < start.column {
        return Err(ProtoConvertError::InvalidValue {
            field: "StackFrame.end.column",
            reason: format!("{} is before start column {}", end.column, start.column),
        });
    }

    // Unconditional: not every consumer checks the line span before
    // deriving caret width from these columns (see below).
    let line_chars = u32::try_from(preview.chars().count()).unwrap_or(u32::MAX);
    if end.column > line_chars.saturating_add(2) {
        return Err(ProtoConvertError::InvalidValue {
            field: "StackFrame.end.column",
            reason: format!("{} is beyond the {line_chars}-character preview line", end.column),
        });
    }
}
```

This retains both of the protocol's protections: caret-width underflow is impossible for the same-line frames whose renderer subtracts the columns, and the width bound continues to cap caret allocation for every frame.

Note that the width bound must stay outside the `start.line == end.line` guard. `StackFrame::fmt` does not use the columns for multi-line frames, but `renderTraceback` in `crates/monty-js/ts/errors.ts` does:

```ts
if (!preview.startsWith('raise') && frame.column > 0 && frame.endColumn > frame.column) {
  const width = Math.max(1, frame.endColumn - frame.column)
  lines.push(`    ${indent}${'~'.repeat(width)}`)
}
```

It compares the columns but never the lines, and `monty-js` reaches these frames through `monty-pool` and therefore through this same conversion. Its `endColumn > column` guard rules out underflow but leaves `width` bounded only by the conversion-time check. For a multi-line frame `line_chars` is the length of the whole preview block, so the bound stays loose enough for real frames — the reported case has a block of roughly forty characters and an `end.column` of 2 — while still rejecting a hostile `u32::MAX`.

Suggested regression coverage:

1. In `crates/monty-proto/tests/roundtrip.rs`, verify that a frame from `(line 1, column 9)` through `(line 3, column 2)` converts successfully.
2. Retain the existing rejection test at `invalid_stack_frame_coordinates_are_rejected` (`roundtrip.rs:234-269`). Both of its cases are same-line, so it passes unchanged under the fix above.
3. Add a case asserting that a multi-line frame with an out-of-range `end.column` is still rejected, covering the JS caret path.
4. Add a Python pool-level test that resumes a multi-line external call with an unhandled exception and verifies that `MontyRuntimeError` preserves the original error.
