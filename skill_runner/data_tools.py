"""Host-side Polars/DuckDB data tools: NDJSON -> Parquet cache, bounded query/aggregate/SQL.

The model calls `query_events`/`aggregate_events`/`describe_events` with typed filters,
pagination, and grouping fields, or `query_sql` to author a read-only SQL `SELECT` executed
host-side by DuckDB against the same cache (see `SQL_QUERY_PLAN.md` §2 for the security model:
the DuckDB *connection* is locked down via `allowed_paths`/`enable_external_access`, not the
query text). The model never supplies a path -- only a *logical name* -- so all path resolution
and traversal defense live here, in `ensure_parquet_cache`.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb
import polars as pl
from pydantic_ai.exceptions import ModelRetry

# Bounds on model-controlled arguments so a tool result can never be unbounded.
MAX_PAGE_ROWS = 500
MAX_GROUP_ROWS = 500
MAX_COLUMNS = 50
MAX_GROUP_BY_FIELDS = 10
MAX_SCHEMA_COLUMNS = 500
MAX_EQUAL_FILTERS = 10
MAX_SQL_ROWS = 500
SQL_QUERY_TIMEOUT_SECONDS = 10
SQL_MEMORY_LIMIT = "512MB"

_DEFAULT_PAGE_LIMIT = 100
_DEFAULT_GROUP_LIMIT = 100

# Bare filename only: no path separators, no '.'/'..', no NULs. Field/column names use the
# same identifier shape so they can't be used to smuggle anything odd into a Polars expression.
_BARE_NAME_RE = re.compile(r"^[^/\\\x00]+$")
_RESERVED_NAMES = {".", ".."}
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Scalar types accepted as `equals` values (and left untouched by JSON normalization).
_EQUALS_VALUE_TYPES = (str, int, float, bool)
_JSON_SCALAR_TYPES = (type(None), bool, int, float, str)

def query_events(
    name: str,
    source_root: Path,
    cache_root: Path,
    event_type: str | None = None,
    equals: dict[str, str | int | float | bool | None] | None = None,
    columns: list[str] | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, object]:
    """Return a bounded page of events from `name`, converting to Parquet on first use.

    Fetches `limit + 1` rows so `has_more` is accurate without counting or materializing the
    entire result. This is a page for inspection, never a statistically complete sample --
    use `aggregate_events` for exact whole-dataset counts.
    """
    _validate_page_args(columns=columns, offset=offset, limit=limit)
    validated_equals = _validate_equals(equals)
    path = ensure_parquet_cache(name, source_root, cache_root)

    lf = pl.scan_parquet(path)
    if event_type is not None:
        lf = lf.filter(pl.col("event_type") == event_type)
    lf = _apply_equals(lf, validated_equals)
    if columns:
        lf = lf.select(columns)

    try:
        rows = lf.slice(offset, limit + 1).collect().to_dicts()
    except pl.exceptions.ColumnNotFoundError as exc:
        raise DataToolError(f"unknown column in query: {exc}") from exc

    return {
        "rows": rows[:limit],
        "offset": offset,
        "returned": min(len(rows), limit),
        "has_more": len(rows) > limit,
    }


def aggregate_events(
    name: str,
    source_root: Path,
    cache_root: Path,
    event_type: str | None = None,
    equals: dict[str, str | int | float | bool | None] | None = None,
    group_by: list[str] | None = None,
    limit: int = 100,
) -> dict[str, object]:
    """Compute an exact count (optionally grouped) over the complete filtered dataset.

    Runs over the full lazy frame regardless of `limit` -- `limit` only bounds how many
    returned groups are sent back, never the input rows that are counted.
    """
    if not (1 <= limit <= MAX_GROUP_ROWS):
        raise DataToolError(f"limit must be between 1 and {MAX_GROUP_ROWS}")
    _validate_field_list(group_by, label="group_by", max_len=MAX_GROUP_BY_FIELDS)
    validated_equals = _validate_equals(equals)

    path = ensure_parquet_cache(name, source_root, cache_root)
    lf = pl.scan_parquet(path)
    if event_type is not None:
        lf = lf.filter(pl.col("event_type") == event_type)
    lf = _apply_equals(lf, validated_equals)

    try:
        if not group_by:
            count = lf.select(pl.len()).collect().item()
            return {"count": int(count)}

        grouped = (
            lf.group_by(group_by)
            .agg(pl.len().alias("count"))
            .sort("count", descending=True)
            .limit(limit + 1)
            .collect()
        )
    except pl.exceptions.ColumnNotFoundError as exc:
        raise DataToolError(f"unknown column in group_by: {exc}") from exc

    rows = grouped.to_dicts()
    return {
        "groups": rows[:limit],
        "truncated": len(rows) > limit,
    }




class DataToolError(ModelRetry):
    """Raised for invalid data-tool arguments or an unusable source/cache file.

    Subclasses `ModelRetry` so the message is returned to the model as retry
    feedback instead of crashing the run -- these are all conditions the model
    can fix by calling the tool again with corrected arguments.
    """


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name or name in _RESERVED_NAMES:
        raise DataToolError(f"invalid name: {name!r}")
    if not _BARE_NAME_RE.match(name):
        raise DataToolError(f"name must be a bare filename with no path separators: {name!r}")


def _validate_directory(path: Path, label: str) -> Path:
    """Require `path` to be a real, non-symlink directory. Returns its resolved form."""
    if path.is_symlink():
        raise DataToolError(f"{label} must not be a symlink: {path}")
    if not path.is_dir():
        raise DataToolError(f"{label} is not a directory: {path}")
    return path.resolve()


def _resolve_source(name: str, source_root: Path) -> Path:
    """Join `name` to `source_root`, reject symlinks/non-regular files, and require the
    resolved parent to equal the resolved source root -- no traversal, no second-root lookup."""
    _validate_name(name)
    resolved_root = _validate_directory(source_root, "source_root")
    candidate = source_root / name
    if candidate.is_symlink():
        raise DataToolError(f"source file must not be a symlink: {name!r}")
    if not candidate.is_file():
        raise DataToolError(f"source file not found: {name!r}")
    resolved = candidate.resolve(strict=True)
    if resolved.parent != resolved_root:
        raise DataToolError(f"source file resolves outside source_root: {name!r}")
    return resolved


def _cache_identity(source_root: Path, name: str, stat_result: os.stat_result) -> str:
    """Opaque cache key derived from source-root identity, complete filename, device/inode,
    byte size, and nanosecond mtime -- distinguishes same-stem files and different source
    roots, and changes whenever the underlying file changes."""
    identity = "|".join(
        str(part)
        for part in (
            source_root,
            name,
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _is_valid_parquet_file(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        return path.is_file()
    except OSError:
        return False


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Per-key inter-process lock via `fcntl.flock`, opened without following symlinks."""
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _convert_to_parquet(
    source_path: Path,
    source_root: Path,
    name: str,
    expected_key: str,
    cache_root: Path,
    final_path: Path,
) -> bool:
    """Stream-convert `source_path` into `final_path` via a uniquely named temporary file.

    Returns True if the conversion was published, False if the source identity changed
    during conversion (caller should discard and retry from the new identity). Never leaves
    a final-looking partial cache: a failed or interrupted conversion leaves only an ignored
    temporary file.
    """
    tmp_path = cache_root / f".{final_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        pl.scan_ndjson(str(source_path)).sink_parquet(str(tmp_path), compression="zstd")

        new_stat = source_path.stat()
        new_key = _cache_identity(source_root, name, new_stat)
        if new_key != expected_key:
            return False

        fd = os.open(str(tmp_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_path, final_path)
        return True
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def ensure_parquet_cache(name: str, source_root: Path, cache_root: Path, *, max_attempts: int = 5) -> Path:
    """Return the validated Parquet cache path for `name`, converting on a cache miss.

    `name` is a logical name, never a host path -- resolution and traversal defense happen
    entirely inside this function. Retries when the source mutates during conversion (a new
    identity means a new cache key, so the loop re-derives it rather than publishing a mixed
    snapshot).
    """
    resolved_cache_root = _validate_directory(cache_root, "cache_root")

    last_error: DataToolError | None = None
    for _ in range(max_attempts):
        source_path = _resolve_source(name, source_root)
        stat_result = source_path.stat()
        key = _cache_identity(source_path.parent, name, stat_result)
        final_path = resolved_cache_root / f"{key}.parquet"
        lock_path = resolved_cache_root / f"{key}.lock"

        with _locked(lock_path):
            # Re-check after acquiring the lock: a competing runner may have completed
            # conversion while this process waited.
            if _is_valid_parquet_file(final_path):
                return final_path
            try:
                published = _convert_to_parquet(
                    source_path, source_path.parent, name, key, resolved_cache_root, final_path
                )
            except Exception as exc:  # noqa: BLE001 -- surfaced to the model as retry feedback
                last_error = DataToolError(f"failed to convert {name!r} to Parquet: {exc}")
                continue
            if published:
                return final_path

    raise last_error or DataToolError(f"source {name!r} kept changing during Parquet conversion")


def _validate_page_args(*, columns: list[str] | None, offset: int, limit: int) -> None:
    if offset < 0:
        raise DataToolError("offset must be >= 0")
    if not (1 <= limit <= MAX_PAGE_ROWS):
        raise DataToolError(f"limit must be between 1 and {MAX_PAGE_ROWS}")
    _validate_field_list(columns, label="columns", max_len=MAX_COLUMNS)


def _validate_field_list(fields: list[str] | None, *, label: str, max_len: int) -> None:
    if fields is None:
        return
    if not fields:
        raise DataToolError(f"{label} must not be empty when provided")
    if len(fields) > max_len:
        raise DataToolError(f"{label} must not exceed {max_len} entries")
    if len(set(fields)) != len(fields):
        raise DataToolError(f"{label} must not contain duplicates")
    for field in fields:
        if not isinstance(field, str) or not _FIELD_NAME_RE.match(field):
            raise DataToolError(f"invalid {label} entry: {field!r}")


def _validate_equals(
    equals: dict[str, str | int | float | bool | None] | None,
) -> dict[str, str | int | float | bool | None]:
    """Validate a top-level exact-match filter mapping shared by `query_events` and
    `aggregate_events`. `event_type` must go through the dedicated argument, not here, so
    there is no ambiguous double specification of the same filter."""
    if equals is None:
        return {}
    if not isinstance(equals, dict):
        raise DataToolError("equals must be a mapping")
    if len(equals) > MAX_EQUAL_FILTERS:
        raise DataToolError(f"equals must not exceed {MAX_EQUAL_FILTERS} entries")
    if "event_type" in equals:
        raise DataToolError("use the event_type argument instead of equals['event_type']")
    for key, value in equals.items():
        if not isinstance(key, str) or not _FIELD_NAME_RE.match(key):
            raise DataToolError(f"invalid equals key: {key!r}")
        if value is not None and not isinstance(value, _EQUALS_VALUE_TYPES):
            raise DataToolError(f"invalid equals value for {key!r}: {value!r}")
    return equals


def _apply_equals(
    lf: pl.LazyFrame, equals: dict[str, str | int | float | bool | None]
) -> pl.LazyFrame:
    for key, value in equals.items():
        if value is None:
            lf = lf.filter(pl.col(key).is_null())
        else:
            lf = lf.filter(pl.col(key) == value)
    return lf


def describe_events(
    name: str,
    source_root: Path,
    cache_root: Path,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, object]:
    """Return a bounded page of top-level Parquet column names and dtype descriptions.

    Uses `collect_schema()` and never scans data rows -- useful before authoring a `query_sql`
    query, since a sampled row may contain a null `dns`/`tls` struct and therefore conceal its
    nested fields.
    """
    if offset < 0:
        raise DataToolError("offset must be >= 0")
    if not (1 <= limit <= MAX_SCHEMA_COLUMNS):
        raise DataToolError(f"limit must be between 1 and {MAX_SCHEMA_COLUMNS}")

    path = ensure_parquet_cache(name, source_root, cache_root)
    schema = pl.scan_parquet(path).collect_schema()
    names = list(schema.names())
    page = names[offset : offset + limit]
    columns = [{"name": column_name, "dtype": str(schema[column_name])} for column_name in page]
    return {
        "columns": columns,
        "offset": offset,
        "returned": len(columns),
        "has_more": offset + len(columns) < len(names),
    }


def _sql_string_literal(path: Path) -> str:
    """Encode a host-controlled path as a DuckDB SQL string literal (doubling single quotes,
    DuckDB's standard escaping) -- DuckDB does not accept a prepared parameter in
    `CREATE VIEW ... read_parquet(?)`, so every host-controlled path used in SQL text must be
    encoded this way rather than interpolated raw."""
    return "'" + str(path).replace("'", "''") + "'"


def _normalize_sql_value(value: object) -> object:
    """Recursively convert a DuckDB result value into something JSON-serializable, walking
    into `STRUCT`/`LIST` values -- a nested field can carry a `datetime`/`Decimal`/etc just as
    easily as a top-level one, so normalizing only the top-level tuple is insufficient."""
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    if isinstance(value, dict):
        return {str(key): _normalize_sql_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_sql_value(item) for item in value]
    return str(value)


def query_sql(
    name: str,
    source_root: Path,
    cache_root: Path,
    sql: str,
    limit: int = 100,
) -> dict[str, object]:
    """Run one read-only SELECT (or WITH ... SELECT) against `name`'s cached Parquet file.

    Exposed as a lazy `events` view over an allowlisted path (never a materialized table --
    see SQL_QUERY_PLAN.md §2's design history for why that first draft OOM'd). The DuckDB
    *connection* is locked down (`allowed_paths` + `enable_external_access=false`) before the
    model's SQL text ever runs, so no filesystem or network access is reachable from that text
    except the one allowlisted cache file -- this is the actual security boundary, not the
    single-SELECT statement-shape check below.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise DataToolError("sql must be a non-empty SELECT statement")
    if not (1 <= limit <= MAX_SQL_ROWS):
        raise DataToolError(f"limit must be between 1 and {MAX_SQL_ROWS}")

    try:
        statements = duckdb.extract_statements(sql)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the model as retry feedback
        raise DataToolError(f"sql failed to parse: {exc}") from exc
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise DataToolError("sql must be exactly one SELECT (or WITH ... SELECT) statement")

    validated_cache_path = ensure_parquet_cache(name, source_root, cache_root)
    resolved_cache_root = _validate_directory(cache_root, "cache_root")

    scratch_dir = resolved_cache_root / f".sql-scratch-{uuid.uuid4().hex}"
    scratch_dir.mkdir(mode=0o700)

    con: duckdb.DuckDBPyConnection | None = None
    try:
        con = duckdb.connect(":memory:")

        # Host-controlled resource limits and the allowlist, ALL before lockdown --
        # temp_directory and allowed_paths can't be changed after enable_external_access=false.
        con.execute(f"SET temp_directory={_sql_string_literal(scratch_dir)}")
        con.execute(f"SET memory_limit='{SQL_MEMORY_LIMIT}'")
        con.execute(f"SET allowed_paths=[{_sql_string_literal(validated_cache_path)}]")

        # Lazy VIEW, not a materialized TABLE -- DuckDB's own Parquet pushdown prunes columns
        # and predicates the same way Polars' lazy scan does, with no eager read here.
        con.execute(
            f"CREATE VIEW events AS SELECT * FROM read_parquet({_sql_string_literal(validated_cache_path)})"
        )

        # Lock down. From this point, no filesystem or network access is reachable from ANY
        # SQL text executed on this connection, including the model's, except the one
        # allowlisted path set above.
        con.execute("SET enable_external_access=false")

        timer = threading.Timer(SQL_QUERY_TIMEOUT_SECONDS, con.interrupt)
        timer.start()
        try:
            # Host-controlled relational LIMIT via the relation API (not textual SQL wrapping
            # -- the parser accepts a terminal semicolon followed by a comment, which cannot
            # be made safe to embed with rstrip(";")), so `has_more` is accurate regardless of
            # whether the model's own query had a LIMIT.
            limited_result = con.sql(sql).limit(limit + 1)
            columns = list(limited_result.columns)
            if len(set(columns)) != len(columns):
                raise DataToolError(f"sql must not project duplicate column names: {columns}")
            fetched = limited_result.fetchall()
        except duckdb.InterruptException as exc:
            raise DataToolError(
                f"sql query exceeded the {SQL_QUERY_TIMEOUT_SECONDS}s timeout"
            ) from exc
        except duckdb.Error as exc:
            raise DataToolError(f"sql query failed: {exc}") from exc
        finally:
            timer.cancel()
    finally:
        if con is not None:
            con.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)

    rows = [
        {column: _normalize_sql_value(value) for column, value in zip(columns, row)}
        for row in fetched
    ]
    return {
        "columns": columns,
        "rows": rows[:limit],
        "returned": min(len(rows), limit),
        "has_more": len(rows) > limit,
    }


