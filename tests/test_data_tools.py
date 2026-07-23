import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from skill_runner import data_tools as dt


def _write_ndjson(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


class TempCaseMixin:
    def make_dirs(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        base = Path(tmpdir.name)
        source_root = base / "data-source"
        source_root.mkdir()
        cache_root = base / "data-sink" / "parquet"
        cache_root.mkdir(parents=True)
        return base, source_root, cache_root


class NameValidationTests(TempCaseMixin, unittest.TestCase):
    def test_rejects_traversal_and_separators(self):
        _base, source_root, cache_root = self.make_dirs()
        for bad_name in ["../evil.json", "a/b.json", "a\\b.json", "/etc/passwd", ".", "..", ""]:
            with self.assertRaises(dt.DataToolError, msg=bad_name):
                dt.ensure_parquet_cache(bad_name, source_root, cache_root)

    def test_rejects_missing_file(self):
        _base, source_root, cache_root = self.make_dirs()
        with self.assertRaises(dt.DataToolError):
            dt.ensure_parquet_cache("missing.json", source_root, cache_root)

    def test_rejects_symlinked_source_file(self):
        base, source_root, cache_root = self.make_dirs()
        real_file = base / "outside.json"
        _write_ndjson(real_file, [{"event_type": "alert"}])
        (source_root / "link.json").symlink_to(real_file)
        with self.assertRaises(dt.DataToolError):
            dt.ensure_parquet_cache("link.json", source_root, cache_root)

    def test_rejects_symlinked_source_root(self):
        base, source_root, cache_root = self.make_dirs()
        real_dir = base / "real-source"
        real_dir.mkdir()
        _write_ndjson(real_dir / "a.json", [{"event_type": "alert"}])
        linked_root = base / "linked-source"
        linked_root.symlink_to(real_dir)
        with self.assertRaises(dt.DataToolError):
            dt.ensure_parquet_cache("a.json", linked_root, cache_root)

    def test_rejects_symlinked_cache_root(self):
        base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}])
        real_cache = base / "real-cache"
        real_cache.mkdir()
        linked_cache = base / "linked-cache"
        linked_cache.symlink_to(real_cache)
        with self.assertRaises(dt.DataToolError):
            dt.ensure_parquet_cache("a.json", source_root, linked_cache)

    def test_source_must_be_direct_child_of_source_root(self):
        base, source_root, cache_root = self.make_dirs()
        nested = source_root / "nested"
        nested.mkdir()
        _write_ndjson(nested / "a.json", [{"event_type": "alert"}])
        # "nested/a.json" is rejected by the bare-name check before it ever gets here, but
        # exercise containment defense too: a name resolving outside source_root must fail.
        with self.assertRaises(dt.DataToolError):
            dt.ensure_parquet_cache("nested/a.json", source_root, cache_root)


class CacheIdentityTests(TempCaseMixin, unittest.TestCase):
    def test_unchanged_source_reuses_completed_cache(self):
        _base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}] * 5)

        first = dt.ensure_parquet_cache("a.json", source_root, cache_root)
        mtime_after_first = first.stat().st_mtime_ns

        with patch.object(dt.pl, "scan_ndjson", wraps=dt.pl.scan_ndjson) as spy:
            second = dt.ensure_parquet_cache("a.json", source_root, cache_root)
            spy.assert_not_called()

        self.assertEqual(first, second)
        self.assertEqual(mtime_after_first, second.stat().st_mtime_ns)

    def test_changed_source_derives_new_cache_path(self):
        _base, source_root, cache_root = self.make_dirs()
        source = source_root / "a.json"
        _write_ndjson(source, [{"event_type": "alert"}])

        first = dt.ensure_parquet_cache("a.json", source_root, cache_root)
        _write_ndjson(source, [{"event_type": "alert"}, {"event_type": "flow"}])
        second = dt.ensure_parquet_cache("a.json", source_root, cache_root)

        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_same_name_different_source_root_does_not_collide(self):
        base, _source_root, cache_root = self.make_dirs()
        root_a = base / "root-a"
        root_a.mkdir()
        root_b = base / "root-b"
        root_b.mkdir()
        _write_ndjson(root_a / "a.json", [{"event_type": "alert"}])
        _write_ndjson(root_b / "a.json", [{"event_type": "alert"}])

        path_a = dt.ensure_parquet_cache("a.json", root_a, cache_root)
        path_b = dt.ensure_parquet_cache("a.json", root_b, cache_root)
        self.assertNotEqual(path_a, path_b)

    def test_different_name_same_content_does_not_collide(self):
        _base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}])
        _write_ndjson(source_root / "b.json", [{"event_type": "alert"}])

        path_a = dt.ensure_parquet_cache("a.json", source_root, cache_root)
        path_b = dt.ensure_parquet_cache("b.json", source_root, cache_root)
        self.assertNotEqual(path_a, path_b)


class AtomicConversionTests(TempCaseMixin, unittest.TestCase):
    def test_failed_conversion_leaves_no_final_or_temp_file(self):
        _base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}])

        with patch.object(dt.pl, "scan_ndjson", side_effect=RuntimeError("boom")):
            with self.assertRaises(dt.DataToolError):
                dt.ensure_parquet_cache("a.json", source_root, cache_root)

        self.assertEqual(list(cache_root.glob("*.parquet")), [])
        self.assertEqual(list(cache_root.glob(".*")), [])

    def test_convert_reports_source_mutation_and_cleans_up(self):
        _base, source_root, cache_root = self.make_dirs()
        source = source_root / "a.json"
        _write_ndjson(source, [{"event_type": "alert"}])
        final_path = cache_root / "bogus.parquet"

        published = dt._convert_to_parquet(source, source_root, "a.json", "stale-key", cache_root, final_path)

        self.assertFalse(published)
        self.assertFalse(final_path.exists())
        self.assertEqual(list(cache_root.glob(".*")), [])

    def test_preexisting_symlink_at_cache_path_is_not_followed(self):
        base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}])
        sensitive = base / "sensitive.txt"
        sensitive.write_text("do-not-touch")

        # Compute the real cache key/path the same way ensure_parquet_cache will, then plant
        # a symlink there before calling it -- simulates an attacker pre-staging a symlink.
        source_path = (source_root / "a.json").resolve()
        stat_result = source_path.stat()
        key = dt._cache_identity(source_root.resolve(), "a.json", stat_result)
        final_path = cache_root / f"{key}.parquet"
        final_path.symlink_to(sensitive)

        result = dt.ensure_parquet_cache("a.json", source_root, cache_root)

        self.assertEqual(result, final_path)
        self.assertFalse(final_path.is_symlink())
        self.assertEqual(sensitive.read_text(), "do-not-touch")

    def test_concurrent_callers_produce_one_valid_cache(self):
        _base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}] * 20)

        results: list[Path] = []
        errors: list[Exception] = []

        def worker():
            try:
                results.append(dt.ensure_parquet_cache("a.json", source_root, cache_root))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(list(cache_root.glob("*.parquet")), [results[0]])
        self.assertEqual(list(cache_root.glob(".*")), [])


class QueryEventsTests(TempCaseMixin, unittest.TestCase):
    def setUp(self):
        _base, self.source_root, self.cache_root = self.make_dirs()
        self.rows = [
            {"event_type": "alert", "src_ip": "1.1.1.1", "dest_ip": "2.2.2.2"},
            {"event_type": "alert", "src_ip": "1.1.1.1", "dest_ip": "3.3.3.3"},
            {"event_type": "flow", "src_ip": "9.9.9.9", "dest_ip": "8.8.8.8"},
        ]
        _write_ndjson(self.source_root / "eve.json", self.rows)

    def test_filters_by_event_type_and_paginates(self):
        page = dt.query_events(
            "eve.json", self.source_root, self.cache_root, event_type="alert", limit=1
        )
        self.assertEqual(page["returned"], 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["rows"][0]["event_type"], "alert")

        page2 = dt.query_events(
            "eve.json", self.source_root, self.cache_root, event_type="alert", offset=1, limit=1
        )
        self.assertEqual(page2["returned"], 1)
        self.assertFalse(page2["has_more"])

    def test_column_selection(self):
        page = dt.query_events("eve.json", self.source_root, self.cache_root, columns=["event_type"])
        self.assertTrue(all(set(row) == {"event_type"} for row in page["rows"]))

    def test_rejects_invalid_offset_and_limit(self):
        with self.assertRaises(dt.DataToolError):
            dt.query_events("eve.json", self.source_root, self.cache_root, offset=-1)
        with self.assertRaises(dt.DataToolError):
            dt.query_events("eve.json", self.source_root, self.cache_root, limit=0)
        with self.assertRaises(dt.DataToolError):
            dt.query_events("eve.json", self.source_root, self.cache_root, limit=dt.MAX_PAGE_ROWS + 1)

    def test_rejects_excessive_or_duplicate_columns(self):
        with self.assertRaises(dt.DataToolError):
            dt.query_events("eve.json", self.source_root, self.cache_root, columns=["a"] * (dt.MAX_COLUMNS + 1))
        with self.assertRaises(dt.DataToolError):
            dt.query_events(
                "eve.json", self.source_root, self.cache_root, columns=["event_type", "event_type"]
            )
        with self.assertRaises(dt.DataToolError):
            dt.query_events("eve.json", self.source_root, self.cache_root, columns=[])

    def test_equals_filters_combine_with_and(self):
        page = dt.query_events(
            "eve.json",
            self.source_root,
            self.cache_root,
            event_type="alert",
            equals={"dest_ip": "3.3.3.3"},
        )
        self.assertEqual(page["returned"], 1)
        self.assertEqual(page["rows"][0]["dest_ip"], "3.3.3.3")

    def test_equals_none_means_is_null(self):
        _write_ndjson(
            self.source_root / "nulls.json",
            [{"event_type": "alert", "note": None}, {"event_type": "alert", "note": "x"}],
        )
        page = dt.query_events(
            "nulls.json", self.source_root, self.cache_root, equals={"note": None}
        )
        self.assertEqual(page["returned"], 1)
        self.assertIsNone(page["rows"][0]["note"])

    def test_equals_rejects_event_type_key(self):
        with self.assertRaises(dt.DataToolError):
            dt.query_events(
                "eve.json", self.source_root, self.cache_root, equals={"event_type": "alert"}
            )

    def test_equals_rejects_unknown_field_and_bad_value_and_too_many(self):
        with self.assertRaises(dt.DataToolError):
            dt.query_events("eve.json", self.source_root, self.cache_root, equals={"not a field!": "x"})
        with self.assertRaises(dt.DataToolError):
            dt.query_events("eve.json", self.source_root, self.cache_root, equals={"src_ip": [1, 2]})
        with self.assertRaises(dt.DataToolError):
            dt.query_events(
                "eve.json",
                self.source_root,
                self.cache_root,
                equals={f"f{i}": "x" for i in range(dt.MAX_EQUAL_FILTERS + 1)},
            )


class AggregateEventsTests(TempCaseMixin, unittest.TestCase):
    def test_ungrouped_count_matches_total(self):
        _base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}] * 3)
        result = dt.aggregate_events("a.json", source_root, cache_root)
        self.assertEqual(result["count"], 3)

    def test_grouped_counts_are_sorted_capped_and_marked_truncated(self):
        _base, source_root, cache_root = self.make_dirs()
        rows = (
            [{"event_type": "alert"}] * 3
            + [{"event_type": "flow"}] * 2
            + [{"event_type": "dns"}] * 1
        )
        _write_ndjson(source_root / "a.json", rows)

        result = dt.aggregate_events("a.json", source_root, cache_root, group_by=["event_type"], limit=2)
        self.assertEqual(len(result["groups"]), 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["groups"][0], {"event_type": "alert", "count": 3})
        self.assertEqual(result["groups"][1], {"event_type": "flow", "count": 2})

    def test_aggregate_covers_full_dataset_beyond_page_row_cap(self):
        _base, source_root, cache_root = self.make_dirs()
        row_count = dt.MAX_PAGE_ROWS + 50
        rows = [{"event_type": "alert"}] * row_count
        _write_ndjson(source_root / "a.json", rows)

        result = dt.aggregate_events("a.json", source_root, cache_root)
        self.assertEqual(result["count"], row_count)

        page = dt.query_events("a.json", source_root, cache_root, limit=dt.MAX_PAGE_ROWS)
        self.assertLess(page["returned"], row_count)

    def test_rejects_invalid_limit_and_group_by(self):
        _base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}])
        with self.assertRaises(dt.DataToolError):
            dt.aggregate_events("a.json", source_root, cache_root, limit=0)
        with self.assertRaises(dt.DataToolError):
            dt.aggregate_events("a.json", source_root, cache_root, limit=dt.MAX_GROUP_ROWS + 1)
        with self.assertRaises(dt.DataToolError):
            dt.aggregate_events(
                "a.json", source_root, cache_root, group_by=["x"] * (dt.MAX_GROUP_BY_FIELDS + 1)
            )
        with self.assertRaises(dt.DataToolError):
            dt.aggregate_events("a.json", source_root, cache_root, group_by=["event_type", "event_type"])

    def test_equals_filters_are_exact_over_full_dataset(self):
        _base, source_root, cache_root = self.make_dirs()
        rows = (
            [{"event_type": "alert", "proto": "TCP"}] * 4
            + [{"event_type": "alert", "proto": "UDP"}] * 2
            + [{"event_type": "flow", "proto": "TCP"}] * 3
        )
        _write_ndjson(source_root / "a.json", rows)

        result = dt.aggregate_events(
            "a.json", source_root, cache_root, event_type="alert", equals={"proto": "TCP"}
        )
        self.assertEqual(result["count"], 4)

    def test_equals_rejects_event_type_key_and_bad_values(self):
        _base, source_root, cache_root = self.make_dirs()
        _write_ndjson(source_root / "a.json", [{"event_type": "alert"}])
        with self.assertRaises(dt.DataToolError):
            dt.aggregate_events("a.json", source_root, cache_root, equals={"event_type": "alert"})
        with self.assertRaises(dt.DataToolError):
            dt.aggregate_events("a.json", source_root, cache_root, equals={"proto": {"nested": True}})
        with self.assertRaises(dt.DataToolError):
            dt.aggregate_events(
                "a.json",
                source_root,
                cache_root,
                equals={f"f{i}": "x" for i in range(dt.MAX_EQUAL_FILTERS + 1)},
            )


class DescribeEventsTests(TempCaseMixin, unittest.TestCase):
    def setUp(self):
        _base, self.source_root, self.cache_root = self.make_dirs()
        _write_ndjson(
            self.source_root / "eve.json",
            [
                {
                    "event_type": "dns",
                    "src_ip": "1.1.1.1",
                    "dns": {"queries": [{"rrtype": "A", "rrname": "example.com"}]},
                    "tls": None,
                },
                {
                    "event_type": "tls",
                    "src_ip": "1.1.1.1",
                    "dns": None,
                    "tls": {"sni": "example.com", "version": "1.3"},
                },
            ],
        )

    def test_returns_bounded_paginated_columns_without_collecting_rows(self):
        dt.ensure_parquet_cache("eve.json", self.source_root, self.cache_root)
        with patch.object(dt.pl.LazyFrame, "collect") as collect_spy:
            dt.describe_events("eve.json", self.source_root, self.cache_root)
        collect_spy.assert_not_called()

    def test_returns_names_and_dtypes_including_nested(self):
        page = dt.describe_events("eve.json", self.source_root, self.cache_root, limit=100)
        names = {col["name"] for col in page["columns"]}
        self.assertIn("dns", names)
        self.assertIn("tls", names)
        self.assertIn("src_ip", names)
        for col in page["columns"]:
            self.assertIsInstance(col["dtype"], str)
        self.assertFalse(page["has_more"])

    def test_paginates(self):
        first = dt.describe_events("eve.json", self.source_root, self.cache_root, offset=0, limit=1)
        self.assertEqual(first["returned"], 1)
        self.assertTrue(first["has_more"])

    def test_rejects_invalid_offset_and_limit(self):
        with self.assertRaises(dt.DataToolError):
            dt.describe_events("eve.json", self.source_root, self.cache_root, offset=-1)
        with self.assertRaises(dt.DataToolError):
            dt.describe_events("eve.json", self.source_root, self.cache_root, limit=0)
        with self.assertRaises(dt.DataToolError):
            dt.describe_events(
                "eve.json", self.source_root, self.cache_root, limit=dt.MAX_SCHEMA_COLUMNS + 1
            )


class QuerySqlTests(TempCaseMixin, unittest.TestCase):
    def setUp(self):
        _base, self.source_root, self.cache_root = self.make_dirs()
        self.rows = [
            {
                "event_type": "dns",
                "src_ip": "1.1.1.1",
                "dns": {"queries": [{"rrtype": "A"}, {"rrtype": "AAAA"}]},
            },
            {
                "event_type": "dns",
                "src_ip": "1.1.1.1",
                "dns": {"queries": [{"rrtype": "A"}]},
            },
            {"event_type": "tls", "src_ip": "2.2.2.2", "dns": None},
        ]
        _write_ndjson(self.source_root / "eve.json", self.rows)

    def test_basic_select_returns_shaped_result(self):
        result = dt.query_sql(
            "eve.json", self.source_root, self.cache_root, sql="SELECT event_type FROM events"
        )
        self.assertEqual(set(result.keys()), {"columns", "rows", "returned", "has_more"})
        self.assertEqual(result["columns"], ["event_type"])
        self.assertEqual(result["returned"], 3)
        self.assertFalse(result["has_more"])

    def test_trailing_comment_after_semicolon_is_accepted(self):
        result = dt.query_sql(
            "eve.json",
            self.source_root,
            self.cache_root,
            sql="SELECT event_type FROM events; -- trailing comment",
        )
        self.assertEqual(result["returned"], 3)

    def test_with_cte_is_accepted(self):
        result = dt.query_sql(
            "eve.json",
            self.source_root,
            self.cache_root,
            sql="WITH t AS (SELECT event_type FROM events) SELECT count(*) AS n FROM t",
        )
        self.assertEqual(result["rows"][0]["n"], 3)

    def test_rejects_multi_statement_and_non_select(self):
        for bad_sql in [
            "SELECT 1; SELECT 2",
            "DROP TABLE events",
            "ATTACH ':memory:' AS x; SELECT 1",
            "EXPLAIN SELECT 1",
            "",
            "   ",
        ]:
            with self.assertRaises(dt.DataToolError, msg=bad_sql):
                dt.query_sql("eve.json", self.source_root, self.cache_root, sql=bad_sql)

    def test_paths_with_single_quote_still_work(self):
        base = self.source_root.parent
        quoted_source = base / "sou'rce"
        quoted_source.mkdir()
        quoted_cache = base / "cac'he"
        quoted_cache.mkdir()
        _write_ndjson(quoted_source / "eve.json", self.rows)

        result = dt.query_sql(
            "eve.json", quoted_source, quoted_cache, sql="SELECT count(*) AS n FROM events"
        )
        self.assertEqual(result["rows"][0]["n"], 3)

    def test_traversal_and_exfiltration_attempts_are_inert(self):
        sensitive = self.source_root.parent / "sensitive.txt"
        sensitive.write_text("secret")
        sibling_cache = self.source_root.parent / "sibling-evil.parquet"

        # Warm the cache, then plant a same-directory sibling sharing the cache filename as a
        # prefix, to prove allowed_paths matches exact paths, not prefixes.
        cache_path = dt.ensure_parquet_cache("eve.json", self.source_root, self.cache_root)
        import shutil as _shutil

        _shutil.copy(cache_path, self.cache_root / f"{cache_path.stem}-evil.parquet")

        exfil_target = self.source_root.parent / "exfil.csv"
        attempts = [
            "SELECT * FROM read_parquet('/etc/passwd')",
            f"COPY events TO '{exfil_target}'",
            "ATTACH '/tmp/pwn-query-sql.db' AS x",
            f"SELECT * FROM read_parquet('{self.cache_root / (cache_path.stem + '-evil.parquet')}')",
        ]
        for sql in attempts:
            with self.assertRaises(dt.DataToolError, msg=sql):
                dt.query_sql("eve.json", self.source_root, self.cache_root, sql=sql)
        self.assertFalse(exfil_target.exists())
        self.assertEqual(sensitive.read_text(), "secret")

    def test_timeout_interrupts_long_running_query_and_cleans_scratch(self):
        with patch.object(dt, "SQL_QUERY_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(dt.DataToolError):
                dt.query_sql(
                    "eve.json",
                    self.source_root,
                    self.cache_root,
                    sql="SELECT count(*) FROM range(100000000) a, range(1000) b",
                )
        self.assertEqual(list(self.cache_root.glob(".sql-scratch-*")), [])

    def test_limit_and_has_more_bounds(self):
        with self.assertRaises(dt.DataToolError):
            dt.query_sql("eve.json", self.source_root, self.cache_root, sql="SELECT 1", limit=0)
        with self.assertRaises(dt.DataToolError):
            dt.query_sql(
                "eve.json", self.source_root, self.cache_root, sql="SELECT 1", limit=dt.MAX_SQL_ROWS + 1
            )

        result = dt.query_sql(
            "eve.json", self.source_root, self.cache_root, sql="SELECT event_type FROM events", limit=2
        )
        self.assertEqual(result["returned"], 2)
        self.assertTrue(result["has_more"])

    def test_group_by_aggregate_is_exact_beyond_max_sql_rows(self):
        _base, source_root, cache_root = self.make_dirs()
        row_count = dt.MAX_SQL_ROWS + 50
        rows = [{"event_type": "alert"}] * row_count
        _write_ndjson(source_root / "a.json", rows)

        result = dt.query_sql(
            "a.json",
            source_root,
            cache_root,
            sql="SELECT event_type, count(*) AS n FROM events GROUP BY event_type",
        )
        self.assertEqual(result["rows"][0]["n"], row_count)

    def test_nested_unnest_query_returns_nested_shapes(self):
        result = dt.query_sql(
            "eve.json",
            self.source_root,
            self.cache_root,
            sql=(
                "WITH u AS (SELECT unnest(dns.queries) AS q FROM events WHERE event_type = 'dns') "
                "SELECT q.rrtype AS rrtype, count(*) AS n FROM u GROUP BY 1 ORDER BY 2 DESC"
            ),
        )
        counts = {row["rrtype"]: row["n"] for row in result["rows"]}
        self.assertEqual(counts, {"A": 2, "AAAA": 1})

    def test_timestamp_and_decimal_values_are_stringified_and_json_serializable(self):
        result = dt.query_sql(
            "eve.json",
            self.source_root,
            self.cache_root,
            sql=(
                "SELECT TIMESTAMP '2024-01-01 12:00:00' AS ts, CAST(1.5 AS DECIMAL(10,2)) AS d, "
                "{'inner_ts': TIMESTAMP '2024-01-01 12:00:00'} AS nested FROM events LIMIT 1"
            ),
        )
        row = result["rows"][0]
        self.assertIsInstance(row["ts"], str)
        self.assertIsInstance(row["d"], str)
        self.assertIsInstance(row["nested"]["inner_ts"], str)
        json.dumps(result)

    def test_duplicate_projected_names_are_rejected(self):
        with self.assertRaises(dt.DataToolError):
            dt.query_sql(
                "eve.json", self.source_root, self.cache_root, sql="SELECT 1 AS x, 2 AS x"
            )

    def test_materialization_regression_guard_stays_fast_under_tight_memory_limit(self):
        _base, source_root, cache_root = self.make_dirs()
        wide_struct = {"a": "x" * 200, "b": "y" * 200, "c": None, "d": list(range(20))}
        rows = [
            {
                "event_type": "alert",
                "flow": wide_struct,
                "tcp": wide_struct,
                "tls": wide_struct,
                "dns": wide_struct,
            }
            for _ in range(2000)
        ]
        _write_ndjson(source_root / "wide.json", rows)

        with patch.object(dt, "SQL_MEMORY_LIMIT", "64MB"):
            start = time.monotonic()
            result = dt.query_sql(
                "wide.json",
                source_root,
                cache_root,
                sql="SELECT event_type, count(*) AS n FROM events GROUP BY event_type",
            )
            elapsed = time.monotonic() - start

        self.assertEqual(result["rows"][0]["n"], 2000)
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
