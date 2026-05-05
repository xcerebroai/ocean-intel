# scrapers/

One scraper per source. Pattern is consistent: fetch only, no scoring, write
JSONL to `data/raw/`, persist a checkpoint for resume, retry/backoff, graceful
Ctrl+C. Joins, normalization, scoring all happen in `pipeline/build_leads.py`.

## Live scrapers

| File | Source | Status | Output |
|---|---|---|---|
| `nj_modiv_parcels.py` | NJ MOD-IV statewide parcel layer (filtered to `COUNTY='OCEAN'`) | **Live** — 298,147 Ocean parcels | `data/raw/nj_modiv_parcels.jsonl` |
| `sheriff_foreclosures.py` | Ocean Sheriff foreclosure listing PDF | **Live** — 80 SEQ rows / week | `data/raw/sheriff_foreclosures_writ_of_sale.jsonl` |
| `civilview_sales.py` | Tyler CivilView (`countyId=85`) | **Live but empty for Ocean** — kept for future republish, portable to other NJ counties | `data/raw/civilview_sales_sheriff_sale_listing.jsonl` |
| `clerk_metadata.py` | Ocean Clerk public metadata (doc types, towns, heartbeat) | **Live** — metadata only; search API blocked by reCAPTCHA v3 | `data/raw/clerk_metadata_heartbeat.jsonl` |
| `_recon.py` | Phase 1 source probe (one-shot recon dump) | Diagnostic | `data/raw/recon_*.json` |

All live scrapers honor the contract:

1. Standalone CLI accepting `--limit N`, `--reset`, `--since YYYY-MM-DD`.
2. Resume-safe via `data/raw/<source>.state.json`.
3. Idempotent (dedupe on stable `_key`).
4. Polite: 2-3 seconds between requests, identifying User-Agent including
   `infinitygauntletllc@gmail.com`.
5. Ctrl+C clean — `_base.StopFlag` traps SIGINT/SIGTERM.

## Sources blocked / skipped

Documented at length in `../RECON.md`. Summary:

| Source | Blocker | Workaround |
|---|---|---|
| Ocean Clerk search API (`sng.co.ocean.nj.us/publicsearch/api/search`) | Server-enforced reCAPTCHA v3 (`enableServerRecaptcha=1`, sitekey `6LciR2kkAAAAAB0mCQA50PvunV2_uuLRwnHpSaRh`) | OPRA monthly bulk request to Clerk John P. Kelly's office; or human-in-loop seeded session reuse. |
| NJ Judgment Search (`portal.njcourts.gov/webe40/JudgmentWeb/jsp/judgmentSearch.faces`) | Imperva/Incapsula bot wall — challenge iframe served on first byte. curl_cffi `chrome120` impersonation insufficient. | OPRA fallback or paid commercial source. |
| NJ Judgment Lien Public Access (`portal.njcourts.gov/webe41/ExternalPGPA/`) | Same Imperva bot wall. | Same. |
| Ocean Tax Board search (`tax.co.ocean.nj.us/frmtaxboardtaxlistsearch`) | reCAPTCHA v2 + redundant with NJ MOD-IV layer | **Intentionally skipped** — same data already pulled via `nj_modiv_parcels`. |
| Ocean County Property Alert Service (`countyclerkpas.co.ocean.nj.us/PropertyAlert/`) | HTTP 500 on probe (broken upstream) | Re-probe nightly via refresh harness; auto-recovers when service comes back. |
| Ocean Surrogate (`co.ocean.nj.us/OC/surrogate/`) | No public docket search UI | OPRA-only fallback. |
| Municipal code enforcement (33 municipalities) | Out of scope for this build (33 separate municipal sites) | Future per-municipality scrapers; today the `code` pattern fires only via clerk-recorded municipal liens. |

## Adding a new scraper

1. Run `_recon.py` against the source first. Capture status, headers, body
   excerpt, anti-bot flags. Update RECON.md.
2. Look at one real raw record before writing the parser. Never guess schema.
3. Decide on the stable `_key` (instrument number, case number, parcel id +
   recording date — whatever the upstream guarantees unique per record).
4. Use `_base.append_jsonl` to write and dedupe.
5. Use `_base.RateLimit` for spacing requests.
6. Use `_base.StopFlag` for graceful Ctrl+C.
7. Document the new scraper in this README's "Live scrapers" table.

## Daily refresh

`pipeline/refresh.py` invokes every live scraper above as a subprocess in
dependency order (parcels first, then signal sources). Failure of any one
scraper does not abort the run — it logs and continues. See `OPERATIONS.md`.
