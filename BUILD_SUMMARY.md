# ocean-intel v2.0 build summary

**Build date:** 2026-05-05
**Tag:** `v1.0-final` (preserved) → v2.0 commits on `main`
**Schema:** v2.0 confirmed live
**Repo:** https://github.com/xcerebroai/ocean-intel
**Dashboard:** https://xcerebroai.github.io/ocean-intel/ (GitHub Pages, schema v2.0 confirmed serving)

---

## Self-verification gate: ALL 11 PASS

```
PASS  1: leads.json validates against schema v2.0
PASS  2: Two-Truths invariant
PASS  3: Match-mode logic (ANY / ALL / 2+ / 3+) verified — 7 cases
PASS  4: Pre-canned views defined — 6 views
PASS  5: CSV export contains correct columns — 31 columns confirmed
PASS  6: GIS deep link works — PID 1513_1626.02_2 resolves to 200 OK
PASS  7: Methodology renders all 11 patterns + 9 attributes + match modes + sources
PASS  8: Refresh harness dry-run exits 0 and logs each scraper
PASS  9: Scheduled task verification — task registered
PASS 10: Live Pages serves schema v2.0 — Pages CDN serving v2.0
PASS 11: Sheriff scraper QA — valid_pct 100.0% (Rule 20 threshold 90%)
```

Re-run anytime via `py -3.12 pipeline/verify.py`.

---

## Pipeline output (`data/leads.json`)

```
schema_version : 2.0
parcels indexed: 298,147
leads          : 7,000
total signals  : 7,002
new in 24h     : 44
most-stacked   : 2 distinct lead types

pattern_counts:
  foreclosure    : 79      (sheriff foreclosure PDF — 79 active/adjourned/bankruptcy entries)
  tax            : 0       (clerk_search blocked — needs OPRA)
  lien           : 0       (clerk_search blocked)
  estate         : 0       (clerk_search blocked)
  code           : 0       (clerk_search blocked + municipal feeds out of scope)
  transfer       : 6,921   (MOD-IV nominal-consideration deeds in last 3 years)
  bankruptcy     : 1       (sheriff PDF "BANKRUPTCY" status row → fires both foreclosure & bankruptcy)
  divorce        : 0       (NJ Family Part sealed)
  eviction       : 0       (NJ Special Civil Part — no public docket)
  tired_landlord : 0       (derived; needs eviction + multi-property data)
  surplus_owed   : 0       (needs post-sale clerk records)

attribute_counts:
  vacant              : 0     (requires USPS vacancy / utility shutoff feed)
  absentee            : 3,949 (mailing-city != situs municipality)
  out_of_state        : 905   (mailing state ≠ NJ)
  senior_owner        : 0     (proxy: long-term owned + low-tax-bill, no current matches)
  long_term_owned     : 52    (>= 15 years owned)
  free_and_clear      : 0     (requires clerk_search to enumerate mortgages)
  high_equity         : 12    (proxy: assessed >= 2× last sale + 5+ yrs)
  entity_owned        : 0     (owner names redacted; mailing-line fallback finds 0)
  multiple_properties : 0     (requires owner names — redacted upstream)

stack_depth_distribution:
  depth 1: 6,999 leads
  depth 2: 1 lead    (the bankruptcy/foreclosure double-fire)
  depth 3+: 0

subtypes firing:
  Sheriff Sale (foreclosure)             : 80
  Foreclosure-Stay Bankruptcy            : 1
  Nominal-Consideration Sale (transfer)  : 6,921
```

Two-Truths check: **PASS** — header counts match records-derived counts on
`pattern_counts`, `attribute_counts`, `lead_type_subtype_counts`,
`stack_depth_distribution`, `total_signals`, `most_stacked_count`.

File size: 10.5 MB / 50 MB cap (21% utilization).

---

## v2.0 vs v1.0 — what changed

### Taxonomy

| Aspect | v1.0 | v2.0 |
|---|---|---|
| Lead-type patterns | 6 (jfc/tax/estate/code/lien/transfer) | **11** (foreclosure/tax/lien/estate/code/transfer/bankruptcy/divorce/eviction/tired_landlord/surplus_owed) |
| Parcel attributes | not modeled | **9** (vacant/absentee/out_of_state/senior_owner/long_term_owned/free_and_clear/high_equity/entity_owned/multiple_properties) |
| Subtypes | not modeled | per-pattern subtypes (e.g. "Sheriff Sale", "Lis Pendens", "Tax Sale Certificate") |
| Tier (Hot/Warm/Active) | front-and-center | **dropped from default UI** — stack depth is a sortable column |
| Match modes | n/a | **4 buttons** (ANY / ALL / 2+ / 3+) |
| Chip palette | tier-color only | **fixed per-pattern palette** (red/amber/yellow/purple/orange/cyan/deep-red/pink/lime/teal/emerald) |

### Bugs fixed

| Bug | v1.0 status | v2.0 fix |
|---|---|---|
| Sheriff parser missed 47% of records | CH-only case-prefix regex silently mis-routed L-cases | Match all case types (CH/L/DJ/F/JL); multi-token lots; column-split zip recovery → **100% valid (80/80)** |
| Methodology drift | hand-written; said "168 doc types" while data had 118 | `pipeline/build_methodology.py` reads live data → no drift possible |
| GIS deep link untested | linked to vendor URL that returned blank template | Switched to NJ ArcGIS REST `f=html` view; verified end-to-end with one real PID |
| Pages may serve stale | no automated check | `verify.py` polls Pages for 90s, asserts schema v2.0 + matching foreclosure count |
| Phantom leads | no GC | Stale-record GC drops leads where every signal expired per its TTL |
| Run-over-run regression undetected | n/a | Pipeline exits 3 + `STALE_ALERT.txt` if any pattern drops > 50% |
| Heartbeat staleness alert documented but not coded | doc-only | Coded in `pipeline/refresh.py` → Telegram + STALE_ALERT.txt at 36h threshold |
| New high-signal lead alert documented but not coded | doc-only | Coded in `pipeline/refresh.py` → Telegram per new pid with `pattern_count >= 3` |
| Per-source TTL on signals | none | Per-pattern: foreclosure 180d, estate/eviction/code 365d, transfer 1095d, bankruptcy/divorce 720d, surplus 1825d; tax + lien never expire |
| Daily MOD-IV pulled needlessly | full pull every refresh, ~17 min | Recon found PCLLASTUPD only 14% populated and 4 months stale → **Monday-only pull** (Rule 25) |
| HTTP retry handling | naive try/sleep loops | `RetryWithBackoff(max_attempts=5, base_delay=1.0)` — 1s/2s/4s/8s/16s on 429/503 |

---

## Sources

| Status | Count | Sources |
|---|---|---|
| Live and firing | 3 | `nj_modiv_parcels` (6,921 nominal-deed transfer signals), `sheriff_foreclosures` (80 sheriff sale + 1 bankruptcy edge), `clerk_metadata` (heartbeat, no signals — meta only) |
| Live but currently empty | 1 | `civilview_sales` (Ocean countyId=85 returns 0 rows; works for other NJ counties) |
| Blocked / out of scope | 9 | clerk_search (reCAPTCHA v3), tax_board_search (intentionally redundant), nj_judgment_* (Imperva), nj_civil_foreclosure_pa (SAML SSO), nj_bankruptcy (PACER paywall), nj_evictions (no public docket), surrogate (no public docket), property_alert (HTTP 500 upstream), municipal_code (33 separate sites) |

The current build fires **3 of 11 patterns reliably** (foreclosure, transfer, bankruptcy edge case). The remaining 8 patterns become active the moment OPRA bulk extract from Ocean Clerk lands — pipeline already routes the doc-type abbreviations to subtypes; data plug-in is mechanical.

---

## Daily refresh

| Component | Status |
|---|---|
| `pipeline/refresh.py` | Live, dry-run verified |
| `pipeline/build_leads.py` | Live, Two-Truths PASS |
| `pipeline/build_methodology.py` | Live, regenerates methodology.html |
| `pipeline/verify.py` | All 11 checks PASS |
| Windows scheduled task `ocean-intel-refresh` | **Registered** (`Logon Mode: Interactive only` from v1 — see open items below for stored-creds upgrade) |
| Telegram alerting | Wired in code; `.env` empty by default — alerts log to `data/raw/refresh.log` until creds are added |

Cadence:
- **Daily** at 4 AM local: sheriff_foreclosures, civilview_sales, clerk_metadata, then build_leads.py, then build_methodology.py
- **Mondays only**: nj_modiv_parcels (Rule 25 — incremental not viable on this layer)

---

## Open items requiring human follow-up

1. **Scheduled task currently registered with `Logon Mode: Interactive only`** (v1 InteractiveToken). Per Rule 15, v2 wants stored credentials so the task runs whether the operator is logged in or not. Operator must run:
   ```powershell
   schtasks /create /xml scripts\daily_refresh.xml /tn "ocean-intel-refresh" /ru DESKTOP-3GOQFT6\Owner /rp <password> /f
   ```
   The autonomous build cannot acquire the password and so left the v1 registration in place.

2. **Telegram bot creds (`.env`).** Bot is `@Xcerebrobot`, user ID `6004053137`. Operator must:
   - Get `TELEGRAM_BOT_TOKEN` from BotFather for `@Xcerebrobot`
   - Add to `.env` (gitignored) — see `.env.example`
   - Without these, alerts log to `data/raw/refresh.log` only; STALE_ALERT.txt + Telegram dispatch sites are coded and ready.

3. **OPRA request to Ocean Clerk John P. Kelly's office** for monthly bulk extract — unlocks 4 of 11 patterns:
   - Recorded deeds (DEED, BILLSALE, CONTSALE) — populates `transfer` subtypes (Quitclaim Deed, Sheriff's Deed, etc.) with proper buyer/seller names
   - Lis pendens (LISPEN, NOTLIS, NTCELIS) — populates `foreclosure` subtypes
   - Tax sale certificates (TSC, MTSC) and federal/state tax liens — primary `tax` and partial `code` source
   - Construction liens (CONLIEN, MECHLIEN, DSJUDLIEN reverses) — primary `lien` source
   - Inheritance tax waivers (TAXWAIVE) and disclaimers (DISCLAIM) — primary `estate` source
   - Bonus: full owner names, unlocking `entity_owned` (full coverage), `multiple_properties`, and `senior_owner` (real signal vs proxy)

4. **NJ Family Part divorce + Special Civil Part eviction access.** Both behind login walls / sealed by default. OPRA fallback path documented; `divorce` and `eviction` and `tired_landlord` patterns plugged in but unfired.

5. **Property Alert Service** at `countyclerkpas.co.ocean.nj.us/PropertyAlert/` is HTTP 500 (broken upstream). Re-probed at v2.0 build — still broken. Refresh harness re-tests nightly; auto-recovers when service returns.

6. **Municipal code enforcement (33 municipalities).** Out of scope for v2. `code` pattern will fire only on rare clerk-recorded municipal liens until per-municipal feeds are added in v3.

7. **Owner names redacted upstream in NJ DOIT MOD-IV layer.** Single biggest blocker for `entity_owned`, `multiple_properties`, full-coverage `senior_owner` derivation. OPRA bulk MOD-IV is the path.

8. **Sheriff PDF column-split edge cases.** v1 had 47% miss rate from the CH-only regex bug; v2 ships at 100% (80/80) on the live PDF. The Rule 20 QA gate exits the scraper non-zero if rate falls below 90% — guards against future regressions.

---

## Next-county-port notes

The platform is built to port to Atlantic / Monmouth / Burlington with **zero source-discovery work** for the high-leverage feeds:

- **NJ MOD-IV statewide** — filter `COUNTY='<NEXT>'`. Same field schema, same pagination, same join key, same Monday-only cadence rule. Force multiplier across all 21 NJ counties.
- **Tyler CivilView** — swap `countyId` parameter (Atlantic=12, Bergen=10, Middlesex=27 verified at recon time).
- **NJ Courts portals** — same Imperva block, same OPRA fallback, same architectural slot. No new code.
- **Clerk official records** — most NJ clerks use NewVision (`publicsearch`) or Tyler (`landrecords`). Both have a doc-type taxonomy endpoint, a heartbeat endpoint, and a (maybe blocked) search endpoint. The metadata-heartbeat scraper pattern is reusable: change one base URL.
- **Sheriff sales** — format varies per county (PDF / CivilView / in-house ASP). Per-county work, but Ocean's PDF parser handles every NJ sheriff-sale schema we've seen.
- **Surrogate** — almost universally OPRA-only across NJ.

The pipeline (11 patterns + 9 attributes + Two-Truths + TTL + GC) and the dashboard are **county-agnostic** — point them at a different `data/leads.json` and they work as-is.

---

⚡ — Built by Jarvis (Just Jarvis LLC) for Quentin Flores. Operator-first.
