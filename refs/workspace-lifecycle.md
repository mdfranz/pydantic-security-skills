# Workspace Lifecycle: Task-Scoped Workspaces

> Supersedes the earlier snapshot-focused draft of this file. Monty snapshotting is **not**
> the mechanism used here — see the Appendix for why it was set aside.

## Purpose

Give the runner two workspace modes without changing anything about how the agent executes:

- **Accumulate** — reuse a workspace across runs so investigations build on prior scripts and
  analyses (today's behavior).
- **Pristine** — start a fresh, isolated workspace that is guaranteed not to be polluted by
  earlier runs.

Both fall out of a single idea: **a task workspace is identified by a *task name*.** Reusing a
name accumulates; a freshly generated name is pristine. The top-level `workspace/` directory is
the case root: it also owns canonical, read-only input data and host-only audit records. No
interpreter-state serialization is involved.

## Goals / non-goals

**Goals**
- Per-run choice between accumulate and pristine.
- Keep files as the source of truth (nothing depends on serialized in-memory state).
- Stop the agent from seeing the host's own audit trail.
- Keep canonical evidence separate from agent-produced state and make it read-only to the agent.
- Retain a complete, durable audit record even when a run fails or is interrupted.

**Non-goals**
- No change to the execution model: `run_code` stays stateless top-level snippets. No
  `MontyRepl`, no cross-call in-memory state.
- No Monty snapshotting for persistence (see Appendix).

## Four domains

Every file belongs to one of four domains, which decides whether and how the agent can see it:

| Artifact | Domain | Written by | Location | Agent-visible? |
|---|---|---|---|---|
| Caller-supplied evidence such as `eve.json` | Input | Operator, before the run | `workspace/data-source/` (or `workspace/data/`, legacy fallback — exactly one is selected for the whole run) | yes — `/data-source`, aliased at `/data`, read-only; also indirectly via `query_events`/`aggregate_events` (by logical filename, never a path) |
| Host-side Parquet cache for `query_events`/`aggregate_events` | Host (derived) | The runner, lazily on first query | `workspace/data-sink/parquet/` | **no** — never mounted; reachable only through the two data tools' return values, never by path |
| Reusable `<prefix>_*.py` scripts | Agent | The agent itself, via `FileSystem.write_file` mid-run | `workspace/<task>/` | yes — the runner injects a listing of these into the next run's prompt, unconditionally |
| `analyst_log-*.md` analysis reports | Agent | The runner, assembled from the completed conversation *after* the run ends | `workspace/<task>/` | yes, but only if the agent finds it — reachable via `list_directory`/`read_file`, never injected into the prompt |
| `generated_code/*.py` (run_code copies) | Agent | The runner, one file per `run_code` call, written *after* the run ends | `workspace/<task>/` | yes, but only if the agent finds it — same as above; this is an unconditional capture of every `run_code` call regardless of success, not something the agent chose to save |
| `MEMORY.md` + topic files | Memory | The agent, via the `write_memory` tool (mediated by the `Memory` capability, not `FileSystem`) | `workspace/memory/<skill>/<task>/` | yes, but never through the filesystem — only via `write_memory`/`read_memory`/`search_memory`/`delete_memory` and a bounded automatic injection into every model request; never `list_directory`/`read_file` or any mount |
| `runner-<task>-<run-id>.jsonl` audit record | Host | The runner, incrementally as the run progresses | `workspace/logs/` (flat) | **no** |
| Oversized tool return | Host | `OverflowingToolOutput`, before model history | `workspace/logs/overflow/<task>/<run-id>/` | only in bounded slices through an opaque `read_tool_result` handle; never through the filesystem |

**Input domain** = canonical source evidence supplied by the operator. It lives in
`workspace/data-source/` if that directory already exists as a real, non-symlink directory,
otherwise in `workspace/data/` (created/reused as the fallback) — selected once at startup, never
a union of both. The runner mounts the selected directory at `/data-source`, with `/data` as an
alias of the same host directory for backwards compatibility. It is intentionally shared between
tasks, including pristine tasks, but it is not prior agent state. The agent must never modify it.
A derived, host-only cache of this domain (`workspace/data-sink/parquet/`) backs the
`query_events`/`aggregate_events` tools — see "A sixth, non-filesystem access point" below; it is
not itself part of the Input domain's visibility story since the agent never addresses it by path.

**Agent domain** = everything produced during or about a task run that the agent may consume
later — not necessarily everything the agent itself writes. Only the reusable scripts are
agent-authored, via a mid-run tool call; the analysis reports and generated-code copies are
written by the runner, after the run completes, from the finished conversation. All three still
belong to this domain because visibility, not authorship, is what the domain boundary tracks:
all three live in the task workspace, the only **writable** tree the agent can reach — mounted
into the sandbox at `/workspace` (read-write) and exposed as the `FileSystem` root. This is what
accumulates or resets. Input data is not copied into a task workspace, so large evidence files
are not duplicated for every pristine run.

Only the script inventory is a *guarantee* — the runner injects it into every prompt on reuse.
Analysis reports and generated-code copies are merely *reachable*: the agent has to call
`list_directory` and decide to read them. In practice it does (confirmed by this project's own
telemetry — the agent has `read_file`'d prior `analyst_log-*.md` files across sessions), but
nothing in the runner forces it, so treat "analyses accumulate" as agent-initiative-dependent, not
runner-guaranteed, until/unless the inventory injection is extended to cover them too.

> A fourth mount — the current skill directory at `/skill` (read-only) — is a
> *read-only input*, not an artifact domain: the agent consults `references/*.md` there but never
> writes to it, and it lives outside `workspace/` entirely (`skills/<name>/`). It is unaffected
> by task scoping and out of scope for this doc, but it is the reason the isolation guarantee
> below counts *four* access points, not three.

**Memory domain** = a persistent, per-`<skill>/<task>` notebook (`MEMORY.md` plus any topic files
the agent chooses to split out), stored as plain Markdown under `workspace/memory/`. It is
agent-authored like the reusable-script part of the Agent domain, but its visibility model is
different: it is never mounted into the sandbox and never rooted by `FileSystem`, so the agent can
only reach it through the `Memory` capability's own tools (`write_memory`, `read_memory`,
`search_memory`, `delete_memory`) and a bounded automatic injection into every model request —
never through file listing or a path the agent supplies. It accumulates identically to reusable
scripts in accumulate mode, scoped to `<skill>/<task_id>` so different tasks never share a
notebook. In pristine mode the `Memory` capability is omitted entirely (not merely pointed at an
empty scope), so a pristine run's tool surface and prompt token count stay identical across models
— see *Isolation guarantee* below and `scripts/compare_models.sh`, whose entire purpose depends on
that.

**Host domain** = runtime records kept outside the task directory. The append-only audit event
stream is about the run, not an input to it; each flat JSONL filename carries the task id and a
unique run id. Complete oversized tool returns live separately under
`workspace/logs/overflow/<task>/`. The `logs/` directory is a sibling of the task dirs, so neither
record type is filesystem-visible to the agent. Spill contents can only be retrieved in bounded
slices through `read_tool_result` using an opaque handle (see *Isolation guarantee* below).

> Only host runtime records leave the task directory. `analyst_log-*.md` and `generated_code/`
> write under `workspace/<task>/` and stay there.

## Workspace identity and modes

The task name is resolved once at startup:

| Invocation | Task id | Mode |
|---|---|---|
| `--task <name>` | `<name>` | accumulate (reusing the name reuses the dir) |
| `--pristine` | `task-<full-uuid4>` (auto-generated) | pristine (fresh, isolated) |
| neither | `default` | accumulate, into a stable default dir |
| `--task` **and** `--pristine` | — | error: mutually exclusive |

- **Pristine is not throwaway.** A pristine run's directory persists after the run (files stay
  source of truth). It is never selected automatically again, but an operator may deliberately
  reopen it with `--task <generated-id>`.
- **Pristine starts with no prior agent state.** A full UUID4 is generated and its directory is
  created exclusively, retrying on the vanishingly unlikely collision. It contains no prior
  scripts, reports, generated code, or task-local outputs. It can still read the intentional,
  read-only evidence in `/data-source` (aliased at `/data`). The `Memory` capability itself is omitted for the run (not
  merely pointed at an empty scope), so pristine never carries memory-tool overhead into the
  prompt or tool surface either.

### Task-id rules

Task IDs are identifiers, **not paths**. A user-supplied `--task` must be a single portable slug
matching `[a-z0-9][a-z0-9_-]{0,63}`. `default`, `data`, `data-source`, `data-sink`, `memory`, and
`logs` are reserved. This rejects path separators, `.` and `..`, absolute paths, control
characters, Unicode-normalization collisions, and filename injection into audit paths.

## Isolation guarantee

The agent touches the filesystem through four access points, and none of them can reach
`workspace/logs/`:

- `FileSystem(root_dir=workspace/<task>)` — FS tools are rooted at the task subdir and cannot
  traverse above their root.
- `MountDir("/workspace", workspace/<task>, read-write)` — the sandbox's `pathlib` can reach this
  writable mount, scoped to the task subdir.
- `MountDir("/data-source", <selected source root>, read-only)` and `MountDir("/data", <same
  source root>, read-only)` — the same host directory mounted under two virtual paths; sandboxed
  code can read canonical evidence, but cannot change it. The FileSystem capability remains rooted
  at the task workspace and cannot reach either.
- `MountDir("/skill", skills/<name>/, read-only)` — a read-only mount of the current skill dir,
  which lives outside `workspace/` entirely, so it can never expose anything under `workspace/`.

**`workspace/data-sink/parquet/` is deliberately not on this list.** It is never mounted into the
sandbox and never rooted by `FileSystem`; the only way to reach the data it holds is the sixth
access point below, which returns parsed values, never a filesystem path into the cache.

**A fifth, non-filesystem access point: the `Memory` capability.** When enabled (accumulate mode
only — see below), the agent additionally reaches `workspace/memory/<skill>/<task>/` through the
`Memory` capability's native tools and its automatic injection — never through `FileSystem`, a
mount, or any path the agent supplies. Every operation is scoped server-side by
`<namespace>/<agent_name>` (here, `<skill>/<task_id>`), and the store defensively raises if a
backend implementation ever returns a path outside that scope. `workspace/logs/` and every other
task's memory scope stay unreachable through this channel for the same reason `workspace/logs/`
stays unreachable through the four filesystem access points below: the scope prefix, like the task
root, is never a value the agent supplies. Omitted entirely in pristine mode, so a pristine run has
no memory access point at all.

**A bounded spill read-back channel: `read_tool_result`.** `OverflowingToolOutput` always exposes
this tool so the model can retrieve slices of an oversized return by opaque handle. The backing
store is fixed server-side to `workspace/logs/overflow/<task>/`; the model cannot choose or
browse a host path, escape that task's spill root, or receive more than the tool's built-in line
and character caps in one call. This is deliberate content access to a specific tool result, not
filesystem access to `workspace/logs/`.

**A sixth, non-filesystem access point: `query_events`/`aggregate_events`.** Two sandboxed
function calls (selected into `run_code` by `CodeMode.tools`), bound host-side to this run's
selected source root and `workspace/data-sink/parquet/`. The model passes a logical filename and
typed filters — never a path — and `ensure_parquet_cache()` (`skill_runner/data_tools.py`) does
all path resolution: it rejects anything but a bare filename, rejects symlinks, and requires the
resolved file's parent to equal the resolved source root exactly, so there is no traversal out of
the selected source root and no way to address the Parquet cache's internal, fingerprinted
filenames directly. Results are plain dicts (rows or counts), never a filesystem path back into
`data-sink/`.

The two writable workspace-side access points are scoped to `workspace/<task>/`; `/data-source`
(and its `/data` alias) grant only read access to the selected input directory; and the skill
mount is outside `workspace/` altogether. Thus anything else under `workspace/`, including audit
JSONL, the spill directory as a filesystem, `data-sink/`, and every other task directory, is
unreachable through those surfaces. The bounded spill read-back channel and the data tools above
are the sole deliberate exceptions, and both return content, never a host path. Four conditions
keep the filesystem guarantee true:

1. **The root and mount must stay the task subdir.** The entire guarantee is "agent root =
   `workspace/<task>/`." If either is ever pointed at the `workspace/` base, `logs/` leaks. This
   is the one invariant a future refactor must not break.
2. **Task IDs must remain validated identifiers.** Concatenating an unvalidated task string into
   a path permits traversal such as `foo/../logs`, even if the literal name `logs` is reserved.
3. **Task roots must be real, direct children of the resolved base.** Reject a pre-existing
   symlink, non-directory, or a resolved root whose parent is not the workspace base. Validate
   this once, then reuse that single validated path for everything that touches the task root —
   the `FileSystem` and `MountDir` constructions, *and* the runner's own direct host-side writes
   (`analyst_log-*.md`, `generated_code/`). The filesystem tool correctly rejects traversal
   *below* its root, but cannot make an unsafe root safe — and the host-side `Path.write_text()`
   calls get no such protection at all, so they depend entirely on this upstream validation.
4. **`data`, `data-source`, `data-sink`, `logs`, `memory`, and `default` are reserved task
   names.** They are siblings of task dirs and must never be selected as a task root.

## Directory layout

```
workspace/                                # base (--workspace, default ./workspace)
  data-source/                            # caller-managed, read-only evidence (primary; wins if present)
    eve.json
  data/                                   # legacy fallback, only selected if data-source/ absent
  data-sink/
    parquet/                              # host-only Parquet cache; NOT mounted, NOT agent-visible
      <sha256-fingerprint>.parquet
  default/                                # no flag → accumulate here   ← agent-visible
    suricata_extract_sni.py               # reusable agent scripts
    analyst_log-25-07-18_...md            # prior analyses (agent can re-read)
    generated_code/
      25-07-18_...-01.py
  suricata-triage/                        # --task suricata-triage (reused = accumulate)
  task-9f2a1c7b4e0d-.../                  # --pristine (fresh each time)
  memory/                                 # reserved; per-(skill,task) notebooks  ← tool-visible only, never via FileSystem
    .memory-store.sqlite3                 # FileStore's own bookkeeping journal, not a memory file
    suricata-analyst/
      default/
        MEMORY.md
      suricata-triage/
        MEMORY.md
  logs/                                   # reserved; host runtime records  ← NOT filesystem-visible
    runner-default-<run-id>.jsonl         # task name is in the filename
    runner-suricata-triage-<run-id>.jsonl
    runner-task-9f2a1c7b4e0d-...-<run-id>.jsonl
    overflow/                             # owner-only full tool returns; not filesystem-visible
      default/
        <run-id>/
          <tool-call-id>.0
```

Audit files at the root of `logs/` are flat — one append-only JSON Lines event stream per run,
with the task id and unique run id in the filename — so an audit record maps one-to-one to its
task. Oversized tool results are nested separately under `logs/overflow/<task>/`; they remain
outside filesystem and sandbox access, while `read_tool_result` can retrieve a bounded slice by
opaque handle. Both stores sit beside the task dirs, not inside any of them.

`memory/` is a flat sibling too, but nested one level deeper than `logs/` — by skill, then task —
since a single `Memory` store root hosts every skill's and task's notebook, each isolated by its
own `<skill>/<task>` scope prefix (see *Isolation guarantee*). No `--pristine` task ever appears
under it, since the capability is omitted for pristine runs entirely.

## Worked examples

`run_stamp` is `YY-MM-DD_HH-MM-SS` for task-local artifact names; each audit file also uses a
unique `run_id`. Only paths created or changed by each run are shown.

### 1. No flags → `default` task (accumulate)

```
$ uv run skill-runner "analyze eve.json"
```
```
workspace/
  data/
    eve.json                             # host-supplied evidence; agent reads at /data/eve.json
  default/
    suricata_extract_sni.py              # agent-saved reusable script
    analyst_log-25-07-18_14-02-11.md
    generated_code/
      25-07-18_14-02-11-01.py
  memory/
    suricata-analyst/
      default/
        MEMORY.md                        # only if the agent chose to write_memory this run
  logs/
    runner-default-<run-id>.jsonl        # agent cannot reach this
```

### 2. Same command later → accumulates in place

```
$ uv run skill-runner "now check for beacons"
```
```
workspace/
  default/
    suricata_extract_sni.py              # ← still here, surfaced to the agent
    suricata_beacon_score.py             # ← new
    analyst_log-25-07-18_14-02-11.md     # ← prior analysis, agent can re-read
    analyst_log-25-07-19_09-15-40.md     # ← new
    generated_code/
      25-07-18_14-02-11-01.py
      25-07-19_09-15-40-01.py            # ← new
  memory/
    suricata-analyst/
      default/
        MEMORY.md                        # ← grows in place across both runs, same scope
  logs/
    runner-default-<first-run-id>.jsonl
    runner-default-<second-run-id>.jsonl # ← new
```

The agent starts run #2 seeing `suricata_extract_sni.py` and the earlier `analyst_log`, plus
whatever `MEMORY.md` already held from run #1, bounded-injected automatically this time instead
of requiring the agent to go read `analyst_log-25-07-18_14-02-11.md` itself.

### 3. `--task suricata-triage` → named, reusable (isolated from `default`)

```
$ uv run skill-runner --task suricata-triage "triage today's alerts"
```
```
workspace/
  default/                               # untouched
  data/                                  # untouched; still mounted at /data read-only
  suricata-triage/
    analyst_log-25-07-19_10-30-00.md
    generated_code/
      25-07-19_10-30-00-01.py
  logs/
    runner-suricata-triage-<run-id>.jsonl
```

Rerun with `--task suricata-triage` and it accumulates into this same folder.

### 4. `--pristine` → fresh task workspace, no prior agent state

```
$ uv run skill-runner --pristine "baseline test run"
```
```
workspace/
  default/                               # untouched
  data/                                  # untouched; intentional read-only input
  suricata-triage/                       # untouched
  task-9f2a1c7b4e0d-.../                 # ← brand new; no prior agent state
    analyst_log-25-07-19_11-45-22.md
    generated_code/
      25-07-19_11-45-22-01.py
  memory/                                # ← unchanged; no suricata-analyst/task-9f2a1c7b4e0d-... entry
    suricata-analyst/                    #   at all -- the Memory capability was omitted for this run,
      default/                           #   not merely pointed at an empty scope
        MEMORY.md
  logs/
    runner-task-9f2a1c7b4e0d-...-<run-id>.jsonl
```

Rerunning `--pristine` yields a different UUID directory. Nothing is deleted; the old task is
not selected automatically, though it can be deliberately reopened with `--task <generated-id>`.
Reopening it *would* attach `Memory` (accumulate mode again), scoped to that same
`task-9f2a1c7b4e0d-...` id — but no `MEMORY.md` would exist yet, since the pristine run that
created the directory never had the capability attached to write one.

### 5. Custom base

```
$ uv run skill-runner --task ir-case-42 --workspace /cases/ws "..."
```
```
/cases/ws/
  data/
    eve.json
  ir-case-42/
    analyst_log-...md
    generated_code/...
  logs/
    runner-ir-case-42-<run-id>.jsonl
```

### 6. Error / reserved cases

```
$ uv run skill-runner --task foo --pristine "..."
error: --task and --pristine are mutually exclusive

$ uv run skill-runner --task logs "..."
error: 'logs' is a reserved task name

$ uv run skill-runner --task foo/../logs "..."
error: task names must match [a-z0-9][a-z0-9_-]{0,63}
```

The invariant across all of these: the agent can write only one `workspace/<task>/` subtree,
read canonical evidence only through `/data-source` (aliased at `/data`), and never reach
`workspace/logs/` or another task.

## Runner changes

Scoped to the `skill_runner` package, the shared sandbox notes, and skill instructions that describe input
discovery. No change to Monty's execution model is required.

- **New args:** `--task NAME` (default none), `--pristine` (flag). `--workspace` stays but
  becomes the workspace base/case root. No input-path flag is added: canonical evidence lives in
  `<workspace-base>/data/`. No `--results` arg — audit lives under the workspace base.
- **Task resolution:** compute `task_id` from the table above (error if `--task` and `--pristine`
  are both set; generate `task-<full-uuid4>` for pristine, creating its directory exclusively).
  User task names must match the task-id rule; reserve `default`, `data`, `logs`, and `memory`.
- **Path derivation:**
  - `ws_path = <workspace-base>/<task_id>`  (mounted + `FileSystem` root, as today)
  - `source_root = <workspace-base>/data-source` if it already exists as a real directory, else
    `<workspace-base>/data`  (mounted at `/data-source`, aliased at `/data`, both read-only)
  - `parquet_cache_root = <workspace-base>/data-sink/parquet`  (created once; never mounted)
  - `logs_dir = <workspace-base>/logs`  (flat; created once)
  - `memory_dir = <workspace-base>/memory`  (flat; created once; never mounted)
- **Validate paths before mounting:** resolve the base once, ensure the selected task root is a
  real direct child rather than a symlink, and use that validated path for both `FileSystem` and
  `MountDir`.
- **Move auditing** out of `ws_path` to an exclusive,
  `logs_dir/runner-<task_id>-<run-id>.jsonl` file. Append an event as it occurs: run start and
  configuration, user/continuation prompts, model responses, every tool call and result,
  `run_code` bodies and returns, errors, and completion. This is deliberately richer than the
  current console transcript, so failed or interrupted runs retain their audit evidence.
  `report_path` and `generated_dir` remain under `ws_path`.
- **Bound tool results** before model history: spill returns at or above 10,000 characters to
  `logs_dir/overflow/<task_id>/<run-id>/`, substitute a bounded preview and opaque handle, and
  record that handle and the original byte count in the audit event. The spill root is owner-only
  and task-scoped; storage failure falls back to truncation rather than admitting an unbounded
  result into context.
- **Startup message** reports the task id and mode so a run's identity is obvious in the log.
- **Telemetry:** keep a stable agent name (normally the skill name) and attach `task_id` and
  `run_id` as run metadata/span attributes. Agent name is a logging identity, not a workspace
  binding, and task-specific names create unhelpful high-cardinality telemetry.
- **Input discovery:** update shared sandbox notes and skills: input files are listed by the
  runner from `source_root` at startup and read in `run_code` as `/data-source/<filename>` (also
  reachable at `/data/<filename>`), or passed by bare filename to `query_events`/
  `aggregate_events`; all agent outputs continue to use `/workspace`.
- **Memory:** attach `pydantic_ai_harness.memory.Memory(store=FileStore(str(memory_dir)),
  namespace=skill_path.name, agent_name=task_id)` to `capabilities` in accumulate mode, scoping
  the notebook to `<skill>/<task_id>` — the same boundary that already governs script/analyst-log
  accumulation. `memory_dir` is created and `chmod 0o700`'d once at startup, like `logs_dir`,
  since notes can contain security-analysis findings; `FileStore` itself never chmods what it
  lazily creates, so this must happen before the capability is ever exercised. In pristine mode
  the capability is omitted entirely (not `inject_memory=False`), so `compare_models.sh`'s
  pristine runs keep an identical tool surface and prompt token count across models — see
  *Isolation guarantee* above.

### Behavior change to confirm

`--workspace` changes meaning from "the agent workspace directory" to "the workspace
**base/case root**." Its default layout is now `./workspace/data-source` (or `./workspace/data`,
legacy fallback), `./workspace/data-sink/parquet`, `./workspace/default`, `./workspace/memory`,
and `./workspace/logs`; the agent's writable root is `./workspace/default`. Anyone currently
passing `--workspace ./foo` will now get `./foo/data-source` (or `./foo/data`), `./foo/data-sink`,
`./foo/default`, `./foo/memory`, and `./foo/logs`. This is an intentional breaking layout change.
No automatic migration is planned; legacy contents are left untouched — an existing `data/`
directory keeps working as the fallback source root, and operators may migrate to `data-source/`
at their own pace since the runner never merges the two.

## Open items

- **Retention and permissions:** audit records can contain prompts, tool data, generated code,
  model responses, and reasoning; overflow files contain complete tool returns. Both are stored
  below the owner-only `logs/` tree and persist without an automatic TTL. Define size/age retention
  before enabling this on sensitive or long-lived cases. Pristine task directories also persist;
  cleanup, if wanted, must be a separate explicit action.
- **Memory retention:** `workspace/memory/<skill>/<task>/` persists indefinitely per accumulate
  task and is not covered by any retention/rotation policy; treat it with the same sensitivity as
  `analyst_log-*.md` — an operator can read it directly, but the agent cannot browse it via
  `FileSystem`, only through the `Memory` capability's own tools.
- **Concurrent reuse:** no task-level locking is proposed now. Every run still receives a unique
  `run_id` and exclusive audit filename so ordinary timestamp collisions cannot merge or
  overwrite audit records. Concurrent writes to the same accumulated task remain unsupported.

---

## Appendix: why not Monty snapshotting

The original draft of this file proposed serializing Monty's interpreter state
(`MontyRepl.dump()` / `FunctionSnapshot` / etc.) to carry work across runs. It was set aside:

- The goal is workspace **lifecycle**, which is a directory concern, not an interpreter-state
  concern. Task-scoped directories solve it completely.
- Files are the source of truth, so any snapshot would only ever be a cache — never the
  persistence mechanism.
- A stateful `MontyRepl` would *fight* the pristine goal: "pollution" would then include
  invisible heap state, not just files, making "start clean" harder to guarantee, and it would
  require abandoning the stateless-snippet execution model that the runner and every `SKILL.md`
  assume.

The only piece worth revisiting later is `Monty.dump()`/`Monty.load()` as a **parse cache**
(avoid re-parsing skill/reference code on cold start). That is a transparent optimization with no
execution-model or pollution impact. If added, its one caveat: a loaded cache blob is
deserialized state read back into the host process, so only cache blobs the runner itself wrote
should be loaded; any externally supplied blob must be treated as untrusted. (Note: the earlier
draft's API table conflated `Monty.dump/load` — a parse cache — with `MontyRepl.dump/load` — a
REPL-session cache; they are distinct mechanisms.)
