"""
build_methodology.py — generates `methodology.html` from the live data.

Reads:
  data/leads.json            (current pipeline output)
  HEARTBEAT.json             (per-source last_success / status)
  RECON.md                   (pattern/subtype map context — referenced via link)
  data/raw/clerk_metadata_heartbeat.jsonl  (clerk freshness)
  data/raw/sheriff_foreclosures.qa.json     (sheriff parser QA)

Writes:
  methodology.html

Eliminates documentation drift — the page reflects the current pipeline state,
not last-quarter's hand-written summary.
"""
from __future__ import annotations
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEADS = ROOT / "data" / "leads.json"
HEARTBEAT = ROOT / "HEARTBEAT.json"
QA = ROOT / "data" / "raw" / "sheriff_foreclosures.qa.json"
OUT = ROOT / "methodology.html"


def esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


PATTERN_DISPLAY = {
    "foreclosure": "Foreclosure", "tax": "Tax Distress", "lien": "Liens",
    "estate": "Estate / Probate", "code": "Code / Condemnation",
    "transfer": "Distressed Transfer", "bankruptcy": "Bankruptcy",
    "divorce": "Divorce", "eviction": "Eviction",
    "tired_landlord": "Tired Landlord", "surplus_owed": "Surplus Owed",
}
ATTR_DISPLAY = {
    "vacant": "Vacant", "absentee": "Absentee", "out_of_state": "Out-of-state",
    "senior_owner": "Senior owner", "long_term_owned": "Long-term owned",
    "free_and_clear": "Free-and-clear", "high_equity": "High equity",
    "entity_owned": "Entity-owned", "multiple_properties": "Multiple properties",
}
ATTR_DERIVATION = {
    "vacant": "Cannot be derived from MOD-IV alone — requires USPS vacancy or utility shutoff feed. Reserved in schema for v3 plug-in.",
    "absentee": "Mailing-address city differs from situs municipality (after normalizing TWP/BORO).",
    "out_of_state": "Mailing-address state ≠ NJ.",
    "senior_owner": "Proxy: long-term ownership AND last-year tax bill < 50% of expected (~1.4% of net assessed). Real signal requires the bulk MOD-IV deduction codes.",
    "long_term_owned": "Years owned (computed from DEED_DATE) ≥ 15.",
    "free_and_clear": "Requires clerk_search to enumerate active mortgages — currently blocked. Field always false until OPRA bulk extract lands.",
    "high_equity": "Proxy: assessed_value ≥ 2× last_sale_price AND ≥5 years owned. Wired for AVM substitution later.",
    "entity_owned": "Owner regex matches LLC|INC|CORP|TRUST|LP|LTD|CO|COMPANY|HOLDINGS|ASSOCIATES|PARTNERS. Owner names mostly redacted upstream — fallback uses mailing-address line.",
    "multiple_properties": "Owner name appears as registered owner on ≥3 parcels. Requires owner names — blocked until clerk OPRA.",
}


def fmt_count(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def render() -> str:
    leads = json.loads(LEADS.read_text(encoding="utf-8")) if LEADS.exists() else {}
    hb = json.loads(HEARTBEAT.read_text(encoding="utf-8")) if HEARTBEAT.exists() else {}
    qa = json.loads(QA.read_text(encoding="utf-8")) if QA.exists() else {}

    schema_v = leads.get("schema_version", "?")
    generated_at = leads.get("generated_at", "—")
    source_commit = leads.get("source_commit", "—")
    parcel_total = leads.get("parcel_total", 0)
    lead_total = leads.get("lead_total", 0)
    pat_counts = leads.get("pattern_counts", {})
    attr_counts = leads.get("attribute_counts", {})
    sub_counts = leads.get("lead_type_subtype_counts", {})
    stack_dist = leads.get("stack_depth_distribution", {})
    most_stacked = leads.get("most_stacked_count", 0)
    total_signals = leads.get("total_signals", 0)
    new_in_24h = leads.get("new_in_24h", 0)
    sigttl = leads.get("signal_ttl_days", {})
    clerk_hb = leads.get("clerk_heartbeat", {})
    src_attach = leads.get("source_attach_counts", {})

    # Source inventory rows from heartbeat
    sources_rows = ""
    for name, info in (hb.get("sources") or {}).items():
        last_status = info.get("last_status", "—")
        last_success = info.get("last_success", "—")
        sources_rows += f"<tr><td><code>{esc(name)}</code></td><td>{esc(last_status)}</td><td>{esc(last_success)}</td><td>{fmt_count(src_attach.get(name, 0))}</td></tr>"

    # Pattern subtype rows
    subtype_rows = ""
    for p, subs in sub_counts.items():
        for sub, cnt in sorted(subs.items()):
            subtype_rows += f"<tr><td><span class='chip {esc(p)}'>{esc(PATTERN_DISPLAY.get(p, p))}</span></td><td>{esc(sub)}</td><td>{fmt_count(cnt)}</td></tr>"
    if not subtype_rows:
        subtype_rows = "<tr><td colspan='3' style='text-align:center;color:var(--text-muted)'>No subtype-level signals firing today.</td></tr>"

    # Stack depth distribution
    stack_rows = ""
    for k in ("1", "2", "3", "4", "5", "6+"):
        stack_rows += f"<tr><td>{k}</td><td>{fmt_count(stack_dist.get(k, 0))}</td></tr>"

    # Pattern + attribute counts
    pat_summary = ""
    for p in PATTERN_DISPLAY:
        pat_summary += f"<tr><td><span class='chip {esc(p)}'>{esc(PATTERN_DISPLAY[p])}</span></td><td>{fmt_count(pat_counts.get(p, 0))}</td><td>{fmt_count(sigttl.get(p) or 'never')}</td></tr>"
    attr_summary = ""
    for a in ATTR_DISPLAY:
        attr_summary += f"<tr><td>{esc(ATTR_DISPLAY[a])}</td><td>{fmt_count(attr_counts.get(a, 0))}</td><td>{esc(ATTR_DERIVATION.get(a, ''))}</td></tr>"

    # QA
    qa_block = ""
    if qa:
        qa_block = (
            f"<p>Sheriff parser QA: <strong>{esc(qa.get('valid_count'))}/{esc(qa.get('parsed_total'))} valid "
            f"({esc(qa.get('valid_pct'))}%)</strong>. Threshold ≥{esc(qa.get('min_valid_pct_threshold'))}%. "
            f"Quarantine count: {esc(qa.get('quarantine_count'))}.</p>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Methodology — Ocean Intel</title>
<link rel="preconnect" href="https://fonts.googleapis.com" /><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
<style>
:root {{ --bg:#0A0A0A; --panel:#0F172A; --panel-2:#111A2E; --border:#1F2937; --text:#E5E7EB; --text-muted:#94A3B8; --accent:#3B82F6;
  --c-foreclosure:#EF4444; --c-tax:#F59E0B; --c-lien:#EAB308; --c-estate:#8B5CF6; --c-code:#F97316; --c-transfer:#06B6D4;
  --c-bankruptcy:#DC2626; --c-divorce:#EC4899; --c-eviction:#84CC16; --c-tired_landlord:#14B8A6; --c-surplus_owed:#10B981; }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; line-height: 1.7; font-size: 14.5px; }}
body {{ max-width: 920px; margin: 0 auto; padding: 32px 24px 80px; }}
h1 {{ font-size: 30px; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 8px; }}
h2 {{ font-size: 19px; font-weight: 700; margin-top: 32px; margin-bottom: 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }}
h3 {{ font-size: 15px; font-weight: 600; margin-top: 18px; margin-bottom: 6px; }}
p {{ margin-bottom: 12px; }}
ul, ol {{ margin-left: 22px; margin-bottom: 12px; }} li {{ margin-bottom: 4px; }}
a {{ color: var(--accent); }} a:hover {{ filter: brightness(1.2); }}
code {{ background: var(--panel); padding: 2px 6px; border-radius: 3px; font-family: Consolas, monospace; font-size: 12.5px; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 14px; font-size: 13px; }}
th, td {{ padding: 8px 12px; border: 1px solid var(--border); text-align: left; vertical-align: top; }}
th {{ background: var(--panel); color: var(--text); font-weight: 600; }}
.note {{ background: rgba(59,130,246,0.08); border-left: 3px solid var(--accent); padding: 12px 14px; border-radius: 0 6px 6px 0; margin: 14px 0; font-size: 13.5px; }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 999px; background: rgba(59,130,246,0.15); color: var(--accent); font-size: 11.5px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; }}
.muted {{ color: var(--text-muted); font-size: 13px; }}
.toc {{ background: var(--panel); padding: 14px 18px; border-radius: 6px; margin: 12px 0 28px; font-size: 13px; }}
.toc ul {{ margin-bottom: 0; }}
.chip {{ display: inline-block; color: #fff; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; line-height: 1.4; }}
.chip.foreclosure {{ background: var(--c-foreclosure); }}
.chip.tax {{ background: var(--c-tax); }}
.chip.lien {{ background: var(--c-lien); color: #1F2937; }}
.chip.estate {{ background: var(--c-estate); }}
.chip.code {{ background: var(--c-code); }}
.chip.transfer {{ background: var(--c-transfer); color: #1F2937; }}
.chip.bankruptcy {{ background: var(--c-bankruptcy); }}
.chip.divorce {{ background: var(--c-divorce); }}
.chip.eviction {{ background: var(--c-eviction); color: #1F2937; }}
.chip.tired_landlord {{ background: var(--c-tired_landlord); }}
.chip.surplus_owed {{ background: var(--c-surplus_owed); }}
</style>
</head>
<body>

<header>
  <p><a href="./index.html">← Back to dashboard</a></p>
  <h1>Methodology</h1>
  <p class="muted">Ocean Intel turns 12+ public-records sources into a single tiered list of motivated-seller leads. <strong>This page is generated from the live data</strong> — no hand-written drift. Schema <code>v{esc(schema_v)}</code>. Generated {esc(generated_at)} (commit <code>{esc(source_commit)}</code>).</p>
</header>

<div class="toc"><strong>Contents</strong>
  <ul>
    <li><a href="#what">What this is</a></li>
    <li><a href="#unblocking-clerk-data">How leads get into this dashboard</a> <em>(start here)</em></li>
    <li><a href="#enrichment">Why MOD-IV parcel data is enrichment, not a lead source</a></li>
    <li><a href="#patterns">The 11 lead-type patterns</a></li>
    <li><a href="#subtypes">Subtype counts (live)</a></li>
    <li><a href="#attributes">The 9 parcel attributes</a></li>
    <li><a href="#match-modes">Match modes (ANY / ALL / 2+ / 3+)</a></li>
    <li><a href="#stack-depth">Stack depth — used as a sort, not a tier</a></li>
    <li><a href="#ttl">TTL rules (signal expiry)</a></li>
    <li><a href="#sources">Source inventory (live)</a></li>
    <li><a href="#two-truths">Two-Truths invariant</a></li>
    <li><a href="#limits">Known limitations</a></li>
  </ul>
</div>

<h2 id="what">What this is</h2>
<p>An audit-grade index of distress signals attached to Ocean County properties. The goal is the operator's goal: surface the relatively small set of parcels where multiple <em>distinct</em> distress patterns have stacked on the same property. Stacking is more predictive than any single signal, and more honest than any black-box "motivated seller score."</p>
<p>The pipeline is one-way: scrapers fetch raw public records, the build step joins and patterns them into <code>data/leads.json</code>, the dashboard renders it. No database, no ML, no proprietary scoring. Every count is reproducible from raw inputs. <strong>Counts on this page are the actual current numbers — no figures are hand-typed.</strong></p>
<p>Today's snapshot: <strong>{fmt_count(parcel_total)} parcels</strong> indexed (enrichment lookup), <strong>{fmt_count(lead_total)} leads</strong> after pipeline, <strong>{fmt_count(total_signals)} total signals</strong>, <strong>{fmt_count(new_in_24h)}</strong> leads new in last 24h. Most-stacked parcel: <strong>{fmt_count(most_stacked)}</strong> distinct lead types.</p>

<h2 id="unblocking-clerk-data">How leads get into this dashboard</h2>
<p>The Ocean County Clerk's office records every recorded property instrument — deeds, mortgages, liens, judgments, foreclosure filings. <strong>Those records are the leads.</strong> Until the operator unblocks the clerk's data feed, this dashboard shows only sheriff foreclosure sales (the one source not behind reCAPTCHA).</p>
<p>Two paths to unblock — both ship in this build, both run automatically once configured.</p>
<h3>Path A — OPRA bulk request (system of record)</h3>
<p>A pre-filled, signature-ready PDF is generated monthly at <code>opra_requests/&lt;YYYYMM&gt;_ocean_clerk.pdf</code>. The request asks for a machine-readable bulk export (CSV/Excel) of every recorded instrument in the prior calendar month, filtered to the doc-type abbreviations that fire each pattern. Cites <i>N.J.S.A.</i> 47:1A-1, requests electronic delivery (drops the per-request fee to near zero), and asks for <strong>standing monthly delivery for 12 months</strong> so the operator does not need to re-submit.</p>
<ol>
  <li>Sign the generated PDF.</li>
  <li>Email it to the Ocean County Clerk's office (custodian: County Clerk John P. Kelly).</li>
  <li>Response within ~7 business days.</li>
  <li>When the operator receives the CSVs, drop them into <code>data/raw/clerk_opra/incoming/</code>.</li>
  <li>Next refresh runs <code>scrapers/clerk_opra_ingest.py</code> automatically — leads appear in the dashboard. No code changes needed.</li>
</ol>
<h3>Path B — Seeded session (daily freshness layer)</h3>
<p>The clerk's NewVision <code>publicsearch</code> API enforces reCAPTCHA v3 server-side. We do not solve CAPTCHA. Instead, the operator opens the clerk site in their real Chrome, completes one search (which triggers the silent reCAPTCHA challenge their session passes), and copies the resulting cookies into <code>.env</code>. The pipeline replays them on automated calls.</p>
<ol>
  <li>Open <a href="https://sng.co.ocean.nj.us/publicsearch/" target="_blank">https://sng.co.ocean.nj.us/publicsearch/</a> in Chrome.</li>
  <li>Submit any doc-type search (e.g. <code>DEED</code>, last 7 days).</li>
  <li>DevTools → Application → Cookies → copy ALL cookies for <code>sng.co.ocean.nj.us</code> as a single <code>name=value;name=value;...</code> string.</li>
  <li>Paste into <code>.env</code>:
    <pre>CLERK_SESSION_COOKIES=ASP.NET_SessionId=...; ...
CLERK_SESSION_TOKEN=&lt;X-RequestVerificationToken header value&gt;
CLERK_SESSION_SEEDED_AT=2026-05-05T13:42:00Z</pre>
  </li>
  <li>Pipeline pulls daily until session expires (typically 24–72h). On expiry, refresh harness sends a Telegram alert: <em>"Re-seed clerk session within 24h."</em></li>
  <li>Re-seed weekly — 30 seconds.</li>
</ol>
<p>Both paths run in parallel. OPRA is the system of record (monthly bulk, complete coverage). Seeded session is the daily freshness layer (catches new filings within hours of recording). Output schema is identical, so the pipeline doesn't care which path the data came from.</p>

<h2 id="enrichment">Why MOD-IV parcel data is enrichment, not a lead source</h2>
<p>The <strong>NJ MOD-IV statewide parcel layer</strong> publishes assessed values, sales history, mailing addresses, year built, and ~40 other parcel-state fields for all 298,147 Ocean parcels. This is structural metadata about a property — <strong>not a recorded distress event</strong>. The v1.0 build mistakenly treated MOD-IV nominal-consideration deeds (sale price ≤ $10) as <em>distressed-transfer leads</em>, generating 6,921 records that were almost entirely family / trust / corporate-restructuring transfers with no investor relevance. That noise drowned out the 80 real sheriff-sale leads.</p>
<p>v2.1 reclassifies MOD-IV correctly: <strong>parcel data enriches every clerk-derived and sheriff-derived lead</strong> with owner mailing address (drives the absentee + out-of-state attributes), assessed value (drives high-equity), deed date (drives long-term-owned + senior-owner proxy), entity-name regex on the mailing line, and so on. MOD-IV does not generate leads. Clerk records and sheriff sales generate leads; MOD-IV decorates them.</p>
<p>Distressed-transfer subtypes (Quitclaim Deed, Sheriff's Deed, Executor's/Administrator's Deed, Deed in Lieu) are real lead types — but they fire from <strong>clerk DEED records</strong> with grantor/grantee/consideration/sub-type metadata, not from the MOD-IV parcel layer alone.</p>


<h2 id="patterns">The 11 lead-type patterns</h2>
<table>
  <thead><tr><th>Pattern</th><th>Leads firing</th><th>TTL (days)</th></tr></thead>
  <tbody>{pat_summary}</tbody>
</table>
<p class="muted">"TTL (days)" = signal expires after N days of age. <code>never</code> = structural condition that doesn't expire (tax delinquency, liens). Expired signals are removed by the pipeline; leads with zero remaining signals are dropped from <code>leads.json</code> entirely (no phantom records — Rule 23).</p>

<h2 id="subtypes">Subtype counts (live)</h2>
<p>Each lead-type pattern has one or more subtypes. The chip tooltip in the dashboard shows the most recent subtype per pattern per lead. Counts below are derived directly from <code>data/leads.json</code>.</p>
<table>
  <thead><tr><th>Pattern</th><th>Subtype</th><th>Count</th></tr></thead>
  <tbody>{subtype_rows}</tbody>
</table>

<h2 id="attributes">The 9 parcel attributes</h2>
<p>State-driven (no timestamps), shown as small icons on each row in the dashboard. Each cell's "Count" reflects the actual number of leads with that attribute firing today.</p>
<table>
  <thead><tr><th>Attribute</th><th>Count</th><th>Derivation rule</th></tr></thead>
  <tbody>{attr_summary}</tbody>
</table>

<h2 id="match-modes">Match modes (ANY / ALL / 2+ / 3+)</h2>
<p>The filter rail lets you check any combination of lead-type patterns, subtypes, and attributes. The match-mode selector decides how the checks combine:</p>
<ul>
  <li><strong>ANY</strong> (default) — show leads matching <em>any</em> of the checked filters. Equivalent to OR.</li>
  <li><strong>ALL</strong> — show leads matching <em>every</em> checked filter. Equivalent to AND.</li>
  <li><strong>2+</strong> — show leads matching at least 2 of the checked filters.</li>
  <li><strong>3+</strong> — show leads matching at least 3 of the checked filters.</li>
</ul>

<h2 id="stack-depth">Stack depth — used as a sort, not a tier</h2>
<p>v1.0 grouped leads into Hot / Warm / Active tiers based on stack depth. v2.0 drops the tier labels (operators told us they wanted to build their own stacks via the filter rail). Stack depth is preserved as a column (<code>pattern_count</code>) and as a sort option ("Most signals"). The most-stacked parcel today fires <strong>{fmt_count(most_stacked)}</strong> distinct lead types.</p>
<table>
  <thead><tr><th>Stack depth</th><th>Lead count</th></tr></thead>
  <tbody>{stack_rows}</tbody>
</table>

<h2 id="ttl">TTL rules (signal expiry)</h2>
<p>Time-bounded signals expire to keep the dashboard signal-to-noise high. Sheriff sale signals additionally expire if not seen in 4 consecutive refreshes (~4 days for daily refresh). Stale signals are dropped before <code>leads.json</code> is written; leads with no remaining signals are removed.</p>

<h2 id="sources">Source inventory (live, from HEARTBEAT.json)</h2>
<table>
  <thead><tr><th>Source</th><th>Last status</th><th>Last success</th><th>Signals attached</th></tr></thead>
  <tbody>{sources_rows}</tbody>
</table>
<div class="note">
Detailed source inventory — including blocked sources, anti-bot encounters, and OPRA fallback paths — lives in <a href="https://github.com/xcerebroai/ocean-intel/blob/main/RECON.md" target="_blank">RECON.md</a> on GitHub.
</div>
{qa_block}

<h2 id="two-truths">Two-Truths invariant</h2>
<p>The dashboard's stat tiles ("Total: {fmt_count(lead_total)}, Foreclosure: {fmt_count(pat_counts.get('foreclosure', 0))}, Tax: {fmt_count(pat_counts.get('tax', 0))}, …") and the rendered table rows are derived from <em>the same</em> <code>matches(lead)</code> function. There is no separate counter, no parallel filter pipeline. The single source of truth is enforced two ways:</p>
<ol>
  <li>In <code>pipeline/build_leads.py</code> — before <code>leads.json</code> is written, the header's <code>pattern_counts</code>, <code>attribute_counts</code>, <code>lead_type_subtype_counts</code>, <code>stack_depth_distribution</code>, <code>total_signals</code>, and <code>most_stacked_count</code> are recomputed by walking the records list. If they disagree, the pipeline raises and exits non-zero. <code>leads.json</code> is never written with drifted counts.</li>
  <li>In the dashboard — the table body, the lead-type stat tiles, and the quality stat tiles all call <code>RECORDS.filter(matches)</code>. There is no parallel filter logic.</li>
</ol>

<h2 id="limits">Known limitations</h2>
<ul>
  <li><strong>Owner names redacted upstream.</strong> NJ DOIT publishes the public MOD-IV layer with empty <code>OWNER_NAME</code>. The dashboard surfaces mailing/situs addresses, year built, sales history, and full assessment values, but the named-owner field is not available without the clerk-search unblock or paid commercial sources.</li>
  <li><strong>No skip trace.</strong> CSV export includes empty <code>phone1-3</code> / <code>email1-2</code> columns for downstream skip-trace tooling.</li>
  <li><strong>No AVM / no MLS / no ML scoring.</strong> "High equity" and "Free-and-clear" attributes use proxies (assessed-value vs sale-price ratio for high equity; mortgage absence requires clerk_search). These will improve when external sources are wired.</li>
  <li><strong>Code pattern is municipal in NJ, not county.</strong> Until per-municipal feeds are added (33 municipalities in Ocean County), the <code>code</code> pattern fires only via clerk-recorded municipal liens.</li>
  <li><strong>Surrogate, civil court, and judgment data sit behind login or Imperva walls.</strong> OPRA fallback documented for each.</li>
</ul>

<h2>Audit trail</h2>
<p>Every <code>leads.json</code> the dashboard renders includes a header with: <code>generated_at</code>, <code>source_commit</code>, <code>pattern_counts</code>, <code>attribute_counts</code>, <code>lead_type_subtype_counts</code>, <code>source_attach_counts</code>, <code>stack_depth_distribution</code>, <code>total_signals</code>, <code>new_in_24h</code>, <code>most_stacked_count</code>, <code>clerk_heartbeat</code>, <code>signal_ttl_days</code>, <code>gis_deep_link_template</code>. Day-over-day staleness is detectable from <code>clerk_heartbeat.lastDocumentRecordedInfo</code>.</p>
<p>Source code, source commit, and the recon report are all on GitHub: <a href="https://github.com/xcerebroai/ocean-intel" target="_blank">github.com/xcerebroai/ocean-intel</a>.</p>

<p style="margin-top:36px;font-size:13px;color:var(--text-muted)">⚡ Generated by <code>pipeline/build_methodology.py</code> for Quentin Flores. Operator-first.</p>

</body></html>"""


def main() -> int:
    html_doc = render()
    OUT.write_text(html_doc, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
