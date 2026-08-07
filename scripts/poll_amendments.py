"""Poll the Federal Register for NDIS instrument amendments and append to a
public, hash-chained, append-only detection log.

The point of this log is not to be useful to a provider. It is to be
*falsifiable*. Anyone can take any row and check it against
legislation.gov.au. Its value is a pure function of elapsed time and of the
fact that it records the unflattering numbers too:

  - Detection latency is computed against the Register's OWN ``registeredAt``
    timestamp, not against our first sighting. A four-day-late detection is
    recorded as four days late.
  - The first sighting of an instrument is a ``baseline`` entry and makes NO
    latency claim. Recording compilation 6 as "detected" months after the
    Register published it would be a lie.
  - Every sweep is recorded, including sweeps that found nothing. A log that
    only records hits cannot distinguish "nothing changed" from "we stopped
    looking", so "we never missed one" would be unfalsifiable.
  - Entries are hash-chained. Rewriting history breaks the chain, and
    ``--verify`` detects it.

Usage:
    python scripts/poll_amendments.py            # poll, append, re-render
    python scripts/poll_amendments.py --verify   # check chain integrity only
    python scripts/poll_amendments.py --render   # re-render the page only

Exit codes:
    0 = no changes
    1 = error
    2 = changes detected
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# The Register's timestamps carry no offset. Treating them as UTC+10 is the
# conservative reading: if it is wrong it OVERSTATES our latency, which is the
# safe direction to be wrong in for a number we publish about ourselves.
AEST = timezone(timedelta(hours=10))

API = "https://api.prod.legislation.gov.au"
USER_AGENT = (
    "ndis-amendment-log "
    "(+https://github.com/lmcdo/ndis-amendment-log) - scheduled metadata poll"
)
# robots.txt on www.legislation.gov.au sets Crawl-delay: 10. The API host is
# separate and unlisted, but we honour the spirit of it rather than the letter.
REQUEST_SPACING_SECONDS = 10.0
GENESIS_HASH = "0" * 64

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "amendment-log.jsonl"
SWEEP_PATH = ROOT / "data" / "sweeps.jsonl"
PAGE_PATH = ROOT / "docs" / "index.html"

# Tier A corpus. Every id verified to resolve against /v1/titles on 2026-08-07.
TRACKED: list[dict[str, str]] = [
    {"id": "C2013A00020", "short": "NDIS Act 2013"},
    {"id": "F2018L00629", "short": "Code of Conduct Rules"},
    {"id": "F2018L00631", "short": "Provider Registration & Practice Standards Rules"},
    {"id": "F2018L00632", "short": "Restrictive Practices & Behaviour Support Rules"},
    {"id": "F2018L00633", "short": "Incident Management & Reportable Incidents Rules"},
    {"id": "F2018L00634", "short": "Complaints Management & Resolution Rules"},
    {"id": "F2018L00887", "short": "Practice Standards — Worker Screening Rules"},
    {"id": "F2018N00041", "short": "Quality Indicators Guidelines"},
    {"id": "F2024L01257", "short": "Getting the NDIS Back on Track Transitional Rules"},
    {"id": "F2025L01383", "short": "Approved Quality Auditors Rules"},
]


def _get(path: str) -> Any:
    """GET a JSON document from the Register API.

    Raises:
        urllib.error.URLError: on transport failure.
        json.JSONDecodeError: if the body is not JSON. The site's SPA shell
            returns HTML for unknown routes, so this is a real failure mode
            and must not be mistaken for "no data".
    """
    req = urllib.request.Request(
        API + path,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_latest_version(title_id: str) -> dict[str, Any] | None:
    """Return the version currently IN FORCE for a title, or None.

    Not simply the newest record. The Register also publishes FUTURE-dated
    sunset/repeal rows — e.g. F2024L01257 carries a row with
    ``start=2034-10-01``, ``status=Repealed`` and a null ``registerId``, being
    its ten-year sunset. Ordering by ``start desc`` and taking the first row
    therefore returns a repeal that has not happened, with no compilation
    attached. Observed on 2 of 10 tracked instruments on 2026-08-07.

    A compliance product making that mistake would silently report an in-force
    instrument as having no current compilation, so the selection is explicit:
    newest row that is in force, has actually commenced, and carries a
    register id.
    """
    query = urllib.parse.urlencode(
        {
            "$filter": f"titleId eq '{title_id}'",
            "$orderby": "start desc",
            "$top": "10",
        }
    )
    payload = _get(f"/v1/versions?{query}")
    values = payload.get("value") if isinstance(payload, dict) else payload
    if not values:
        return None

    now = datetime.now(AEST)
    for version in values:
        if not version.get("registerId"):
            continue  # future sunset/repeal placeholder
        if version.get("status") != "InForce":
            continue
        start = version.get("start")
        if start:
            try:
                if datetime.fromisoformat(start[:19]).replace(tzinfo=AEST) > now:
                    continue  # commences in the future; not the operative text yet
            except ValueError:
                pass
        return version
    return None


def fetch_title(title_id: str) -> dict[str, Any]:
    """Return title metadata (name, status, unincorporated-amendment flag)."""
    return _get(f"/v1/titles('{title_id}')")


def _hash_entry(entry: dict[str, Any]) -> str:
    """Hash an entry over its canonical form, excluding the hash field itself."""
    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_log() -> list[dict[str, Any]]:
    """Read every entry from the append-only log."""
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_sweeps() -> list[dict[str, Any]]:
    """Read the sweep history, including sweeps that found nothing."""
    if not SWEEP_PATH.exists():
        return []
    with SWEEP_PATH.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def verify_chain(entries: list[dict[str, Any]]) -> list[str]:
    """Verify hash-chain integrity.

    Returns:
        A list of human-readable problems. Empty means the chain is intact.
    """
    problems: list[str] = []
    expected_prev = GENESIS_HASH
    for index, entry in enumerate(entries):
        if entry.get("prev_hash") != expected_prev:
            problems.append(
                f"entry {index} (seq {entry.get('seq')}): prev_hash does not "
                f"match the preceding entry hash — the log has been rewritten "
                f"or reordered"
            )
        if entry.get("entry_hash") != _hash_entry(entry):
            problems.append(
                f"entry {index} (seq {entry.get('seq')}): contents do not match "
                f"entry_hash — this entry was edited after it was written"
            )
        if entry.get("seq") != index + 1:
            problems.append(
                f"entry {index}: seq is {entry.get('seq')}, expected {index + 1}"
            )
        expected_prev = entry.get("entry_hash", "")
    return problems


def append_entries(new_entries: list[dict[str, Any]]) -> None:
    """Append entries to the log, chaining each to the last written hash."""
    existing = read_log()
    prev_hash = existing[-1]["entry_hash"] if existing else GENESIS_HASH
    seq = len(existing)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        for entry in new_entries:
            seq += 1
            entry["seq"] = seq
            entry["prev_hash"] = prev_hash
            entry["entry_hash"] = _hash_entry(entry)
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            prev_hash = entry["entry_hash"]


def append_sweep(sweep: dict[str, Any]) -> None:
    """Record that a sweep happened, whether or not it found anything."""
    SWEEP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SWEEP_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(sweep, sort_keys=True) + "\n")


def latency_hours(registered_at: str | None, detected_at: datetime) -> float | None:
    """Hours between the Register publishing a compilation and us seeing it.

    Args:
        registered_at: The Register's own ``registeredAt``, a naive timestamp
            in Canberra local time.
        detected_at: When this poller observed the change.

    Returns:
        Hours elapsed, or None if the input is missing or unparseable.
    """
    if not registered_at:
        return None
    try:
        published = datetime.fromisoformat(registered_at.split(".")[0])
    except ValueError:
        return None
    published = published.replace(tzinfo=AEST)
    delta = detected_at.astimezone(timezone.utc) - published.astimezone(timezone.utc)
    return round(delta.total_seconds() / 3600.0, 1)



def poll() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Poll every tracked instrument.

    Returns:
        A tuple of (new log entries, sweep record).
    """
    known: dict[str, dict[str, Any]] = {}
    for entry in read_log():
        known[entry["title_id"]] = entry

    detected_at = datetime.now(timezone.utc).astimezone()
    new_entries: list[dict[str, Any]] = []
    errors: list[str] = []
    checked = 0

    for index, tracked in enumerate(TRACKED):
        title_id = tracked["id"]
        if index:
            time.sleep(REQUEST_SPACING_SECONDS)
        try:
            version = fetch_latest_version(title_id)
            title = fetch_title(title_id)
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            # A fetch failure is recorded, never swallowed. An unreported
            # outage is indistinguishable from "nothing changed".
            errors.append(f"{title_id}: {type(exc).__name__}: {exc}")
            continue

        checked += 1
        if not version:
            errors.append(f"{title_id}: no version records returned")
            continue

        register_id = version.get("registerId")
        previous = known.get(title_id)
        if previous and previous.get("register_id") == register_id:
            continue

        is_baseline = previous is None
        entry: dict[str, Any] = {
            "type": "baseline" if is_baseline else "amendment",
            "title_id": title_id,
            "title_name": title.get("name"),
            "short_name": tracked["short"],
            "register_id": register_id,
            "compilation_number": version.get("compilationNumber"),
            "effective_from": (version.get("start") or "")[:10],
            "registered_at": version.get("registeredAt"),
            "detected_at": detected_at.isoformat(),
            "previous_register_id": previous.get("register_id") if previous else None,
            "has_commenced_unincorporated_amendments": title.get(
                "hasCommencedUnincorporatedAmendments"
            ),
            "source_url": f"https://www.legislation.gov.au/{register_id}",
        }
        if is_baseline:
            # No latency claim is possible: we were not watching when this was
            # published. Claiming one would be the exact dishonesty this log
            # exists to rule out.
            entry["detection_latency_hours"] = None
            entry["latency_note"] = (
                "First observation of this instrument. Not a detection — no "
                "latency is claimed."
            )
        else:
            entry["detection_latency_hours"] = latency_hours(
                version.get("registeredAt"), detected_at
            )
        new_entries.append(entry)

    sweep = {
        "swept_at": detected_at.isoformat(),
        "instruments_tracked": len(TRACKED),
        "instruments_checked": checked,
        "changes_found": len(new_entries),
        "errors": errors,
    }
    return new_entries, sweep


def _esc(value: Any) -> str:
    """Escape a value for HTML text content."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render(entries: list[dict[str, Any]], sweeps: list[dict[str, Any]]) -> str:
    """Render the public log page."""
    detections = [e for e in entries if e["type"] == "amendment"]
    latencies = [
        e["detection_latency_hours"]
        for e in detections
        if e.get("detection_latency_hours") is not None
    ]
    worst = max(latencies) if latencies else None
    median = sorted(latencies)[len(latencies) // 2] if latencies else None
    error_sweeps = sum(1 for s in sweeps if s.get("errors"))
    first_sweep = sweeps[0]["swept_at"][:10] if sweeps else "—"

    # Instruments whose law has changed but whose consolidated text has not yet
    # caught up. Reading the current compilation will NOT show these amendments,
    # which makes them the most consequential thing on the page.
    latest_by_title: dict[str, dict[str, Any]] = {}
    for entry in entries:
        latest_by_title[entry["title_id"]] = entry
    unincorporated = [
        e
        for e in latest_by_title.values()
        if e.get("has_commenced_unincorporated_amendments")
    ]
    if unincorporated:
        unincorp_block = (
            '<h2>Commenced but not yet consolidated</h2><div class="alert">'
            "<b>These instruments have amendments in force that the current "
            "compilation does not yet include.</b> Reading the compilation "
            "alone will miss them.<ul>"
            + "".join(
                f'<li>{_esc(e["short_name"])} '
                f'(<span class="m">{_esc(e["title_id"])}</span>) — current '
                f'compilation <span class="m">{_esc(e["register_id"])}</span></li>'
                for e in unincorporated
            )
            + "</ul></div>"
        )
    else:
        unincorp_block = (
            '<h2>Commenced but not yet consolidated</h2><div class="note">'
            "No tracked instrument currently has commenced amendments missing "
            "from its compilation. This is checked on every sweep — when it is "
            "not empty, reading the compilation alone would give the wrong "
            "answer.</div>"
        )

    rows: list[str] = []
    for entry in reversed(entries):
        latency = entry.get("detection_latency_hours")
        if entry["type"] == "baseline":
            lat_cell = '<span class="muted">baseline — no claim</span>'
        elif latency is None:
            lat_cell = '<span class="muted">unknown</span>'
        else:
            cls = "good" if latency <= 24 else ("warn" if latency <= 72 else "bad")
            lat_cell = f'<span class="{cls}">{latency:.1f} h</span>'
        prev = entry.get("previous_register_id")
        rows.append(
            "<tr>"
            f'<td class="m">{_esc(entry["detected_at"][:10])}</td>'
            f'<td>{_esc(entry["short_name"])}'
            f'<div class="sub m">{_esc(entry["title_id"])}</div></td>'
            f'<td class="m"><a href="{_esc(entry["source_url"])}">'
            f'{_esc(entry["register_id"])}</a>'
            f'<div class="sub">comp. {_esc(entry["compilation_number"])}'
            + (f" — was {_esc(prev)}" if prev else "")
            + "</div></td>"
            f'<td class="m">{_esc(entry["effective_from"])}</td>'
            f'<td class="m">{_esc((entry.get("registered_at") or "")[:10])}</td>'
            f"<td>{lat_cell}</td>"
            "</tr>"
        )

    body = "".join(rows) or '<tr><td colspan="6">No entries yet.</td></tr>'
    median_txt = f"{median:.1f} h" if median is not None else "—"
    worst_txt = f"{worst:.1f} h" if worst is not None else "—"
    median_note = "no detections yet" if median is None else "since first sweep"

    return f"""<title>NDIS amendment detection log</title>
<style>
:root{{--bg:#f6f7fb;--fg:#171a2b;--mut:#767b94;--rule:#dcdfeb;--surf:#fff;
--acc:#3b3f8f;--good:#0d6153;--warn:#8a5206;--bad:#9c2a3c;
--m:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--s:Georgia,"Iowan Old Style",serif}}
@media(prefers-color-scheme:dark){{:root{{--bg:#101220;--fg:#e9eaf2;--mut:#7e839c;
--rule:#2b2f43;--surf:#181b2b;--acc:#9298e8;--good:#6fc0ae;--warn:#e0ab63;--bad:#e8909e}}}}
:root[data-theme=dark]{{--bg:#101220;--fg:#e9eaf2;--mut:#7e839c;--rule:#2b2f43;
--surf:#181b2b;--acc:#9298e8;--good:#6fc0ae;--warn:#e0ab63;--bad:#e8909e}}
:root[data-theme=light]{{--bg:#f6f7fb;--fg:#171a2b;--mut:#767b94;--rule:#dcdfeb;
--surf:#fff;--acc:#3b3f8f;--good:#0d6153;--warn:#8a5206;--bad:#9c2a3c}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
.w{{max-width:1080px;margin:0 auto;padding:32px 22px 70px}}
h1{{font-family:var(--s);font-weight:400;font-size:clamp(25px,3.5vw,36px);
margin:0 0 8px;letter-spacing:-.01em;text-wrap:balance}}
.lede{{color:var(--mut);max-width:66ch;margin:0 0 26px}}
.stats{{display:grid;gap:13px;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));
margin-bottom:28px}}
.stat{{background:var(--surf);border:1px solid var(--rule);border-radius:4px;padding:14px 15px}}
.stat .k{{font-family:var(--m);font-size:10px;letter-spacing:.1em;text-transform:uppercase;
color:var(--mut);font-weight:600;margin-bottom:6px}}
.stat .v{{font-family:var(--m);font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}}
.stat .n{{font-size:12px;color:var(--mut);margin-top:3px}}
.scroll{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;background:var(--surf);
border:1px solid var(--rule);border-radius:4px;overflow:hidden}}
th{{font-family:var(--m);font-size:10px;letter-spacing:.09em;text-transform:uppercase;
color:var(--mut);text-align:left;padding:10px 13px;border-bottom:1px solid var(--rule);
white-space:nowrap}}
td{{padding:11px 13px;border-bottom:1px solid var(--rule);vertical-align:top}}
tr:last-child td{{border-bottom:none}}
.m{{font-family:var(--m);font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}}
.sub{{font-size:11px;color:var(--mut);margin-top:2px}}
a{{color:var(--acc)}}
.good{{color:var(--good);font-family:var(--m);font-weight:600}}
.warn{{color:var(--warn);font-family:var(--m);font-weight:600}}
.bad{{color:var(--bad);font-family:var(--m);font-weight:600}}
.muted{{color:var(--mut);font-size:12px}}
h2{{font-family:var(--m);font-size:11px;letter-spacing:.11em;text-transform:uppercase;
color:var(--mut);margin:34px 0 12px}}
.note{{background:var(--surf);border:1px solid var(--rule);border-left:3px solid var(--acc);
border-radius:3px;padding:15px 17px;color:var(--mut);font-size:13.5px;max-width:78ch}}
.note b{{color:var(--fg)}}
.alert{{background:var(--surf);border:1px solid var(--rule);border-left:3px solid var(--warn);
border-radius:3px;padding:15px 17px;color:var(--mut);font-size:13.5px;max-width:78ch}}
.alert b{{color:var(--warn)}}
.alert ul{{margin:9px 0 0;padding-left:19px}}
.alert li{{margin:3px 0}}
code{{font-family:var(--m);font-size:12px}}
</style>
<div class="w">
<h1>NDIS amendment detection log</h1>
<p class="lede">Every amendment to the {len(TRACKED)} tracked NDIS instruments, and how
long we took to notice. Latency is measured against the Federal Register's own
publication timestamp, not against our first sighting. Check any row against
legislation.gov.au — that is the point of publishing it.</p>

<div class="stats">
  <div class="stat"><div class="k">Instruments tracked</div>
    <div class="v">{len(TRACKED)}</div><div class="n">Tier A corpus</div></div>
  <div class="stat"><div class="k">Amendments detected</div>
    <div class="v">{len(detections)}</div><div class="n">excludes baselines</div></div>
  <div class="stat"><div class="k">Median latency</div>
    <div class="v">{median_txt}</div><div class="n">{median_note}</div></div>
  <div class="stat"><div class="k">Worst latency</div>
    <div class="v">{worst_txt}</div><div class="n">the number that matters</div></div>
  <div class="stat"><div class="k">Sweeps run</div>
    <div class="v">{len(sweeps)}</div>
    <div class="n">{error_sweeps} with errors · since {_esc(first_sweep)}</div></div>
</div>

<div class="scroll"><table>
<thead><tr><th>Detected</th><th>Instrument</th><th>Compilation</th>
<th>Effective</th><th>Registered</th><th>Latency</th></tr></thead>
<tbody>{body}</tbody>
</table></div>

{unincorp_block}

<h2>How to check this</h2>
<div class="note">
<b>Every row is falsifiable.</b> Click a compilation ID to open it on the Federal
Register and compare the compilation number and effective date.
<br><br>
<b>Baseline rows make no latency claim.</b> The first time an instrument is seen we
were not watching when it was published, so no detection is claimed. Only rows after
that measure anything.
<br><br>
<b>Sweeps are recorded even when nothing changed</b>, in <code>data/sweeps.jsonl</code>,
including failures. A log that records only hits cannot tell you the difference between
"nothing changed" and "we stopped looking", which would make any completeness claim
unfalsifiable.
<br><br>
<b>The log is hash-chained and append-only.</b> Each entry commits to the previous one,
so silently backfilling a missed amendment breaks the chain. Run
<code>python scripts/poll_amendments.py --verify</code> to check it, and read the git
history — commit timestamps come from the CI runner, not from us.
</div>
</div>
"""


def write_page(entries: list[dict[str, Any]], sweeps: list[dict[str, Any]]) -> None:
    """Write the rendered page to disk."""
    PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAGE_PATH.write_text(render(entries, sweeps), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    """Entry point. See module docstring for exit codes."""
    parser = argparse.ArgumentParser(description="NDIS amendment detection log")
    parser.add_argument("--verify", action="store_true", help="check chain integrity")
    parser.add_argument("--render", action="store_true", help="re-render page only")
    args = parser.parse_args(argv)

    if args.verify:
        problems = verify_chain(read_log())
        if problems:
            print("CHAIN BROKEN:")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(f"chain intact — {len(read_log())} entries")
        return 0

    if args.render:
        write_page(read_log(), read_sweeps())
        print(f"rendered {PAGE_PATH}")
        return 0

    problems = verify_chain(read_log())
    if problems:
        print("Refusing to append to a broken chain:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    new_entries, sweep = poll()
    append_sweep(sweep)
    if new_entries:
        append_entries(new_entries)
        for entry in new_entries:
            kind = "BASELINE " if entry["type"] == "baseline" else "AMENDMENT"
            print(
                f"{kind} {entry['title_id']} -> {entry['register_id']} "
                f"(comp {entry['compilation_number']}, eff {entry['effective_from']})"
            )
    else:
        print("no changes")

    write_page(read_log(), read_sweeps())

    if sweep["errors"]:
        print(f"errors on {len(sweep['errors'])} instrument(s):")
        for error in sweep["errors"]:
            print(f"  - {error}")
        return 1
    return 2 if new_entries else 0


if __name__ == "__main__":
    sys.exit(main())
