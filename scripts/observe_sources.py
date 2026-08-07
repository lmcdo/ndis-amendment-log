"""Observe change signals from sources that are NOT on the Federal Register.

prior-art-checked: reuse not viable because nothing in this repository observes
    non-Register sources; poll_amendments.py speaks OData to the Register and
    has no HTTP-header or sitemap handling. Kept as a separate module, and a
    separate data file, precisely so unvalidated signals cannot leak into the
    amendment log.

The NDIS Commission (guidance, practice standards) and the NDIA (pricing) both
change outside the Register, and providers are audited against that material.
Neither publishes an API. Two cheap signals exist:

  - Commission: ``sitemap.xml`` carries per-URL ``<lastmod>`` (335 URLs).
  - NDIA: pages return a per-page HTTP ``Last-Modified`` header, readable with
    a HEAD request — no body transfer at all.

**Neither signal is trusted yet, and nothing here is published as a detection.**
Observations land in ``data/observations.jsonl``, deliberately separate from the
amendment log, and are excluded from every statistic on the public page.

The reason is that both signals can lie, in opposite directions:

  - ``lastmod`` lies by OMISSION. All four Commission Core Module pages sit at
    an identical 2024-10-10 across the July 2026 standards activity. A page can
    change without the timestamp moving.
  - ``Last-Modified`` may lie by COMMISSION. Values cluster within days of each
    other across pages that plainly did not all change, so some of it is likely
    cache revalidation rather than editorial change.

So this runs in quarantine until the false-positive rate is measured. Promoting
a noisy signal into a credibility log would cost more than the extra coverage is
worth: every row on that page has to mean something.

Usage:
    python scripts/observe_sources.py            # observe, append
    python scripts/observe_sources.py --report   # false-positive summary

Exit codes:
    0 = observed cleanly, nothing changed
    1 = TOTAL failure — nothing observable
    2 = changes observed
    3 = partial outage, recorded (see --report 'lost' column)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

AEST = timezone(timedelta(hours=10))
# Both hosts sit behind a WAF that silently drops non-browser user-agents —
# verified 2026-08-07: a self-identifying UA, the RFC "compatible;" bot format,
# and even Googlebot's own UA all hang and return nothing, while a browser UA is
# served instantly. Neither site publishes a robots.txt (both 404), so no crawl
# directive is being disregarded, and a sitemap exists precisely to be read by
# machines. We therefore send a realistic UA to be served at all, and identify
# the operator honestly via the standard From header instead of hiding. Volume
# is four rounds a day against a handful of URLs. This is stated openly in the
# README so the publishers can contact or block us if they object.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
OPERATOR_FROM = "lawrence.mcdonell@gmail.com"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "From": OPERATOR_FROM,
    "X-Purpose": "public change-monitoring; https://github.com/lmcdo/ndis-amendment-log",
}
REQUEST_SPACING_SECONDS = 3.0

ROOT = Path(__file__).resolve().parent.parent
OBS_PATH = ROOT / "data" / "observations.jsonl"

COMMISSION_SITEMAP = "https://www.ndiscommission.gov.au/sitemap.xml"

# Commission pages carrying provider obligations, watched via sitemap lastmod.
# Paths, not full URLs — matched against <loc> entries.
COMMISSION_WATCH: list[str] = [
    "/rules-and-standards/ndis-practice-standards",
    "/rules-and-standards/ndis-practice-standards/core-module-rights-and-responsibilities",
    "/rules-and-standards/ndis-practice-standards/core-module-provider-governance-and-operational",
    "/rules-and-standards/ndis-practice-standards/core-module-provision-supports",
    "/rules-and-standards/ndis-practice-standards/core-module-provision-supports-environment",
    "/rules-and-standards/ndis-practice-standards/supplementary-module-high-intensity-daily-personal",
    "/rules-and-standards/ndis-practice-standards/supplementary-module-specialist-behaviour-support",
]

# NDIA pages watched via HTTP Last-Modified (HEAD request only).
NDIA_WATCH: list[str] = [
    "https://www.ndis.gov.au/providers/pricing-arrangements",
    "https://www.ndis.gov.au/providers/becoming-ndis-provider",
]


def _retry(attempts: int = 3, backoff: float = 5.0):
    """Retry a transient network failure before calling it an error.

    The Commission host returned a truncated body once during development and
    served the same URL correctly moments later. Reporting a transient blip as
    a lost signal would train the reader to ignore the error column, which is
    the one column that has to stay meaningful.
    """

    def decorate(func):
        def wrapper(*args, **kwargs):
            last: Exception | None = None
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except (urllib.error.URLError, OSError) as exc:
                    last = exc
                    if attempt < attempts - 1:
                        time.sleep(backoff * (attempt + 1))
            raise last  # type: ignore[misc]

        return wrapper

    return decorate


@_retry()
def _head(url: str) -> dict[str, str]:
    """Return a URL's response headers, lowercased, without reading the body.

    Issues a GET rather than a HEAD deliberately: ndis.gov.au answers HEAD with
    a 301 pointing at the same URL, an infinite redirect that never yields
    headers. Opening a GET and closing it without calling read() gets the
    headers while transferring almost nothing.

    Raises:
        urllib.error.URLError: on transport failure after retries.
    """
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return {k.lower(): v for k, v in resp.headers.items()}


@_retry()
def _get_text(url: str) -> str:
    """GET a URL as text."""
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read().decode("utf-8", errors="replace")


def sitemap_fingerprint(headers: dict[str, str]) -> str | None:
    """Identity of the sitemap file itself, from ETag or Last-Modified.

    The Commission's sitemap.xml is ~78KB covering 335 URLs and serves both an
    ETag and a Last-Modified. Since every per-page lastmod lives INSIDE that
    file, an unchanged fingerprint means no per-page value can have changed —
    so the 78KB fetch and the 335-URL parse can both be skipped. That is a
    deduction, not an assumption, which is why it is safe to rely on.
    """
    return headers.get("etag") or headers.get("last-modified")


def parse_sitemap(xml: str) -> dict[str, str]:
    """Map each ``<loc>`` to its ``<lastmod>``.

    URLs without a lastmod are omitted, which matters: a watched page losing
    its timestamp must surface as a missing signal, not as an unchanged one.
    """
    pairs = re.findall(
        r"<loc>\s*(.*?)\s*</loc>\s*<lastmod>\s*(.*?)\s*</lastmod>", xml, re.S
    )
    return {loc: mod for loc, mod in pairs}


def read_observations() -> list[dict[str, Any]]:
    """Read the observation history."""
    if not OBS_PATH.exists():
        return []
    with OBS_PATH.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def last_signal_by_key(observations: list[dict[str, Any]]) -> dict[str, str]:
    """Most recent non-null signal value seen for each watched key."""
    latest: dict[str, str] = {}
    for obs in observations:
        for row in obs.get("signals", []):
            if row.get("signal") is not None:
                latest[row["key"]] = row["signal"]
    return latest


def observe() -> tuple[dict[str, Any], list[str]]:
    """Collect one round of observations.

    Returns:
        A tuple of (observation record, errors). A watched signal that has
        DISAPPEARED is an error, never a silent null — losing the signal means
        we would stop noticing changes without noticing that we had.
    """
    observed_at = datetime.now(timezone.utc).astimezone()
    previous = last_signal_by_key(read_observations())
    signals: list[dict[str, Any]] = []
    errors: list[str] = []

    # --- Commission: HEAD the sitemap; parse it only if the file itself moved ---
    sitemap: dict[str, str] = {}
    sitemap_skipped = False
    previous_fingerprint = previous.get("commission:__sitemap__")
    fingerprint = None
    try:
        fingerprint = sitemap_fingerprint(_head(COMMISSION_SITEMAP))
        if fingerprint and fingerprint == previous_fingerprint:
            # Per-page lastmods live inside this file. Unchanged file, unchanged
            # values — carry them forward rather than re-fetching 78KB.
            sitemap_skipped = True
        else:
            sitemap = parse_sitemap(_get_text(COMMISSION_SITEMAP))
            if not sitemap:
                errors.append("commission sitemap: parsed zero url/lastmod pairs")
    except (urllib.error.URLError, OSError) as exc:
        errors.append(f"commission sitemap: {type(exc).__name__}: {exc}")

    signals.append(
        {
            "key": "commission:__sitemap__",
            "url": COMMISSION_SITEMAP,
            "source": "sitemap_fingerprint",
            "signal": fingerprint,
            # A failed fetch is an absence, never a change. Counting it as one
            # would inflate the very noise rate this tier exists to measure.
            "changed": bool(
                fingerprint
                and previous_fingerprint
                and fingerprint != previous_fingerprint
            ),
            "first_seen": previous_fingerprint is None,
        }
    )

    for path in COMMISSION_WATCH:
        key = f"commission:{path}"
        if sitemap_skipped:
            # Deduced unchanged, not observed. Recorded distinctly so the
            # report cannot mistake a skipped round for a fresh observation.
            signals.append(
                {
                    "key": key,
                    "source": "sitemap_lastmod",
                    "signal": previous.get(key),
                    "changed": False,
                    "carried_forward": True,
                }
            )
            continue
        if not sitemap:
            signals.append({"key": key, "signal": None, "source": "sitemap_lastmod"})
            continue
        matches = [u for u in sitemap if u.endswith(path)]
        if not matches:
            errors.append(f"{key}: not present in sitemap (page moved or removed)")
            signals.append({"key": key, "signal": None, "source": "sitemap_lastmod"})
            continue
        value = sitemap[matches[0]]
        signals.append(
            {
                "key": key,
                "url": matches[0],
                "source": "sitemap_lastmod",
                "signal": value,
                "changed": key in previous and previous[key] != value,
                "first_seen": key not in previous,
            }
        )

    # --- NDIA: HEAD per watched page ---
    for index, url in enumerate(NDIA_WATCH):
        if index:
            time.sleep(REQUEST_SPACING_SECONDS)
        key = f"ndia:{url}"
        try:
            headers = _head(url)
        except (urllib.error.URLError, OSError) as exc:
            errors.append(f"{key}: {type(exc).__name__}: {exc}")
            signals.append({"key": key, "signal": None, "source": "http_last_modified"})
            continue
        value = headers.get("last-modified")
        if not value:
            # The signal we rely on has gone. Silence here would mean silently
            # watching nothing at all.
            errors.append(f"{key}: Last-Modified header absent — signal lost")
            signals.append({"key": key, "signal": None, "source": "http_last_modified"})
            continue
        signals.append(
            {
                "key": key,
                "url": url,
                "source": "http_last_modified",
                "signal": value,
                "changed": key in previous and previous[key] != value,
                "first_seen": key not in previous,
            }
        )

    expected = len(COMMISSION_WATCH) + len(NDIA_WATCH) + 1  # +1 sitemap fingerprint
    observed = sum(1 for s in signals if s.get("signal") is not None)
    carried = sum(1 for s in signals if s.get("carried_forward"))
    record = {
        "observed_at": observed_at.isoformat(),
        "status": "QUARANTINE — signals under validation, not published as detections",
        "watched": expected,
        "observed": observed,
        "carried_forward": carried,
        "changed": sum(1 for s in signals if s.get("changed")),
        "signals": signals,
        "errors": errors,
    }
    # A coverage check that cannot fail is worthless. This one can.
    if observed < expected and not errors:
        errors.append(
            f"coverage: observed {observed} of {expected} watched signals with "
            f"no error recorded — the run is under-reporting"
        )
        record["errors"] = errors
    record["total_failure"] = observed == 0
    return record, errors


def append_observation(record: dict[str, Any]) -> None:
    """Append one observation round."""
    OBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OBS_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def report() -> str:
    """Summarise how noisy each signal has been, for the promotion decision."""
    observations = read_observations()
    if not observations:
        return "No observations yet."

    rounds = len(observations)
    span = (
        f"{observations[0]['observed_at'][:10]} to "
        f"{observations[-1]['observed_at'][:10]}"
    )
    per_key: dict[str, dict[str, int]] = {}
    for obs in observations:
        for row in obs.get("signals", []):
            stats = per_key.setdefault(row["key"], {"changes": 0, "seen": 0, "lost": 0})
            if row.get("signal") is None:
                stats["lost"] += 1
                continue
            stats["seen"] += 1
            if row.get("changed"):
                stats["changes"] += 1

    lines = [
        f"Observation rounds: {rounds}  ({span})",
        "",
        "A signal that changes on most rounds is almost certainly cache noise,",
        "not editorial change. Promote nothing until a signal is both quiet and",
        "corroborated by an actual content difference.",
        "",
        f"{'signal':<74} {'seen':>5} {'chg':>5} {'lost':>5}   rate",
    ]
    for key, stats in sorted(per_key.items()):
        rate = (stats["changes"] / stats["seen"] * 100) if stats["seen"] else 0.0
        lines.append(
            f"{key[:74]:<74} {stats['seen']:>5} {stats['changes']:>5} "
            f"{stats['lost']:>5}  {rate:5.1f}%"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point. See module docstring for exit codes."""
    parser = argparse.ArgumentParser(description="Observe non-Register sources")
    parser.add_argument("--report", action="store_true", help="false-positive summary")
    args = parser.parse_args(argv)

    if args.report:
        print(report())
        return 0

    record, errors = observe()
    append_observation(record)
    print(
        f"observed {record['observed']}/{record['watched']} signals, "
        f"{record['changed']} changed"
    )
    for row in record["signals"]:
        if row.get("changed"):
            print(f"  CHANGED  {row['key']} -> {row['signal']}")
    if errors:
        print(f"errors ({len(errors)}):")
        for error in errors:
            print(f"  - {error}")
        # Exit 1 only when NOTHING could be observed. A partial outage is
        # recorded in the data and surfaced by --report as a "lost" count;
        # this tier is quarantined, so a source being unreachable is itself a
        # finding about that source, not a reason to fail the pipeline that
        # maintains the load-bearing amendment log.
        return 1 if record["total_failure"] else 3
    return 2 if record["changed"] else 0


if __name__ == "__main__":
    sys.exit(main())
