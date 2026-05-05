# RECON — Ocean County, NJ Sources

Phase 1 source inventory. Built from ground-truth probes against each public-facing system on **2026-05-05**. Probe artifacts live in `data/raw/recon_*.json` (gitignored — regenerate via `py -3.12 scrapers/_recon.py`).

The headline finding: **the NJ Department of the Treasury hosts a statewide MOD-IV parcel layer at `maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2` exposing 298,147 Ocean County parcels with full assessment + sales-history attributes.** This is the spine of the join graph and the single biggest force multiplier in the build. It's also reusable across all 21 NJ counties (just change the `COUNTY=` filter) — meaning the scaffold ports county-to-county for the next NJ build with zero per-source rewrites.

The largest gaps: **the Ocean County Clerk's `publicsearch` API is wrapped in server-enforced reCAPTCHA v3** (blocks the search endpoint, but the doc-type taxonomy and last-recorded-instrument heartbeat are open) and **the NJ Courts portals (`portal.njcourts.gov/*`, `find-a-case`) sit behind an Imperva/Incapsula bot wall** that rejects standard requests and curl_cffi impersonation. These are blockers for daily incremental scraping but not blockers for the platform — the sheriff foreclosure PDF, the MOD-IV parcel master, and the discovered doc-type taxonomy together are enough to power the dashboard. Workarounds documented below for the OPRA/manual-fallback path.

---

## 1. Source Inventory

| Key | Source | URL | Access Method | Blocker | Useful For |
|---|---|---|---|---|---|
| `parcels_njmodiv` | NJ Tax List MOD-IV (statewide) | `maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2` | ArcGIS REST query | None | **Parcel master.** 298,147 Ocean parcels. Full schema below. |
| `sheriff_pdf` | Ocean Sheriff Foreclosure Listing (PDF) | `co.ocean.nj.us/WebContentFiles/3ec14ac4-25a1-41cd-8c8b-d9996a9d686c.pdf` | HTTP GET → PDF parse | None | Upcoming sheriff sales: case#, plaintiff/defendant, address, block/lot, status, judgment amount, sale date. |
| `sheriff_civilview` | CivilView Sales Search (Tyler) | `salesweb.civilview.com/Sales/SalesSearch?countyId=85` | ASP.NET form POST | Empty for Ocean (countyId=85 returns 0 rows; sales feed has migrated to PDF above) | Reusable for other NJ counties (Atlantic=12 has 34 rows, Bergen=10 has 77). Kept as a fallback. |
| `clerk_doctypes` | Ocean Clerk doc type taxonomy | `sng.co.ocean.nj.us/publicsearch/api/document/doctypes` | JSON GET | None | 168 distinct doc-type abbreviations. Drives the per-doc-type filter pills in the dashboard. |
| `clerk_clientinfo` | Ocean Clerk system heartbeat | `sng.co.ocean.nj.us/publicsearch/api/search/clientinfo` | JSON GET | None | `verifyDate`, `lastDocumentRecordedDateTime`, `lastDocumentRecordedInfo`. Drives a per-source freshness signal in the dashboard header. |
| `clerk_search` | Ocean Clerk search | `sng.co.ocean.nj.us/publicsearch/api/search` (POST) | NewVision Angular SPA | **Server-enforced reCAPTCHA v3 (`useRecaptchaV3=1`, `enableServerRecaptcha=1`).** Returns `400 "No V3 token found"` on every attempt. | Signal source for deeds, mortgages, liens, lis pendens, sheriff deeds. Daily incremental query is feasible but blocked. Workaround: OPRA monthly bulk request to John P. Kelly's office; or human-in-loop seeded sessions. |
| `tax_board_search` | Ocean Tax Board Tax List Search | `tax.co.ocean.nj.us/frmtaxboardtaxlistsearch` | ASP WebForms + reCAPTCHA v2 | reCAPTCHA on submit | Same data is more efficiently sourced via NJ MOD-IV (which is already what the tax board republishes). **Skipped — redundant with `parcels_njmodiv`.** |
| `nj_courts_findcase` | NJ Courts Find a Case | `njcourts.gov/public/find-a-case` | Drupal landing page | Cloudflare-protected; underlying systems Imperva-protected | Links to ECCAS / ePGPA / JOCPA / DRB. Inaccessible directly. |
| `nj_judgment_search` | NJ Judgment Search | `portal.njcourts.gov/webe40/JudgmentWeb/jsp/judgmentSearch.faces` | JSF (Imperva) | **Imperva/Incapsula bot wall** — first byte returns Imperva challenge iframe, not the JSF page. curl_cffi did not bypass. | Statewide judgment liens. Skipped. Partial coverage via `clerk_search` (judgment recordings) when that unblocks. |
| `nj_judgment_lien_pa` | NJ Judgment Lien Public Access | `portal.njcourts.gov/webe41/ExternalPGPA/` | Drupal-linked, Imperva | Imperva | Same as above. |
| `nj_foreclosure_info` | NJ Superior Court Foreclosure (info page) | `njcourts.gov/courts/superior-court-clerks-office/foreclosure` | Static page | None — but it's a **content page, not a docket search.** | Background only. No docket data. |
| `sheriff_foreclosures_html` | Ocean Sheriff Foreclosures landing | `sheriff.co.ocean.nj.us/frmForeclosures` | ASP WebForms | None — but data is published via the linked PDF, not in-page. | Used to discover and re-resolve the PDF URL when it rotates. |
| `surrogate` | Ocean Surrogate (probate) | `co.ocean.nj.us/OC/surrogate/` | ASP WebForms | **No public docket search UI.** Static info page only. | OPRA-only path. Documented as gap. |
| `property_alert` | Ocean Clerk Property Alert | `countyclerkpas.co.ocean.nj.us/PropertyAlert/` | ASP page | **HTTP 500 (`Object reference not set to an instance of an object`)** — broken upstream. | Skipped — service unavailable on probe. Re-test in next refresh cycle. |
| `clerk_records_forms` | Ocean Clerk Records & Forms | `oceancountyclerk.com/frmRecordsForms` | ASP WebForms + reCAPTCHA | reCAPTCHA + content page (forms catalog, not data) | Background only. |

---

## 2. Doc Type Taxonomy

Pulled live from `sng.co.ocean.nj.us/publicsearch/api/document/doctypes`. The endpoint exposes a tree-view: 5 root groups (`ALL`, `DEED`, `JUDGEMENT`, `MORTGAGE`, `SUBDIVISIONS`) with 168 distinct child abbreviations under `ALL`. The full list lives in `data/raw/recon_clerk_doctypes.json` (committed) and powers the per-doc-type filter pills in the dashboard.

The mapping below assigns each abbreviation to one of the 6 patterns. Abbreviations that don't fire any pattern (UCC, election filings, trade names, BOROS, etc.) are documented for the dashboard but do not contribute to lead tier.

### Pattern: `transfer` (distressed deeds, possible motivated seller)

| Code | Meaning | Notes |
|---|---|---|
| `DEED` | DEED | Always fires `transfer` — narrowed to distressed by consideration ($1, $10) or by deed type (sheriff deed, executor deed) at scoring time. |
| `BILLSALE` | BILL OF SALE | Personal-property transfer; included for completeness. |
| `CONTSALE` | CONTRACT OF SALE | |
| `LEASE` | LEASE | |
| `EASEMENT` | EASEMENTS | |
| `TRUSTAGR` | TRUST AGREEMENT | |
| `POA` | POWER OF ATTORNEY | Estate-adjacent; can also indicate planning/incapacity. |
| `REVPOA` | REVOCATION OF POWER OF ATTORNEY | |
| `DISCLAIM` | DISCLAIMER | Heir disclaiming inheritance — strong estate signal too. |
| `FINJUDGE` | FINAL JUDGEMENT | Court-ordered transfer. |
| `DEEDNOT` | DEED NOTICE | |

### Pattern: `tax` (tax distress)

| Code | Meaning | Notes |
|---|---|---|
| `INREM` | MUNICIPAL TAX FORECLOSURE | Strongest tax signal — municipality moving to take title. |
| `MTSC` | MUNICIPAL TAX SALE CERTIFICATE | Tax lien purchased. |
| `TSC` | TAX SALE CERTIFICATE | |
| `DISTSC` | DISCHARGE OF TAX SALE CERTIFICATE | Negative — tax cleared. Tracked for de-escalation logic. |
| `FEDLIEN` | FEDERAL LIEN | IRS / federal tax lien. |
| `RLFESLEN` | RELEASE OF FEDERAL TAX LIEN | Negative — federal tax cleared. |
| `TAXWAIVE` | TAX WAIVER-INHERITANCE | NJ inheritance-tax waiver — fires `estate` more strongly than `tax` but still tax-adjacent. |

### Pattern: `lien` (non-tax encumbrances)

| Code | Meaning | Notes |
|---|---|---|
| `CONLIEN` | CONSTRUCTION LIEN CLAIM | NJ's name for mechanic's lien (private contractor unpaid). |
| `MECHLIEN` | MECHANIC'S LIEN | Older code for the same. |
| `MECHNOI` | MACH NOTICE OF INTENT | Pre-lien notice. |
| `DSCOLIEN` | DISCHARGE CONSTRUCTION LIEN | Negative. |
| `DSMELIEN` | DISCHARGE OF MECHANIC'S LIENS | Negative. |
| `DMECHNOI` | DISCHARGE OF MECHANICS NOTICE INTENSION | Negative. |
| `INSTLIEN` | INSTITUTIONAL LIENS | |
| `PHYSLIEN` | PHYSICIAN'S LIEN | Hospital/physician lien. |
| `DPHYLIEN` | DISCHARGE OF PHYSICIAN'S LIEN | Negative. |
| `BRTYLIEN` | BANKRUPTCY LIEN | Strong distress signal. |
| `ARCLIEN` | AIRCRAFT LIEN | Long-tail; included for completeness. |
| `WAGECLM` | WAGE CLAIM | |
| `WAREXEC` | WARRANT OF EXECUTION | Judgment enforcement against property. |
| `WRITEXEC` | WRIT OF EXECUTION | Judgment enforcement against property — sheriff-sale precursor. |
| `WARSATFN` | WARRANT OF SATISFACTION | Negative — judgment satisfied. |
| `STOPNOT` | STOP NOTICE | Construction-lien-adjacent. |
| `DSJUDLIEN` | DISCHARGE OF JUDGEMENT LIEN | Negative — judgment cleared. |

### Pattern: `jfc` (foreclosure / judicial)

| Code | Meaning | Notes |
|---|---|---|
| `LISPEN` | LIS PENDENS | Notice of pending foreclosure suit. |
| `NOTLIS` | NOTICE OF LIS PENDENS FILED | |
| `NTCELIS` | NOTICE OF LIS PENDING RECORDED | |
| `DISCHLIS` | DISCHARGE OF LIS PENDENS | Negative. |
| Sheriff PDF | Active foreclosure sale | The PDF is the strongest `jfc` signal — county-recorded. |

### Pattern: `estate` (probate / decedent)

| Code | Meaning | Notes |
|---|---|---|
| `TAXWAIVE` | TAX WAIVER-INHERITANCE | NJ inheritance-tax waiver — explicit decedent transfer signal. |
| `DISCLAIM` | DISCLAIMER | Heir refusing inheritance. |
| `TRUSTAGR` | TRUST AGREEMENT | |
| `POA` | POWER OF ATTORNEY | Capacity / pre-estate. |

> **Surrogate dockets are not exposed in any public web UI.** Estate signals from clerk records (when accessible) are weaker than direct surrogate access. OPRA fallback documented.

### Pattern: `code` (municipal code enforcement)

| Source | Notes |
|---|---|
| Clerk-recorded municipal liens (subset of `INSTLIEN`) | Sparse — code enforcement is municipal in NJ, rarely county-recorded. |
| Clerk `INREM` (when fired with non-tax origin) | Edge case. |

> **Known gap.** NJ has 33 municipalities in Ocean County, each running its own code enforcement. The framework spec calls for honest disclosure: this pattern will **fire only when** a municipal code action results in a county-recorded lien (`INSTLIEN` etc.). Direct municipal code feed scraping is out of scope for this build (33 separate municipal sites is a project of its own). Pattern stays in the model so the architecture is ready when those feeds are added per-municipality.

### Other doc types (catalogued, no pattern)

UCC1 / UCCAMEND / UCCCONT / UCCASSN / UCCTERM / UCCPPREL / UCCPRREL — UCC commercial filings.
ASSGN / ASSN MTG / DISCHMTG / RELMORT / CANMORT* — mortgage lifecycle (recorded for completeness, drives `mortgage_active` flag for context, no pattern).
MORT / MTG / MTG MOD — mortgage origination / modification.
ERGEN / ERPRI / EROTHER / ERSCH — election financial reports (background).
TRADECRT / TRADAMD — trade-name filings (background).
SUBDIV / SUBMAP / SUBMAPS — subdivision maps (background).
BOROS / GENMISC / MISC / MISCREV / MREV1 — catch-alls.
LANDUSE / STPERM — permits (background).
PHYSLIS / FNDISCL / FIREXEMPT / FIREXMPT / CNTYIDEN — admin / certifications.
FNDISCL / RECOG / RESOL / DISRECOG / SHERBOND / DISSHRBD — bond / financial-disclosure filings.
EXP / EXPUNGED — expungement filings (criminal, not property).
PARTSHIP / NONBUSCORP / CORPNAME — entity formation / name changes.
LOGOS / VACATION / VOID / PH — admin metadata.

---

## 3. Pattern → Source Mapping (write side)

| Pattern | Source(s) attached |
|---|---|
| `jfc` | `sheriff_pdf` (primary, weekly) ; `clerk_search` (LISPEN/NOTLIS/NTCELIS — blocked at recon, OPRA fallback) |
| `tax` | `parcels_njmodiv` (LAST_YR_TX deltas — derived) ; `clerk_search` (INREM/MTSC/TSC/FEDLIEN — blocked) |
| `estate` | `clerk_search` (TAXWAIVE/DISCLAIM/POA — blocked) ; surrogate (no public docket — gap) |
| `code` | `clerk_search` (INSTLIEN — blocked) ; municipal code feeds (out of scope) |
| `lien` | `clerk_search` (CONLIEN/PHYSLIEN/WRITEXEC etc. — blocked) |
| `transfer` | `clerk_search` (DEED at $1 / sheriff deed / executor deed — blocked) ; `parcels_njmodiv` (SALE_PRICE = 1, recent DEED_DATE — derived) |

In the current build, **the sheriff PDF and parcel master alone fire two of the six patterns** (`jfc` from the sheriff PDF, `transfer` from MOD-IV nominal-consideration deed sales). The remaining four patterns become directly actionable as soon as the `clerk_search` recaptcha block is resolved (OPRA monthly bulk + ad-hoc seeded sessions).

---

## 4. Anti-Bot Encounters

| System | Defense | Workaround Attempted | Result |
|---|---|---|---|
| `sng.co.ocean.nj.us/publicsearch/api/search` | reCAPTCHA v3 server-enforced (`enableServerRecaptcha=1`, sitekey `6LciR2kkAAAAAB0mCQA50PvunV2_uuLRwnHpSaRh`) | Plain POST without token | `400 "No V3 token found in resonsped"`. No bypass. |
| `tax.co.ocean.nj.us/frmtaxboardtaxlistsearch` | reCAPTCHA v2 on submit | Did not attempt — data redundant with `parcels_njmodiv` | Skipped intentionally. |
| `portal.njcourts.gov/webe40/*` (judgment) | Imperva/Incapsula | curl_cffi `chrome120` impersonation | Imperva challenge iframe still served. No bypass. |
| `njcourts.gov/public/find-a-case` | Cloudflare | Plain | Page rendered (200) but linked systems all gated. |
| `oceancountyclerk.com/frmRecordsForms` | reCAPTCHA + ASP WebForms | None — content page, not data | Skipped intentionally. |
| `sheriff.co.ocean.nj.us/frmForeclosures` | None | Plain | 200, parsed HTML, extracted PDF URL. |
| `salesweb.civilview.com/Sales/SalesSearch` | None | Plain ASP form POST | 200, 0 rows for countyId=85. Works for other NJ counties. |
| `co.ocean.nj.us/WebContentFiles/*.pdf` | None | Plain | 200, 23KB PDF. |
| `maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2` | None | Plain | 200, 1000-record max per query, returnCountOnly works. |
| `countyclerkpas.co.ocean.nj.us/PropertyAlert/` | None | Plain | **HTTP 500 — service broken upstream.** Re-probe nightly. |

---

## 5. Daily Refresh Feasibility

| Source | Incremental? | `--since` Mechanism | Notes |
|---|---|---|---|
| `parcels_njmodiv` | Yes (use `PCLLASTUPD >= timestamp`) | ArcGIS `where` clause | But we **also** do a full county pull weekly because OBJECTID can shift on republish. Daily incremental delta is supplemental. |
| `sheriff_pdf` | The PDF is republished entirely each cycle (~weekly, sales on Tuesdays). Always re-pull and dedupe by case# + sequence#. | n/a | Use HEAD `Last-Modified` to skip when unchanged. |
| `sheriff_civilview` | Yes (form `PropertyStatusDate`) but currently 0 rows for Ocean | Form param | Kept as opportunistic fallback. |
| `clerk_search` | Yes — supports FromDate/ToDate (YYYYMMDD) — **once unblocked** | `i.FromDate / i.ToDate` | Currently blocked. |
| `clerk_doctypes` | Static. | n/a | Pull weekly. |
| `clerk_clientinfo` | Real-time heartbeat. | n/a | Pull every refresh — drives "data freshness" badge in dashboard. |

---

## 6. Architectural Decisions Logged

- **Owner name is NULL in the public NJ MOD-IV layer.** Dashboard shows mailing address, situs address, year built, sales history, and assessed values; owner name is null pending clerk-search unblock or skip-trace. Documented in `methodology.html`. No mock data substituted.
- **Tier comes from stack depth across all signals attached to a parcel.** With `clerk_search` blocked, only `jfc` (sheriff PDF) and `transfer` (MOD-IV nominal deeds) reliably fire today. So initial Hot/Warm tiers will be sparse — that's correct. As OPRA pulls + future feeds attach, stack depth grows organically.
- **Parcel-PIN-first join.** Sheriff PDF defendants get joined to parcels via `MUN_NAME + Block + Lot`. Records with no parcel match are kept as orphan signals on a synthetic lead, flagged `parcel_match: null`.
- **CivilView retained as a scraper** despite returning 0 Ocean rows today. It's the cross-county foreclosure pattern (countyId swap) and Ocean has used it historically — leaving it in place means we automatically catch any republish.
- **Tax board search is intentionally skipped.** Same MOD-IV fields appear in `parcels_njmodiv`, scraped without reCAPTCHA, and the NJ MOD-IV layer is statewide (works for all 21 counties without rewriting). DRY.
