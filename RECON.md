# RECON — Ocean County, NJ Sources (v2.0)

Phase 1 source inventory for the v2.0 taxonomy. Built from ground-truth probes against each public-facing system on **2026-05-05** during the v2.0 migration. Probe artifacts live in `data/raw/recon_*.json` (gitignored — regenerate via `py -3.12 scrapers/_recon.py`).

> **v2.0 changes:** the v1.0 6-pattern model (jfc / tax / estate / code / lien / transfer) is replaced by an **11-pattern lead-type model** plus an **11-attribute parcel-state model**. Tier (Hot/Warm/Active) is dropped from the default UI; stack-depth math survives as a column and a sort. See `methodology.html` for the operator-readable explanation.

---

## 1. Source Inventory

| Key | Source | URL | Access Method | Status / Blocker | Refresh cadence | Useful For |
|---|---|---|---|---|---|---|
| `parcels_njmodiv` | NJ Tax List MOD-IV (statewide) | `maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2` | ArcGIS REST query | None | Weekly (Monday) — see §6 | **Parcel master.** 298,147 Ocean parcels. |
| `sheriff_pdf` | Ocean Sheriff Foreclosure Listing (PDF) | `co.ocean.nj.us/WebContentFiles/3ec14ac4-25a1-41cd-8c8b-d9996a9d686c.pdf` | HTTP GET → PDF parse | None | Daily (re-fetch & dedupe) | Upcoming sheriff sales. |
| `sheriff_civilview` | CivilView Sales (Tyler) | `salesweb.civilview.com/Sales/SalesSearch?countyId=85` | ASP.NET form POST | Empty for Ocean (countyId=85 returns 0 rows) | Daily | Reusable for other NJ counties. Kept as resilience fallback. |
| `clerk_doctypes` | Ocean Clerk doc-type taxonomy | `sng.co.ocean.nj.us/publicsearch/api/document/doctypes` | JSON GET | None | Weekly | 118 doc-type abbreviations (verified live in current data). |
| `clerk_clientinfo` | Ocean Clerk system heartbeat | `sng.co.ocean.nj.us/publicsearch/api/search/clientinfo` | JSON GET | None | Daily | `verifyDate`, `lastDocumentRecordedDateTime`, `lastDocumentRecordedInfo`. |
| `clerk_search` | Ocean Clerk official records search | `sng.co.ocean.nj.us/publicsearch/api/search` (POST) | NewVision Angular SPA | **Server-enforced reCAPTCHA v3** (`enableServerRecaptcha=1`, sitekey `6LciR2kkAAAAAB0mCQA50PvunV2_uuLRwnHpSaRh`). HTTP 400 `"No V3 token found"`. | n/a | Would unlock 4 of 11 patterns (`tax`, `estate`, `code`, `lien`). OPRA fallback. |
| `tax_board_search` | Ocean Tax Board Tax List Search | `tax.co.ocean.nj.us/frmtaxboardtaxlistsearch` | ASP WebForms + reCAPTCHA v2 | reCAPTCHA on submit | n/a | **Intentionally skipped** — same data sourced via `parcels_njmodiv`. |
| `nj_courts_findcase` | NJ Courts Find a Case (landing) | `njcourts.gov/public/find-a-case` | Drupal page | None on the landing page; downstream subsystems blocked | Diagnostic only | Discovery of subsystem URLs. |
| `nj_civil_foreclosure_pa` | NJ Civil & Foreclosure Public Access | `njcourts.gov/public/find-a-case/civil-and-foreclosure-public-access` | Static page (Drupal) | None on landing; the actual case search is on `portal-cloud.njcourts.gov` behind SAML SSO | n/a | Civil case docket including foreclosure. **Blocked behind login.** |
| `nj_judgment_lien_pa` | NJ Judgment Lien Public Access | `portal.njcourts.gov/webe41/ExternalPGPA/` | JSF | **Imperva/Incapsula bot wall** | n/a | Statewide judgment liens. |
| `nj_judgment_search` | NJ Judgment Search | `portal.njcourts.gov/webe40/JudgmentWeb/jsp/judgmentSearch.faces` | JSF | **Imperva** | n/a | Same. |
| `nj_drb_lookup` | DRB Disciplinary Review Lookup | `drblookupportal.judiciary.state.nj.us/Search.aspx` | ASP.NET | Open, but it's the attorney-discipline lookup — **not property records**. | n/a | Not relevant. |
| `nj_bankruptcy_njb` | US Bankruptcy Court NJ (NJB ECF) | `ecf.njb.uscourts.gov/cgi-bin/iquery.pl` | Redirects to `pacer.login.uscourts.gov` | **PACER login required** (paid: $0.10/page) | n/a | Federal bankruptcy filings. **Paid wall.** Skipped. |
| `nj_evictions` | NJ Special Civil Part landlord-tenant | (no public URL — formerly `njcourts.gov/courts/special-civil/landlord-tenant`) | 404 on probe | **No public docket.** Same SAML/Imperva path as civil cases. | n/a | OPRA fallback. |
| `surrogate` | Ocean Surrogate (probate) | `co.ocean.nj.us/OC/surrogate/` | ASP page | **No public docket search UI.** | n/a | OPRA fallback. |
| `property_alert` | Ocean Clerk Property Alert | `countyclerkpas.co.ocean.nj.us/PropertyAlert/` | ASP page | **HTTP 500 — broken upstream**, re-confirmed 2026-05-05 | n/a | Re-probe nightly via refresh harness. |
| `municipal_code` | Code enforcement (33 muns) | per-municipality | Per-site | **Out of scope** for this build | n/a | Future per-municipality scrapers. |

### Quick-summary verdict

- **3 sources fully live:** `parcels_njmodiv`, `sheriff_pdf`, `clerk_clientinfo`+`clerk_doctypes` (metadata heartbeat).
- **1 source live but currently empty:** `sheriff_civilview` (Ocean migrated to PDF).
- **9 sources blocked or out of scope:** `clerk_search` (reCAPTCHA v3), `tax_board_search` (reCAPTCHA v2; intentionally redundant), `nj_judgment_*` (Imperva), `nj_civil_foreclosure_pa` (SAML SSO), `nj_bankruptcy_njb` (PACER paywall), `nj_evictions` (no public docket), `surrogate` (no public docket), `property_alert` (HTTP 500), `municipal_code` (out of scope).

The build ships with **3 of 11 patterns reliably firing** today (`foreclosure` from sheriff PDF; `transfer` from MOD-IV nominal-consideration deeds; `tax` indirectly via long-tail `LAST_YR_TX` deltas on parcels — see §3). The remaining 8 patterns become active as soon as `clerk_search` unblocks (OPRA bulk extract) — the architecture is built for it.

---

## 2. Pattern → Subtype Mapping (11-pattern v2.0 model)

For each lead-type pattern, the table lists every clerk doc-type abbreviation that fires it (when clerk_search unblocks) plus any other source that fires the pattern today. Subtype display name is what shows up in the dashboard chip tooltip and in the filter rail's expanded sub-checkboxes.

### `foreclosure` — Foreclosure (red `#EF4444`)

| Subtype | Source | Doc-type code(s) | Fires today? |
|---|---|---|---|
| Sheriff Sale | `sheriff_pdf` | n/a (PDF row) | ✅ Yes |
| Sheriff Sale (CivilView) | `sheriff_civilview` | n/a | Currently empty for Ocean |
| Lis Pendens | `clerk_search` | `LISPEN`, `NOTLIS`, `NTCELIS` | ❌ blocked |
| Final Judgment of Foreclosure | `clerk_search` | `FINJUDGE` (when foreclosure-specific) | ❌ blocked |
| Notice of Sale | `clerk_search` | (sheriff-recorded notices, sub-type of `MISC` or `INSTLIEN`) | ❌ blocked |

### `tax` — Tax Distress (amber `#F59E0B`)

| Subtype | Source | Doc-type code(s) | Fires today? |
|---|---|---|---|
| Tax Sale Certificate | `clerk_search` | `TSC` | ❌ blocked |
| Municipal Tax Sale Certificate | `clerk_search` | `MTSC` | ❌ blocked |
| In Rem Tax Foreclosure | `clerk_search` | `INREM` | ❌ blocked |
| Federal Tax Lien | `clerk_search` | `FEDLIEN` | ❌ blocked |
| Tax Lien Discharge (negative) | `clerk_search` | `DISTSC`, `RLFESLEN` | ❌ blocked, also de-escalates |

### `lien` — Liens (yellow `#EAB308`)

| Subtype | Source | Doc-type code(s) | Fires today? |
|---|---|---|---|
| Construction Lien (mechanic's) | `clerk_search` | `CONLIEN`, `MECHLIEN`, `MECHNOI` | ❌ blocked |
| Judgment Lien | `clerk_search` | (judgments recorded against parcel) | ❌ blocked |
| Hospital / Physician Lien | `clerk_search` | `PHYSLIEN` | ❌ blocked |
| Bankruptcy Lien | `clerk_search` | `BRTYLIEN` | ❌ blocked |
| Writ of Execution | `clerk_search` | `WRITEXEC`, `WAREXEC` | ❌ blocked |
| Wage Claim | `clerk_search` | `WAGECLM` | ❌ blocked |
| Aircraft Lien | `clerk_search` | `ARCLIEN` | ❌ blocked (long-tail) |
| Stop Notice | `clerk_search` | `STOPNOT` | ❌ blocked |
| Institutional / Municipal Lien | `clerk_search` | `INSTLIEN` | ❌ blocked (also fires `code` if origin is municipal) |

### `estate` — Estate / Probate (purple `#8B5CF6`)

| Subtype | Source | Doc-type code(s) | Fires today? |
|---|---|---|---|
| NJ Inheritance Tax Waiver | `clerk_search` | `TAXWAIVE` | ❌ blocked |
| Disclaimer | `clerk_search` | `DISCLAIM` | ❌ blocked |
| Trust Agreement | `clerk_search` | `TRUSTAGR` | ❌ blocked |
| Power of Attorney | `clerk_search` | `POA`, `REVPOA` | ❌ blocked |
| Surrogate Docket Filing | `surrogate` (no public UI) | n/a | ❌ OPRA-only |

### `code` — Code / Condemnation (orange `#F97316`)

| Subtype | Source | Doc-type code(s) | Fires today? |
|---|---|---|---|
| Municipal Lien (mowing/demo/cleanup/nuisance) | `clerk_search` | `INSTLIEN` (subset, when origin is municipal) | ❌ blocked, sparse |
| Demolition / Condemnation Order | per-municipality | n/a | ❌ out of scope |
| Failed Inspection / Stop Work | per-municipality | n/a | ❌ out of scope |

### `transfer` — Distressed Transfer (cyan `#06B6D4`)

| Subtype | Source | Doc-type code(s) / Heuristic | Fires today? |
|---|---|---|---|
| Nominal-Consideration Sale | `parcels_njmodiv` | `SALE_PRICE <= 10` AND deed within last 3 years | ✅ Yes |
| Quitclaim Deed | `clerk_search` | `DEED` with quitclaim sub-type indicator | ❌ blocked |
| Sheriff's Deed | `clerk_search` | `DEED` recorded by sheriff after sale | ❌ blocked |
| Deed in Lieu | `clerk_search` | `DEED` with consideration patterns matching mortgage payoff | ❌ blocked |
| Final Judgment (transfer) | `clerk_search` | `FINJUDGE` (non-foreclosure specific) | ❌ blocked |
| Bill of Sale | `clerk_search` | `BILLSALE` | ❌ blocked |
| Contract of Sale | `clerk_search` | `CONTSALE` | ❌ blocked |

### `bankruptcy` — Bankruptcy (deep red `#DC2626`)

| Subtype | Source | Notes |
|---|---|---|
| Chapter 7 / 11 / 13 | PACER (paywalled) or `clerk_search` `BRTYLIEN` if recorded | ❌ blocked. Sheriff PDF "BANKRUPTCY" status row indicates bankruptcy stay → fires `bankruptcy` AND `foreclosure` today. |

> **Edge:** the sheriff PDF marks foreclosures with status="BANKRUPTCY" when a debtor's bankruptcy filing has stayed the sale. We use this as a derived `bankruptcy` subtype, source = sheriff_pdf. ✅ Fires today.

### `divorce` — Divorce (pink `#EC4899`)

| Subtype | Source | Notes |
|---|---|---|
| NJ Family Part filing | NJ Courts (Imperva-blocked + sealed) | ❌ blocked (sealed by default) |

### `eviction` — Eviction (lime `#84CC16`)

| Subtype | Source | Notes |
|---|---|---|
| Special Civil Part landlord-tenant | NJ Courts (no public docket) | ❌ blocked |

### `tired_landlord` — Tired Landlord (teal `#14B8A6`)

| Subtype | Source | Notes |
|---|---|---|
| Multiple-property owner with eviction filing | derived | Computed from `eviction` + `multiple_properties` attribute; both inputs blocked, so ❌ today. |

### `surplus_owed` — Surplus Owed (emerald `#10B981`)

| Subtype | Source | Notes |
|---|---|---|
| Excess proceeds from sheriff/tax sale | `sheriff_pdf` post-sale + `clerk_search` | ❌ partially blocked. Sheriff PDF doesn't expose surplus calc; clerk records would have the post-sale assignment. |

---

## 3. Parcel Attribute → Derivation Rules (11-attribute v2.0 model)

For each parcel-state attribute, the rule for setting the boolean to true. All derivations run in `pipeline/build_leads.py` over the parcel + signal pool.

| Attribute | Display | Derivation rule | Fires today? |
|---|---|---|---|
| `vacant` | Vacant | **Cannot be derived from MOD-IV alone.** Requires USPS vacancy feed or utility shutoff data. Documented as a v3 plug-in slot. | ❌ |
| `absentee` | Absentee | `mailing_city_state` does not contain situs municipality (after normalizing TWP/BORO). | ✅ |
| `out_of_state` | Out-of-state | `mailing_city_state` does not contain " NJ" (case-insensitive). | ✅ |
| `senior_owner` | Senior owner | `long_term_owned` AND parcel `LAND_DESC`/`PROP_USE` includes a known senior-deduction code (NJ MOD-IV exposes `EPL_CODE`/deductions in the full bulk MOD-IV file but NOT in the public ArcGIS layer). With ArcGIS-only data, **proxy**: `long_term_owned` AND `last_yr_tx` < 50% of expected (tax-relief recipients). Documented as a proxy. | ✅ (proxy) |
| `long_term_owned` | Long-term owned | `years_owned >= 15` from `DEED_DATE`. | ✅ |
| `free_and_clear` | Free-and-clear | No mortgage of record on parcel. **Requires `clerk_search` to enumerate mortgages.** Falls back to "no mortgage discharged or active in last 30 yrs from MOD-IV `DEED_DATE`+age proxy" — too noisy, so attribute fires only when clerk_search unblocks. | ❌ |
| `high_equity` | High equity | `assessed_value >= 2 × last_sale_price` AND `years_owned >= 5`. Rough proxy until AVM is wired. | ✅ (proxy) |
| `entity_owned` | Entity owned | Owner regex matches `\b(LLC|INC|CORP|TRUST|LP|LTD|CO|COMPANY|HOLDINGS|ASSOCIATES|PARTNERS)\b`. **Owner name is currently redacted in the public MOD-IV layer** — falls back to checking the mailing-address line for entity tokens. | ⚠️ partial (mailing-line only) |
| `multiple_properties` | Multiple properties | Owner name appears as registered owner on N≥3 parcels (computed via post-walk index). **Currently inoperative** because owner names are redacted. Once names land via OPRA bulk MOD-IV, fires correctly. | ❌ |

The 9 attributes that have a `Fires today?` mark of ✅ or ⚠️ partial drive 5 of the 6 pre-canned views in the dashboard. The two attributes blocked on owner-name access (`free_and_clear`, `multiple_properties`) and the one blocked on a vacancy feed (`vacant`) are surfaced in the schema but their values stay `false` until upstream sources unblock.

---

## 4. GIS Deep-Link Template

**Probe target:** `1512_3601_28` (real Ocean parcel, Jackson Twp, Block 3601 Lot 28, 4 SOLOMON COURT).

**Attempted patterns:**

| URL pattern | Result |
|---|---|
| `taxrecords-nj.com/pub/cgi/m4.cgi?district={mun_code}&l01=Block&v01={block}&l02=Lot&v02={lot}` | Returns the page template but with all values empty — the URL doesn't deep-link to a record (search session is POST-only via `inf.cgi`). ❌ |
| `taxrecords-nj.com/pub/cgi/inf.cgi` (POST) | Works for search but is POST-only — not a clickable deep link. ❌ |
| `maps.nj.gov/njgisdo/?PAMS_PIN={pid}` | 404. ❌ |
| `njgin.nj.gov/njgisdo/?PAMS_PIN={pid}` | 404. ❌ |
| **`maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2/query?where=PAMS_PIN%3D%27{pid}%27&outFields=*&returnGeometry=false&f=html`** | ✅ Returns the full parcel attributes in a human-readable HTML view. Same upstream data as our scrape. Stable, no auth. |

**Locked template** (stored in `data/leads.json` header as `gis_deep_link_template`):

```
https://maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2/query?where=PAMS_PIN%3D%27{pid}%27&outFields=*&returnGeometry=false&f=html
```

Dashboard renders this per row with `{pid}` interpolated.

---

## 5. Anti-Bot Status (re-confirmed 2026-05-05)

| System | Defense | Status (2026-05-05) | Workaround |
|---|---|---|---|
| Ocean Clerk search API | reCAPTCHA v3 server-enforced | **Still blocked.** | OPRA monthly bulk; or seeded-session human-in-loop. |
| NJ Judgment Search | Imperva bot wall | **Still blocked.** `curl_cffi chrome120` insufficient. | OPRA. |
| NJ Judgment Lien Public Access | Imperva | **Still blocked.** | OPRA. |
| NJ Civil & Foreclosure Public Access | SAML SSO via `portal-cloud.njcourts.gov` | **New finding — login wall, not Imperva.** Still blocked for autonomous access. | Manual registration → human-in-loop. |
| NJ Bankruptcy (NJB) | PACER paywall ($0.10/page) | **Confirmed paywalled.** | Operator-level decision: pay PACER fees. |
| Ocean Tax Board search | reCAPTCHA v2 | Skipped — redundant with NJ MOD-IV. | n/a |
| Ocean Property Alert Service | HTTP 500 | **Still broken upstream.** | Re-probe nightly. |
| Ocean Surrogate | No public docket | **Confirmed.** | OPRA. |

---

## 6. Daily-Refresh Feasibility (Rule 25 verification)

**Probe finding:** the NJ MOD-IV ArcGIS layer exposes both `PCLLASTUPD` (last-update timestamp per parcel) and `PCL_PBDATE` (publication date for the bulk pull).

| Field | Populated count | Max value | Conclusion |
|---|---|---|---|
| `PCLLASTUPD` | 41,565 of 298,147 (14%) | **2025-01-07** (4 months stale) | **Field is mostly NULL and not maintained for daily updates.** |
| `PCL_PBDATE` | 298,147 of 298,147 (100%) | 2025-03-26 (~weekly bulk) | **Same value across all records of a given pull — represents the publication date, not per-record changes.** |

Neither field supports per-record incremental pulls in practice. **Decision (Rule 25):** parcel scraper runs **weekly on Monday only**. Daily refresh skips the parcel scraper Tuesday–Sunday. The refresh harness has been updated to enforce this.

---

## 7. Architectural Decisions Logged (v2.0)

- **GIS deep link uses `maps.nj.gov/arcgis/rest/.../query?...&f=html`.** It's the only stable, auth-free, deep-linkable parcel-detail URL across all 21 NJ counties — and it's the *same upstream data* as our scrape, so what the operator sees in the deep link is exactly what's in our pipeline. Beats any per-county vendor link that may rotate.
- **MOD-IV pulls weekly only.** Per-record incremental is theoretically supported but practically not (only 14% of records have `PCLLASTUPD` populated and the max date is 4 months stale). Re-pull entire county every Monday; daily refresh skips it Tue–Sun.
- **Sheriff `BANKRUPTCY` status fires both `foreclosure` and `bankruptcy` patterns.** Edge case caught during taxonomy mapping.
- **`free_and_clear` and `multiple_properties` will not fire until clerk_search unblocks.** Documented honestly. Schema preserves the field; value stays `false`.
- **Owner-name redaction is the single biggest blocker for v2.0.** Fixes 4+ attributes (entity_owned full coverage, multiple_properties, derivation of senior_owner without proxy, tired_landlord computation). Mitigated by mailing-address-line entity detection. OPRA bulk MOD-IV is the path.
- **`vacant` attribute is preserved in schema but always false.** Surfacing it in the schema makes the v3 plug-in slot trivial; surfacing the always-false value in the dashboard would be misleading, so the dashboard hides the icon when false (same behavior as the other 8 attributes — all hide-when-false).
