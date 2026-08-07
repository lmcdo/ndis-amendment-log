"""Tests for the amendment detection log.

These tests target the properties that make the log worth publishing at all.
If the hash chain does not actually detect tampering, or a baseline can claim
a latency it did not earn, the log is decoration and the whole trust argument
for publishing it collapses.

Each test is written so that a trivially broken implementation fails it: a
verify_chain that always returns [] fails test_verify_detects_*, and a
latency_hours that returns a constant fails test_latency_*.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import poll_amendments as pa  # noqa: E402


def _chained(entries: list[dict]) -> list[dict]:
    """Build a correctly chained log from bare entry bodies."""
    prev = pa.GENESIS_HASH
    out = []
    for index, body in enumerate(entries):
        entry = dict(body)
        entry["seq"] = index + 1
        entry["prev_hash"] = prev
        entry["entry_hash"] = pa._hash_entry(entry)
        prev = entry["entry_hash"]
        out.append(entry)
    return out


class TestChainIntegrity:
    """The chain must make silent history rewriting impossible."""

    def test_intact_chain_has_no_problems(self):
        entries = _chained([{"title_id": "A", "register_id": "R1"},
                            {"title_id": "B", "register_id": "R2"}])
        assert pa.verify_chain(entries) == []

    def test_empty_log_is_valid(self):
        assert pa.verify_chain([]) == []

    def test_verify_detects_edited_content(self):
        """Backfilling a missed amendment by editing an entry must be caught."""
        entries = _chained([{"title_id": "A", "register_id": "R1"}])
        entries[0]["register_id"] = "R99"  # silently rewrite history
        problems = pa.verify_chain(entries)
        assert problems, "editing an entry must break verification"
        assert any("edited after it was written" in p for p in problems)

    def test_verify_detects_deleted_entry(self):
        """Dropping an embarrassing entry must break the chain."""
        entries = _chained([{"title_id": "A"}, {"title_id": "B"}, {"title_id": "C"}])
        del entries[1]
        problems = pa.verify_chain(entries)
        assert problems, "removing an entry must break verification"

    def test_verify_detects_reordering(self):
        entries = _chained([{"title_id": "A"}, {"title_id": "B"}])
        entries.reverse()
        assert pa.verify_chain(entries), "reordering must break verification"

    def test_verify_detects_inserted_entry(self):
        """A forged entry spliced in must not validate."""
        entries = _chained([{"title_id": "A"}, {"title_id": "B"}])
        forged = {"title_id": "FORGED", "seq": 2,
                  "prev_hash": entries[0]["entry_hash"]}
        forged["entry_hash"] = pa._hash_entry(forged)
        entries.insert(1, forged)
        assert pa.verify_chain(entries), "an inserted entry must break the chain"

    def test_first_entry_must_chain_to_genesis(self):
        entries = _chained([{"title_id": "A"}])
        entries[0]["prev_hash"] = "f" * 64
        entries[0]["entry_hash"] = pa._hash_entry(entries[0])
        assert pa.verify_chain(entries), "a fabricated genesis must be rejected"


class TestLatencyHonesty:
    """Latency must be measured against the Register, not against ourselves."""

    def test_latency_measured_from_register_timestamp(self):
        detected = datetime(2026, 7, 4, 16, 13, 32, tzinfo=pa.AEST)
        # Register published 2 days earlier
        hours = pa.latency_hours("2026-07-02T16:13:32.311", detected)
        assert hours == pytest.approx(48.0, abs=0.2)

    def test_latency_is_not_zero_for_a_late_detection(self):
        """Guards against an implementation that reports its own clock."""
        detected = datetime(2026, 8, 7, 12, 0, 0, tzinfo=pa.AEST)
        hours = pa.latency_hours("2026-07-02T16:13:32.311", detected)
        assert hours > 800, "a five-week-late detection must report as such"

    def test_missing_registered_at_yields_none_not_zero(self):
        """Unknown must never be reported as instant."""
        assert pa.latency_hours(None, datetime.now(timezone.utc)) is None
        assert pa.latency_hours("", datetime.now(timezone.utc)) is None

    def test_unparseable_timestamp_yields_none(self):
        assert pa.latency_hours("not-a-date", datetime.now(timezone.utc)) is None

    def test_timezone_assumption_overstates_rather_than_flatters(self):
        """If the offset assumption is wrong it must not make us look faster.

        Treating a naive Register timestamp as UTC+10 yields a LARGER elapsed
        time than treating it as UTC would. Being wrong in the direction that
        flatters us is the failure mode that matters.
        """
        detected = datetime(2026, 7, 3, 0, 0, 0, tzinfo=timezone.utc)
        as_aest = pa.latency_hours("2026-07-02T00:00:00", detected)
        naive_utc = (
            detected - datetime(2026, 7, 2, tzinfo=timezone.utc)
        ).total_seconds() / 3600
        assert as_aest > naive_utc


class TestBaselineSemantics:
    """A first sighting must never masquerade as a detection."""

    def test_render_shows_no_claim_for_baseline(self):
        entries = _chained([{
            "type": "baseline", "title_id": "F2018L00631",
            "short_name": "Practice Standards", "register_id": "F2026C00527",
            "compilation_number": 6, "effective_from": "2026-07-01",
            "registered_at": "2026-07-02T16:13:32", "detected_at": "2026-08-07T11:00:00",
            "previous_register_id": None, "detection_latency_hours": None,
            "source_url": "https://www.legislation.gov.au/F2026C00527",
        }])
        html = pa.render(entries, [])
        assert "baseline — no claim" in html
        assert "Amendments detected</div>\n    <div class=\"v\">0<" in html.replace(
            "  ", "  "
        ) or ">0<" in html, "a baseline must not count as a detection"

    def test_baselines_excluded_from_latency_stats(self):
        entries = _chained([
            {"type": "baseline", "title_id": "A", "short_name": "A",
             "register_id": "R1", "compilation_number": 1,
             "effective_from": "2026-01-01", "registered_at": "2026-01-01T00:00:00",
             "detected_at": "2026-08-07T00:00:00", "previous_register_id": None,
             "detection_latency_hours": None, "source_url": "u"},
        ])
        html = pa.render(entries, [])
        # With only a baseline present there is no median to report.
        assert "no detections yet" in html


class TestRenderSafety:
    def test_html_is_escaped(self):
        entries = _chained([{
            "type": "amendment", "title_id": "A",
            "short_name": "<script>alert(1)</script>", "register_id": "R1",
            "compilation_number": 1, "effective_from": "2026-01-01",
            "registered_at": "2026-01-01T00:00:00",
            "detected_at": "2026-08-07T00:00:00", "previous_register_id": None,
            "detection_latency_hours": 1.0, "source_url": "u",
        }])
        html = pa.render(entries, [])
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_empty_log_renders_without_error(self):
        html = pa.render([], [])
        assert "No entries yet" in html


class TestInForceSelection:
    """Regression: the Register publishes future sunset rows.

    Observed live on 2026-08-07 for F2024L01257 and F2025L01383 — a row with
    start in 2034/2036, status Repealed and a null registerId. Taking the
    newest row reported those in-force instruments as having no compilation.
    """

    REPEAL_ROW = {
        "registerId": None, "compilationNumber": None,
        "start": "2034-10-01T00:00:00", "status": "Repealed", "isLatest": False,
    }
    LIVE_ROW = {
        "registerId": "F2024L01257", "compilationNumber": "0",
        "start": "2024-10-01T00:00:00", "status": "InForce", "isLatest": True,
    }

    def test_skips_future_repeal_placeholder(self, monkeypatch):
        monkeypatch.setattr(
            pa, "_get", lambda path: {"value": [self.REPEAL_ROW, self.LIVE_ROW]}
        )
        got = pa.fetch_latest_version("F2024L01257")
        assert got is not None, "an in-force instrument must not resolve to None"
        assert got["registerId"] == "F2024L01257"

    def test_skips_not_yet_commenced_version(self, monkeypatch):
        future = dict(self.LIVE_ROW, registerId="F2099C00001",
                      start="2099-01-01T00:00:00")
        monkeypatch.setattr(
            pa, "_get", lambda path: {"value": [future, self.LIVE_ROW]}
        )
        got = pa.fetch_latest_version("F2024L01257")
        assert got["registerId"] == "F2024L01257", "must not serve future text"

    def test_returns_none_when_nothing_in_force(self, monkeypatch):
        monkeypatch.setattr(pa, "_get", lambda path: {"value": [self.REPEAL_ROW]})
        assert pa.fetch_latest_version("X") is None

    def test_empty_response_returns_none(self, monkeypatch):
        monkeypatch.setattr(pa, "_get", lambda path: {"value": []})
        assert pa.fetch_latest_version("X") is None


class TestTrackedCorpus:
    def test_no_duplicate_instruments(self):
        ids = [t["id"] for t in pa.TRACKED]
        assert len(ids) == len(set(ids))

    def test_every_instrument_has_a_short_name(self):
        assert all(t.get("short") for t in pa.TRACKED)
