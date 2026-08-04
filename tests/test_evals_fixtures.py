import json
import tempfile
import unittest
from pathlib import Path

from evals.fixtures import build_suricata_fixture


class BuildSuricataFixtureTests(unittest.TestCase):
    def _rows(self, dest_dir: Path) -> list[dict]:
        manifest = self.manifest = build_suricata_fixture(dest_dir)
        text = (dest_dir / manifest.filename).read_text()
        return [json.loads(line) for line in text.splitlines()]

    def test_writes_exactly_row_count_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self._rows(Path(directory))
            self.assertEqual(len(rows), self.manifest.row_count)

    def test_long_lived_flow_is_the_only_matching_record(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self._rows(Path(directory))
            matches = [
                r
                for r in rows
                if r["event_type"] == "flow"
                and r["src_ip"] == self.manifest.long_lived_flow_src
                and r["dest_ip"] == self.manifest.long_lived_flow_dest
            ]
            self.assertEqual(len(matches), 1)
            flow = matches[0]["flow"]
            self.assertAlmostEqual(
                flow["age"] / 3600,
                self.manifest.long_lived_flow_duration_hours,
                places=3,
            )
            self.assertGreater(self.manifest.long_lived_flow_duration_hours, 48)

    def test_beacon_has_exact_planted_count_and_regular_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self._rows(Path(directory))
            matches = sorted(
                (
                    r
                    for r in rows
                    if r["event_type"] == "flow"
                    and r["src_ip"] == self.manifest.beacon_src
                    and r["dest_ip"] == self.manifest.beacon_dest
                ),
                key=lambda r: r["flow"]["start"],
            )
            self.assertEqual(len(matches), self.manifest.beacon_count)
            self.assertTrue(all(r["dest_port"] == self.manifest.beacon_port for r in matches))

            from datetime import datetime

            starts = [datetime.fromisoformat(r["flow"]["start"]) for r in matches]
            intervals = {int((b - a).total_seconds()) for a, b in zip(starts, starts[1:])}
            self.assertEqual(intervals, {self.manifest.beacon_interval_seconds})

    def test_benign_host_reuses_a_small_server_pool_over_udp(self):
        # Regression test for the fixture bug found live on 2026-08-03 (see the comment on
        # evals/fixtures.py's _BENIGN_* constants): every model flagged the earlier version
        # (40 distinct never-repeated TCP destinations) as textbook C2 IP-rotation beaconing --
        # correctly, since that's not what real NTP traffic looks like.
        with tempfile.TemporaryDirectory() as directory:
            rows = self._rows(Path(directory))
            matches = [
                r
                for r in rows
                if r["event_type"] == "flow" and r["src_ip"] == self.manifest.benign_host_src
            ]
            self.assertEqual(len(matches), self.manifest.benign_host_connection_count)
            self.assertTrue(all(r["dest_port"] == self.manifest.benign_host_port for r in matches))
            self.assertTrue(all(r["proto"] == "UDP" for r in matches), "real NTP is UDP, not TCP")
            distinct_dests = {r["dest_ip"] for r in matches}
            self.assertEqual(len(distinct_dests), self.manifest.benign_host_dest_count)
            self.assertLess(
                self.manifest.benign_host_dest_count,
                self.manifest.benign_host_connection_count,
                "a real client reuses a small server pool, not one connection per distinct host",
            )

    def test_benign_host_timing_and_payload_size_are_not_mechanically_uniform(self):
        # A perfectly uniform interval/byte-count (stddev 0) was itself cited by multiple
        # models as evidence of automation -- guard against regressing to that shape.
        with tempfile.TemporaryDirectory() as directory:
            rows = self._rows(Path(directory))
            matches = sorted(
                (
                    r
                    for r in rows
                    if r["event_type"] == "flow" and r["src_ip"] == self.manifest.benign_host_src
                ),
                key=lambda r: r["timestamp"],
            )

            from datetime import datetime

            starts = [datetime.fromisoformat(r["timestamp"]) for r in matches]
            intervals = {int((b - a).total_seconds()) for a, b in zip(starts, starts[1:])}
            self.assertGreater(len(intervals), 1, "intervals should vary, not be perfectly uniform")

            byte_counts = {r["flow"]["bytes_toserver"] for r in matches}
            self.assertGreater(len(byte_counts), 1, "byte counts should vary, not be identical every time")

    def test_stats_events_are_present_as_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self._rows(Path(directory))
            stats = [r for r in rows if r["event_type"] == "stats"]
            self.assertGreater(len(stats), 0)

    def test_expected_findings_reference_the_planted_destination_ips(self):
        with tempfile.TemporaryDirectory() as directory:
            self._rows(Path(directory))
            self.assertEqual(
                self.manifest.expected_findings["long_lived_flow"],
                self.manifest.long_lived_flow_dest,
            )
            self.assertEqual(
                self.manifest.expected_findings["beacon"],
                self.manifest.beacon_dest,
            )

    def test_deterministic_across_calls(self):
        with tempfile.TemporaryDirectory() as directory_a, tempfile.TemporaryDirectory() as directory_b:
            manifest_a = build_suricata_fixture(Path(directory_a))
            manifest_b = build_suricata_fixture(Path(directory_b))
            text_a = (Path(directory_a) / manifest_a.filename).read_text()
            text_b = (Path(directory_b) / manifest_b.filename).read_text()
            self.assertEqual(text_a, text_b)


if __name__ == "__main__":
    unittest.main()
