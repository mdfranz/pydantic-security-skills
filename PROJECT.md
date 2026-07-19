## Development Phases

### Phase 1: Foundation & Core Architecture (2026-07-16, 22:08)

**Commits:** `2105b62`

**Objective:** Establish the core runner architecture integrating Pydantic AI, Monty sandbox, and skill system.

**Work:**
- Implemented `runner.py` with `Agent` initialization, Monty sandbox configuration, and skill loader
- Wired `FileSystem` and `CodeMode` capabilities with basic mounts and OSAccess isolation
- Created `suricata-analyst` skill with initial SKILL.md instructions covering:
  - Sandbox constraints (stdlib subset, path namespaces, file I/O quirks)
  - Workflow steps (discovery, sampling, event enumeration, reference reading)
  - Working agreements (no deletions, artifact persistence, reuse patterns)
- Built initial CLI: skill directory, prompt, model selection, debug flag

**Design Decisions:**
- Load skill instructions directly from SKILL.md (no templating layer) — skill *is* the system prompt
- Store workspace in a single directory scoped to filesystem operations and code mounts
- Default model: `google:gemini-3-flash-preview` (replaceable via `--model`)

**Result:** A working end-to-end pipeline where a model can run Python analysis code safely against log data.

---

### Phase 2: Artifact Persistence & Sandbox Refinement (2026-07-17, 08:19-12:32)

**Commits:** `b004b9d` (08:19), `d2b2091` (09:49), `62e99ba` (12:32)

**Objective:** Implement persistent artifact storage and resolve a critical design issue with tool sandboxing.

**Commit b004b9d: Persist run artifacts and refine sandbox instructions**
- Implemented triple-artifact persistence:
  - `runner-*.log` — full transcript tee'd to both console and file
  - `generated_code/*.py` — sequential storage of every `run_code` call for auditability
  - `analyst_log-*.md` — final answer + conversation history, human-readable without parsing console
- Added `TeeWriter` class to mirror stdout/stderr to both terminal and transcript file
- Extended SKILL.md with detailed sandbox constraints (path namespaces, file I/O, exception handling)
- Configured logging to file with timestamp and level formatting

**Commit d2b2091: Keep FileSystem tools native**
- **Critical fix:** Changed `CodeMode(tools=[])` from the default `'all'` to empty list
- **Problem:** With `tools='all'`, FileSystem tools were only reachable from inside `run_code`, forcing every `list_directory()` call to burn a full sandbox round-trip (write code → run → parse result)
- **Solution:** By setting `tools=[]`, FileSystem tools remain **native** — the model calls them directly. Confirmed via Logfire traces that `execute_tool list_directory` is now a sibling of `execute_tool run_code`, not a child.
- **Implication:** SKILL.md's instruction "call `list_directory(path='.')` ... the FileSystem tool, not `run_code`" now has teeth — it's actually performant to follow.

**Commit 62e99ba: Add read-only skill mount**
- Added second MountDir: `/skill` → skill directory in read-only mode
- **Purpose:** Skills can reference `references/eve_format.md` (or domain-specific reference material) from inside `run_code` via `pathlib.Path("/skill/references/eve_format.md").read_text()`
- **Safety:** Read-only prevents generated code from modifying the skill's source or configuration
- **Generalization:** Any future skill's reference material is auto-exposed without per-skill code changes

**Result:** Complete artifact trail for every run, plus a performant, correct sandbox configuration that matches the skill's written instructions.

---

### Phase 3: Knowledge Accumulation & Historical Context (2026-07-17, 12:55-15:58)

**Commits:** `dfd08cf` (12:55), `0eb2ded` (15:57), `d71b8ca` (15:58)

**Objective:** Enable analysts to build on prior investigations without re-deriving findings.

**Commit dfd08cf: Discover prior analyst_log reports**
- Added pre-run scan for existing `analyst_log-*.md` files
- Modified run prompt to notify the agent:
  - How many reports exist (counted via `find_files`)
  - That they should use `search_files` with regex to locate reports relevant to the current task, not `list_directory` (which floods context on large report counts)
  - That they should read and incorporate relevant prior findings before starting new analysis
- **Instruction in SKILL.md (Step 1):** Explicitly calls out this workflow—don't assume you start from scratch; check prior reports first using bounded search, not full listing

**Commit 0eb2ded: Full conversation history in analyst_log**
- Enhanced `format_conversation_history()` to capture:
  - User prompt
  - Every model request/response (skipping large context to keep logs concise)
  - Every tool call with arguments (except run_code bodies, which are saved separately)
  - Every tool result (truncated if >1000 chars)
  - Model text responses
- Structured this as a "Conversation History" section in `analyst_log-*.md`
- **Benefit:** The report is self-contained; readers don't need to parse the transcript to understand what tools were invoked and why

**Commit d71b8ca: Skip run_code bodies in analyst_log**
- Realized that storing large code blocks in the analyst_log duplicates the `generated_code/*.py` index
- Changed log to skip `run_code` bodies and instead note "(Code saved to generated_code/ directory)"
- **Rationale:** The code is audit-accessible; including it in the narrative bloats the report without adding value

**Result:** A memory layer for multi-run analysis—analysts can search prior reports, understand what was already discovered, and pivot to new angles without starting cold.

---

### Phase 4: Observability & Interactive Analysis (2026-07-17, 16:02-17:25)

**Commits:** `be08593` (16:02), `0c41751` (17:01), `8035e08` (17:09), `24629c0` (17:25)

**Objective:** Add observability instrumentation and interactive control flow for analyst-guided investigations.

**Commit be08593: Graceful Logfire configuration**
- Wrapped Logfire setup in try-except to make it optional and non-fatal
- If `--logfire` is passed but Logfire is not installed or not authenticated, logs a warning and continues without tracing
- Enables `logfire.instrument_pydantic_ai()` to auto-instrument the Agent with OTel spans for every model call, tool execution, and tool result
- **Benefit:** Empirical verification of design decisions (e.g., verifying `tools=[]` actually makes FileSystem calls native, not sandboxed) and post-hoc debugging of complex runs

**Commit 0c41751: Interactive mode with frequent checkpoints**
- Added `--interactive` flag to enable checkpoint-driven analysis
- Modified agent instructions: after major phases, agent must ask the user:
  - "Here's what I've found so far..."
  - "Here's what I plan to investigate next..."
  - "Continue with [next analysis]?" (yes/no/focus on X instead)
- Implemented interactive loop in `main()`:
  - After first run, loop until user says "stop"
  - Accept three responses:
    - `continue` → proceed with outlined next phase
    - `stop` → wrap up and save findings
    - `focus on <topic>` → pivot to user-specified angle
  - Each continuation generates a new result and appends to findings
- **Rationale:** Batch analysis often produces wall-of-text silence. Interactive mode keeps analyst in control and let's them steer the investigation.

**Commit 8035e08: Accumulate findings across checkpoints**
- Modified artifact persistence to handle multiple outputs (one per checkpoint)
- In interactive mode, `analyst_log-*.md` now includes all checkpoint findings as separate sections:
  ```markdown
  ## Analysis Checkpoints
  
  ### Checkpoint 1
  [findings from first run]
  
  ### Checkpoint 2
  [findings from continuation after user pivot]
  ```
- Preserved run metadata (skill, prompt, mode) so reports are self-documenting
- **Benefit:** A single report captures the full investigation arc, including user-directed pivots

**Commit 24629c0: Dependency stability**
- Removed `exclude-newer` constraint from `uv.lock` to enable fresh dependency resolution
- Ensures new runs aren't pinned to outdated transitive dependencies

**Result:** Observability and interactive workflows that let analysts drive investigations in real time while preserving the full investigation trail.

---

### Phase 5: Thinking & Reasoning Integration (2026-07-18, 08:02-08:28)

**Objective:** Add unified support for model thinking/reasoning capabilities (e.g. Gemini 3+ / Claude Opus 4.6+ reasoning) in the runner.

**Work:**
- Added a `--thinking` CLI argument supporting effort level options: `low`, `medium`, `high`, `xhigh`
- Added a `--max-tokens` CLI argument to control maximum output token limits
- **Validation Fix:** Implemented auto-scaling of `max_tokens` when thinking effort is enabled. Anthropic APIs reject requests if `max_tokens` is less than `thinking.budget_tokens` (which defaults to a limit higher than Pydantic AI's default of 4096 tokens). The runner now maps budget allocations and automatically adjusts `max_tokens` (e.g., setting it to `budget_tokens + 4096`) unless overridden by the user.
- Integrated the Pydantic AI `Thinking` capability class into the `capabilities` pipeline
- Exposes thinking/reasoning logs across supported providers dynamically, allowing the runner to request reasoning depth per request

**Result:** Deeper analytical capabilities for security queries using LLM step-by-step thinking models natively supported by Pydantic AI, with stable parameter validation for Anthropic models.

**Correction (see Phase 6):** the auto-scaling fix described above only applied when `--max-tokens`
was *omitted*. Passing an explicit `--max-tokens` alongside `--thinking` skipped the safety
calculation entirely, so an insufficient explicit value still reached the Anthropic API unguarded
— confirmed live via two real `400` errors in Logfire before Phase 6 closed the gap.

---

### Phase 6: Trace-Driven Bug Fixes & Dependency Refresh (2026-07-18, 08:44-09:23)

**Objective:** Use Logfire traces (not just local `runner-*.log` output, which only shows
`HTTP 400 Bad Request` with no body) to find and close real gaps from Phase 5, then bring
dependencies current.

**Local logs vs. Logfire:** Local logs recorded the `--thinking`/`--max-tokens` testing failures
from Phase 5 as bare `HTTP 400 Bad Request` lines. Querying Logfire for the same time window
surfaced the actual API error bodies — two `` `max_tokens` must be greater than
`thinking.budget_tokens` `` rejections against `claude-haiku-4-5`, plus two earlier `404`s from
model-id typos (`haiku-4.5`, `haiku-4-5`) before landing on the correct `claude-haiku-4-5`. This
is what exposed the Phase 5 gap described above.

**Fix: unconditional `max_tokens` floor**
- `runner.py`'s `--thinking` handling now floors `model_settings["max_tokens"]` to
  `budget_tokens + 4096` unconditionally (`max(explicit_value, floor)`), instead of only when
  `max_tokens` wasn't already set — an explicit but insufficient `--max-tokens` is now raised,
  not trusted as-is.
- Replaced the hand-duplicated `budget_map` dict with `ANTHROPIC_THINKING_BUDGET_MAP`, imported
  directly from `pydantic_ai.profiles.anthropic` — the same table pydantic-ai's own Anthropic
  model class uses internally, so the two can no longer drift apart.
- **Verified live** against `anthropic:claude-haiku-4-5` with the exact previously-failing
  combination (`--thinking high --max-tokens 100`): now returns `200 OK` with a visible thinking
  block, where it previously 400'd twice.
- **Cross-model check:** the same `--thinking high --max-tokens 100` combination was also run
  live against `openai:gpt-5-mini` and `openai:gpt-5.6-sol` — both succeeded (`200 OK`). The
  `max_tokens`/`budget_tokens` constraint is Anthropic-specific, so the floor is harmless (simply
  unused) on providers that don't enforce it.

**Fix: recurring `ValidationError` in a reused workspace script**
- Reviewing a full `gpt-5.6-sol` run (interactive mode, 4 checkpoints, `~/workspace/analyst_log-26-07-18_08-48-53.md`)
  against its Logfire trace turned up a `pydantic_core.ValidationError` in a `run_code` call —
  the same error independently hit by an earlier `claude-opus-4-8` run. Root cause:
  `workspace/suricata_protocol_classification.py` built `failed_proto_ports` keyed by a raw int
  `dest_port` instead of `str(dest_port)`; the sandbox's tool-result schema requires string dict
  keys. Since this script is a saved, reusable artifact (the whole point of the workspace reuse
  pattern), the bug was costing a wasted round-trip on every future reuse until fixed.
  `workspace/` is gitignored, so this fix lives only on disk, not in this history — recorded here
  for context.
- Documented the general gotcha in `prompts/sandbox_notes.md` (a `run_code` return value with a
  non-string dict key fails validation) so future generated scripts avoid it up front instead of
  rediscovering it live.

**Logfire-derived performance comparison (Claude vs. others):** average output throughput
(`output_tokens ÷ duration`, reasoning tokens included) across real runs this session:
`claude-haiku-4-5` ~128 tok/s, `gemini-3-flash-preview` ~122 tok/s, `claude-opus-4-8` ~67 tok/s,
`gpt-5.6-sol` ~47 tok/s. Prompt caching on the `gpt-5.6-sol` interactive run was effective —
`cache_read_tokens` tracked ~97%+ of the prior turn's `input_tokens`.

**`PYDANTIC-STACK.md` updates**
- Added a new §2 documenting the `Thinking` capability (core `pydantic-ai`, not the harness) and
  `model_settings`, including the `ANTHROPIC_THINKING_BUDGET_MAP` floor described above.
  Renumbered §2-§8 to §3-§9 accordingly and fixed the two internal `§N` cross-references.
- Updated §5 (Capability ordering) to show the conditional third capability and note that its
  list position doesn't matter, unlike `FileSystem`/`CodeMode`.
- Added a row to the §9 design-rationale table for the `Thinking`/`model_settings` decision.

**Dependency refresh**
- `uv lock --upgrade` + `uv sync`: `pydantic-ai` 2.9.1 → 2.11.0 (re-verified live against
  Anthropic post-upgrade). `pydantic-ai-harness`, `pydantic-monty`, `logfire`, and `pyyaml` were
  left unchanged.
- Discovered why: a global `~/.config/uv/uv.toml` sets `exclude-newer = "2 days"`, a standing
  cooldown that excludes any release younger than 2 days from resolution. `pydantic-ai-harness`
  0.7.1 was published the same day as the upgrade, so it stayed on 0.7.0 by design, not neglect —
  this explains why `uv.lock` will generally trail PyPI's true latest by up to 2 days even right
  after an `--upgrade`.
- Updated `PYDANTIC-STACK.md`'s version table (`pydantic-ai` row only) to match.

**Result:** The `--thinking`/`--max-tokens` path is now verified correct (not just believed
correct) against real API responses across three providers, a recurring reused-script bug is
fixed and documented against recurrence, and dependencies are current within the project's
standing update policy.

### Phase 7: Task-Scoped Workspaces (2026-07-18, 15:38-15:53)

**Objective:** Implement `refs/workspace-lifecycle.md` — give the runner an accumulate/pristine
choice per run without touching the execution model, replace the console transcript with a
richer host-only audit trail, and separate canonical input evidence from agent-produced state.
A superseded, snapshot-based (`MontyRepl.dump()`) draft of that same doc was set aside earlier;
this phase implements the file-based directory design that replaced it, not any interpreter-state
serialization.

**`--task`/`--pristine` CLI flags and task-id resolution**
- `--workspace` now means the workspace **base/case root**, not the agent's own directory —
  a breaking layout change (see "Behavior change to confirm" in the design doc). Its default
  layout is `./workspace/data`, `./workspace/<task>`, `./workspace/logs`.
- Added `resolve_task_id()`: `--task <name>` accumulates into a named directory (validated
  against `^[a-z0-9][a-z0-9_-]{0,63}$`, `default`/`data`/`logs` reserved), `--pristine` defers to
  auto-generated `task-<uuid4>` id, neither falls back to `default`. `--task`/`--pristine` are a
  mutually exclusive `argparse` group.
- Added `create_task_root()`/`create_pristine_task_root()`: task roots must resolve to a real,
  direct child of the workspace base (rejecting symlinks and non-directories); pristine creates
  its directory exclusively (`mkdir` without `exist_ok`), retrying on the vanishingly unlikely
  UUID4 collision.

**Three-domain mount layout**
- Added a fourth `MountDir` at `/data` (read-only) alongside the existing `/workspace` (rw,
  now task-scoped) and `/skill` (ro) mounts — canonical input evidence, shared across tasks
  (including pristine ones) but never copied into a task workspace.
- `FileSystem`'s root moved from the workspace base to the resolved task root, so it structurally
  cannot reach `/data`, `workspace/logs/`, or another task's directory.
- Runner now lists `data_dir` at startup and injects the `/data/<filename>` inventory into the
  prompt, since the FileSystem tool can't see `/data` for the agent to discover it itself.
  Updated `prompts/sandbox_notes.md` (three path namespaces → four) and both skills'
  `SKILL.md` "Find the input file" steps accordingly.

**Host-only JSONL audit log, replacing the console transcript**
- Removed the old `TeeWriter`-into-workspace transcript and `logging.basicConfig` file handler.
- Added `AuditLog` (flushes each event immediately) and `make_event_stream_handler()`, which taps
  `agent.run_sync`'s `event_stream_handler` to log model text/thinking, tool calls/results, and
  `run_code` bodies/returns *incrementally* — not reconstructed after the fact — so an
  interrupted or failed run still leaves a complete record. Files land at
  `workspace/logs/runner-<task_id>-<run_id>.jsonl`, a flat sibling of the task directories and
  therefore unreachable through either agent access point.
- `run_start`/`prompt`/`error`/`run_end` events bracket each `run_sync` call, including the
  interactive-mode continuation/focus prompts.
- `Agent(name=skill_path.name, ...)` keeps a stable telemetry identity; `task_id`/`run_id` are
  attached as run `metadata` on every `run_sync` call instead.

**Verified live** against `google:gemini-3-flash-preview` in a scratch workspace: reserved-name
and invalid-regex task ids correctly hit `parser.error()` before any model call; default,
named-accumulate (with a real `eve-*.json` under `data/`), and `--pristine` runs each produced
the expected `data/` + `<task>/` + `logs/` layout; the JSONL audit file contained a well-formed
`run_start → prompt → tool_call → tool_result → model_text → run_end` sequence. The repo's own
`workspace/data/eve-2026-01-06-01.json` was already laid out correctly, so no migration was
needed there.

**Docs:** Updated `README.md` (case-root layout, task flags, audit-log description) and
`ARCHITECTURE.md` (component diagram, capabilities, Monty sandbox, workspace/audit-log sections,
trust-boundary diagram) to match. `refs/workspace-lifecycle.md` itself is the design source of
truth and was not modified.

**Result:** The runner now supports both accumulate and pristine task workspaces from a single
`--task`/`--pristine` flag pair, canonical input evidence is structurally separated from agent
state, and every run leaves a complete, host-only audit trail even when it fails partway through.

### Phase 8: Compliance Audit & Two Hardening Fixes (2026-07-18, 16:00-16:41)

**Objective:** Verify Phase 7's implementation against `refs/workspace-lifecycle.md` with live
runs (not just re-reading the code), across three real model/task combinations, then close the
gaps that verification actually found.

**Live verification against three models, three tasks:** Ran the same "summarize ports and
protocols" prompt against `google:gemini-3-flash-preview`, `google:gemini-3.5-flash` (same
accumulated task, `logfire-metadata-check`), and `openrouter:deepseek/deepseek-v4-pro` (fresh
task, `deepseek-v4-pro-check`) with `--logfire` enabled. Cross-checked each run two ways: the
audit JSONL on disk, and the actual span tree queried live from the `tomfoolery` Logfire project
via `mcp__logfire__query_run`. Confirmed the `invoke_agent suricata-analyst` root span carries
`metadata.task_id`/`metadata.run_id` matching the audit file's `run_id` in every case (note:
metadata lands on the root span only, not propagated to child `chat`/`execute_tool` spans —
pydantic-ai's behavior, not a bug here). Confirmed empirically, not just by reading the mount
config, that `FileSystem`'s `list_directory` never once surfaced `data/`, `logs/`, or another
task's contents across 5 real audit logs — only task-local files.

**Gaps found:**
1. Killing a run with an unhandled `SIGTERM` (e.g. a shell `timeout` wrapper, as opposed to
   `Ctrl+C`) bypassed `runner.py`'s `finally` block entirely, since Python installs no default
   handler for `SIGTERM`. Reproduced live: `runner-logfire-metadata-check-9267c4dd....jsonl`
   has 46KB of real events but no `run_end` — every individual event was still flushed on write,
   but the file never got its closing marker.
2. `workspace/logs/*.jsonl` was `0644` / dirs `0755` (umask defaults) — the doc's own "Retention
   and permissions" open item flags this as needed before use on sensitive cases, and it wasn't
   done yet.
3. (Not fixed, not a bug — confirmed as documented, agent-dependent behavior) deepseek-v4-pro's
   run left zero reusable `suricata_*.py` scripts despite 10 `run_code` calls, matching the
   doc's explicit caveat that only the script *listing* mechanism is runner-guaranteed, not that
   the agent chooses to write one.

**Fixes applied (user chose these two from a 4-option menu; task locking and retention/pruning
were declined as legitimate scope expansions beyond what the doc specifies):**
- Added `RunInterrupted(BaseException)` + a `signal.signal(signal.SIGTERM, ...)` handler in
  `runner.py`, registered right after the `AuditLog` is constructed. `SIGTERM` now raises
  through the same `try/except/finally` `KeyboardInterrupt` already used, logging a distinct
  `{"event": "interrupted", "reason": "received signal 15"}` before `run_end`, and the process
  exits `143` (the `128 + SIGTERM` convention) instead of a raw traceback. `SIGKILL` remains
  uncatchable by design — an OS limit, out of scope for any fix.
- `logs_dir.chmod(0o700)` (re-applied every run, not just on first creation) and
  `AuditLog.path.chmod(0o600)` at file creation.

**Verified live:** Interrupted a real in-flight `gemini-3.5-flash` `run_code` call with
`kill -TERM` after ~12s — confirmed exit code `143`, `workspace/logs/` at `drwx------`, the
`.jsonl` at `-rw-------`, and the audit tail showing a clean
`interrupted → run_end: failed` sequence, in direct contrast to the pre-fix file from item 1
above (no closing event, world-readable).

**Docs:** Updated `ARCHITECTURE.md`'s audit-log section with a "Retention and permissions"
subsection describing both fixes and the `SIGKILL` limit. `refs/workspace-lifecycle.md` itself
was not modified (design source of truth).

**Result:** Every audit record for a run terminated by `SIGTERM` now closes cleanly with an
explicit `interrupted`/`run_end` pair instead of trailing off mid-event, and `workspace/logs/`
is owner-only regardless of the host's umask. Task-level locking and a retention/pruning policy
remain deliberately unimplemented, per the doc's own "Open items" section.

### Phase 9: Cross-Session Agent Memory (2026-07-18, 18:38-18:43)

**Objective:** Give the agent a way to persist curated notes across runs of the same task,
without reintroducing the "prior agent state" that `--pristine` exists to exclude, and without
skewing the cross-model comparisons `compare_models.sh` depends on.

**Design:** Wired `pydantic_ai_harness.memory.Memory` — a separate, already-installed
(`pydantic-ai-harness==0.7.0`) capability providing a bounded, auto-injected `MEMORY.md`
notebook plus `read_memory`/`write_memory`/`search_memory`/`delete_memory` tools — into
`runner.py`, following the same task-id boundary that already governs script/analyst-log
accumulation rather than introducing a separate persistence concept:

- Scoped by `Memory(store=FileStore(str(memory_dir)), namespace=skill_path.name,
  agent_name=task_id)` — every operation is scope-prefixed server-side by `<skill>/<task_id>`
  (verified against the library source: a defensive `RuntimeError` guards against a backend ever
  returning a path outside the requested scope), so one shared store root safely hosts every
  task's notebook. A fresh pristine `task-<uuid4>` gets an empty notebook as a structural
  consequence, not a special case.
- The `Memory` capability is nonetheless **omitted entirely** in pristine mode (not merely
  `inject_memory=False`) — an empty notebook is harmless, but the tools/guidance text it adds
  would still change pristine's tool surface and prompt token count, undermining
  `compare_models.sh`'s apples-to-apples comparisons across models.
- Backed by `FileStore` (plain Markdown + a hidden SQLite journal), matching this project's
  "files are the source of truth" stance, at a new flat sibling `workspace/memory/<skill>/<task>/`
  — never mounted into the Monty sandbox, so `run_code`/pathlib can't reach it; the `MemoryToolset`
  is native, the same category as `FileSystem`'s tools.
- `memory` added to `RESERVED_TASK_NAMES`; `memory_dir` created and `chmod 0o700`'d at startup,
  before `Memory`/`FileStore` ever touch disk (the store's own lazy `mkdir` never chmods).

**Verified live** against `google:gemini-3-flash-preview` and `google:gemini-3.5-flash`:
- Accumulate mode (`--task memory-test`, three runs): first run created no memory file (agent
  wasn't prompted to); a second, explicit prompt ("write a short note... using write_memory")
  produced `workspace/memory/suricata-analyst/memory-test/MEMORY.md` (0600, under the 0700
  `memory/` tree) with the expected content; a third run recalled the persisted fact verbatim
  from the bounded injection without re-reading any file.
- Pristine mode, both models: startup print showed `memory: disabled (pristine)`; zero
  `workspace/memory/suricata-analyst/task-<uuid>/` directories were created; zero
  `read_memory`/`write_memory`/`search_memory` tool calls appeared in either transcript or audit
  `.jsonl`.
- `--task memory` correctly rejected the same way `--task logs` already is.

**Docs:** Updated `refs/workspace-lifecycle.md` (three domains → four, a fifth non-filesystem
access point, updated diagrams/worked examples/open items) and `ARCHITECTURE.md`/`README.md` to
match. `refs/workspace-lifecycle.md` remains the design source of truth.

**Result:** Accumulate-mode tasks now carry forward model-curated notes across runs, bounded and
automatically injected rather than depending on the agent's initiative to re-read old
`analyst_log-*.md` files; `--pristine` and `compare_models.sh` are structurally unaffected — the
capability doesn't exist for those runs at all, not just an empty one.

---

### Phase 10: Caching, Token-Overflow Safeguards, and Interactive Wrapper (2026-07-18, 21:00)

**Objective:** Address high-priority developer issues in `ISSUES.md` related to redundant log rescans and token overflow, and provide a simplified wrapper script for running models interactively.

**Work:**
- Added caching guidelines to the `osqueryd-analyst` skill instruction `Step 2: Targeted Analysis` in `skills/osqueryd-analyst/SKILL.md` to instruct the agent to save derived data (such as process/socket mapping) in the task workspace and reuse it to avoid linear rescans of large files.
- Added a `Python Style & Working Agreements` section to `prompts/sandbox_notes.md` to enforce token-overflow prevention across all skills. It requires models to aggregate/summarize in Python code and restricts detailed listings to at most ~50 lines per run.
- Implemented `run.sh` as a convenient bash wrapper at the repository root. It runs a model in interactive mode by default, prompts the user for the prompt/query, supports an optional skill directory, and forwards any extra arguments (e.g. `--debug`) to the python runner.
- Updated `README.md` to document the usage of the new `run.sh` wrapper.

**Result:** Improved performance and execution safety across all skills by eliminating token limit failures and raw log scanning overhead, and simplified interactive local execution.

---

### Phase 11: Hierarchical Provider Refactoring for Models (2026-07-18, 22:00)

**Objective:** Refactor model configuration file to group models under hierarchical providers instead of maintaining a flat list, and update resolving logic accordingly.

**Work:**
- Refactored [models.yaml](models.yaml) to group models by their provider (`google`, `openai`, `anthropic`, `openrouter`) in a hierarchical structure under the `providers` key.
- Updated `resolve_model` in [runner.py](skill_runner/runner.py) to parse the new hierarchical format while retaining a backward-compatible fallback to the legacy flat `models` list format.

**Result:** Cleaner models configuration taxonomy and more robust model resolution.

---

### Phase 12: Textual TUI Mode Alongside Console UX (2026-07-18, 23:00)

**Objective:** Implement `refs/textual-ui-plan.md`: add a multi-panel Textual TUI as a second,
opt-in UI, without changing console mode's behavior or artifact shape.

**Commit `3a986a8`: Split `runner.py` into `audit`/`script_lint`/`run_core`/`console_ui` modules**
- Extracted `AuditLog`, `RunInterrupted`, the SIGTERM-to-exception plumbing, and
  `make_event_stream_handler` into `audit.py`. The handler now calls both `audit.event(...)` and
  `sink.emit(...)` with identical `(kind, fields)` for every event, so the active UI always sees
  exactly what the audit log persists; per-kind label formatting moved out of the handler and into
  each sink.
- Extracted `lint_and_fix_scripts` into `script_lint.py`, adding an `on_message` callback so
  neither UI leaks a raw `print()`.
- Extracted task/workspace setup, skill/prompt assembly, `Agent` construction, the model-alias
  resolver, the `RunSink` protocol, shared `run_turn_sync`/`run_turn_async` per-turn helpers, and
  `ArtifactSession`/`write_artifacts` into `run_core.py` — the sole module both UI drivers depend
  on for run behavior.
- Extracted today's console behavior (`ConsoleSink`, checkpoint parsing, `run_console`) into
  `console_ui.py`, unchanged byte-for-byte: same `agent.run_sync`, same blocking `input()`
  checkpoint loop, same report/artifact shape.
- `runner.py` reduced to a thin CLI entrypoint.
- Verified via unit tests against the extracted pure functions (`parse_checkpoint_input`,
  `build_continuation_prompt`, `write_artifacts`, `lint_and_fix_scripts`) and live runs (`--model
  test`, TestModel) confirming identical console output, audit-log sequencing, and report
  structure pre/post-refactor.

**Commit `9a9e165`: Add Textual TUI mode alongside console UX**
- New `ui_textual.py`: `BufferedTextualSink` (accumulates setup-time status lines until
  `AnalystApp` mounts and can drain them), `TextualSink` (routes streamed events into a
  `DataTable` that pairs each tool call with its result by `tool_call_id` — updated in place
  rather than appended, since `RichLog` has no such API — plus a `RichLog` for model
  text/thinking and a live `DirectoryTree` of the task workspace), `AnalystApp`, and
  `run_textual(...)`.
- `--ui {console,textual}` (default `console`); the `[skill_dir] prompt` positional grammar
  generalized to accept 0/1/2 values (0 valid only with `--ui textual`, opening an empty session
  where the first bottom-bar message becomes the first turn); a `find_spec` guard before any
  workspace side effects; `textual` added as an optional `tui` extra (`uv sync --extra tui`),
  imported lazily so a console-only install never imports it.
- Multi-turn Textual sessions checkpoint the same `analyst_log-<run_stamp>.md` after every
  successful turn, listing every submitted prompt rather than claiming the latest follow-up was
  the sole original one; a failed/interrupted turn leaves the prior checkpoint intact.
- **SIGTERM finding during implementation:** headless testing (`App.run_test()` /
  `App.run(headless=True)`, a real OS thread sending itself `SIGTERM` mid-turn) showed the raw
  `signal.signal(SIGTERM, ...)` handler `run_core.prepare_run` installs (used as-is by console
  mode) can land inside asyncio's own internals under Textual — observed interrupting
  `_run_once`/`selector.select()` rather than the running turn's own coroutine, converting into an
  `asyncio.CancelledError` at the `run_test()` boundary instead of a clean shutdown. Fixed by
  having `AnalystApp` take over `SIGTERM` via `asyncio`'s own `add_signal_handler` once mounted,
  which resolves any delivery to a deterministic `self.exit()` regardless of whether a turn is
  active. Re-verified end to end in headless mode: a mid-turn `SIGTERM` now reliably produces the
  `interrupted`/`run_end` audit events and propagates `RunInterrupted` for the exit-143 mapping.
  Real-terminal verification (not just headless) is still a recommended manual check.

**Docs:** Updated `ARCHITECTURE.md` (mermaid diagram routes agent events through a `RunSink`
before `write_artifacts`; new "Two UI backends, one `RunSink` contract" subsection under Runner)
and `README.md` (`--ui`/optional-prompt examples, new "TUI mode" section, updated flag list).

**Result:** A second, fully opt-in UI for interactive investigation sessions, sharing 100% of run
behavior with console mode through `run_core.py` — no divergence in artifacts, audit trail, or
event vocabulary between the two.

**Uncommitted documentation update (2026-07-19): Harden shared Monty sandbox guidance**
- Reviewed runner audit logs and added explicit shared-prompt guidance for unavailable
  `ipaddress` and `str.format()`, the unsupported comma variants of f-string formatting, and the
  correct distinction between native `read_file` and relative `pathlib` access when reusing a
  saved workspace script. This applies to every skill because `prompts/sandbox_notes.md` is
  prepended to each skill prompt.

---

### Phase 13: Package the Runner (2026-07-19)

**Objective:** Keep shell wrappers and Markdown at the repository root while moving all Python
runner implementation into an importable package.

- Moved the CLI, shared run behavior, audit logging, script linting, and both UI drivers into
  `skill_runner/`; package-local imports make the runtime independent of root-level Python
  modules.
- Added the `skill-runner` console command through `pyproject.toml`, backed by Hatchling, so
  users invoke `uv run skill-runner …` rather than executing a source file directly.
- Kept `run.sh` and `compare_models.sh` at the root and updated them, operational docs, and the
  workspace-lifecycle examples to use the installed command.

**Result:** The repository root now contains shell scripts, Markdown, project configuration, and
content directories only; the Python runner is a packaged application.

---

### Phase 14: Session-Oriented Runner Refactor (2026-07-19)

**Objective:** Reduce orchestration complexity left after the package split, make run lifecycle
ownership explicit, and prevent the console and Textual drivers from drifting apart.

**Work:**
- Added immutable `RunOptions` and a normalized `ModelCatalog` in `skill_runner/config.py`,
  removing `argparse.Namespace` mutation and the duplicate hierarchical/flat model traversals.
- Reduced `skill_runner/run_core.py` to secure workspace preparation, prompt/capability assembly,
  agent construction, and a narrow `RunSetup` handoff. Setup failures after audit creation now
  record `setup_error`/failed `run_end`, close the log, and restore the prior SIGTERM handler.
- Added `RunSession` as the application boundary shared by both UIs. It owns first-turn inventory
  prefixing, sync/async submission, message history, structured turns, checkpoint policy, script
  cleanup, artifact persistence, failure/interruption state, audit finalization, and signal-handler
  restoration.
- Moved report rendering and generated-code persistence into `skill_runner/artifacts.py` around
  valid-by-construction `Turn` and `Transcript` models.
- Split reusable Textual tables and modal screens into `skill_runner/tui_widgets.py`, leaving
  `ui_textual.py` focused on event rendering and application interaction.
- Added focused unit coverage for model normalization, multi-turn report rendering, prompt-prefix
  behavior, successful and failed session finalization, and post-audit setup failures.

**Result:** The UI drivers now collect input and render output while one session layer enforces
run behavior. `run_core.py` fell from 674 to 381 lines and `ui_textual.py` from 561 to 309 lines,
with audit cleanup defined for both setup-time and turn-time failures.

---

### Phase 15: Model-Comparison Testing & Stuck-Run Safeguards (2026-07-19, 11:21-14:47)

**Objective:** Use `--pristine`/`--logfire` model comparisons — not just code review — to find
real behavioral and reliability gaps across the model roster, then close the ones with the
clearest evidence.

**Commit `ba285bb` (11:21): Pristine model-comparison results and expanded `ISSUES.md`**
- Ran a 9-run `--pristine` matrix (gemini-3-flash-preview, glm-5.2, qwen3.6-flash × 3 reps,
  identical prompt/data, isolated workspaces) specifically to separate genuine per-model habits
  from workspace-accumulation confounds present in the 2026-07-18 comparison.
- **Structural findings:** whether a model ever calls `write_file` (a persistent, named script) vs.
  relying solely on `run_code` (auto-snapshotted to `generated_code/`, regardless of intent) is a
  real per-model habit, not chance — glm-5.2 did it in all 3 reps, gemini in 1 of 3, qwen in 0 of 3.
  glm-5.2 was also 5-8x slower than the other two (495-812s vs. 105-166s) while doing comparable
  work — thorough, not stuck, which specifically motivated a wall-clock (not turn-count) safety net.
- **Qualitative findings (the more important half):** read each run's actual `## Final Findings`
  write-up, not just call counts, and verified the disputed signals directly against the raw
  `eve-2026-01-06-01.json` rather than trusting the reports' own numbers. A real 53-hour flow
  (`192.168.2.197→192.200.0.106:80`, confirmed via `grep`: exactly 1 matching record, `flow.age:
  192346`) was flagged as likely C2 by gemini-3-flash-preview in only 1 of its 3 identical reps. A
  real MQTT beacon (`192.168.3.105→20.44.17.102:8883`, confirmed: exactly 125 flows) was
  investigated by glm-5.2 in all 3 reps using the same beacon-scan methodology each time, but its
  own severity verdict flipped — "low risk" in rep 1, "HIGH confidence, isolate the host" in reps 2
  and 3. Same model, same prompt, same data, opposite conclusions on the actual security question —
  the first hard evidence in this project that model non-determinism affects analytical outcomes,
  not just process/style.
- qwen3.6-flash crashed 2 of 3 reps, each a different mode: tool-retry exhaustion on a
  Monty-unsupported `{x:,}` format specifier, and a context-window overflow from an unbounded
  `print(json.dumps(s))` loop over all 1,440 `stats` events (24.7M chars fed back into its own
  context). A project-wide scan of every `workspace/logs/*.jsonl` and the full Logfire exception
  history (not just this test's own logs) found both failure modes recurring independently in
  earlier, unrelated sessions — confirming systemic weaknesses, not flukes — plus five more issues:
  undocumented Monty stdlib/builtin gaps (`collections` missing entirely, 7 occurrences — the
  single most common recoverable error project-wide), a static-type-check-vs-runtime error class
  distinction that actively misleads the model, the single most-repeated error in the project's
  history (`FileSystem` "path resolves outside the root directory" — models reaching for `/data`
  through the wrong tool, 11+ occurrences despite SKILL.md already warning against it), and
  unvalidated `--model` CLI input surfacing typos as opaque provider 404s instead of a local hint.
- Filed as `ISSUES.md` #5-#11.

**Commit `d548c1b` (14:47): `--max-retries`/`--max-run-seconds`/`--max-turns`, and a real exit-code
bug found while validating them**
- A second `--pristine` comparison (deepseek-v4-pro, kimi-k3, minimax-m3 × 3 reps, same
  prompt/data) surfaced a failure class distinct from #5/#6: raw network exceptions, uncaught,
  including a kimi-k3 rep that hung **silently for ~2 hours** (audit log: last real activity at
  `15:37:40 UTC`, `httpx.ReadTimeout` not raised until `17:34:32 UTC`) before producing nothing.
  Filed as `ISSUES.md` #12 — the strongest evidence yet that a wall-clock budget, not just a
  turn-count or retry-count cap, was needed.
- Added three CLI flags: `--max-retries` (default 5, up from `CodeMode`'s hardcoded 3 —
  `run_core.py`), `--max-turns` (`UsageLimits(request_limit=...)`, previously silently defaulted to
  50 and untunable — `session.py`), and `--max-run-seconds` (a per-turn wall-clock budget). The
  wall-clock budget is enforced by `audit.py`'s new `start_turn_watchdog()`: a background
  `threading.Timer` that sends the process its own `SIGTERM` on overrun, deliberately reusing the
  existing `SIGTERM`→`RunInterrupted` clean-shutdown path from Phase 8 instead of a second
  interruption mechanism — verified this delivers correctly to the main thread even from inside a
  running `asyncio` event loop (isolated repro: interrupted an `asyncio.run()` sleep at exactly
  2.0017s against a 2s budget).
- **Bug found while validating live against a real model:** an interrupted run crashed with a raw
  traceback and exit 1, not the clean exit 143 `RunInterrupted`/`map_run_interrupted_exit_code()`
  are designed to produce. Root cause: `runner.py`'s `main()` never actually caught
  `RunInterrupted` — that mapping only existed under `if __name__ == "__main__":`, which the
  installed `skill-runner` console-script entry point (`pyproject.toml`'s `skill-runner =
  "skill_runner.runner:main"`, what `uv run skill-runner` actually invokes everywhere in this repo)
  never runs through; confirmed via the crash traceback itself
  (`.venv/bin/skill-runner: sys.exit(main())`, no guard in between). **This means real Ctrl+C and
  SIGTERM have crashed the same way since the console-script entry point was added in Phase 13** —
  not something this change introduced, just something it exposed. Fixed by moving the catch into
  `main()` itself. Filed and closed as `ISSUES.md` #13.
- Added `tests/test_audit.py` (watchdog: no-budget, cancel-before-fire, fire-and-interrupt) and a
  `test_session.py` integration test proving the wiring through `RunSession.submit_sync`. 9/9 tests
  pass.
- **Verified live:** `--max-run-seconds 5` against `google:gemini-3-flash-preview` now exits `143`
  with no traceback in ~7s (5s budget + startup overhead), and the audit log shows a clean
  `interrupted` → `run_end status=failed` sequence instead of an `error` event.

**Result:** Two rounds of `--pristine` model-comparison testing found nine new, evidenced issues
(`ISSUES.md` #5-#13) — including one, #13, that had been silently defeating the project's own
Ctrl+C/SIGTERM safety net since Phase 13. Three now have shipped fixes: retry exhaustion
(`--max-retries`), unbounded hangs (`--max-run-seconds`), and the exit-code bug itself. Six remain
open and documented for follow-up (Monty stdlib gaps, the `FileSystem` path-confusion error, model
CLI validation, and the qwen3.6-flash-specific crash patterns' upstream mitigations).

---

### Phase 16: Runtime Output and Provider Resilience (2026-07-19)

**Objective:** Enforce the tool-output boundary at runtime and tolerate a transient provider rate
limit without replaying an agent run.

- Added `OverflowingToolOutput` to every agent. Tool returns at or above 10,000 characters are
  stored in an owner-only, task-scoped `workspace/logs/overflow/<task>/` store before model
  history is assembled; the model receives a bounded preview and `read_tool_result` handle, with
  a 4,000-character truncation fallback if persistence fails.
- Extended tool-result audit events with the spill handle and original byte count, preserving a
  durable link to the complete output without copying that output into the JSONL record.
- Added a provider request hook that retries HTTP 429 once, honors
  `metadata.retry_after_seconds`, clamps waits to 0.1–30 seconds, and audits the retry. Other HTTP
  failures and a second 429 still propagate normally.

**Result:** Issues #2/#6 no longer rely on prompt compliance to protect model context, and a
transient OpenRouter/Kimi rate limit gets one bounded retry at the failed request boundary rather
than requiring a monkeypatch or replaying completed tool calls.
