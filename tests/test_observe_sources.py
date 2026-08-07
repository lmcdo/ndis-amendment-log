"""Tests for the quarantined observation tier.

prior-art-checked: reuse not viable because test_poll_amendments.py covers the
    Register/OData path and shares no code with the sitemap and HTTP-header
    signal handling tested here.

The properties that matter are about not fooling ourselves. An observation
tier whose coverage check cannot fail, or that records a lost signal as an
unchanged one, is worse than no tier at all: it would manufacture false
confidence in exactly the artifact whose only value is being trustworthy.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import observe_sources as obs  # noqa: E402


class TestSitemapParsing:
    def test_extracts_loc_and_lastmod_pairs(self):
        xml = """<urlset>
          <url><loc>https://x/a</loc><lastmod>2026-01-01T00:00:00+11:00</lastmod></url>
          <url><loc>https://x/b</loc><lastmod>2025-06-30T10:00:00+10:00</lastmod></url>
        </urlset>"""
        got = obs.parse_sitemap(xml)
        assert got == {
            "https://x/a": "2026-01-01T00:00:00+11:00",
            "https://x/b": "2025-06-30T10:00:00+10:00",
        }

    def test_url_without_lastmod_is_omitted_not_defaulted(self):
        """A page with no timestamp must not read as 'unchanged'."""
        xml = "<urlset><url><loc>https://x/a</loc></url></urlset>"
        assert obs.parse_sitemap(xml) == {}

    def test_empty_document_yields_empty_map(self):
        assert obs.parse_sitemap("") == {}
        assert obs.parse_sitemap("<urlset></urlset>") == {}


class TestFingerprint:
    def test_prefers_etag(self):
        assert obs.sitemap_fingerprint(
            {"etag": '"123"', "last-modified": "Fri, 07 Aug 2026 04:20:00 GMT"}
        ) == '"123"'

    def test_falls_back_to_last_modified(self):
        assert (
            obs.sitemap_fingerprint({"last-modified": "Fri, 07 Aug 2026 04:20:00 GMT"})
            == "Fri, 07 Aug 2026 04:20:00 GMT"
        )

    def test_returns_none_when_neither_present(self):
        """No fingerprint must mean 'cannot short-circuit', not 'unchanged'."""
        assert obs.sitemap_fingerprint({"content-type": "application/xml"}) is None


class TestSignalHistory:
    def test_last_signal_ignores_nulls(self):
        """A lost signal must not overwrite the last known good value."""
        history = [
            {"signals": [{"key": "k", "signal": "v1"}]},
            {"signals": [{"key": "k", "signal": None}]},
        ]
        assert obs.last_signal_by_key(history) == {"k": "v1"}

    def test_latest_value_wins(self):
        history = [
            {"signals": [{"key": "k", "signal": "v1"}]},
            {"signals": [{"key": "k", "signal": "v2"}]},
        ]
        assert obs.last_signal_by_key(history) == {"k": "v2"}

    def test_empty_history(self):
        assert obs.last_signal_by_key([]) == {}


class TestObserveIntegrity:
    """The observation round must not be able to quietly under-report."""

    @staticmethod
    def _patch(monkeypatch, tmp_path, *, headers=None, xml=None, head_raises=False):
        monkeypatch.setattr(obs, "OBS_PATH", tmp_path / "observations.jsonl")
        monkeypatch.setattr(obs, "REQUEST_SPACING_SECONDS", 0)
        monkeypatch.setattr(obs, "COMMISSION_WATCH", ["/a"])
        monkeypatch.setattr(obs, "NDIA_WATCH", ["https://n/x"])

        def fake_head(url):
            if head_raises:
                raise OSError("boom")
            return headers if headers is not None else {}

        monkeypatch.setattr(obs, "_head", fake_head)
        monkeypatch.setattr(obs, "_get_text", lambda url: xml or "")

    def test_missing_last_modified_is_an_error_not_a_null(self, monkeypatch, tmp_path):
        """Losing the signal must surface, or we watch nothing and never know."""
        self._patch(
            monkeypatch, tmp_path,
            headers={"content-type": "text/html"},
            xml="<urlset><url><loc>https://c/a</loc><lastmod>X</lastmod></url></urlset>",
        )
        record, errors = obs.observe()
        assert any("signal lost" in e or "Last-Modified" in e for e in errors)

    def test_watched_page_absent_from_sitemap_is_an_error(self, monkeypatch, tmp_path):
        self._patch(
            monkeypatch, tmp_path,
            headers={"last-modified": "Fri, 07 Aug 2026 00:00:00 GMT"},
            xml="<urlset><url><loc>https://c/other</loc><lastmod>X</lastmod></url></urlset>",
        )
        record, errors = obs.observe()
        assert any("not present in sitemap" in e for e in errors)

    def test_fetch_failure_is_recorded_not_swallowed(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, head_raises=True)
        record, errors = obs.observe()
        assert errors, "a total fetch failure must produce errors"
        assert record["observed"] < record["watched"]

    def test_clean_round_reports_full_coverage_and_no_errors(
        self, monkeypatch, tmp_path
    ):
        self._patch(
            monkeypatch, tmp_path,
            headers={"last-modified": "Fri, 07 Aug 2026 00:00:00 GMT",
                     "etag": '"sm1"'},
            xml="<urlset><url><loc>https://c/a</loc><lastmod>2026-01-01</lastmod></url></urlset>",
        )
        record, errors = obs.observe()
        assert errors == []
        assert record["observed"] == record["watched"]

    def test_coverage_shortfall_cannot_pass_silently(self, monkeypatch, tmp_path):
        """A completion check that cannot fail is worthless — this one can.

        The real scenario it guards: a page is ADDED to the watchlist while the
        sitemap fingerprint is unchanged. The short-circuit then carries that
        key forward as None — no exception, no per-signal error — and the round
        would otherwise report clean while silently watching one fewer page.

        Written after a mutation check: an earlier version of this test tripped
        the zero-pairs check instead and passed even with the coverage gate
        disabled, which is the exact "green for the wrong reason" this repo's
        tests are supposed to rule out.
        """
        obs_path = tmp_path / "observations.jsonl"
        monkeypatch.setattr(obs, "OBS_PATH", obs_path)
        monkeypatch.setattr(obs, "REQUEST_SPACING_SECONDS", 0)
        monkeypatch.setattr(obs, "NDIA_WATCH", [])
        monkeypatch.setattr(obs, "_head", lambda url: {"etag": '"unchanged"'})
        monkeypatch.setattr(obs, "_get_text", lambda url: "")

        # Round 1: one watched page, fingerprint recorded.
        monkeypatch.setattr(obs, "COMMISSION_WATCH", ["/a"])
        monkeypatch.setattr(
            obs, "parse_sitemap",
            lambda xml: {"https://c/a": "2026-01-01T00:00:00+11:00"},
        )
        first, first_errors = obs.observe()
        obs.append_observation(first)
        assert first_errors == []

        # Round 2: watchlist grows, fingerprint identical -> short-circuit.
        monkeypatch.setattr(obs, "COMMISSION_WATCH", ["/a", "/newly-watched"])
        record, errors = obs.observe()

        assert record["observed"] < record["watched"], "setup must under-report"
        assert errors, "an under-reporting round must not return clean"
        assert any("coverage" in e for e in errors)

    def test_status_field_marks_quarantine(self, monkeypatch, tmp_path):
        """Nothing here may be mistaken for a published detection."""
        self._patch(
            monkeypatch, tmp_path,
            headers={"last-modified": "Fri, 07 Aug 2026 00:00:00 GMT"},
            xml="<urlset><url><loc>https://c/a</loc><lastmod>2026-01-01</lastmod></url></urlset>",
        )
        record, _ = obs.observe()
        assert "QUARANTINE" in record["status"]


class TestReport:
    def test_report_on_empty_history(self, monkeypatch, tmp_path):
        monkeypatch.setattr(obs, "OBS_PATH", tmp_path / "nope.jsonl")
        assert "No observations yet" in obs.report()


class TestFailureSemantics:
    """A lost signal is an absence, not a change; partial != total outage."""

    def test_failed_sitemap_fetch_is_not_counted_as_a_change(
        self, monkeypatch, tmp_path
    ):
        """Regression: a WAF block was inflating the observed change count.

        GitHub Actions IPs are refused by the Commission host. The fingerprint
        went None while a previous value existed, which read as "changed" and
        would have polluted the noise rate this tier exists to measure.
        """
        obs_path = tmp_path / "observations.jsonl"
        monkeypatch.setattr(obs, "OBS_PATH", obs_path)
        monkeypatch.setattr(obs, "REQUEST_SPACING_SECONDS", 0)
        monkeypatch.setattr(obs, "COMMISSION_WATCH", ["/a"])
        monkeypatch.setattr(obs, "NDIA_WATCH", [])

        monkeypatch.setattr(obs, "_head", lambda url: {"etag": '"sm1"'})
        monkeypatch.setattr(
            obs, "_get_text", lambda url: (
                "<urlset><url><loc>https://c/a</loc>"
                "<lastmod>2026-01-01</lastmod></url></urlset>"
            ),
        )
        first, _ = obs.observe()
        obs.append_observation(first)

        def blocked(url):
            raise OSError("blocked by WAF")

        monkeypatch.setattr(obs, "_head", blocked)
        record, errors = obs.observe()

        assert errors, "the outage must be recorded"
        assert record["changed"] == 0, "an unreachable source is not a change"

    def test_total_failure_is_distinguished_from_partial(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(obs, "OBS_PATH", tmp_path / "o.jsonl")
        monkeypatch.setattr(obs, "REQUEST_SPACING_SECONDS", 0)
        monkeypatch.setattr(obs, "COMMISSION_WATCH", ["/a"])
        monkeypatch.setattr(obs, "NDIA_WATCH", ["https://n/x"])

        def blocked(url):
            raise OSError("down")

        monkeypatch.setattr(obs, "_head", blocked)
        monkeypatch.setattr(obs, "_get_text", blocked)
        record, _ = obs.observe()
        assert record["total_failure"] is True

        # Partial: NDIA answers, Commission does not.
        monkeypatch.setattr(
            obs, "_head",
            lambda url: (
                {"last-modified": "Fri, 07 Aug 2026 00:00:00 GMT"}
                if "n/x" in url else (_ for _ in ()).throw(OSError("down"))
            ),
        )
        record2, _ = obs.observe()
        assert record2["total_failure"] is False
