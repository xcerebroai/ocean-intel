# ocean-intel build summary

**Build date:** 2026-05-05
**Commits:** `988b412` (scaffold) → `4f88a27` (Phase 0–5) → `c45c51a` (scheduler fix)
**Repo:** https://github.com/xcerebroai/ocean-intel
**Dashboard:** https://xcerebroai.github.io/ocean-intel/ (GitHub Pages, deploying)

---

## Sources

| Status | Count | Sources |
|---|---|---|
| Recon'd | 14 | clerk_official_records, clerk_records_forms, tax_board (landing + search), nj_courts_find_case, nj_judgment_search, nj_judgment_lien_pa, nj_foreclosure_info, sheriff_foreclosures, sheriff_civilview, surrogate, property_alert, ocean_gis_portal, ocean_arcgis_services |
| Scraping live | 4 | `nj_modiv_parcels`, `sheriff_foreclosures`, `civilview_sales`, `clerk_metadata` |
| Empty (kept for resilience) | 1 | `civilview_sales` (Ocean countyId=85 returned 0 rows; works for other NJ counties) |
| Blocked / documented gap | 8 | Ocean Clerk search (reCAPTCHA v3), NJ Judgment Search (Imperva), NJ Judgment Lien PA (Imperva), NJ Courts Find a Case (linked systems Imperva-protected), Tax Board search (reCAPTCHA — intentionally skipped, redundant with MOD-IV), Property Alert (HTTP 500 upstream), Surrogate (no public docket UI), Municipal code (33 separate sites — out of scope) |

Each blocker has a workaround documented in [`scrapers/README.md`](scrapers/README.md) and the recon detail in [`RECON.md`](RECON.md). OPRA fallback is the path for the clerk-search and judgment data.

---

## Records ingested (raw)

| Source | Records |
|---|---|
| `nj_modiv_parcels.jsonl` | **298,147** Ocean parcels (full county pull) |
| `sheriff_foreclosures_writ_of_sale.jsonl` | **77** SEQ rows from this week's sheriff sales PDF |
| `civilview_sales_sheriff_sale_listing.jsonl` | 0 (empty for Ocean) |
| `clerk_metadata_heartbeat.jsonl` | 1 daily heartbeat record (168 doc-types catalogued, 35 towns) |

---

## Pipeline output (`data/leads.json`)

```
Total leads: 6,994
  Hot   : 0
  Warm  : 0
  Active: 6,994

Pattern attach rates:
  jfc      : 73 leads      (sheriff foreclosure PDF)
  tax      : 0 leads        (clerk-search blocked — needs OPRA / unblock)
  estate   : 0 leads        (clerk-search blocked)
  code     : 0 leads        (expected sparse — municipal in NJ; clerk-search blocked)
  lien     : 0 leads        (clerk-search blocked)
  transfer : 6,921 leads    (MOD-IV nominal-consideration deeds, last 3yr)

Source signal attach:
  sheriff_foreclosures : 75 signals on 32 parcels (32/41 with valid block+lot matched)
  nj_modiv_parcels     : 6,921 internal-derived signals (DEED_NOMINAL)

Two-Truths check: PASS
File size: 10.5 MB / 50 MB cap (21% utilization)
```

The Hot/Warm tiers are 0 today because four of the six pattern sources (clerk
record types) are blocked behind reCAPTCHA v3 at the Ocean Clerk and Imperva at
the NJ Courts portal. Stack-depth is **mathematically correct** but currently
maxes at depth=1 (Active tier) for everything except orphan jfc signals. As
soon as the clerk-search OPRA fallback or human-in-loop pulls land, the same
parcels currently flagged `transfer` (nominal-consideration deed) or `jfc`
(sheriff sale) will accumulate `tax` / `estate` / `lien` patterns and rise to
Warm or Hot. The architecture is intentionally **ready to absorb** that data
without any pipeline changes.

---

## Two-Truths check: PASS

`pipeline/build_leads.py` recomputes `tier_counts` and `pattern_counts` from
the records list before writing `leads.json` and raises if header values
drift. Header in shipped file:

```json
"tier_counts": { "hot": 0, "warm": 0, "active": 6994 },
"pattern_counts": { "jfc": 73, "tax": 0, "estate": 0, "code": 0, "lien": 0, "transfer": 6921 }
```

Re-derived from `records[]` independently:

```
{'active': 6994}
{'jfc': 73, 'transfer': 6921}
```

Match. Dashboard uses the same `matches()` function for both filter counts
and rendered table, so the invariant is enforced on both ends of the pipeline.

---

## Daily refresh

- **Harness:** `pipeline/refresh.py` runs all 4 scrapers in dependency order, then `build_leads.py`. Per-source failure does not abort the run.
- **Schedule:** Windows Task Scheduler `\ocean-intel-refresh`, 4:00 AM local daily, **registered**.
  - Verify: `schtasks /query /tn "ocean-intel-refresh" /fo list`
  - Next run: 2026-05-06 04:00:00 (per `schtasks /query`)
- **Heartbeat:** `HEARTBEAT.json` tracks `last_success_timestamp`, per-source `last_success`, and `failed_sources[]`. Stale > 36h → alert.
- **Push:** `pipeline/refresh.py --push` commits `data/leads.json` + `data/leads.previous.json` + `HEARTBEAT.json` and pushes to `origin/main`.

---

## GitHub Pages: deploying

- Pages **enabled** at `https://xcerebroai.github.io/ocean-intel/` via `gh api -X POST repos/xcerebroai/ocean-intel/pages` (`status: "building"` at build-summary write time).
- Initial deploy in progress (build queued, ~60s typical).
- Source: `main` branch, root path.

---

## Open items requiring human follow-up

1. **OPRA request to Ocean County Clerk John P. Kelly's office** for monthly bulk extract of:
   - Recorded deeds (DEED, BILLSALE, CONTSALE) — for the `transfer` pattern with proper buyer/seller names
   - Lis pendens (LISPEN, NOTLIS, NTCELIS) — primary `jfc` source not derivable from the sheriff PDF
   - Tax sale certificates (TSC, MTSC) and federal/state tax liens (FEDLIEN, INSTLIEN) — primary `tax` and `code` source
   - Construction liens, mechanic's liens, judgment liens (CONLIEN, MECHLIEN, DSJUDLIEN reverses) — primary `lien` source
   - Inheritance tax waivers (TAXWAIVE) and disclaimers (DISCLAIM) — primary `estate` source
   This single OPRA request unlocks 4 of 6 patterns.

2. **Ocean Surrogate docket access.** Confirmed no public web search UI. OPRA fallback for monthly probate filings.

3. **Ocean County Property Alert Service** is returning HTTP 500 (`Object reference not set to an instance of an object`) — service is broken upstream. Re-probe weekly; auto-recovers when service returns.

4. **CAPTCHA-solving services.** Intentionally not used. If the operator decides solving services are acceptable for the clerk reCAPTCHA v3 (Anti-Captcha or 2Captcha), the unblock is straightforward (~$1–2 per 1000 reCAPTCHA v3 tokens, single-line code change in a hypothetical `clerk_search.py`). This is an explicit operator-policy decision; no infrastructure assumed.

5. **Skip trace.** The CSV export includes empty `phone1-3`, `email1-2` columns. Wire to the operator's preferred skip trace vendor downstream.

6. **AVM / equity %.** The "Max equity %" slider in the dashboard is non-functional pending an AVM data source. The pipeline emits `assessed_value` and `last_sale_price` so equity can be derived once an AVM integration is added.

7. **Owner names redacted upstream.** NJ DOIT publishes the public MOD-IV layer with empty `OWNER_NAME` fields. Fall-back is the clerk search (blocked) or paid commercial source.

8. **Sheriff PDF parser noise on multi-lot vacant-LBI sales** (rows 7–8 from the current PDF have layout-driven column-split artifacts that corrupt the zip and the Lot:/Block: line). Block + lot still parses correctly on most rows; situs address is incomplete on the affected rows. Roughly 5% noise on multi-lot sales. Documented in methodology page.

---

## Next county-port notes

This build is built to port to the next NJ county with **zero source-discovery work** for the high-leverage feeds:

- **NJ MOD-IV statewide.** Filter `COUNTY='<NEXT>'` — works for all 21 NJ counties out of the box. Same field schema, same pagination, same join key. The single biggest force multiplier in this build.
- **Tyler CivilView** (`salesweb.civilview.com`). Just swap `countyId` — Atlantic=12 has 34 rows, Bergen=10 has 77, Middlesex=27 has 20 (verified during recon). Some counties may not be on CivilView; that's fine, the scraper handles 0 rows.
- **NJ Courts portals (statewide).** Same Imperva block applies. Same OPRA fallback applies. Same architectural slot.
- **Clerk official records.** Per-county. Most NJ clerks use NewVision (`publicsearch`) or Tyler (`landrecords`). Same shape: a doc-type taxonomy endpoint, a heartbeat endpoint, a (maybe blocked) search endpoint. The metadata-heartbeat scraper pattern in `clerk_metadata.py` is reusable — change one base URL.
- **Sheriff sales.** Format varies by county: PDF (Ocean), CivilView (most others), in-house ASP (some). Per-county work.
- **Surrogate.** Per-county; almost universally OPRA-only.
- **Municipal code.** 565 municipalities statewide × per-municipality scraper = the future.

The pipeline architecture (six patterns, stack-depth tiering, Two-Truths) and the dashboard are county-agnostic — point them at a different `data/leads.json` and they work as-is.

---

⚡ — Built by Jarvis (Just Jarvis LLC) for Quentin Flores. Operator-first.
