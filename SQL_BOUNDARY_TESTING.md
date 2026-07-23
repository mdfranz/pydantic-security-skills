# Technical Specification: Adversarial SQL Boundary Testing

This document defines the security test plan for the host-side `query_sql` DuckDB interface in
`skill_runner`. It focuses on validating security invariants under model-authored SQL, rather than
collecting examples that merely happen to raise an error.

For architecture and implementation details, see [SQL_QUERY_PLAN.md](SQL_QUERY_PLAN.md). The
Monty sandbox is a separate security boundary; only the `run_code` integration cases needed to
exercise `query_sql` end to end are included here. For the system-wide adversary model and how
this tool's coverage compares to every other trust boundary, see
[THREAT_MODEL.md](THREAT_MODEL.md) — §1 below restates trust assumptions only as precisely as
`query_sql` itself needs them, not as the general case.

---

## 1. Scope and Threat Model

The model controls:

- `sql`: arbitrary text presented as one read-only query.
- `name`: a logical bare filename resolved by the host; never an intended host path.
- `limit`: a caller-selected result row limit within a host-defined maximum.

The model does not have direct access to the host filesystem, DuckDB connection, Parquet cache
path, scratch directory, host environment, or network. The SQL interface must remain secure even
when the query text is deliberately malicious.

The primary security control is the DuckDB connection:

1. Resolve `name` through the existing source/cache validation boundary.
2. Set `temp_directory`, `memory_limit`, and one exact `allowed_paths` entry.
3. register the validated Parquet cache as the lazy `events` view.
4. Set `enable_external_access=false` before model-authored SQL executes.
5. Execute only one parser-classified `SELECT`/`WITH ... SELECT`.
6. Interrupt DuckDB execution after a fixed timeout.
7. Return a host-bounded, JSON-safe result and clean up the per-call scratch directory.

The single-`SELECT` rule is defense in depth and a usability constraint. It must not be mistaken
for proof that the connection itself prevents external access.

### 1.1 Trust assumptions

The initial suite assumes:

- The host process, source root, cache root, and skill configuration are trusted.
- The attacker controls model-authored SQL and public tool arguments, but is not a second local
  process with write access to the cache root.
- The allowlisted Parquet file contains the same evidence intentionally exposed through `events`.
  Reading that dataset through another DuckDB projection is not a cross-dataset disclosure.
- Loopback servers, sentinel files, and temporary directories created by tests contain no real
  secrets.

Symlink replacement, hard-link pre-seeding, and time-of-check/time-of-use attacks by a hostile
local process should be tracked separately if the cache root ever becomes shared with an
untrusted principal.

The per-call scratch directory (`temp_directory`) is created as a sibling of the cached Parquet
files, inside `cache_root`, not in an independently rooted scratch area. This is implementation
detail rather than contract, but it raises the stakes on exact-allowlist and path-spelling
coverage in §4: a `..`-relative path resolved from the allowlisted `temp_directory` lands one
level up, directly in a directory that can contain other tasks' cached Parquet files.

### 1.2 Security properties

The suite validates four classes of property:

- **Confidentiality:** SQL cannot read other files, reach the network, inspect host secrets, or
  expose host-only paths if path confidentiality is part of the contract.
- **Integrity:** SQL cannot write files, attach persistent databases, install/load extensions, or
  weaken the connection lockdown.
- **Availability:** query execution, returned data, temporary resources, and cleanup remain
  bounded according to an explicit contract.
- **Correctness:** valid analytical SQL still works, uses the complete input for aggregation, and
  returns an unambiguous JSON-safe shape.

---

## 2. Test Status and Evidence Rules

Every test case should carry one of these statuses:

| Status | Meaning |
| :--- | :--- |
| **Covered** | A current test exercises the invariant with an adequate oracle. |
| **Strengthen** | A current test exercises the vector but can pass for the wrong reason or uses a weak oracle. |
| **Add** | The invariant is not represented in the current suite. |
| **Decision** | The intended security contract must be decided before a passing assertion can be defined. |
| **Characterize** | Version/platform-dependent engine behavior should be measured without silently treating unsupported functionality as a security pass. |

A security test must identify:

1. The invariant being protected.
2. The layer expected to enforce it: argument validation, statement parser, connection lockdown,
   result boundary, or Monty integration.
3. A positive control proving the adversarial capability exists in the tested DuckDB version when
   the lockdown is absent.
4. A sentinel or side-effect oracle showing that protected data or state was not reached.
5. The expected error category or exception cause.
6. Cleanup expectations after success and every failure class.

Do not treat a generic `DataToolError` as sufficient evidence. A missing function, binder error,
parser rejection, permission denial, and timeout are materially different outcomes.

---

## 3. Public SQL Grammar and Statement Gate

These tests validate the public `query_sql` contract. They do not by themselves validate the
connection lockdown.

| Status | Test | SQL/vector | Expected evidence |
| :--- | :--- | :--- | :--- |
| **Covered** | Basic query | `SELECT event_type FROM events` | Correct result shape and values. |
| **Covered** | CTE | `WITH t AS (...) SELECT ... FROM t` | Accepted as one `SELECT`. |
| **Covered** | Terminal comment | `SELECT ...; -- comment` | Accepted and host-bounded. |
| **Covered** | Multiple statements | `SELECT 1; SELECT 2` | Rejected by statement-count validation before cache/connection setup. |
| **Covered** | Non-`SELECT` | `DROP`, `ATTACH`, `EXPLAIN` | Rejected by statement-type validation. |
| **Covered** | Empty SQL | Empty and whitespace-only input | Rejected as invalid input. |
| **Add** | Comment/format variants | Leading comments, block comments, nested comments supported by DuckDB, mixed whitespace | Classification remains correct without a text blocklist. |
| **Add** | Semicolon literals | Semicolons inside string literals and comments | No false multi-statement rejection. |
| **Add** | Obfuscated multiple statements | Comments and unusual whitespace around statement separators | Still classified as multiple statements. |
| **Add** | Dynamic SQL wrapper | DuckDB `query(...)` or equivalent supported by the pinned version | An outer `SELECT` cannot use dynamic SQL to mutate state or escape the connection boundary. |
| **Decision** | SQL input bound | Oversized text, deeply nested expressions, very large `UNION` chain | Reject under an explicit byte/depth policy before unbounded parser work. |

For parser-rejected input, assert that no Parquet conversion, DuckDB connection, or scratch
directory is created. This guards the ordering of validation as well as the final error.

---

## 4. Filesystem Confidentiality and Integrity

Filesystem-read attacks must use outer-`SELECT` forms so they pass the public statement gate and
actually exercise the connection lockdown.

Create:

- A readable sentinel file outside the cache root containing a unique random token.
- A valid but non-allowlisted Parquet sibling whose filename shares the allowlisted filename as a
  prefix.
- Optional CSV, JSON, text, and blob fixtures outside the allowed path.
- A write target whose existence and content can be checked after every attempt.

| Status | Test | Adversarial vector | Expected evidence |
| :--- | :--- | :--- | :--- |
| **Strengthen** | Arbitrary absolute read | `read_parquet('/outside/sentinel.parquet')` | `DataToolError` caused by DuckDB permission enforcement; sentinel content absent. The current test (`test_traversal_and_exfiltration_attempts_are_inert`) only asserts `DataToolError`, not `cm.exception.__cause__`; add the `duckdb.PermissionException` cause check from §11.1 to close this. |
| **Add** | Reader-function coverage | `read_csv`, `read_json`, `read_text`, `read_blob` against fixture files | Each available reader works in an unlocked positive control and is permission-blocked through `query_sql`. |
| **Strengthen** | Exact allowlist | Prefix-sharing Parquet sibling | Allowlisted `events` succeeds; sibling fails specifically at the permission boundary. |
| **Add** | Directory/wildcard discovery | `glob`, wildcard readers, directory paths | No names or contents outside the exact allowlisted path are returned. |
| **Add** | Path spelling variants | Relative path, `..`, redundant separators, `file:` URI, percent/URI forms supported by DuckDB | No alternate spelling bypasses the exact allowlist. |
| **Add** | Scratch-relative traversal | `..`-relative path resolved from the allowlisted `temp_directory`, targeting a sibling cache file one level up in `cache_root` (see §1.1: scratch is nested inside `cache_root`, not an independent root) | Blocked by `allowed_paths` canonicalization. Treat as a named must-pass case rather than folding it into the generic path-spelling row above — a regression here is a direct cross-task disclosure, not just a boundary nuance. |
| **Add** | Non-allowlisted lazy view | Query a predeclared view over a non-allowlisted file in a low-level fixture | The lazy read fails after lockdown, proving the view itself grants no authority. |
| **Covered** | Host path quoting | Source/cache roots containing `'` | Valid `events` query succeeds without SQL injection or malformed setup. |
| **Strengthen** | Public write rejection | `COPY`, file-backed `ATTACH`, `EXPORT`, `INSTALL`, `LOAD` | Public API rejects non-`SELECT` input before execution; write sentinel remains unchanged. |
| **Add** | Connection write lockdown | Issue the same operations directly against the production-configured locked connection | DuckDB independently denies them, proving parser rejection is not the only control. |
| **Add** | Lockdown mutation | Attempt to change `enable_external_access`, `allowed_paths`, or `temp_directory` after lockdown | Settings cannot be weakened or redirected. |

### 4.1 Required test seam

Public `query_sql` cannot prove that DuckDB would block a `COPY` or `ATTACH`, because the statement
gate rejects those inputs first. Connection-level tests should use the same production connection
setup code as `query_sql`, not a duplicate configuration assembled in the test.

If necessary, extract a narrow private context manager that:

- Opens the in-memory connection.
- Applies resource settings and the allowlist.
- Creates `events`.
- Locks external access.
- Guarantees connection and scratch cleanup.

That seam is a testability recommendation, not a second public SQL API.

---

## 5. Network and Extension Boundary

Parser rejection of `LOAD httpfs` or `INSTALL spatial` does not demonstrate that a
`SELECT`-shaped network read is blocked.

Use an in-process loopback HTTP canary that records connection/request attempts. It must serve a
small valid fixture so an unlocked positive control can demonstrate that the selected DuckDB
reader would contact it.

| Status | Test | Adversarial vector | Expected evidence |
| :--- | :--- | :--- | :--- |
| **Add** | HTTP file read | HTTP URL passed to available CSV/JSON/Parquet reader | Locked query fails and canary receives zero requests. |
| **Add** | Loopback/private targets | `127.0.0.1`, `localhost`, and supported alternate loopback notation | No request reaches the canary. |
| **Add** | Cloud metadata SSRF | `169.254.169.254` (AWS/GCP/Azure instance metadata) via any HTTP-capable reader | No request reaches a canary bound to that role. Name this target explicitly rather than folding it into generic loopback coverage — it is the highest-impact SSRF target given plausible cloud deployment, and its distinct routing (link-local, not `127.0.0.1`/`localhost`) means loopback coverage does not automatically exercise it. |
| **Add** | Extension autoload | `sqlite_scan`, `postgres_scan`, HTTP, Excel, or other autoloading function available in the pinned version | No network request, extension download, extension-directory mutation, or external connection. |
| **Add** | Secret/credential surface | Catalog or functions related to DuckDB secrets and cloud credentials | No host credential or environment canary is returned. |
| **Add** | Host environment variable access | Any scalar function exposed by the pinned DuckDB version or an autoloaded extension that reads process environment variables (e.g. a `getenv`-equivalent) | No host environment value is returned. This is neither filesystem nor network I/O, so `enable_external_access=false` may not gate it — verify independently rather than assuming coverage from the secrets-catalog row above. |
| **Characterize** | Unsupported feature | Attack function is unavailable in the current build | Record as not applicable; do not count “function does not exist” as permission-boundary evidence. |

Tests must never contact the public internet. Network positive controls and denial oracles stay on
loopback.

---

## 6. Metadata and Error Disclosure

Connection lockdown may prevent using a path without preventing SQL from observing that path.
Because production setup embeds absolute paths in DuckDB settings and the `events` view
definition, catalog introspection requires explicit coverage.

Candidate probes include:

```sql
SELECT name, value
FROM duckdb_settings()
WHERE name IN ('allowed_paths', 'temp_directory', 'home_directory', 'extension_directory')
```

```sql
SELECT sql
FROM duckdb_views()
WHERE view_name = 'events'
```

Also enumerate database/catalog introspection functions exposed by the pinned DuckDB version and
identify fields that contain paths, connection strings, secrets, or environment-derived values.

| Status | Test | Expected evidence |
| :--- | :--- | :--- |
| **Decision** | Settings disclosure | Decide whether absolute cache, scratch, home, and extension paths are confidential. If yes, no unique host-path canary may appear in a successful result. |
| **Decision** | View-definition disclosure | If paths are confidential, the `events` view definition must not expose the embedded Parquet path. |
| **Add** | Database/catalog disclosure | No host-only path, credential, or environment canary appears in accessible catalog rows. |
| **Add** | Error hygiene | Permission, binder, timeout, spill, setup, and serialization errors contain no host-only canary that was not already supplied in model SQL. |
| **Add** | Cross-call use | A value learned through metadata cannot be used in a later call to gain additional filesystem authority. |

This decision must stay separate from access control:

- “The model can see a host path but cannot use it” may be an acceptable contract.
- “A host path never reaches the model” is stronger and requires a design that prevents catalog,
  view-definition, and error disclosure rather than merely filtering obvious result strings.

String filtering is not a complete fix because SQL can transform, split, hash, or encode a value
before it reaches result normalization.

---

## 7. Result Bounds and Serialization

The relational `.limit(limit + 1)` bounds rows only. It does not bound bytes, projected columns,
container size, or normalization cost.

`query_sql` is exempted from the host's generic tool-output overflow truncation
(`resilience.py`'s `DATA_TOOL_NAMES`), confirming these bounds are the only backstop, not a
defense-in-depth layer on top of a generic one.

| Status | Test | SQL/vector | Expected evidence |
| :--- | :--- | :--- | :--- |
| **Covered** | Host row cap | Query returns more than caller `limit` | Exactly `limit` rows and `has_more=True`. |
| **Covered** | Limit argument bounds | Zero, negative, and over-maximum values | Rejected with `DataToolError`. |
| **Covered** | Complete aggregation | Aggregate scans more input rows than `MAX_SQL_ROWS` | Exact aggregate; only returned result rows are capped. |
| **Covered** | Duplicate projection names | `SELECT 1 AS x, 2 AS x` | Rejected before dict construction. |
| **Covered** | Nested analytical result | `UNNEST` and struct access | Correct nested/result shape. |
| **Strengthen** | Scalar normalization | Timestamp and decimal, plus date, time, interval, UUID, and supported integer widths | Documented JSON-safe values at top level and when nested. |
| **Decision** | Result byte cap | `repeat(...)`, large blob, or large textual representation in one row | Deterministic bounded result or error before the value crosses into Monty. |
| **Decision** | Per-cell cap | One cell exceeds the configured size | Bounded behavior without constructing an unbounded Python string. |
| **Decision** | Column cap | One row projects thousands of uniquely named expressions | Rejected or bounded under an explicit maximum. |
| **Decision** | Nested element/depth cap | Huge list/map/struct or deeply nested result | Normalization remains bounded in size and recursion/work. |
| **Decision** | Strict JSON numbers | NaN and positive/negative infinity | Reject or normalize under a documented policy; validate with strict JSON (`allow_nan=False`). |
| **Add** | Binary values | Blob/bytes result | Documented representation and byte-bound enforcement. |
| **Add** | Normalization crash safety | Recursive JSON normalization currently runs after the connection is closed and scratch is removed, with no exception handling of its own | A pathological nested/oversized value raises `DataToolError`, not an unguarded `RecursionError`/`MemoryError` that crashes the run. Whatever bound is chosen for the byte/cell/column/nesting Decision rows above must be enforced from inside error handling, not defined only as a size policy. |

Result-size enforcement must occur inside the data tool. The outer tool-output overflow mechanism
cannot substitute for it: nested data tools intentionally preserve their dict contract, and an
oversized value may already have consumed host memory or crossed the host/sandbox bridge before an
outer preview could be applied.

---

## 8. Resource Exhaustion and Timeout

Timeout fixtures must require complete input consumption before producing the first result row.
Avoid queries that can be satisfied after the host-applied relational limit reads a few rows.

Suitable query shapes include a blocking aggregate over very large generated relations or a
large sort/group operation. Warm the Parquet cache before measuring SQL execution so cold-cache
conversion is not accidentally treated as query timeout behavior.

| Status | Test | Expected evidence |
| :--- | :--- |
| **Covered** | Blocking long-running query | DuckDB raises an interruption-derived `DataToolError`; scratch is removed. |
| **Strengthen** | Timeout timing | Completion occurs within timeout plus a documented scheduling/cleanup tolerance; avoid a brittle exact duration. |
| **Add** | Successful timer cancellation | A successful query is never interrupted later by a stale watchdog callback. |
| **Add** | Blocking sort/group/join | Expensive non-generated query shape is interrupted without killing the host process. |
| **Decision** | Parser/input exhaustion | Explicit SQL text/nesting bound protects work performed before the watchdog starts. |
| **Decision** | Normalization exhaustion | Result byte/depth bounds protect work performed after the watchdog is cancelled. |
| **Characterize** | Memory pressure | Under a lower test memory limit, DuckDB spills or returns a controlled error and the host process survives. |
| **Decision** | Temporary disk bytes | Decide whether time-bounded but byte-unbounded spill is acceptable or requires a quota/isolated filesystem. |
| **Add** | Spill confinement | Any temporary files are created only under the per-call scratch directory. |

Run genuinely destructive resource characterization in a subprocess with an independent outer
wall-clock kill and, where available, OS/cgroup memory and temporary-disk limits. A unit-test
watchdog must not be the sole protection for the test runner itself.

### 8.1 Timeout contract

The current watchdog covers DuckDB planning/execution/fetching after connection setup. It does not
cover:

- Initial `duckdb.extract_statements` parsing.
- Cold-cache NDJSON-to-Parquet conversion.
- Connection creation and pre-lockdown setup.
- Python result normalization after fetching.

Tests and user-facing documentation should call this a DuckDB query-execution timeout unless the
implementation grows an outer total-call deadline.

---

## 9. Lifecycle, Cleanup, and Concurrency

Cleanup is part of the boundary because scratch data may contain query intermediates derived from
case evidence.

| Status | Test | Expected evidence |
| :--- | :--- | :--- |
| **Covered** | Timeout cleanup | No `.sql-scratch-*` directory remains. |
| **Add** | Success cleanup | No scratch directory remains after a valid query. |
| **Add** | Permission-error cleanup | No scratch directory remains after blocked filesystem/network access. |
| **Add** | Binder/execution-error cleanup | No scratch directory remains after unknown columns/functions or invalid runtime operations. |
| **Add** | Result-shape-error cleanup | Duplicate columns and result-bound failures leave no scratch data. |
| **Add** | Scratch permissions | Directory is owner-only (`0700`) while it exists. |
| **Add** | Connection setup failure | Partial setup still closes the connection and removes scratch, **and** the failure surfaces as `DataToolError` rather than a bare `duckdb.Error`. The current setup statements (`SET temp_directory`/`memory_limit`/`allowed_paths`, `CREATE VIEW`, `SET enable_external_access=false`) run outside the `except duckdb.Error` block that wraps query execution, so a setup-phase failure today would crash the run instead of becoming a retry — cleanup alone does not prove recoverability. |
| **Add** | Concurrent isolation | Two calls use distinct connections and scratch paths; one timeout cannot interrupt the other. |
| **Add** | Repeated calls | A timed-out/failed call does not poison a later valid call. |
| **Characterize** | Watchdog race | `threading.Timer.cancel()` does not join the timer thread; a callback already mid-flight when `cancel()` runs can still call `con.interrupt()` concurrently with `con.close()` in the outer `finally`. Repeated near-timeout calls reveal no interrupt-on-closed-connection error or lingering timer thread. |

Concurrency tests should use bounded synchronization primitives and deterministic query shapes,
not arbitrary sleeps.

---

## 10. End-to-End `run_code` Integration

Unit tests against `query_sql` validate the host function. Integration tests must also prove that
the same behavior survives tool binding, Monty serialization, retry handling, audit capture, and
model-visible output.

| Status | Test | Expected evidence |
| :--- | :--- | :--- |
| **Covered** | Reachability/cache reuse | Synchronous `query_sql` works from `run_code` and shares the typed tools' cache. |
| **Covered** | Recoverable attack | A blocked external read becomes model retry feedback rather than crashing the run. |
| **Add** | Path confidentiality | If host paths are confidential, no result, retry message, audit-visible model content, or overflow preview exposes a host-path canary. |
| **Add** | Oversized single value | Result boundary acts before an oversized value crosses into Monty; the documented dict/error contract remains intact. |
| **Add** | Serialization parity | Nested and special values seen inside `run_code` match direct-unit-test normalization. |
| **Add** | Logical-name traversal | Invalid `name` values are rejected by host validation and remain recoverable retries. |
| **Add** | Cross-task isolation | One task cannot select another task's source/cache by name, guessed path, metadata, or SQL reader. |

The full Monty sandbox suite belongs in a dedicated sandbox-boundary specification if expanded.
Direct sandbox filesystem tests should use supported APIs such as
`pathlib.Path('/etc/passwd').read_text()`, not the unavailable `open()` builtin. A mounted symlink
escape test must exercise Monty's mount resolver; `_resolve_source` symlink tests separately
exercise the host data-tool filename boundary.

---

## 11. Recommended Test Organization

Keep the security tests close to the implementation while separating mechanisms:

```text
QuerySqlGrammarTests
QuerySqlFilesystemBoundaryTests
QuerySqlNetworkBoundaryTests
QuerySqlMetadataDisclosureTests
QuerySqlResultBoundaryTests
QuerySqlResourceBoundaryTests
QuerySqlCleanupAndConcurrencyTests
QuerySqlRunCodeIntegrationTests
```

Shared fixtures should provide:

- A small valid event source and warmed Parquet cache.
- A unique per-test content sentinel.
- Host/cache/scratch paths containing unique path canaries.
- Non-allowlisted CSV, JSON, text, blob, and Parquet fixtures.
- A prefix-sharing sibling path.
- A write target checked for non-creation/non-modification.
- A loopback request canary.
- Helpers that assert scratch cleanup and scan results/errors for host-only canaries.

### 11.1 Mechanism-aware assertion pattern

Parser rejection and connection rejection should be asserted differently:

```python
# Parser/statement gate: rejected before DuckDB execution.
with self.assertRaises(DataToolError) as cm:
    query_sql(..., sql="COPY events TO '/tmp/x'")
self.assertIsNone(cm.exception.__cause__)

# Connection boundary: a single SELECT reaches DuckDB and is permission-blocked.
with self.assertRaises(DataToolError) as cm:
    query_sql(..., sql="SELECT * FROM read_parquet('/outside/sentinel.parquet')")
self.assertIsInstance(cm.exception.__cause__, duckdb.PermissionException)
```

Prefer exception types/causes and sentinel state over exact error-message text. DuckDB wording may
change between releases without changing the security property.

### 11.2 Positive controls

For each filesystem/network function:

1. Use a test-only unlocked DuckDB connection to prove the function can read the harmless fixture
   or reach the loopback canary in the tested version.
2. Run the equivalent single-`SELECT` through the production-configured boundary.
3. Assert permission denial and no protected side effect.

If the positive control is unsupported, mark the vector not applicable for that version. Do not
report it as a passing lockdown test.

---

## 12. DuckDB Version Regression

DuckDB's statement classification, external-access controls, extension autoload behavior,
catalog functions, and path handling are dependency behavior. The security corpus must run after
every DuckDB upgrade.

At minimum, an upgrade review should:

- Record the exact DuckDB version under test.
- Enumerate new filesystem, network, secret, extension, and dynamic-query functions.
- Re-run positive controls and locked negative controls.
- Re-run metadata/path-disclosure probes.
- Confirm `allowed_paths` exact-match behavior.
- Confirm post-lockdown settings cannot be weakened.
- Confirm `con.interrupt()` still interrupts the selected blocking queries.
- Review changed exception types without weakening the sentinel-based oracles.

An unsupported attack function disappearing between versions is not evidence that the overall
boundary improved; newly added equivalent functions must also be considered.

---

## 13. Priority

### P0: define before calling the interface fully bounded

1. Decide whether absolute host-path disclosure is permitted.
2. Define and enforce result byte, cell, column, and nesting bounds.
3. Replace short-circuitable DoS vectors with blocking queries.
4. Separate public statement-gate assertions from connection-lockdown assertions.
5. Wrap connection-setup failures and result normalization in the same `DataToolError` contract
   as query execution, so the bounds decided in (2) actually prevent a crash rather than only
   defining a size policy (§7, §9).

### P1: core adversarial regression suite

1. Add the filesystem reader corpus with positive controls, including the scratch-relative
   traversal case (§4).
2. Add loopback network, cloud-metadata SSRF, extension-autoload, and host-environment-variable
   canaries (§5).
3. Add metadata/error-disclosure probes.
4. Cover cleanup across every failure class.
5. Add dynamic-SQL and lockdown-setting mutation cases.

### P2: resilience and upgrade confidence

1. Add concurrent query and watchdog-race coverage.
2. Add subprocess-isolated memory/disk characterization.
3. Add cross-task and full `run_code` confidentiality coverage.
4. Re-run the corpus on every DuckDB upgrade and supported platform.

