# Adopt Pydantic Evals for suricata-analyst model comparisons

## Context

Model comparisons in this project are currently done by hand: `compare_models.sh` loops
over `$MODELS` calling `skill-runner --pristine --logfire`, and a human then writes a
narrative report in `results/*.md`, manually `grep`-verifying specific claims against the
raw 184MB Suricata capture. Those reports have repeatedly surfaced the same limitation:
"3 reps isn't enough for a reliable rate estimate" — and the most important finding across
all of them ([results/pristine-model-comparison-2026-07-19.md](../results/pristine-model-comparison-2026-07-19.md)) is that **the same model,
same prompt, same data reaches different security verdicts across reps** (GLM-5.2 flipped
"low risk" → "HIGH confidence, isolate host" on an identical MQTT beacon in 2 of 3 reps).
The real capture also has no independently-confirmed ground truth (see the "Limitations"
section of each `results/*.md` report), so today's "verification" is limited to checking that
specific structural facts (record counts, specific IPs) are real — never whether a verdict is
*correct*.

`pydantic-evals` (2.20.0, already transitively installed via `pydantic-ai-slim[evals]`,
matching the pinned `pydantic-ai` version) turns this into a repeatable harness: `Dataset`,
`Case`, `Evaluator`, and native `repeat=N` reruns with per-case-group aggregation via
`ReportEvaluator`. Combined with a small synthetic fixture with *known, planted* findings,
this gives deterministic ground-truth checks the real capture can't offer, while a
`ReportEvaluator` reading `EvaluationReport.case_groups()` operationalizes the "does this
model agree with itself across reps" question directly. `compare_models.sh` and `results/*.md`
stay as-is for ad hoc real-data narrative runs; this is a separate, repeatable regression path.

Decisions already made with the user: first `Dataset` uses a small ~4-model roster (one per
provider tier: `google:gemini-3-flash-preview`, `anthropic:claude-haiku-4-5`,
`openrouter:deepseek/deepseek-v4-flash`, `openrouter:z-ai/glm-5.2`) — no deliberately
crash-prone model in v1. `compare_models.sh` is kept unchanged, documented as serving a
different purpose (ad hoc/real-data) from `evals/runner.py` (repeatable/ground-truth).

## Plan

### Phase 0 — dependency
- `uv add pydantic-evals` (promotes it from transitive to a direct `pyproject.toml`
  dependency, version-floor matching the existing `pydantic-ai>=2.9.1` style). Per this
  repo's Python guidance, use `uv` for all install/venv/script operations below, not `pip`.
- Add `evals/__init__.py` (empty package marker).
- Add `[project.scripts] skill-evals = "evals.runner:main"` to `pyproject.toml`.

### Phase 1 — synthetic fixture with planted, verifiable findings
`evals/fixtures.py`:
- `write_ndjson(path: Path, rows: list[dict]) -> None` — reusable NDJSON writer.
- `FixtureManifest` (frozen dataclass) — long-lived-flow IPs/duration, beacon IP/port/
  interval, benign-high-volume-host IP, plus `expected_findings: dict[str, str]` substring
  markers, all derived in one place so `Case.metadata`, evaluators, and tests never
  duplicate magic strings.
- `build_suricata_fixture(dest_dir: Path) -> FixtureManifest` — deterministic (fixed
  timestamps/IPs, no RNG), ~500-2000 rows, matching the field names in
  [skills/suricata-analyst/references/eve_format.md](../skills/suricata-analyst/references/eve_format.md). Plants:
  1. One `flow` event, internal→external, 53h duration (mirrors the real report's already-
     documented finding, for direct comparability).
  2. A fixed-interval beacon (~300s cadence, ~50 reps) to an external IP on port 1883 (MQTT),
     mirroring the real GLM-5.2 beacon finding's shape.
  3. A benign high-volume host (many distinct external IPs, boring well-known port) as a
     deliberate false-positive trap.
  4. Baseline `dns`/`http`/`tls`/`stats` noise (`stats` events specifically included, since
     `SKILL.md` instructs ignoring them — lets an evaluator check reports don't cite
     stats-derived numbers as findings).
- Scope: `skills/suricata-analyst` only for v1 (matches every existing report/`compare_models.sh`
  precedent; `osqueryd-analyst` fixture is a future extension, not in this plan).

`tests/test_evals_fixtures.py` — asserts the planted facts directly against the written
NDJSON (row counts, exact flow duration, exact IPs) — the "grep the raw log" verification
pattern from the real reports, codified as a test with zero agent/API calls.

`tests/test_data_tools.py`'s existing inline `_write_ndjson` helper stays independent
(no cross-import from `evals/` into `tests/`'s existing suite) — avoids coupling a shipped
test file to the new dev-only `evals/` package for a two-line helper.

### Phase 2 — task function wiring the real runner
`evals/task.py`, built on the confirmed programmatic entrypoints (`skill_runner/run_core.py`'s
`prepare_run`, `skill_runner/session.py`'s `RunSession`/`submit_async`, `skill_runner/config.py`'s
`RunOptions`):
- `EvalCaseInputs` (frozen dataclass): `skill_dir`, `prompt`, `model`, `workspace`,
  `max_turns`, `max_run_seconds`, `model_override: Any | None = None` (test-only escape
  hatch enabling `FunctionModel`/`TestModel` substitution via `submit_async(prompt, model=...)`,
  the one parameter `RunSession` already exposes for this).
- `EvalCaseMetadata` (frozen dataclass): `expected_findings`, `forbidden_near` — sourced
  from `FixtureManifest`.
- `EvalSink` — minimal `StatusSink`/`RunSink` implementation; `emit(kind, **fields)` calls
  `pydantic_evals.dataset.increment_eval_metric(f"emit.{kind}", 1)` (and a `write_file`-
  specific counter) so tool-call/write-file counts land in `EvaluatorContext.metrics` for
  free, without requiring `--logfire`/OTel.
- `async def run_skill_eval(inputs: EvalCaseInputs) -> str` — builds `RunOptions(pristine=True,
  logfire=False, ...)`, calls `prepare_run` → `RunSession(...).submit_async(prompt,
  model=inputs.model_override)`, calls `set_eval_attribute("task_id", ...)`, returns
  `str(result.output)`. Exceptions propagate uncaught, so `Dataset.evaluate` records a
  native case failure — this is what makes the crash modes in `ISSUES.md` #5/#6/#7/#12/#18
  show up as failed reps instead of being silently swallowed.
- Shared workspace root across all cases/reps (one `evals/.runs/<stamp>/` base dir, only the
  per-task `--pristine` subdirectory is isolated) — reuses the existing `data-source`/
  `data-sink/parquet` sharing already built into `run_core.py`, so the fixture's Parquet
  cache converts once, not once per rep.

`tests/test_evals_task.py` — `FunctionModel`-driven tests of `run_skill_eval`, following
`tests/test_run_core.py`'s existing pattern exactly (scripted tool-call responses, no
network calls).

### Phase 3 — evaluators
`evals/evaluators.py`:
- `SurfacesPlantedFindings` — per-finding substring check against `ctx.metadata
  .expected_findings`, returned as a `Mapping[str, bool]` so each finding is its own
  reportable assertion column (makes "only 1 of 9 runs caught this" visible per-rep).
- `AvoidsBenignFalsePositive` — deterministic keyword-proximity check around the planted
  benign host, explicitly docstringed as grounded-but-heuristic.
- `WithinToolCallBudget(max_calls)` — reads `ctx.metrics["emit.tool_call"]`, no OTel needed.
- Built-in `MaxDuration(seconds=...)` used directly as the wall-clock budget (addresses the
  "GLM-5.2 is 5-8x slower" finding and `ISSUES.md` #17).
- `VerdictLabel` — small independent judge `Agent` (default `openai:gpt-5-mini`, deliberately
  not a model under comparison) classifying the report into
  `Literal["benign","monitor","high-risk"]`, surfaced as a per-case **label**, not pass/fail.
- `CrashRateByCase(ReportEvaluator)` — walks `ctx.report.case_groups()`, computes
  failures/runs per `source_case_name`, returns a `TableResult`.
- `VerdictConsistencyAcrossReps(ReportEvaluator)` — checks whether `VerdictLabel` agrees with
  itself within each case group's reps, returned as a `TableResult`. Docstring explicitly
  states this is informational (self-consistency, not correctness) — it directly
  operationalizes the GLM-5.2 flip-flop finding without claiming to know which verdict is right.

`tests/test_evals_evaluators.py` — unit tests constructing `EvaluatorContext(...)` by hand
and calling `.evaluate(ctx)` directly on each evaluator, no `Dataset`/agent involved.

### Phase 4 — concrete Dataset + entrypoint
`evals/dataset_suricata.py` — `build_dataset(fixture_manifest, workspace_root) ->
Dataset[EvalCaseInputs, str, EvalCaseMetadata]`: one `Case` per model in the agreed 4-model
roster (`google:gemini-3-flash-preview`, `anthropic:claude-haiku-4-5`,
`openrouter:deepseek/deepseek-v4-flash`, `openrouter:z-ai/glm-5.2`), same prompt/fixture,
`evaluators=[SurfacesPlantedFindings(), AvoidsBenignFalsePositive(),
WithinToolCallBudget(30), MaxDuration(600), VerdictLabel()]`,
`report_evaluators=[CrashRateByCase(), VerdictConsistencyAcrossReps()]`.

`evals/runner.py` — `main()` CLI (installed as `skill-evals`): builds the fixture into a
fresh run directory, optionally `logfire.configure()` + `instrument_pydantic_ai()` under
`--logfire` (opt-in, matching the existing CLI flag's default; pushes to the existing
"tomfoolery" project with zero extra `pydantic_evals` instrumentation calls needed — it
auto-forwards through `logfire_api` once `logfire.configure()` has run), then
`dataset.evaluate_sync(run_skill_eval, repeat=N, name=...)`, `report.print()`.

`tests/test_evals_dataset.py` — smoke-tests `build_dataset()` shape (case count, evaluator
wiring) with no API calls.

### Documentation updates
- `ARCHITECTURE.md` — new `### Evals harness (evals/)` subsection near "Runner (skill_runner
  package)"; note in "Observability (Logfire, optional)" that `pydantic_evals` piggybacks on
  the existing `logfire.configure()` call.
- `README.md` — usage section for `skill-evals` alongside the existing `skill-runner` entry,
  including a short note on how it differs in purpose from `compare_models.sh`.
- `ISSUES.md` — cross-reference #5/#6/#7/#12/#18 to `CrashRateByCase` as the mechanism that
  now surfaces their recurrence automatically.
- `PROJECT.md` — one phase entry noting when/why `pydantic-evals` was adopted.

## Critical files
- `skill_runner/run_core.py` (`prepare_run`, `_build_agent`) — reused, not modified.
- `skill_runner/session.py` (`RunSession.submit_async`) — reused, not modified.
- `skill_runner/config.py` (`RunOptions`, `ModelCatalog`) — reused, not modified.
- `tests/test_run_core.py` — pattern to replicate for `FunctionModel`-driven eval tests.
- `models.yaml` — source of the 4-model roster's canonical ids.
- New: `evals/__init__.py`, `evals/fixtures.py`, `evals/task.py`, `evals/evaluators.py`,
  `evals/dataset_suricata.py`, `evals/runner.py`, and their `tests/test_evals_*.py` counterparts.

## Verification
- `uv run pytest tests/test_evals_fixtures.py tests/test_evals_task.py
  tests/test_evals_evaluators.py tests/test_evals_dataset.py` — all pass with zero real
  model API calls (fixture assertions + `FunctionModel`-driven task tests + hand-built
  `EvaluatorContext` tests + dataset-shape smoke test).
- `uv run pytest` (full suite) — confirm no regressions in existing `skill_runner` tests.
- Live run: `uv run skill-evals --repeat 3 --logfire` against the real 4-model roster;
  confirm `report.print()` shows the `SurfacesPlantedFindings` columns catching the planted
  53h-flow/beacon/benign-host cases correctly, `CrashRateByCase` and
  `VerdictConsistencyAcrossReps` tables render, and spans appear in the "tomfoolery" Logfire
  project under an `evaluate suricata-model-comparison-...` root span.
