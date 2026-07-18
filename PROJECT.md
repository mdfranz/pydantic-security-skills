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
