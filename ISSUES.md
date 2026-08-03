# Known Issues / Improvement Backlog

Findings from a review of `workspace/` transcripts (2026-07-17 osqueryd-analyst sessions; a
2026-07-19 `--pristine`/`--logfire` suricata-analyst comparison across three models; and a
project-wide scan of `workspace/logs/*.jsonl` and Logfire traces for the `tomfoolery` project).
Most items below are still open; #3 documents an issue that already has a mitigation shipped (see
git history — message-history threading across interactive checkpoints; Monty sandbox gotchas
documented in both skills' Sandbox Notes; a runtime lint/auto-fix pass in
`skill_runner/run_core.py`) but flags a deeper Monty-level question worth investigating
separately.

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

## 2. [Mitigated] Token-overflow risk for large osqueryd logs

Earlier the same day (before the current Sandbox Notes existed), a suricata-analyst run hit:

```
ModelHTTPError: status_code: 400, body: {'error': {'code': 400, 'message': 'The input token
count exceeds the maximum number of tokens allowed 1048576.', ...}}
```

caused by printing too much raw data into the model's own context instead of aggregating in
code. That failure hasn't recurred since (current sampling in both skills is capped at 5 lines
for schema discovery), but osqueryd's input file is far larger than the eve.json that triggered
it, and the process-lineage / shell-history dumps observed on 07-17 already print dozens of
multi-line entries per call. At the time of the finding, there was no runtime guardrail against
this in either skill.

**Mitigation shipped**: The prompt-level ~50-line instruction remains useful guidance, but runtime
enforcement no longer depends on model compliance. `OverflowingToolOutput` now intercepts every
tool result at 10,000 characters, preserves the complete value under the owner-only,
task-scoped `workspace/logs/overflow/<task>/` store, and puts only a bounded preview plus an opaque
read handle into model history. If the spill fails, a 4,000-character truncation prevents the
original result from entering context. The audit event records the handle and original byte count.

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

## 5. `run_code` retry-exhaustion crashes the whole run uncaught (observed with qwen3.6-flash)

While running a `--pristine --logfire` comparison matrix (same suricata-analyst prompt against
`eve-2026-01-06-01.json`, 3 models x 3 reps) to investigate issue-adjacent question "does
`write_file` vs `run_code` usage vary by model?", `openrouter:qwen/qwen3.6-flash`'s rep-1 run
crashed outright instead of finishing or degrading gracefully:

```
pydantic_ai.exceptions.UnexpectedModelBehavior: Tool 'run_code' exceeded max retries count of 3.
Consider raising the retry limit, or see the docs on tool retries:
https://ai.pydantic.dev/tools-advanced/#tool-retries
```

Root cause: the model kept emitting Monty-incompatible Python — `p.stat().st_size:,` (comma
thousands-separator format specifier) and then `"...".format(...)` (`str.format()` isn't
supported in Monty either) — across 3 consecutive `run_code` calls, exhausting pydantic_ai's
per-tool retry cap. Gemini-3-flash-preview and GLM-5.2, run concurrently against the same
prompt/data, both completed normally in the same window.

Task: `task-37baec46-8e50-4f8c-bb9f-822d989e3d27`. Audit log:
`workspace/logs/runner-task-37baec46-8e50-4f8c-bb9f-822d989e3d27-bd3a4c4ee93e43d1985f4faf4395a34f.jsonl`
— shows the audit trail itself closed cleanly (`error` then `run_end` events written, per
`RunSession.fail()`/`close()`), but the exception still propagates out of `run_console()`
uncaught: `runner.py`'s `main()` only special-cases `TaskError` and `RunInterrupted` for exit-code
mapping, so this exits 1 with a raw multi-frame traceback to the terminal instead of a clean
message — and, unlike a SIGTERM/Ctrl+Q interruption, produces no partial `analyst_log-*.md` or
`generated_code/` artifacts at all, since the crash happens before any turn ever reaches
`_record_success`.

**Suggested fix**: Catch `pydantic_ai.exceptions.UnexpectedModelBehavior` (and other
non-`RunInterrupted` run-time exceptions) in `runner.py`'s `main()` alongside `TaskError`, print a
short message instead of the full traceback, and exit non-zero without a stack dump — the audit
log already has the detail. Separately, worth adding a Monty-format-specifier gotcha
(`str.format()`, comma thousands-separators) to the Sandbox Notes shared with issue #4, since this
is the same class of "idiomatic Python that silently doesn't exist in Monty" problem as the
`__name__`/`sys.argv` cases in #3 — except here the model never self-corrected before running out
of retries, where the `__name__` cases historically did.

**Not a one-off**: a project-wide Logfire scan (`exception_type='pydantic_ai.exceptions.
UnexpectedModelBehavior'`, all time) turns up this same crash from an earlier, unrelated
`qwen/qwen3.6-flash` session (`workspace/logs/runner-default-14b76039162441d5b59d416eb3f70d98.jsonl`),
also triggered by the same `f"...{x:,}"` comma-format-specifier syntax error, also exhausting
3 retries without self-correcting. Two independent occurrences of the identical Monty gotcha
defeating the same model is stronger grounds for raising `CodeMode`'s `max_retries` default (see
the `--max-retries`/`--max-run-seconds` CLI discussion below) than a single incident would be.

**Update**: `--max-retries` is now implemented (default raised from `CodeMode`'s built-in 3 to 5)
— see issue #13. The `UnexpectedModelBehavior` clean-exit catch suggested above turned out to
already exist in code, but only under `runner.py`'s `if __name__ == "__main__":` guard, which the
real `skill-runner` entry point never runs through — see #13 for the full story. Only the
`RunInterrupted` half of that gap is fixed so far; `UnexpectedModelBehavior` itself is still
uncaught by `main()` and still exits with a raw traceback.

**Update (eval harness)**: `evals/evaluators.py`'s `CrashRateByCase` report evaluator now
computes a per-model failure rate from `Dataset.evaluate(repeat=N)` directly — this class of
crash surfaces as a number in `uv run python -m evals.runner`'s report instead of requiring a
manual project-wide Logfire scan (as this issue's "not a one-off" section did by hand) to
notice it's recurring. See `refs/pydantic-evals-plan.md`.

## 6. [Mitigated] `qwen3.6-flash` dumps every Suricata `stats` event verbatim, blowing the context window

During the same `--pristine` comparison matrix, `qwen3.6-flash`'s rep-3 run crashed with:

```
pydantic_ai.exceptions.ModelHTTPError: status_code: 400, ... This endpoint's maximum context
length is 1000000 tokens. However, you requested about 6654694 tokens (6653059 of text input,
1635 of tool input).
```

Cause: a single `run_code` call collected all 1,440 `event_type == "stats"` records into a list
and printed each one with `json.dumps(s, indent=2)` in a loop — no sampling, no aggregation — and
the resulting `run_code` return (24,738,153 chars) got fed straight back into the model's own
context. Task: `task-c3acb612-c3e6-4c13-9994-ccd9e4373ffa`; audit log:
`workspace/logs/runner-task-c3acb612-c3e6-4c13-9994-ccd9e4373ffa-b8b4f4efe7ca4aeaaa5e1c7f6b1d8d19.jsonl`.

**Not a one-off, and not incidental to this test run**: a project-wide Logfire scan for
`exception_type='pydantic_ai.exceptions.ModelHTTPError'` turns up the *identical* failure
(23,702,777-char `stats`-dump return, same 6.6M-token overflow) from a completely separate,
earlier `suricata-triage` session
(`workspace/logs/runner-suricata-triage-be329e9fb3024c6c9168ccdd41f5623d.jsonl`) — the offending
code in that run is a near-verbatim match: `stats_events = []`, append every `stats` record while
scanning, then `for s in stats_events: print(json.dumps(s, indent=2))`. Two independent sessions,
same model, same specific event type, same unbounded-dump pattern. This reads as a `qwen3.6-flash`-
specific blind spot rather than random chance — Gemini-3-flash-preview and GLM-5.2, given the
identical prompt/data in the same comparison matrix, never produced a `run_code` return anywhere
near this size.

This is issue #2 ("Token-overflow risk for large osqueryd logs") recurring on suricata-analyst,
which issue #2 explicitly said "hasn't recurred since" — that was true only for the models tested
at the time; it had not been fixed at the tool/skill level, just not re-triggered until this model
was tried.

**Mitigation shipped**: The runtime guard described in #2 now spills this 24 MB return before it
can enter model history, leaving a bounded preview and audited read handle. The existing prompt
guidance to aggregate `stats` records remains defense in depth, but a noncompliant model can no
longer reproduce this context-window failure through one oversized tool return.

**Update (eval harness)**: `evals/fixtures.py`'s synthetic capture deliberately includes `stats`
noise events specifically so a future `Evaluator` could assert a report never cites stats-derived
numbers as findings (`SKILL.md` says to ignore them) — not yet wired as its own evaluator, but
the fixture already carries the raw material for one.

## 7. Several Monty stdlib/builtin gaps beyond `__name__`/`sys.argv` cost real retries across nearly every session

A project-wide scan of `pydantic_ai.exceptions.ToolRetryError` (72 occurrences total, all time)
shows the `__name__`/`sys.argv` cases from issue #3 are just two entries in a longer list of
"idiomatic Python that doesn't exist in Monty" mistakes models make and then have to
self-correct out of, mid-run, at the cost of a retry each time:

- **`collections` module is entirely absent** (`ModuleNotFoundError: No module named
  'collections'`) — the single most common recoverable error in the whole history, 7 occurrences.
  `Counter`/`defaultdict` are extremely idiomatic for the kind of tallying this skill does
  constantly, so this gets hit often.
- **`os` module is missing most of its usual surface** — `os.listdir`, `os.walk`, `os.path`,
  `os.getcwd` all raise (5 occurrences combined, phrased inconsistently — see issue #8 below).
- **`exec(...)` is not a callable at all** (`NameError: Unknown function: exec`, 3 occurrences) —
  hit specifically when a model tries to reuse a saved script by reading its text and `exec`-ing
  it, rather than pasting the text directly into a fresh `run_code` call.
- **`socket`, `ipaddress` modules absent** (`ModuleNotFoundError`) — reasonable modules for network
  log analysis to reach for.
- **`str.format()` is not supported** (`AttributeError: 'str' object has no attribute 'format'`) —
  also implicated in issue #5's qwen crash.

None of this is documented today; each one is currently discovered the hard way, per-session, by
whichever model happens to reach for it.

**Suggested fix**: extend the shared Sandbox Notes (issue #4) with an explicit "what's in the
Monty stdlib" allowlist/denylist — even a short one covering `collections`, the missing `os`
functions, `socket`/`ipaddress`, and "no `exec`/`eval`" would likely eliminate most of these 72
retries. This is a documentation gap, not a Monty behavior bug — the restrictions themselves may
well be intentional sandbox design.

**Update (eval harness)**: `evals/evaluators.py`'s `WithinToolCallBudget` reads the same
`run_code`/tool-call counts these retries inflate, so a model burning retries on Monty stdlib
gaps shows up as an evaluated regression (more calls for the same task) in `uv run python -m
evals.runner`'s report, not just as a raw `ToolRetryError` count found by a manual scan.

## 8. File objects aren't iterable (`for line in f:`) — recurs despite the documented `readline()` pattern

`TypeError: '_io.TextIOWrapper' object is not iterable` shows up 3 times across sessions, always
from a model writing the natural `for line in f:` idiom instead of the explicit
`while True: line = f.readline(); if not line: break` loop both skills' Step 2 sample code already
demonstrates. Same shape of problem as issue #3's `__name__` guard — the documented-correct
pattern is right there in the skill, but the model reaches for ordinary Python first and only
self-corrects after hitting the error.

**Suggested fix**: fold into whichever fix issue #3 lands on (deterministic lint/rewrite vs.
clearer error vs. Monty-side change) rather than solving separately — it's the same category of
"idiomatic Python landmine," just a different idiom.

## 9. Two different failure *classes* for "unavailable in Monty," and one of them is actively misleading

The `os`-module errors above don't all fail the same way. `os.listdir('.')` and `os.walk('.')` fail
at what looks like a **static type-check pass** (a tool called `ty`), before the code ever runs:

```
error[unresolved-attribute]: Module `os` has no member `walk`
 --> main.py:6:30
info: Python 3.14 was assumed...
```

But `open(...)` and `FileNotFoundError` fail the *same* static-check way — `error[unresolved-
reference]: Name 'open' used when not defined` / `'FileNotFoundError' used when not defined` —
with a footer that reads like a confirmation the identifier *should* exist: `` `open` was added as
a builtin in Python 3.0 ``. That phrasing actively suggests a version mismatch or a real bug,
not "this sandbox doesn't have it," and — unlike a runtime `NameError` — it can't be caught with
`try`/`except` since the code never starts executing. A model has no way to distinguish "you
typo'd this" from "this is permanently unavailable here" from the message alone.

This is the same ergonomics gap issue #3 already flagged for `__name__`/`sys.argv` (`"name
'__name__' is not defined" reads like any other undefined-name typo"`), but one level worse: those
were at least genuine `NameError`s from real execution; these are pre-execution rejections dressed
up as ordinary Python facts.

**Suggested fix**: same follow-up as issue #3 — worth a Monty-level pass to make "permanently
unavailable in this sandbox" distinguishable from "genuinely undefined," particularly for the
static-check-stage errors where the current phrasing points the model in exactly the wrong
direction.

## 10. `FileSystem` "path resolves outside the root directory" is the single most frequent error in the project's history — models keep reaching for `/data`, `/skill`, and `/workspace` through the wrong tool

Across all `pydantic_ai.exceptions.ToolRetryError`/`ModelRetry` records, one family of message
dominates: `Path {path!r} resolves outside the root directory` / `does not match any allowed
pattern`, hit at least 11 times across variants — `'osqueryd.results.log'` (7x, all identical,
the single most repeated exception message in the whole project), `'/data/eve-2026-01-06-01.json'`,
`'/skill/references/eve_format.md'`, and `'/workspace'` (2x, one via `ModelRetry`).

The `FileSystem` toolset (`read_file`/`write_file`/`list_directory`/etc.) is deliberately scoped to
the task workspace only (`root_dir=ws_path` in `run_core.py:244`) — the `/data` (read-only input)
and `/skill` (read-only reference) mounts are only reachable from *inside* `run_code`'s sandboxed
`pathlib`, per `SANDBOX_DATA_MOUNT`/`SANDBOX_SKILL_MOUNT` in `run_core.py`. `suricata-analyst`'s
own SKILL.md already says this explicitly ("The FileSystem tool's `list_directory` only sees the
task workspace, not `/data`"), yet this is still the most-repeated error of any kind, meaning
models keep trying it anyway, on both skills, across many sessions.

The `'osqueryd.results.log'` case in particular deserves closer, source-level investigation before
assuming the same root cause as the `/data`/`/skill`/`/workspace` cases: `_resolve_path` in
`filesystem/_toolset.py` computes `self._root`/`self._real_root` via `.resolve()`/`os.path.
realpath` at construction time, so a *bare relative filename* with no `/` or `..` shouldn't
normally land outside root at all — the likeliest explanation is a symlink inside the workspace
(e.g., a convenience link to the real `/data` file) that `os.path.realpath` correctly dereferences
and then correctly rejects, but that hasn't been confirmed by reproduction, only inferred from the
message pattern.

**Suggested fix**: since this is already documented in SKILL.md and still recurs constantly, treat
it like issue #3/#9 — the instruction alone isn't reliably preventing it. Consider having the
`PermissionError` message itself name the right tool when the rejected path starts with a known
sandbox mount prefix (e.g. "`/data` is only reachable from inside `run_code`, not this tool") so
the model gets a corrective hint at the point of failure instead of only in the system prompt.

## 11. `--model` accepts any string with no validation against `models.yaml`, so typos surface as opaque provider 404s

`ModelCatalog.resolve()` (`config.py:88`) falls back to returning the input unchanged whenever it
doesn't match a known alias or id — silent by design, so arbitrary `provider:model` strings
(needed for models not yet in the catalog) keep working. But it means a plain typo gets no local
feedback at all: `--model haiku-4.5` and `--model haiku-4-5` (the catalog's actual alias is
`claude-haiku`, id `anthropic:claude-haiku-4-5`) both reached the provider and came back as a raw
`404 not_found_error: model: haiku-4.5` from Anthropic, twice each, rather than a runner-level
"not in models.yaml — did you mean 'claude-haiku'?" message.

**Suggested fix**: in `resolve()`, when a `--model` value matches neither an alias/id nor looks
like a plausible `provider:model` string (e.g. no `:` separator), have the CLI print the
close/available aliases before making any request — cheap to add, and turns an opaque 400+ round
trip into an immediate, actionable message.

## 12. Network-level exceptions crash the run exactly like issue #5, and one produced a ~2-hour silent hang with no wall-clock safety net

A second `--pristine`/`--logfire` OpenRouter comparison (deepseek-v4-pro, kimi-k3, minimax-m3 —
same prompt/data/skill as the first) turned up a failure class distinct from anything in issues
#5-#6: raw network/provider exceptions, uncaught, exactly like issue #5's `UnexpectedModelBehavior`
crash. `kimi-k3` failed all 3 reps — 2 with `pydantic_ai.exceptions.ModelHTTPError: status_code:
429 ... temporarily rate-limited upstream ... retry_after_seconds: 1`, one with a bare
`httpx.ReadTimeout`. `minimax-m3` failed 2 of 3 reps: one with issue #5's own retry-exhaustion
pattern, one with another `httpx.ReadTimeout`.

**The `httpx.ReadTimeout` cases are the concerning part.** These aren't quick failures:

- `kimi-k3` rep 1: audit log shows the last real activity (a `write_file` tool result) at
  `15:37:40 UTC`; `ReadTimeout` didn't fire until `17:34:32 UTC` — a **silent ~1h57m stall** on
  what should have been the next model response, for a total run duration of 7,737 seconds (~2h9m)
  before producing anything. Task: `task-4605d7d6-cb46-4a0f-9be3-9dce905cacbc`.
- `minimax-m3` rep 3: a shorter but still severe ~10-minute stall (`17:52:28` →`18:02:38`) after
  what looked like a completed turn, for a 1,115s total run. Task:
  `task-d39f5931-5975-4a83-8077-6aad10b55d5d`.

Both ended with nothing to show for the wait — no partial report, no partial `generated_code/`
snapshot beyond whatever the last successful turn had already checkpointed, and (same as issue #5)
a raw multi-frame traceback to the terminal rather than a clean error.

This is the single strongest piece of evidence yet for the `--max-run-seconds` wall-clock-budget
CLI flag discussed alongside issues #5/#6 (see `results/pristine-model-comparison-2026-07-19.md`'s
duration section, where GLM-5.2 was slow-but-making-progress at 500-800s) — a 2-hour dead stall
producing zero output is a fundamentally worse failure than "thorough but slow," and neither
`CodeMode.max_retries` nor `UsageLimits.request_limit` would catch it, since nothing ever gets far
enough to retry or spend another turn.

Separately, the two `429` cases are notable because the provider's own response explicitly said
`retry_after_seconds: 1` — a signal that a 1-second backoff-and-retry would very likely have
succeeded — yet the run failed immediately rather than retrying. Worth checking whether
`pydantic_ai`'s OpenRouter provider (or the underlying OpenAI-compatible client it wraps) has any
built-in retry-on-429 behavior at all, since none was observed here.

**Suggested fix**: same `runner.py::main()` catch-and-clean-exit fix as issue #5, broadened to
network-layer exceptions (`httpx.ReadTimeout`, `pydantic_ai.exceptions.ModelHTTPError`), not just
`UnexpectedModelBehavior`. Independently, the `--max-run-seconds` wall-clock cap from the
`--max-retries`/`--max-turns` CLI discussion would bound the damage from a hang like kimi-k3's
regardless of cause. And consider a single top-level retry (with the provider's own
`retry_after_seconds` as the delay) for 429 responses specifically, before giving up.

**Update**: `--max-retries`, `--max-run-seconds`, and `--max-turns` are now implemented (see issue
#13) — `--max-run-seconds` directly bounds a hang like kimi-k3's regardless of cause. The
`UnexpectedModelBehavior`/`ModelHTTPError`/`ReadTimeout` clean-exit catch from issue #5's suggested
fix (broadened here to network exceptions) is still open — only the `RunInterrupted` half of that
fix shipped as part of #13, since it was a hard blocker for `--max-run-seconds` itself.

**Update (eval harness)**: `evals/task.py`'s `run_skill_eval` lets any of these exceptions
(network, retry-exhaustion, or otherwise) propagate uncaught by design, so `Dataset.evaluate`
records them as a native `ReportCaseFailure` and `CrashRateByCase` aggregates them — a silent
multi-hour hang like kimi-k3's rep 1 would now also be bounded by each case's own
`max_run_seconds`, turning it into a fast, visible failure in the eval report rather than a
multi-hour wait discovered only by re-reading an audit log afterward.

## 13. `RunInterrupted`'s exit-143 mapping never actually ran via the real `skill-runner` entry point — Ctrl+C/SIGTERM crashed with a raw traceback all along

Discovered while validating `--max-run-seconds` (issue #12's fix, below): a run interrupted by the
new watchdog exited with a raw multi-frame traceback and exit code 1, not the clean "exit 143, no
traceback" behavior `audit.py`'s `RunInterrupted`/`map_run_interrupted_exit_code()` are explicitly
designed to produce (see that module's own docstrings, and `refs/textual-ui-plan.md`'s "top-level
`RunInterrupted` → exit `143` mapping").

Root cause: `runner.py`'s `main()` only ever caught `TaskError`. The `except RunInterrupted:
map_run_interrupted_exit_code()` mapping existed, but only under `if __name__ == "__main__":` at
the bottom of the file — which runs when `runner.py` is executed directly (`python
skill_runner/runner.py`), but **not** when invoked through the installed console-script entry
point, `pyproject.toml`'s `skill-runner = "skill_runner.runner:main"`. That entry point's
auto-generated shim (`.venv/bin/skill-runner`) does `from skill_runner.runner import main;
sys.exit(main())` directly — it never touches the `__main__` guard, so the mapping silently never
ran. Confirmed via the actual crash traceback: `File ".../bin/skill-runner", line 10, in <module>
sys.exit(main())`.

Since `skill-runner`/`uv run skill-runner` (not `python skill_runner/runner.py`) is how this tool
is actually invoked everywhere in this repo (`README.md`, `compare_models.sh`, every test run in
`results/*.md`), **this means real Ctrl+C and SIGTERM have crashed with a raw traceback and exit
code 1 instead of a clean exit 143 for as long as the console-script entry point has existed** —
this issue's own earlier entries (#5, #12) mischaracterized `main()` as already catching
`RunInterrupted` when suggesting it be "broadened"; it was never catching it at all.

**Mitigation shipped**: moved the `except RunInterrupted: map_run_interrupted_exit_code()` clause
into `main()` itself (`runner.py`), alongside the existing `except TaskError`, so it runs
regardless of entry point. The `if __name__ == "__main__":` block is now a trivial `main()` call.
Verified end-to-end: `--max-run-seconds 5` against a real model now exits `143` with no traceback,
and the audit log shows a clean `interrupted` → `run_end status=failed` sequence instead of an
`error` event.

## 14. [Mitigated] The project's own canonical `UNNEST` example combined it with `GROUP BY` in one `SELECT` — the exact form DuckDB's binder rejects

Caught live in a `suricata-analyst` / `deepseek-v4-pro` run (`workspace/logs/runner-task-5635a679-
4669-4512-b923-8128e344b840-*.jsonl`, 2026-07-23 ~13:24 UTC, cross-checked against the matching
`tomfoolery` Logfire traces): the model authored `SELECT unnest(dns.queries).rrtype AS rrtype,
count(*) AS n FROM events WHERE event_type = 'dns' GROUP BY 1 ORDER BY 2 DESC` via `query_sql` and
got `DataToolError: sql query failed: Binder Error: UNNEST not supported here`.

That query wasn't a model mistake — it's a near-verbatim copy of the worked example in
`prompts/sandbox_notes.md`'s `query_sql` section (shown to every skill run) and the "manual
verification" target query named in `SQL_QUERY_PLAN.md` §3. Both examples put `UNNEST(...)` and
`GROUP BY` in the same `SELECT`, which DuckDB's binder does not support — `UNNEST` is a
set-returning expression and can't be reconciled with aggregation in one scope. The *only* tested
form (`tests/test_data_tools.py::test_nested_unnest_query_returns_nested_shapes`) wraps the
`UNNEST` in a `WITH` CTE first and aggregates in the outer query — the design doc's own canonical
example was never actually exercised by that test.

**Mitigation shipped**: rewrote both examples (`prompts/sandbox_notes.md`, `SQL_QUERY_PLAN.md` §3)
to the working CTE form — `WITH u AS (SELECT unnest(dns.queries) AS q FROM events WHERE
event_type = 'dns') SELECT q.rrtype AS rrtype, count(*) AS n FROM u GROUP BY 1 ORDER BY 2 DESC` —
and added an explicit gotcha (both inline next to the `UNNEST` guidance and in the compact
stdlib-gotchas list) telling the model never to combine `UNNEST` with `GROUP BY` directly. No
`data_tools.py` code changes were needed; this was purely a prompt/doc defect, but one that was
actively steering every model toward a query guaranteed to fail on first try.

## 15. Unexplained latency spikes in `query_events`/`query_sql` — no telemetry to tell cache-miss from cold I/O from lock contention

While reviewing a `suricata-analyst` trace (`tomfoolery` Logfire span `b8b3eb78a3f121f6`, trace
`019f8fb8f5cb733f47672ece66c0f480`) at the user's request to look at `duration_ms` performance, a
project-wide query over the last 2 days of `execute_tool` spans showed `query_events` with a
p95 of 45.4ms but a **max of 2070.2ms** — a ~46x outlier. The 5 slowest calls were all small
(`limit<=50`) queries, and all were the first data-tool call of their run.

The obvious hypothesis — each new task gets a fresh, empty Parquet cache, so the first query per
task always pays the NDJSON-to-Parquet conversion cost — turned out to be **wrong**, and worth
recording so it isn't re-investigated the same way twice:

- `run_core.py:203-213` sets `parquet_cache_root` from `options.workspace.resolve()`, the
  **top-level** `./workspace` directory (the CLI default), not the per-task `workspace/task-
  <uuid>/` directory — confirmed against `refs/workspace-lifecycle.md`'s documented shared path.
  On disk, `workspace/data-sink/parquet/` held exactly one cache file, and none of the 56
  `task-*/` directories had their own — the cache is genuinely shared and persistent.
- The cache key (`_cache_identity`, `data_tools.py`) is `sha256(source_root|name|st_dev|st_ino|
  st_size|st_mtime_ns)`. The actual source file's inode and mtime are stable (nothing re-copies
  it between runs), so every run computes the same key and should hit the same warm cache.
- Checking `process_pid` on the Logfire records ruled out "one-time Polars import cost per OS
  process" too: two runs sharing the same PID (21282), 3 minutes apart, both paid a slow first
  `query_events` call despite the second run reusing the same warm process and the same
  already-built cache file.

So the cache is not being rebuilt, and it is not a one-time process-level cost — but *something*
about the first data-tool call of each run is still consistently slower than subsequent calls in
the same run, and the current telemetry (Logfire's `execute_tool` span duration, and the local
audit log's `duration_ms`, which only times the outer `run_code` call and never isolates
`query_sql`/`aggregate_events`/`query_events` at all — see the conversation that produced this
entry) can't distinguish cold-disk-I/O-on-first-touch from lock contention from something else.

**Mitigation shipped**: `data_tools.py` now logs at `DEBUG` (via `logging.getLogger(__name__)`,
silent by default — no `logfire` dependency added, since `data_tools.py` doesn't otherwise import
it and `logfire.configure()` is optional/CLI-gated in `run_core.py`) three previously-invisible
timings:
- `ensure_parquet_cache`: `lock_wait_ms` (fcntl contention) and, separately, `convert_ms` on a
  miss — a debug line literally says `parquet cache hit` or `parquet cache miss`, closing the
  "was this actually a rebuild?" question directly instead of by inference.
- `query_events`/`aggregate_events`: `collect_ms` for the actual Polars `.collect()` call, after
  cache resolution.
- `query_sql`: `execute_ms` for the DuckDB `fetchall()` call, after cache resolution.

Verified manually (see conversation) that a cold call logs `parquet cache miss ... convert_ms=`
and a warm repeat logs `parquet cache hit ... lock_wait_ms=` with no `convert_ms`, and that
`collect_ms`/`execute_ms` are logged on every call regardless of cache state — so the next
occurrence of this latency pattern will show directly whether it's `convert_ms` (still, somehow,
a real cache rebuild), `lock_wait_ms` (contention from concurrent runs), or a large `collect_ms`/
`execute_ms` with a cache *hit* (genuine cold-disk-I/O or query-plan cost) still open as the
likely explanation.

These logs are plain stdlib `logging` (`logging.getLogger("skill_runner.data_tools")`) and are
silent with no handler attached — they don't reach the local audit log (`AuditLog.event()` in
`audit.py` is only fed by `pydantic_ai`'s `FunctionToolCallEvent`/`FunctionToolResultEvent`
stream, i.e. only tool calls the model itself makes; `query_events`/`aggregate_events`/
`query_sql` run *inside* `run_code` as plain function calls, invisible to that stream, same
reason their timing was never in the audit log to begin with). `run_core.py:_configure_logfire`
now attaches `logfire.LogfireLoggingHandler()` to that logger at `DEBUG` and only when
`--logfire` is passed, so these lines ride along as log entries in the same Logfire traces this
project already queries for analysis — verified end-to-end with a real `logfire.configure(...)`
+ console exporter. A non-`--logfire` run pays no cost and sees nothing, by design.

All 58 tests (`test_data_tools.py` + `test_run_core.py`) pass unchanged; this is logging only, no
behavior change.

## 16. [Mitigated] Interactive follow-up phases could ignore the checkpoint contract indefinitely

A Qwen 3.7 Max run on 2026-07-24 demonstrated that an initial-phase boundary alone is
insufficient. It correctly used two `run_code` calls and returned an initial checkpoint, but after
the user chose a DNS-hunting focus it issued 16 more `run_code` calls and 17 model requests before
returning control. The matching Logfire trace attributed 263.1 seconds to model calls and only
4.4 seconds to all tool spans: the problem was an unbounded model loop, not DuckDB/Polars latency.

**Mitigation shipped:** `InteractivePhaseBudget` now applies to every interactive turn. The first
discovery phase permits two `run_code` calls; each later user-directed phase permits four. When a
phase reaches its cap, the next sandbox call is skipped with a checkpoint instruction. If the
model continues by attempting another tool, the runner returns a deterministic successful
checkpoint instead of failing the session. This preserves the completed evidence while restoring
user control.

## 17. Interactive-run wall time is overwhelmingly model inference, while the runner has no graceful per-request latency boundary

Three 2026-07-24 interactive runs reached the same conclusion from independent audit and Logfire
evidence: data-tool execution is not the source of the long waits. The Kimi K3 focused turn spent
337.3 of 339.1 seconds in seven model calls (99.5%); Qwen 3.7 Max spent 263.1 seconds in 17 model
calls versus 4.4 seconds in all tool spans (98.4%); and Claude Sonnet 5 spent 210.6 of 212.1
seconds in 21 model calls (99.3%). Gemini 3.1 Pro was materially faster per call, but still spent
91.0 of 92.0 seconds in model calls (98.97%).

The current `--max-run-seconds` watchdog is deliberately a whole-turn SIGTERM interruption, so
it protects against a hang but discards the interactive turn instead of returning a checkpoint.
The phase `run_code` budgets mitigate excessive *numbers* of model requests but cannot bound a
single 60- to 90-second model response (as seen in Kimi) or give the user control while it is in
flight.

**Suggested fix:** add an optional per-model-request deadline with a graceful recovery path. On
expiry, preserve completed tool results and transcript state, emit an explicit latency checkpoint,
and return control to the interactive UI rather than treating the whole process as interrupted.
Compare provider support and behavior first: cancelling an in-flight request must not corrupt
message history or leave a provider-side background request running.

## 18. [Upstream] `pydantic-monty` 0.0.19 kills the run when a host tool raises at a multi-line call site

Any exception raised by a host-side tool (`query_sql`, etc.) at a **multi-line** call in `run_code`
is replaced by a fatal parent-side protocol error instead of reaching the model:

```
RuntimeError: monty worker protocol error: invalid exception payload:
invalid value for StackFrame.end.column: 2 is before start column 12
```

`StackFrame::try_from` in `crates/monty-proto/src/convert/exception.rs` compares `end.column`
against `start.column` without first comparing the lines, so a frame spanning from the callee name
on one line to the closing parenthesis on a later line is rejected as malformed. The check was
added one day before multi-line source previews were, both inside the 0.0.19 release window; its
"previews only exist for same-line spans" premise was true when written and stale by the next
commit.

Observed in four runs across two providers (`deepseek-v4-pro`, `google:gemini-3.6-flash`) with
four distinct underlying exceptions — an unregistered `offset` kwarg, and three DuckDB binder/
parser errors — each reporting a start column matching its own assignment prefix. The gemini run
contains a within-session control: a single-line `query_sql` call that raised seconds earlier
produced a normal `ToolRetryError` the agent recovered from, while the multi-line call ended the
run. Traces are in the `tomfoolery` Logfire project (e.g. trace
`019fbafe5a91dbd79225056e95adbde0`); with `--logfire` the masked exception survives on the inner
`execute_tool query_sql` span even though every ancestor carries only the protocol error.

Impact is worse than lost diagnostics: these are ordinary, recoverable SQL mistakes that the model
demonstrably fixes on the next turn when it can see them. Here they end the agent run instead.

**Workaround (not a fix)**: `pydantic-monty` 0.0.18 does not have the check and completes these
runs normally — a verification run finished with exit 0 and 14 `run_code` calls, recovering from a
binder error at a multi-line call site mid-run. Note that monty cannot be downgraded on its own:
`pydantic-ai-harness` 0.11.0 raised its `codemode` floor to `pydantic-monty>=0.0.19`, and the 0.0.19
`Monty` class is a worker pool (`Monty()` + `.checkout()`) while 0.0.18's is a single interpreter
constructed with the code, so the control ran on harness 0.10.0 + monty 0.0.18.

**Suggested fix (upstream)**: scope the column-order comparison to same-line frames while keeping
the preview-width bound unconditional — `renderTraceback` in `crates/monty-js/ts/errors.ts` derives
caret width from the columns without checking the line span, so that bound is still load-bearing.
Full analysis, reproducer, and patch in [monty-issue-report.md](monty-issue-report.md).

**Workaround now applied (2026-08-03)**: after the eval harness reproduced this live and
quantified it at 4/8 cases across four models (see the "Confirmed live" update above),
`pyproject.toml` now pins `pydantic-ai-harness[codemode]==0.10.0` and `pydantic-monty==0.0.18`
directly (both previously unpinned/floor-only, resolving to the buggy 0.13.0/0.0.19 pair). The
`host_path=`/`virtual_path=` `MountDir` keyword-argument call shape added in Phase 28's upgrade
(`skill_runner/run_core.py`) turned out to already work under 0.0.18 too — confirmed directly by
constructing a `MountDir` both ways against the downgraded package — so no code reversion was
needed there, only the two version pins. Re-running the full test suite and a live
`uv run python -m evals.runner --repeat 1 --logfire` pass under the downgrade is the verification
for this update; see `PROJECT.md` for the outcome. Re-upgrading past 0.0.19 should stay blocked
until the upstream fix lands or is independently re-verified not to affect this call pattern.

**Update (eval harness)**: since this regression is upstream and version-triggered rather than
prompt-triggered, `evals/`'s `CrashRateByCase` report evaluator (run periodically against a
pinned `pydantic-monty` version) would surface a jump in failure rate the next time a dependency
upgrade reintroduces a class of protocol-error crash like this one, rather than requiring it to
be caught by chance during a live trace review.

**Confirmed live (2026-08-03), and worse than previously known**: two real `uv run python -m
evals.runner --repeat 1 --logfire` runs against the synthetic fixture, across two entirely
different 4-model rosters, both hit this exact `StackFrame.end.column` protocol error — first
`google:gemini-3-flash-preview` and `anthropic:claude-haiku-4-5` (2 of 4), then, after swapping
the roster, `openrouter:qwen/qwen3.7-flash` and `openrouter:deepseek/deepseek-v4-pro` (2 of
4 again). **4 of 8 total cases across four different models from three different providers** hit
the identical crash, each triggered by an ordinary recoverable `query_sql` mistake (a missing
table/struct key) at a multi-line call site — this is not one model's quirk, it's model-agnostic
and appears to trigger routinely whenever a `query_sql` retry error happens to land on a
multi-line call. `evals/dataset_suricata.py`'s `CrashRateByCase` table made this visible as a
plain number (`1.0` per affected case) instead of requiring a manual trace review to notice the
pattern repeating across unrelated models.
