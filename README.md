# Ocean Intel

A flat-file motivated-seller intelligence pipeline for **Ocean County, NJ**. Joins public-records signals (parcel master, sheriff sales, tax board, surrogate, NJ courts, official records) on block-lot-qualifier and surfaces parcels where multiple distinct distress patterns stack on the same property. Output is a single static dashboard.

> **Status:** build in progress. See `RECON.md` for the source map and the 6-pattern stack.

---

## How it works

1. **Scrapers** in `scrapers/` fetch raw public records. Each writes JSONL to `data/raw/`. One JSONL file per discovered doc type.
2. **`pipeline/build_leads.py`** loads everything, joins on NJ block-lot-qualifier (or fuzzy address / owner-name when no parcel ID is exposed), runs each parcel through the 6-pattern stack (`jfc`, `tax`, `estate`, `code`, `lien`, `transfer`), assigns a tier from stack depth, and writes a single `data/leads.json`.
3. **`index.html`** is a single-file vanilla-JS dashboard reading `data/leads.json`. One `matches(lead)` function powers both filter counts and the rendered table — no Two-Truths drift.
4. **`pipeline/refresh.py`** is the daily-refresh harness — runs all scrapers in dependency order, regenerates `leads.json`, stages diff, commits + pushes when `--push`. Designed for OpenClaw cron.

The two non-negotiables: **tier comes from how many distinct patterns stack** (never from raw score sum), and **filter counts are derived from the same `matches(lead)` function that builds the visible table**.

---

## Layout

```
ocean-intel/
├── pipeline/
│   ├── build_leads.py            # joins + scoring + leads.json
│   └── refresh.py                # daily refresh harness
├── scrapers/                     # one scraper per source — fetch only
├── scripts/
│   └── daily_refresh.xml         # Windows Task Scheduler config
├── data/
│   ├── raw/                      # gitignored — raw JSONL
│   ├── leads.json                # the deliverable; committed
│   └── leads.previous.json       # last run, for new-in-24h diff
├── docs/                         # screenshots
├── index.html                    # single-file dashboard
├── methodology.html              # data sources, scoring, limitations
├── HEARTBEAT.json                # refresh heartbeat (per-source last_success)
├── OPERATIONS.md                 # OpenClaw integration spec
├── RECON.md                      # source inventory + doc type taxonomy
└── README.md
```

---

## Local dev

Requires Python 3.12. Quickstart:

```powershell
# Install deps
py -3.12 -m pip install -r requirements.txt

# Recon (probe a single source)
py -3.12 scrapers\<source>.py --limit 50 --reset

# Daily refresh, dry run (no push)
py -3.12 pipeline\refresh.py --dry-run

# Full daily cycle with push
py -3.12 pipeline\refresh.py --push

# Serve the dashboard locally
py -3.12 -m http.server 8765 --bind 127.0.0.1
# open http://127.0.0.1:8765/
```

---

## Why daily-live data

Competitive moat against PropStream / DealMachine / PropertyRadar is freshness — vendors buy bulk monthly extracts; this stack hits source the day filings hit the docket. Daily refresh is the product, not a nice-to-have.

---

⚡ — Built by Jarvis (Just Jarvis LLC) for Quentin Flores. Operator-first.
