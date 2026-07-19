# Known Issues / Improvement Backlog

Findings from a review of `workspace/` transcripts (2026-07-17 osqueryd-analyst sessions) and
Logfire traces for the `tomfoolery` project. Most items below are still open; #3 documents an
issue that already has a mitigation shipped (see git history — message-history threading across
interactive checkpoints; Monty sandbox gotchas documented in both skills' Sandbox Notes; a
runtime lint/auto-fix pass in `skill_runner/run_core.py`) but flags a deeper Monty-level question worth
investigating separately.

## 1. osqueryd-analyst re-scans the full raw log for every new analysis angle

The `21:09` session ran 5-7 separate full linear passes over the 1.4GB / ~1.5M-line
`osqueryd.results.log` for different questions in the same run: query-name counts (twice —
once as a truncated 10k-line sample, once in full once the truncation was discovered
misleading), top-network-processes, Node.js app inventory, process-tree construction (twice,
once with a naive PID map and once with a start-time-aware rebuild), and the shell-history/env
audit. One process-tree build alone took ~2.5 minutes wall clock
(`21:15:23` -> `21:18:06`, per `workspace/runner-26-07-17_21-09-28.log`).

The skill already tells the agent to persist reusable **code** (`osqueryd_<purpose>.py`) but
never tells it to persist reusable **derived data**. Since query results are already assembled
once per phase (e.g. the `(pid, start_time) -> process` map built for lineage analysis), that
map could be written to `/workspace/osqueryd_process_index.json` (or split per query type, e.g.
`osqueryd_net_processes.jsonl`) the same way scripts are saved, so later phases/sessions load a
small pre-parsed file instead of re-streaming the raw log from scratch.

**Suggested fix**: Add a Step 2 pattern to `skills/osqueryd-analyst/SKILL.md` — "Cache derived
data, not just code" — instructing the agent to write a compact intermediate JSON/JSONL file
after any full-file pass whose result (a pid map, a per-query-type extract, an aggregated
count table) is likely to be reused, and to check for/reuse that cache before re-scanning.
Given the file is ~1000x larger than suricata's `eve.json`, this is much higher-value for
osqueryd-analyst than for suricata-analyst.

## 2. Token-overflow risk for large osqueryd logs

Earlier the same day (before the current Sandbox Notes existed), a suricata-analyst run hit:

```
ModelHTTPError: status_code: 400, body: {'error': {'code': 400, 'message': 'The input token
count exceeds the maximum number of tokens allowed 1048576.', ...}}
```

caused by printing too much raw data into the model's own context instead of aggregating in
code. That failure hasn't recurred since (current sampling in both skills is capped at 5 lines
for schema discovery), but osqueryd's input file is far larger than the eve.json that triggered
it, and the process-lineage / shell-history dumps observed on 07-17 already print dozens of
multi-line entries per call. There's no explicit guardrail against this in either skill.

**Suggested fix**: Add an explicit reminder to both skills' "Python Style" / Working Agreements
— aggregate and summarize in code, and only print bounded/paginated output (a top-N table, not
full per-record dumps) — with a concrete cap (e.g. "print at most ~50 lines of detail per
`run_code` call unless explicitly asked for a full listing").

## 3. `if __name__ == "__main__":` kept recurring despite documentation — worth a Monty-side fix?

Saved scripts are reused by pasting their text into `run_code`, where they always execute as a
top-level snippet — `__name__` is never defined there, so `if __name__ == "__main__":` raises
`NameError: name '__name__' is not defined`. This bit `osqueryd_process_lineage.py`
(2026-07-17 21:09 session), was documented as banned in both skills' Sandbox Notes right after,
and then recurred anyway in the very next session's freshly-generated
`osqueryd_python_applications.py` (21:32 session) — plus, once actually checked with the new
lint pass (see below), turned out to already be present in `osqueryd_nodejs_applications.py`
and `osqueryd_top_network_processes.py` too. So the doc-only fix did not reliably stop the
model from writing the pattern; it only helped when the model hit the error directly and
self-corrected within that same run.

**Mitigation shipped**: `skill_runner/run_core.py` now has `lint_and_fix_scripts()`, run at the start of every
invocation (before the "existing scripts" inventory is shown to the model) and again at the end
(after the run's own artifacts are saved). It deterministically strips the `__main__` guard via
regex + `textwrap.dedent` and warns (without auto-fixing, since the right replacement is
contextual) on any `sys.argv` usage — another idiom that fails the same way
(`AttributeError: 'module' object has no attribute 'argv'`). This closes the loop regardless of
whether the model reads/obeys the SKILL.md instructions, but it's a workaround applied after
the fact, not a fix at the source.

**Worth investigating later**: since `__name__` (and `sys.argv`) are permanently absent/unsettable
in Monty by design, not just unimplemented, it may be worth checking whether Monty itself could:
- define `__name__` as `"__main__"` (or similar) for top-level `run_code` execution, so the
  idiomatic guard just works instead of needing to be banned and linted away, or
- give a clearer, more specific error for this exact case (today's message —
  `name '__name__' is not defined` — reads like any other undefined-name typo, not like "this
  identifier is intentionally unavailable in this execution mode").
This is a Monty-level ergonomics question, not just a skill-content one — flagging here to
follow up on separately rather than deciding now.

## 4. `suricata-analyst` and `osqueryd-analyst` Sandbox Notes are duplicated verbatim

Both `SKILL.md` files carry an almost byte-for-byte identical "Sandbox Notes" section (Monty
stdlib subset, path namespaces, `readline` loop pattern, exception-catching caveat, and now the
`__name__`/`sys.argv`/stdlib-gotchas block added in this pass). Every future Monty-specific
lesson learned in one skill needs to be manually ported to the other, which is exactly how the
`__name__` bug drifted (fixed in spirit for osqueryd's newer template but the guard was still
present until this pass). If a third skill is added, this duplication will triple.

**Suggested fix**: Factor the sandbox-mechanics portion of "Sandbox Notes" (everything that's
about Monty/the runner, not about the domain-specific log format) into a shared reference file
(e.g. `skills/_shared/sandbox_notes.md`) that both `SKILL.md` files point to or embed via a
build step, so it only needs to be updated once. Lower priority than #1/#2 — purely a
maintainability concern, not something that has caused a failure yet.
