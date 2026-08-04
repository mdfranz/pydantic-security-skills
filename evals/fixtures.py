"""Synthetic Suricata EVE JSON fixture with deliberately planted, independently-verifiable
findings.

Every real model-comparison in `results/*.md` verified specific claims by hand (`grep`-ing the
raw 184MB capture) because that capture has no independently-confirmed ground truth (see those
reports' own Limitations sections). This fixture inverts that: every finding it contains is fixed at
generation time, asserted directly in `tests/test_evals_fixtures.py`, and exposed via
`FixtureManifest` so evaluators never duplicate the underlying facts as separate magic strings.

Deterministic by construction (fixed timestamps/IPs, no RNG) so the same fixture -- and the same
expected findings -- are produced on every run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

FIXTURE_FILENAME = "eve-eval-fixture.json"

_BASE_TIME = datetime(2026, 1, 6, 0, 0, 0)

# Finding 1: a single long-lived flow, mirroring the real capture's already-documented
# 53-hour outlier (results/pristine-model-comparison-2026-07-19.md) for direct comparability.
_LONG_FLOW_SRC = "192.168.50.10"
_LONG_FLOW_DEST = "203.0.113.50"
_LONG_FLOW_PORT = 443
_LONG_FLOW_START = _BASE_TIME - timedelta(days=3, hours=3, minutes=54, seconds=2)
_LONG_FLOW_END = _BASE_TIME + timedelta(hours=1, minutes=31, seconds=44)
_LONG_FLOW_AGE_SECONDS = int((_LONG_FLOW_END - _LONG_FLOW_START).total_seconds())

# Finding 2: a fixed-interval beacon to an external MQTT port, mirroring the real capture's
# GLM-5.2 beacon finding's shape (results/pristine-model-comparison-2026-07-19.md).
_BEACON_SRC = "192.168.50.20"
_BEACON_DEST = "203.0.113.99"
_BEACON_PORT = 1883
_BEACON_INTERVAL_SECONDS = 300
_BEACON_COUNT = 50

# Finding 3: a benign, high-connection-count host -- a small, stable, realistic NTP server
# pool over UDP, no alert signature. A deliberate false-positive trap.
#
# An earlier version of this fixture hit 40 *distinct*, never-repeated destination IPs in
# strict ascending order, at a perfectly uniform interval (stddev 0) with byte-identical
# payloads, over TCP. Live model runs against that version (see PROJECT.md/ISSUES.md,
# 2026-08-03) showed every model correctly read that as textbook C2 IP-rotation beaconing --
# not a false positive on the models' part, a bug in the fixture: real NTP is UDP, not TCP,
# and a real NTP client polls a small, stable server pool, not 40 distinct hosts. The pattern
# below fixes both: UDP, a small reused pool, and deterministic (non-RNG) jitter on interval
# and payload size so it isn't mechanically perfect either -- perfect regularity was itself
# called out by name in several reports as automation evidence.
_BENIGN_SRC = "192.168.50.30"
_BENIGN_PORT = 123
_BENIGN_SERVER_POOL = ("198.51.100.1", "198.51.100.2", "198.51.100.3")
_BENIGN_CONNECTION_COUNT = 30
_BENIGN_INTERVAL_SECONDS = 300
_BENIGN_INTERVAL_JITTER_SECONDS = (0, -6, 4, -3, 7, -2)
_BENIGN_BYTE_JITTER = (0, 2, -1, 3, -2, 1)
_BENIGN_BASE_BYTES = 48  # a real NTP client/server exchange is a fixed-size ~48-byte packet

_MALICIOUS_KEYWORDS = ("c2", "malicious", "isolate", "compromise", "beacon", "exfil")


@dataclass(frozen=True)
class FixtureManifest:
    """Ground truth for the synthetic fixture -- the single source of `Case.metadata`,
    evaluator assertions, and test assertions all derive from, so none of them can drift
    from what was actually written to disk."""

    filename: str
    row_count: int

    long_lived_flow_src: str
    long_lived_flow_dest: str
    long_lived_flow_port: int
    long_lived_flow_duration_hours: float

    beacon_src: str
    beacon_dest: str
    beacon_port: int
    beacon_interval_seconds: int
    beacon_count: int

    benign_host_src: str
    benign_host_port: int
    benign_host_dest_count: int
    benign_host_connection_count: int

    malicious_keywords: tuple[str, ...] = _MALICIOUS_KEYWORDS

    expected_findings: dict[str, str] = field(default_factory=dict)
    """label -> substring that a correct report should contain (case-insensitive)."""


def write_ndjson(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def _flow_event(
    *,
    timestamp: datetime,
    src_ip: str,
    dest_ip: str,
    dest_port: int,
    flow_start: datetime,
    flow_end: datetime,
    bytes_toserver: int = 4200,
    bytes_toclient: int = 1800,
    proto: str = "TCP",
) -> dict:
    age = int((flow_end - flow_start).total_seconds())
    return {
        "timestamp": timestamp.isoformat(),
        "event_type": "flow",
        "src_ip": src_ip,
        "dest_ip": dest_ip,
        "src_port": 51000,
        "dest_port": dest_port,
        "proto": proto,
        "flow": {
            "start": flow_start.isoformat(),
            "end": flow_end.isoformat(),
            "age": age,
            "bytes_toserver": bytes_toserver,
            "bytes_toclient": bytes_toclient,
        },
    }


def _dns_event(timestamp: datetime, src_ip: str, rrname: str) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "event_type": "dns",
        "src_ip": src_ip,
        "dest_ip": "192.168.50.1",
        "src_port": 52000,
        "dest_port": 53,
        "proto": "UDP",
        "dns": {"rrname": rrname, "rrtype": "A", "type": "query"},
    }


def _http_event(timestamp: datetime, src_ip: str, dest_ip: str, hostname: str) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "event_type": "http",
        "src_ip": src_ip,
        "dest_ip": dest_ip,
        "src_port": 53000,
        "dest_port": 80,
        "proto": "TCP",
        "http": {"hostname": hostname, "url": "/", "http_user_agent": "eval-fixture/1.0"},
    }


def _tls_event(timestamp: datetime, src_ip: str, dest_ip: str, sni: str) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "event_type": "tls",
        "src_ip": src_ip,
        "dest_ip": dest_ip,
        "src_port": 54000,
        "dest_port": 443,
        "proto": "TCP",
        "tls": {"sni": sni, "version": "TLS 1.3"},
    }


def _stats_event(timestamp: datetime, index: int) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "event_type": "stats",
        "stats": {"capture": {"kernel_packets": 1000 + index, "kernel_drops": 0}},
    }


def build_suricata_fixture(dest_dir: Path) -> FixtureManifest:
    """Write the deterministic synthetic EVE-JSON fixture into `dest_dir` (a source root) and
    return the ground-truth manifest describing what was planted."""
    rows: list[dict] = []

    # Finding 1: one long-lived flow.
    rows.append(
        _flow_event(
            timestamp=_LONG_FLOW_END,
            src_ip=_LONG_FLOW_SRC,
            dest_ip=_LONG_FLOW_DEST,
            dest_port=_LONG_FLOW_PORT,
            flow_start=_LONG_FLOW_START,
            flow_end=_LONG_FLOW_END,
        )
    )

    # Finding 2: fixed-interval beacon.
    beacon_start = _BASE_TIME - timedelta(hours=4)
    for i in range(_BEACON_COUNT):
        beacon_time = beacon_start + timedelta(seconds=i * _BEACON_INTERVAL_SECONDS)
        rows.append(
            _flow_event(
                timestamp=beacon_time,
                src_ip=_BEACON_SRC,
                dest_ip=_BEACON_DEST,
                dest_port=_BEACON_PORT,
                flow_start=beacon_time,
                flow_end=beacon_time + timedelta(seconds=2),
                bytes_toserver=120,
                bytes_toclient=90,
            )
        )

    # Finding 3: benign, high-connection-count host reusing a small NTP server pool over UDP,
    # with deterministic jitter on interval and payload size (see the comment on the constants
    # above for why -- an earlier version of this loop was itself a false-positive generator).
    benign_start = _BASE_TIME - timedelta(hours=6)
    elapsed_seconds = 0
    for i in range(_BENIGN_CONNECTION_COUNT):
        dest_ip = _BENIGN_SERVER_POOL[i % len(_BENIGN_SERVER_POOL)]
        interval_jitter = _BENIGN_INTERVAL_JITTER_SECONDS[i % len(_BENIGN_INTERVAL_JITTER_SECONDS)]
        elapsed_seconds += _BENIGN_INTERVAL_SECONDS + interval_jitter
        benign_time = benign_start + timedelta(seconds=elapsed_seconds)
        byte_jitter = _BENIGN_BYTE_JITTER[i % len(_BENIGN_BYTE_JITTER)]
        rows.append(
            _flow_event(
                timestamp=benign_time,
                src_ip=_BENIGN_SRC,
                dest_ip=dest_ip,
                dest_port=_BENIGN_PORT,
                proto="UDP",
                flow_start=benign_time,
                flow_end=benign_time + timedelta(seconds=1),
                bytes_toserver=_BENIGN_BASE_BYTES + byte_jitter,
                bytes_toclient=_BENIGN_BASE_BYTES + byte_jitter,
            )
        )

    # Baseline noise: dns/http/tls/stats events unrelated to any finding, including `stats`
    # events specifically -- SKILL.md instructs ignoring them, so a report citing stats-derived
    # numbers as a "finding" is itself a defect an evaluator could check for.
    noise_start = _BASE_TIME - timedelta(hours=8)
    noise_ips = [f"192.168.50.{100 + i}" for i in range(10)]
    noise_domains = [f"noise-{i}.example.com" for i in range(10)]
    noise_dests = [f"192.0.2.{i + 1}" for i in range(10)]
    for i in range(100):
        t = noise_start + timedelta(seconds=i * 90)
        src = noise_ips[i % len(noise_ips)]
        dest = noise_dests[i % len(noise_dests)]
        domain = noise_domains[i % len(noise_domains)]
        rows.append(_dns_event(t, src, domain))
        rows.append(_http_event(t, src, dest, domain))
        rows.append(_tls_event(t, src, dest, domain))
        rows.append(_stats_event(t, i))

    dest_path = dest_dir / FIXTURE_FILENAME
    write_ndjson(dest_path, rows)

    return FixtureManifest(
        filename=FIXTURE_FILENAME,
        row_count=len(rows),
        long_lived_flow_src=_LONG_FLOW_SRC,
        long_lived_flow_dest=_LONG_FLOW_DEST,
        long_lived_flow_port=_LONG_FLOW_PORT,
        long_lived_flow_duration_hours=_LONG_FLOW_AGE_SECONDS / 3600,
        beacon_src=_BEACON_SRC,
        beacon_dest=_BEACON_DEST,
        beacon_port=_BEACON_PORT,
        beacon_interval_seconds=_BEACON_INTERVAL_SECONDS,
        beacon_count=_BEACON_COUNT,
        benign_host_src=_BENIGN_SRC,
        benign_host_port=_BENIGN_PORT,
        benign_host_dest_count=len(_BENIGN_SERVER_POOL),
        benign_host_connection_count=_BENIGN_CONNECTION_COUNT,
        expected_findings={
            "long_lived_flow": _LONG_FLOW_DEST,
            "beacon": _BEACON_DEST,
        },
    )
