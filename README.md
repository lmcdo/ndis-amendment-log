# NDIS amendment detection log

A public, append-only record of every amendment to the ten core NDIS
legislative instruments — and, more to the point, **how long it took us to
notice each one**.

📊 **[View the log](https://lmcdo.github.io/ndis-amendment-log/)**

## Why this exists

Anyone can claim they keep up with regulatory change. The claim is almost
impossible to check, because the hard part is not accuracy on a single clause —
you can verify that yourself in ten seconds against the Federal Register — it is
**completeness**. Did we catch everything that moved last month? That question
is normally unanswerable from outside.

This log exists to make it answerable. It is designed to be **falsifiable**, and
it records the unflattering numbers as readily as the good ones.

## What that means concretely

**Latency is measured against the Register, not against ourselves.** The clock
starts when the Federal Register published the compilation (`registeredAt`), not
when we first saw it. A detection four days late is recorded as four days late.

**First sightings claim nothing.** The first time an instrument appears here it
is a `baseline` row. We were not watching when it was published, so no detection
is claimed. Only rows after that measure anything.

**Sweeps are recorded even when nothing changed** — see
[`data/sweeps.jsonl`](data/sweeps.jsonl), including failed ones. A log that
records only hits cannot distinguish *nothing changed* from *we stopped looking*,
which would make any completeness claim unfalsifiable. Gaps in the sweep history
are visible, permanently.

**Entries are hash-chained.** Each entry commits to the hash of the one before
it, so quietly backfilling a missed amendment breaks the chain:

```bash
python scripts/poll_amendments.py --verify
```

**It runs in CI, not on a laptop.** Commit timestamps and run records come from
GitHub, not from the author. A log self-published from a machine the author
controls proves considerably less.

## Checking it yourself

You are encouraged to. Every row links to its compilation on
legislation.gov.au — open it and compare the compilation number and effective
date. If a row is wrong, [open an issue](../../issues); corrections are appended,
never silently substituted.

The raw record is [`data/amendment-log.jsonl`](data/amendment-log.jsonl), one
JSON object per line.

## What is tracked

Ten instruments: the NDIS Act 2013 and the nine Rules and Guidelines made under
it that carry provider obligations — Code of Conduct, Provider Registration and
Practice Standards, Restrictive Practices and Behaviour Support, Incident
Management and Reportable Incidents, Complaints Management and Resolution,
Worker Screening, Quality Indicators, the transitional Rules, and Approved
Quality Auditors.

The page also flags any instrument with **commenced but unconsolidated
amendments** — law that is already in force which the current compilation does
not yet include. Reading the compilation alone misses these, which makes them
the most consequential rows on the page.

## Method

Metadata is read from the Federal Register's public OData API
(`api.prod.legislation.gov.au`), polled every six hours with requests spaced to
respect the crawl delay published in `robots.txt`. **Only metadata is stored** —
compilation numbers, dates, and identifiers. No legislative text is reproduced
here; the Register is the authoritative source and every row links back to it.

Selecting the current compilation is less obvious than it sounds: the Register
also publishes future-dated sunset rows, so the newest version record for an
instrument may be a repeal ten years out with no compilation attached. Selection
is therefore explicitly *in force, commenced, and carrying a register ID* — see
`fetch_latest_version` in [`scripts/poll_amendments.py`](scripts/poll_amendments.py).

## Running it

```bash
python scripts/poll_amendments.py           # poll, append, re-render
python scripts/poll_amendments.py --verify  # check chain integrity
python scripts/poll_amendments.py --render  # rebuild the page only
python -m pytest tests/ -q                  # 22 tests
```

No dependencies beyond the Python standard library (`pytest` for the tests).

## Sources beyond the Register — observed, not yet trusted

Providers are audited against guidance and priced against the NDIA schedule,
and both change outside the Federal Register. Those sources are now being
*observed* in `data/observations.jsonl`, but **nothing from them is published
as a detection** and none of it counts toward any figure on the log page.

They sit in quarantine because both available signals can lie, in opposite
directions:

- The Commission's `sitemap.xml` carries a per-URL `lastmod`, but it lies by
  **omission** — all four Core Module pages sit at an identical 2024-10-10
  across the July 2026 standards activity.
- NDIA pages return an HTTP `Last-Modified`, but it appears to lie by
  **commission** — within one hour of first observation the pricing page's
  value moved *backwards*, from 5 August to 31 July. Editorial change cannot
  move backwards, so at least some of that is cache behaviour.

`python scripts/observe_sources.py --report` shows how often each signal has
moved. Nothing gets promoted into the log until a signal is demonstrably quiet
and corroborated by a real content difference. A noisy row would cost more than
the coverage is worth.

**Access, stated openly.** Both hosts sit behind a WAF that drops non-browser
user-agents — a self-identifying agent, the standard `compatible;` bot format,
and Googlebot's own agent all hang and return nothing. Neither site publishes a
`robots.txt`. This poller therefore sends a conventional browser user-agent so
it is served at all, and identifies its operator honestly with the standard
`From:` header rather than hiding. Volume is a handful of requests, four times
a day. If you publish either site and would rather we didn't, the contact
address is in every request we make.

The Commission's host additionally refuses datacenter IPs, so its sitemap is
unreachable from CI. Those rounds are recorded as outages in the data, visible
in the `lost` column of the report — never as "unchanged".

## Limitations, stated plainly

- Detections cover **legislative instruments only**. Guidance and pricing are
  observed but quarantined, as above.
- Detection is bounded by the poll interval: worst case is about six hours plus
  the Register's own publication lag.
- The record began in August 2026. It cannot tell you anything about amendments
  before that date, and does not pretend to.

## Licence

Code is MIT. The log data is factual metadata about Commonwealth legislation;
the instruments themselves are © Commonwealth of Australia and are linked, not
reproduced.
