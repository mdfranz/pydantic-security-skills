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

    def test_benign_host_reaches_many_distinct_destinations_on_one_boring_port(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self._rows(Path(directory))
            matches = [
                r
                for r in rows
                if r["event_type"] == "flow" and r["src_ip"] == self.manifest.benign_host_src
            ]
            self.assertEqual(len(matches), self.manifest.benign_host_dest_count)
            self.assertTrue(all(r["dest_port"] == self.manifest.benign_host_port for r in matches))
            distinct_dests = {r["dest_ip"] for r in matches}
            self.assertEqual(len(distinct_dests), self.manifest.benign_host_dest_count)

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
