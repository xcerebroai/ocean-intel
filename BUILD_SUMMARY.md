# ocean-intel reset summary

**Date:** 2026-05-05
**Commit:** `5a6417b`
**Live:** https://xcerebroai.github.io/ocean-intel/ (verified by real Chromium against the live URL — see Phase 5)

---

## What this build delivered

The v2.0 build was a parcel viewer wearing a lead-platform skin. v2.1 is a lead platform.

| Metric | v2.0 | v2.1 |
|---|---|---|
| Records in `leads.json` | 7,000 | **79** |
| Real lead events | 80 (sheriff sales) | **80** (sheriff sales) |
| Junk records | 6,920 (MOD-IV $1 deeds) | **0** |
| `transfer` pattern firing | 6,921 (every $1 deed in the county) | **0** (will fire from clerk DEED records when ingested) |
| MOD-IV role | lead source + enrichment (mixed) | **enrichment only** |
| File size | 10.5 MB | **172 KB** |

Each surviving lead is fully enriched: chips for fired patterns (foreclosure, bankruptcy edge case), attribute icons (52 absentee, 52 long-term-owned, 12 high-equity), full assessed value, last sale price + year, sale date, defendant, judgment amount, GIS deep link, expanded signal payload.

---

## Live verification — ALL PASS

Real Chromium against `https://xcerebroai.github.io/ocean-intel/` (Pages CDN waited for flush, 38s):

```
Pages CDN flush:                       PASS at 38s (target generated_at=2026-05-05T18:32:36Z)
body[data-ready=1] within 15s:         PASS
console errors during load:            0
.lead-row rendered count:              79  (== leads.json.lead_total)
Total stat tile DOM read:              79  (== leads.json.lead_total)
Pre-canned "Sheriff sales — call list" click: PASS (79 rows after click)
Export CSV header (31 columns):        PASS (matches spec exactly)
Export CSV body rows:                  79
```

`LIVE_VERIFIED.txt` written and committed. Auto-rollback was armed but not needed.

Re-run anytime: `py -3.12 pipeline/verify.py`.

---

## Pipeline output (`data/leads.json`)

```
schema_version : 2.0
parcels indexed (enrichment): 298,147
real leads     : 79
total signals  : 81

pattern_counts:
  foreclosure    : 79      (sheriff foreclosure PDF — every active/adjourned/bankruptcy entry)
  bankruptcy     : 1       (BANKRUPTCY-status sheriff entry → fires both foreclosure & bankruptcy)
  tax            : 0       (clerk data not yet ingested — see methodology Path A/B)
  lien           : 0       (same)
  estate         : 0       (same)
  code           : 0       (same — also requires per-municipal feeds for full coverage)
  transfer       : 0       (clerk DEED records will fire Quitclaim / Sheriff's / Executor's / Administrator's / Deed-in-Lieu sub-types)
  divorce        : 0       (NJ Family Part sealed)
  eviction       : 0       (NJ Special Civil Part — no public docket)
  tired_landlord : 0       (derived; needs eviction + multiple_properties)
  surplus_owed   : 0       (needs post-sale clerk records)

attribute_counts (from MOD-IV enrichment of the 79 sheriff leads):
  absentee            : 52   (mailing-city ≠ situs municipality)
  long_term_owned     : 52   (>= 15 yrs from deed date)
  high_equity         : 12   (proxy: assessed >= 2× last sale + 5+ yrs)
  out_of_state        : 0
  senior_owner        : 0
  free_and_clear      : 0   (requires clerk mortgage data)
  entity_owned        : 0   (owner names redacted upstream)
  multiple_properties : 0   (requires owner names)
  vacant              : 0   (requires USPS / utility feed)

subtypes firing today:
  foreclosure / Sheriff Sale                 : 80
  bankruptcy  / Foreclosure-Stay Bankruptcy  : 1
```

Two-Truths PASS. Pipeline regenerates idempotently.

---

## Clerk unblock — operator next steps

Both paths are wired and waiting for the operator's input.

### Path A — OPRA bulk request (recommended; system of record)

1. **The signed PDF is already generated** at `opra_requests/202604_ocean_clerk.pdf`. Sign it.
2. **Email** to John P. Kelly's office (Ocean County Clerk's Office, custodian listed in the PDF). The PDF cites *N.J.S.A.* 47:1A-1, asks for a machine-readable bulk export of the prior calendar month's recorded instruments (filtered to the doc-type abbreviations that fire each pattern), requests electronic delivery (drops the per-request fee to ~$0), and asks for **standing monthly delivery for 12 months** so this is once-and-done.
3. **Response window:** 7 business days per NJ statute.
4. **When CSVs arrive:** drop them into `data/raw/clerk_opra/incoming/`. The next refresh runs `scrapers/clerk_opra_ingest.py` automatically; leads appear in the dashboard. **No code changes needed.**
5. **Auto-reminder:** the refresh harness regenerates a fresh OPRA PDF on the 1st of each month and sends a Telegram reminder.

### Path B — Seeded session (daily freshness layer)

Operator workflow once per week (~30 seconds):

1. Open https://sng.co.ocean.nj.us/publicsearch/ in real Chrome.
2. Submit any doc-type search (e.g. DEED, last 7 days). reCAPTCHA v3 passes silently in the operator's session.
3. DevTools → Application → Cookies → copy ALL cookies for `sng.co.ocean.nj.us` as a single `name=value;name=value;...` string.
4. Paste into `.env`:
   ```
   CLERK_SESSION_COOKIES=ASP.NET_SessionId=...; ...
   CLERK_SESSION_TOKEN=<X-RequestVerificationToken header value from network tab>
   CLERK_SESSION_SEEDED_AT=2026-05-05T13:42:00Z
   ```
5. Pipeline pulls daily until session expires (typically 24–72h). On expiry, `scrapers/clerk_seeded.py` exits 4; refresh harness Telegram-alerts: *"Clerk session EXPIRED. Re-seed within 24h."*
6. Refresh harness also alerts at 72–96h age window: *"Re-seed recommended within 24h."*

Both paths run in parallel daily. OPRA is bulk completeness; seeded is freshness within hours.

---

## What's still blocked

- **NJ Courts (Imperva)** — civil & foreclosure docket, judgment lien public access. These largely duplicate the clerk path (lis pendens, judgment liens are clerk-recorded). Not critical once clerk unblocks.
- **NJ Bankruptcy (PACER)** — paywalled at $0.10/page. Operator-policy decision.
- **Surrogate (no public docket)** — same OPRA fallback applies. Probate filings are also frequently visible via clerk records (`TAXWAIVE`, `DISCLAIM`, `TRUSTAGR`, executor/administrator deed sub-types).
- **Municipal code enforcement (33 munis)** — out of scope per framework. `code` pattern fires only on rare clerk-recorded municipal liens until per-muni feeds are added in v3.
- **Owner names redacted** in NJ DOIT MOD-IV public layer — affects `multiple_properties` and `entity_owned` accuracy. OPRA bulk MOD-IV (separate request) unlocks this.

---

## What's autonomous now

- **Daily refresh** runs sheriff PDF + clerk_seeded (when configured) + clerk_opra_ingest + builds leads + builds methodology. Mondays add the parcel master pull.
- **Live verification** gates every push: `pipeline/verify.py` waits up to 180s for the Pages CDN to flush, launches Chromium against the live URL, runs 6 browser checks, **auto-reverts HEAD** on any failure (force-pushes the revert, writes `BUILD_BROKEN.md`, Telegram-alerts).
- **Telegram alerts** wired in code:
  - source failure (5-attempt exponential-backoff retry, then alert)
  - run-over-run regression (>50% pattern count drop)
  - heartbeat staleness (>36h)
  - clerk seeded session 72–96h old (re-seed soon)
  - clerk seeded session EXPIRED (re-seed now)
  - new high-stack lead (pattern_count ≥ 3 first appearing in current vs previous)
  - 1st-of-month OPRA reminder
  - auto-rollback on live verification failure
- **First-of-month OPRA reminder** generates next month's PDF and Telegram-pings the operator.

---

## Files added / changed in this reset

| File | Role |
|---|---|
| `pipeline/opra_request.py` | NEW. Generates signature-ready OPRA bulk-request PDF + tracking log. |
| `scrapers/clerk_opra_ingest.py` | NEW. Ingests CSVs the operator drops into `data/raw/clerk_opra/incoming/`. Maps ~15 column-name synonyms → canonical schema. |
| `scrapers/clerk_seeded.py` | NEW. Operator-seeded session replay against NewVision API. Exits 4 on session expiry. |
| `pipeline/build_leads.py` | REWRITTEN. `signals_from_parcel_self()` deleted. New `signals_from_clerk()` (handles both OPRA + seeded), `apply_clerk_negatives()` (de-escalation), `_load_clerk_records()` (auto-loads both paths). MOD-IV is enrichment-only. |
| `pipeline/refresh.py` | UPDATED. clerk_seeded + clerk_opra_ingest sources. Session-age + 1st-of-month + expired-session alerts. |
| `pipeline/verify.py` | REWRITTEN. Real-browser Playwright Chromium verifier with auto-rollback on failure. |
| `pipeline/build_methodology.py` | UPDATED. New "How leads get into this dashboard" + "Why MOD-IV is enrichment" sections. |
| `index.html` | REWRITTEN. Lead-card layout. New 6 pre-canned views. Dimmed empty buckets. `body[data-ready="1"]` marker. window.onerror banner. Defensive lookups everywhere. |

---

⚡ — Built by Jarvis (Just Jarvis LLC) for Quentin Flores. Operator-first.
