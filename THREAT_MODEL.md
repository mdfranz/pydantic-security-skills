# Threat Model

This document states the system's adversary model and tracks, per asset, which boundary defends
it, which document has the real enforcement mechanism, and which document has real adversarial
test coverage. It is an index, not an encyclopedia: it does not redraw [`ARCHITECTURE.md`'s trust-boundary diagram](ARCHITECTURE.md#trust-boundaries), repeat [`SQL_QUERY_PLAN.md`'s DuckDB verification detail](SQL_QUERY_PLAN.md#verified-duckdb-155-behavior-not-assumed), or re-derive [`refs/workspace-lifecycle.md`'s directory invariants](refs/workspace-lifecycle.md#isolation-guarantee). Follow each row's linked document for the
actual mechanism and, where one exists, its test plan.

---

## 1. Adversary Model

**In scope — treated as attacker-controlled, regardless of intent:**

- Case-evidence content (log files under `data-source`/`data`) — a classic prompt-injection
  surface, since the model reads this content directly.
- The user's prompt.
- Model-authored Python executed via `run_code` (the Monty sandbox).
- Model-authored SQL executed via `query_sql` (the DuckDB connection).

None of these actors need to be "malicious" for the model to be worth defending against — a model
following attacker-planted instructions in a log file is indistinguishable, from the system's
perspective, from a model that's simply wrong or overly compliant. Every boundary below assumes
worst-case content in all four surfaces simultaneously.

**Trusted — not defended against:**

- The host process and its operator.
- The skill directory and its instructions (`SKILL.md`/`skill.yaml`/`references/*.md`).
- The source root, cache root, and skill configuration passed to the runner.

(Same trust line already stated per-tool in [`SQL_BOUNDARY_TESTING.md` §1.1](SQL_BOUNDARY_TESTING.md#11-trust-assumptions); restated here once,
system-wide, so per-tool docs can reference it instead of repeating it.)

**Explicitly out of scope:**

- A compromised host process or host OS — if the host itself is untrusted, the sandbox/mount model
  this document tracks doesn't apply.
- A malicious or compromised skill author — skill instructions are trusted input (see above).
- A hostile second local process with write access to `cache_root`/`workspace` (symlink
  replacement, hard-link pre-seeding, TOCTOU). [`SQL_BOUNDARY_TESTING.md` §1.1](SQL_BOUNDARY_TESTING.md#11-trust-assumptions) already flags this
  as "should be tracked separately if the cache root ever becomes shared with an untrusted
  principal" — that judgment generalizes to the whole workspace tree, not just the SQL cache.
- Supply-chain compromise of a pinned dependency. [`PKG.md`](PKG.md) inventories and classifies dependencies
  but does not analyze this threat class; out of scope here too.
- Physical or host-OS-level access (disk theft, root compromise, etc.).

---

## 2. Assets

- Host secrets, environment variables, and network reachability.
- Other tasks' workspace directories, audit logs, and memory notebooks.
- Case evidence beyond what a skill's tools are designed to expose (e.g. reading it through an
  unintended path rather than the validated one).
- The Parquet cache's host filesystem path and any other file reachable from `cache_root`.
- Model/provider API keys and other runner-level secrets.
- Any host filesystem path outside the three Monty mounts (`/workspace`, `/data-source`+`/data`,
  `/skill`).

---

## 3. Asset × Boundary Traceability Matrix

| Asset | Enforcing boundary | Mechanism doc | Adversarial test doc | Residual risk |
| :--- | :--- | :--- | :--- | :--- |
| Host secrets / env vars / network | Monty sandbox — no ambient OS access; `os.environ = {}`; no sockets | [`ARCHITECTURE.md` § Monty sandbox](ARCHITECTURE.md#monty-sandbox) and [Trust boundaries](ARCHITECTURE.md#trust-boundaries) | **None.** | No dedicated sandbox-boundary adversarial test spec exists. [`SQL_BOUNDARY_TESTING.md` §10](SQL_BOUNDARY_TESTING.md#10-end-to-end-run_code-integration) explicitly defers this: "The full Monty sandbox suite belongs in a dedicated sandbox-boundary specification if expanded." This is the largest attack surface in the system with zero adversarial test coverage today. |
| Other tasks' workspace / audit log / memory notebook | Task-root validation, reserved names, no symlink/traversal task ids; `Memory`'s server-side `<skill>/<task_id>` scope prefixing | [`refs/workspace-lifecycle.md` § Isolation guarantee](refs/workspace-lifecycle.md#isolation-guarantee); [`ARCHITECTURE.md` § Workspace](ARCHITECTURE.md#workspace) and [Capabilities](ARCHITECTURE.md#capabilities) | **None.** | Same gap as above — no adversarial corpus exercises cross-task access attempts through `FileSystem`, `run_code`, or `Memory` tools. |
| Parquet cache file / any other host path reachable via SQL | DuckDB connection lockdown: `allowed_paths` (exact-match), `enable_external_access=false`, set before model SQL executes | [`SQL_QUERY_PLAN.md` §2](SQL_QUERY_PLAN.md#2-security-model-constraining-the-connection-not-the-query-text) | [`SQL_BOUNDARY_TESTING.md`](SQL_BOUNDARY_TESTING.md) | Best-covered boundary in the system. Still has 8 `Decision` rows (result bounds, path-disclosure policy, temp-disk quota) and several `Add` rows open — see [`SQL_BOUNDARY_TESTING.md` §13 Priority](SQL_BOUNDARY_TESTING.md#13-priority). |
| Case evidence file, via traversal or wrong-name access (rather than the validated `name` → path lookup) | `ensure_parquet_cache` name/containment validation | [`skill_runner/IMPL.md`](skill_runner/IMPL.md); [`DATA_SOURCE_SINK_PLAN.md` §4E](DATA_SOURCE_SINK_PLAN.md#e-name-validation--reserved-names) | **None.** | [`SQL_BOUNDARY_TESTING.md` §10](SQL_BOUNDARY_TESTING.md#10-end-to-end-run_code-integration) lists "Logical-name traversal" and "Cross-task isolation" as `Add` rows scoped to `query_sql` specifically — the same validation is shared by `query_events`/`aggregate_events`/`describe_events`, none of which have adversarial coverage of their own. |
| Oversized tool output routed to the overflow store | `OverflowingToolOutput`: 10,000-char threshold, owner-only `workspace/logs/overflow/<task>/`, bounded read-back handle | [`ARCHITECTURE.md` § Audit log and overflow store](ARCHITECTURE.md#audit-log-and-overflow-store-workspacelogs) | **None.** | No test exercises whether a crafted result can escape the preview/spill boundary or whether the opaque handle can be forged/guessed. |
| Audit log confidentiality/integrity | `chmod 0700`/`0600`, host-only tree, never mounted into the sandbox | [`ARCHITECTURE.md` § Audit log and overflow store](ARCHITECTURE.md#audit-log-and-overflow-store-workspacelogs) | **None.** | Permissions are re-applied every run per the doc, but nothing adversarially confirms the sandbox truly cannot reach `workspace/logs/` under any mount misconfiguration. |
| Provider/API keys, model network egress | Real env vars and network stay host-side only; never passed into the sandbox | [`ARCHITECTURE.md` § Monty sandbox](ARCHITECTURE.md#monty-sandbox) (`os.environ = {}`) | **None.** | Covered by the same sandbox-boundary gap as the first row. |
| Host-path visibility via `query_sql` metadata (visibility, not reachability) | Undecided | [`SQL_BOUNDARY_TESTING.md` §6](SQL_BOUNDARY_TESTING.md#6-metadata-and-error-disclosure) | [`SQL_BOUNDARY_TESTING.md` §6](SQL_BOUNDARY_TESTING.md#6-metadata-and-error-disclosure) | Explicit unresolved `Decision`: whether absolute cache/scratch/home paths may appear in `duckdb_settings()`/`duckdb_views()` output even though they can't be used to read anything. |

Every "None" above was checked, not assumed — it reflects that [`SQL_BOUNDARY_TESTING.md`](SQL_BOUNDARY_TESTING.md) is
currently the only adversarial test specification in this repository. Everything else has real
enforcement code and an architecture-level description, but no document that tries to break it.

---

## 4. Out of Scope / Non-Goals

Carried forward from existing docs, not re-derived:

- **No defense against a compromised host, skill author, or dependency** — see [§1's trust assumptions](#1-adversary-model).
- **No defense against a hostile co-tenant process** sharing `cache_root`/`workspace` — tracked
  as a future concern, not a current guarantee, per [`SQL_BOUNDARY_TESTING.md` §1.1](SQL_BOUNDARY_TESTING.md#11-trust-assumptions).
- **No cross-task or cross-skill data sharing is a goal** — [`refs/workspace-lifecycle.md`'s "Goals / non-goals"](refs/workspace-lifecycle.md#goals--non-goals)
  section is authoritative on workspace scoping intent; this document tracks
  whether that intent is *tested*, not whether it's *designed*.
- **No SQL-text blocklist is treated as a security control** — [`SQL_BOUNDARY_TESTING.md`](SQL_BOUNDARY_TESTING.md) is
  explicit that the DuckDB connection lockdown is the boundary, and statement-shape rejection is
  defense in depth only. This document defers entirely to that framing.

---

## 5. Open Items / Recommended Next Steps

Priority order, by residual risk in [§3](#3-asset--boundary-traceability-matrix):

**P0 — largest uncovered surface**

1. Write a Monty sandbox-boundary adversarial test specification (env/network/filesystem escape
   attempts from `run_code`, independent of any specific data tool). Nothing today exercises this
   even though it's the outermost boundary every other row depends on.
2. Write an adversarial test spec for task/workspace isolation (cross-task reads via `FileSystem`,
   `run_code` mounts, and `Memory` tool calls) against [`refs/workspace-lifecycle.md`'s stated invariants](refs/workspace-lifecycle.md#isolation-guarantee).

**P1 — extend existing coverage to siblings**

3. Extend `ensure_parquet_cache` traversal/cross-task adversarial coverage to
   `query_events`/`aggregate_events`/`describe_events`, not only `query_sql` ([`SQL_BOUNDARY_TESTING.md` §10](SQL_BOUNDARY_TESTING.md#10-end-to-end-run_code-integration)'s `Add` rows are currently scoped to one tool but the validation is shared).
4. Resolve [`SQL_BOUNDARY_TESTING.md` §6's path-disclosure `Decision`](SQL_BOUNDARY_TESTING.md#6-metadata-and-error-disclosure) — it's the one row in this matrix with test coverage but no adopted policy.

**P2 — lower-likelihood but currently silent**

5. Add adversarial coverage for the overflow store (spill/read-back handle) and audit-log
   isolation.

Related non-security reliability issues that touch these boundaries indirectly, tracked in
[`ISSUES.md`](ISSUES.md) and not restated here: #10 (`FileSystem` path-resolution errors — the most frequent
error in the project's history, though not itself a security bypass) and #13 (SIGTERM/exit-code
handling, relevant to whether a killed run leaves cleanup incomplete).

---

## 6. Maintenance

Update this document whenever a new trust boundary or capability is added — see
[`ARCHITECTURE.md`'s "Extension points" section](ARCHITECTURE.md#extension-points) for what counts as one (a new skill does not; a new
sandboxed tool, a new mount, or a new capability does). Each new boundary should get one row in [§3](#3-asset--boundary-traceability-matrix)
naming its mechanism doc and, once written, its test doc — even if that row starts as `None` in
the test-doc column, the way most rows do today.
