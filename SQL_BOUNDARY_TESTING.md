# Technical Specification: Adversarial SQL & Sandbox Boundary Testing

This document details the test matrix, security boundary verification, and implementation patterns for testing the `query_sql` DuckDB interface and the Monty Python sandbox execution boundary in `skill_runner`.

For architecture and implementation details on `query_sql`, see [SQL_QUERY_PLAN.md](SQL_QUERY_PLAN.md).

---

## 1. Overview & Threat Model

The `query_sql` tool enables host-side execution of model-authored SQL queries via DuckDB against Parquet-cached events. Because the query text is model-authored, the security model relies on **constraining the DuckDB connection**, not attempting to sanitize arbitrary SQL text:

1. **Connection Lockdown:** `allowed_paths=[<validated_cache_path>]`, `temp_directory=<scratch_dir>`, `memory_limit='512MB'`, followed immediately by `enable_external_access=false`.
2. **Lazy `VIEW` Access:** The cached Parquet file is registered as a lazy `VIEW` over the single allowlisted path (`events`).
3. **Statement Type Enforcement:** Defense-in-depth single-`SELECT` validation via `duckdb.extract_statements`.
4. **Timeout Enforcement:** Wall-clock limit via a `threading.Timer` thread calling `con.interrupt()`.

The adversarial test suite validates that these host-side boundaries hold under malformed, malicious, or resource-heavy input.

---

## 2. DuckDB SQL Interface (`query_sql`) Test Matrix

### A. Access Control & Exfiltration Vectors

Verify that DuckDB strictly blocks any filesystem or network I/O outside the single allowlisted Parquet cache file.

| Test Case | Adversarial SQL Vector | Expected Result | Verified Mechanism |
| :--- | :--- | :--- | :--- |
| **Arbitrary File Read** | `SELECT * FROM read_csv('/etc/passwd')` | `DataToolError` | `PermissionException` from `enable_external_access=false` |
| **Sibling Path Access** | `SELECT * FROM read_parquet('/cache/<hash>-evil.parquet')` | `DataToolError` | `allowed_paths` exact-match check (prefix-matching disabled) |
| **Data Export / Copy** | `COPY events TO '/tmp/exfil.csv'` | `DataToolError` | Statement type check & `PermissionException` |
| **Database Attachment** | `ATTACH '/tmp/evil.db' AS evil` | `DataToolError` | Statement type check & `PermissionException` |
| **Extension Loading** | `LOAD httpfs;` / `INSTALL spatial;` | `DataToolError` | Network & filesystem access disabled |

### B. Statement Control & Parser Bypasses

Verify that defense-in-depth single-`SELECT` statement validation (`duckdb.extract_statements`) cannot be bypassed by SQL formatting or administrative commands.

| Test Case | Adversarial SQL Vector | Expected Result | Verified Mechanism |
| :--- | :--- | :--- | :--- |
| **Multi-Statement Execution** | `SELECT 1; DROP VIEW events;` | `DataToolError` | `extract_statements` rejects `len(statements) != 1` |
| **Trailing Semicolon + Comment** | `SELECT 1 FROM events; -- comment` | **Allowed** | DuckDB relation `.limit()` handles trailing comments safely |
| **Lockdown Manipulation** | `SET enable_external_access=true; SELECT 1;` | `DataToolError` | DuckDB prevents re-enabling external access once false |
| **DDL / Administrative** | `PRAGMA version` / `CREATE TABLE x AS SELECT 1` | `DataToolError` | `StatementType` is not `SELECT` |
| **CTE Chaining** | `WITH cte AS (...) SELECT * FROM cte` | **Allowed** | CTEs parse correctly as `StatementType.SELECT` |

### C. Resource Exhaustion & Denial of Service (DoS)

Verify that expensive or infinite queries spill safely to disk without exhausting host memory, and are interrupted within the wall-clock timeout.

| Test Case | Adversarial SQL Vector | Expected Result | Verified Mechanism |
| :--- | :--- | :--- | :--- |
| **Memory Cartesian Bomb** | `SELECT e1.*, e2.* FROM events e1 CROSS JOIN events e2` | `DataToolError` | Spills to `scratch_dir` under `memory_limit`, then interrupted by watchdog timer |
| **Infinite CTE Recursion** | `WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT * FROM c` | `DataToolError` | Interrupted after `SQL_QUERY_TIMEOUT_SECONDS` (10s) via `con.interrupt()` |
| **Unbounded Pagination** | `SELECT * FROM events LIMIT 10000000` | **Bounded** | Host-side `.limit(limit + 1)` caps output rows and sets `has_more=True` |

### D. Schema & Serialization Edge Cases

Verify that non-standard query output shapes do not corrupt JSON outputs or silently drop data.

| Test Case | Adversarial SQL Vector | Expected Result | Verified Mechanism |
| :--- | :--- | :--- | :--- |
| **Duplicate Aliases** | `SELECT 1 AS x, 2 AS x` | `DataToolError` | Rejects duplicate column projection before dict zipping |
| **Nested Struct Unnesting** | `SELECT unnest(dns.queries) FROM events` | Normalized Dict | Recursively normalizes `Decimal`/`Timestamp`/`UUID` to strings |

---

## 3. Python Sandbox (`run_code`) Test Matrix

Verify that the Python execution environment (Monty sandbox) maintains host filesystem isolation and module restriction.

| Test Case | Adversarial Python Vector | Expected Result | Verified Mechanism |
| :--- | :--- | :--- | :--- |
| **Directory Traversal** | `open('../../../etc/passwd').read()` | Sandboxed Error | Path resolution restricted to `/data` and `/workspace` |
| **Symlink Escape** | `os.readlink('/data/symlink_to_root')` | Sandboxed Error | Host `_resolve_source` & OSAccess symlink checks |
| **Subprocess Execution** | `import subprocess; subprocess.run(['id'])` | Import Error | Disallowed standard module import |
| **Network Socket Creation** | `import socket; socket.socket()` | Import Error | Unmounted network interfaces |
| **Dunder Introspection** | `().__class__.__base__.__subclasses__()` | Restricted Access | Python class hierarchy traversal restricted inside sandbox |

---

## 4. Test Implementation Patterns

Below are canonical `unittest` implementations for validating these security bounds in `tests/test_data_tools.py`:

```python
import unittest
from skill_runner.data_tools import DataToolError, query_sql

class AdversarialSQLBoundaryTests(unittest.TestCase):

    def test_query_sql_traversal_attempt_blocked(self):
        """Assert arbitrary file read attempts fail at the DuckDB connection layer."""
        with self.assertRaises(DataToolError) as cm:
            query_sql(
                name="eve",
                source_root=self.source_dir,
                cache_root=self.cache_dir,
                sql="SELECT * FROM read_csv('/etc/passwd')",
            )
        self.assertIn("PermissionException", str(cm.exception))

    def test_query_sql_timeout_interruption_and_cleanup(self):
        """Assert infinite CTE loops are interrupted and scratch space is cleaned up."""
        with self.assertRaises(DataToolError) as cm:
            query_sql(
                name="eve",
                source_root=self.source_dir,
                cache_root=self.cache_dir,
                sql="WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT * FROM c",
            )
        self.assertIn("timeout", str(cm.exception).lower())
        
        # Verify scratch directories are unlinked
        scratch_dirs = list(self.cache_dir.glob(".sql-scratch-*"))
        self.assertEqual(len(scratch_dirs), 0)

    def test_query_sql_duplicate_alias_rejected(self):
        """Assert duplicate column names raise DataToolError to prevent dict key collision."""
        with self.assertRaises(DataToolError) as cm:
            query_sql(
                name="eve",
                source_root=self.source_dir,
                cache_root=self.cache_dir,
                sql="SELECT 1 AS col, 2 AS col",
            )
        self.assertIn("duplicate column names", str(cm.exception))
```
