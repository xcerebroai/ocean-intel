"""
build_leads.py (v2.0) — joins raw scraper output, derives the 11-pattern
lead-type model + 11-attribute parcel-state model, enforces TTLs, garbage-
collects expired-only leads, runs the Two-Truths invariant + run-over-run
regression check, emits a single static `data/leads.json` for the dashboard.

Schema v2.0. See `methodology.html` (auto-generated) for the operator-readable
model. The schema is documented at the bottom of this file.

Exit codes:
  0 = success, leads.json written
  1 = Two-Truths violation
  2 = generic pipeline failure (raise → printed traceback)
  3 = run-over-run regression detected (>50% drop in any pattern)
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DATA = ROOT / "data"
LEADS_PATH = DATA / "leads.json"
LEADS_PREV = DATA / "leads.previous.json"
STALE_ALERT = ROOT / "STALE_ALERT.txt"

SCHEMA_VERSION = "2.0"

# ---------------- The 11 lead-type patterns + chip palette --------------------

PATTERN_NAMES = (
    "foreclosure", "tax", "lien", "estate", "code", "transfer",
    "bankruptcy", "divorce", "eviction", "tired_landlord", "surplus_owed",
)

PATTERN_DISPLAY = {
    "foreclosure":    "Foreclosure",
    "tax":            "Tax Distress",
    "lien":           "Liens",
    "estate":         "Estate / Probate",
    "code":           "Code / Condemnation",
    "transfer":       "Distressed Transfer",
    "bankruptcy":     "Bankruptcy",
    "divorce":        "Divorce",
    "eviction":       "Eviction",
    "tired_landlord": "Tired Landlord",
    "surplus_owed":   "Surplus Owed",
}

# Per-pattern signal-age TTL. None = never expires (structural condition).
SIGNAL_TTL_DAYS = {
    "foreclosure":    180,
    "tax":            None,
    "lien":           None,
    "estate":         365,
    "code":           365,
    "transfer":       1095,
    "bankruptcy":     720,
    "divorce":        720,
    "eviction":       365,
    "tired_landlord": 365,
    "surplus_owed":   1825,
}

# Sheriff sale TTL — expires after N consecutive refreshes without seeing the
# case (handled in pipeline by checking absence between current and previous
# leads.json, decrementing a counter stored on the signal). Default ~4 days.
SHERIFF_SALE_TTL_REFRESHES = 4

# ---------------- The 11 parcel-state attributes ------------------------------

ATTRIBUTE_NAMES = (
    "vacant", "absentee", "out_of_state", "senior_owner", "long_term_owned",
    "free_and_clear", "high_equity", "entity_owned", "multiple_properties",
)

ATTRIBUTE_DISPLAY = {
    "vacant":              "Vacant",
    "absentee":            "Absentee",
    "out_of_state":        "Out-of-state",
    "senior_owner":        "Senior owner",
    "long_term_owned":     "Long-term owned",
    "free_and_clear":      "Free-and-clear",
    "high_equity":         "High equity",
    "entity_owned":        "Entity-owned",
    "multiple_properties": "Multiple properties",
}

# ---------------- Doc-type → pattern + subtype map (NJ-specific) --------------
# Used when clerk_search unblocks. Today most are documented but unreached.
# Keyed by doc-type abbreviation. Value = (pattern, subtype-display-name).
DOCTYPE_TO_PATTERN_SUBTYPE: dict[str, tuple[str, str]] = {
    # foreclosure
    "LISPEN":   ("foreclosure", "Lis Pendens"),
    "NOTLIS":   ("foreclosure", "Notice of Lis Pendens"),
    "NTCELIS":  ("foreclosure", "Notice of Lis Pendens (Recorded)"),
    "FINJUDGE": ("foreclosure", "Final Judgment"),
    # tax
    "INREM":    ("tax",         "In Rem Tax Foreclosure"),
    "MTSC":     ("tax",         "Municipal Tax Sale Certificate"),
    "TSC":      ("tax",         "Tax Sale Certificate"),
    "FEDLIEN":  ("tax",         "Federal Tax Lien"),
    # lien
    "CONLIEN":  ("lien",        "Construction Lien"),
    "MECHLIEN": ("lien",        "Mechanic's Lien"),
    "MECHNOI":  ("lien",        "Mechanic's Notice of Intent"),
    "PHYSLIEN": ("lien",        "Physician's Lien"),
    "BRTYLIEN": ("lien",        "Bankruptcy Lien"),
    "ARCLIEN":  ("lien",        "Aircraft Lien"),
    "WAGECLM":  ("lien",        "Wage Claim"),
    "WAREXEC":  ("lien",        "Warrant of Execution"),
    "WRITEXEC": ("lien",        "Writ of Execution"),
    "STOPNOT":  ("lien",        "Stop Notice"),
    "INSTLIEN": ("lien",        "Institutional / Municipal Lien"),
    # estate
    "TAXWAIVE": ("estate",      "NJ Inheritance Tax Waiver"),
    "DISCLAIM": ("estate",      "Disclaimer"),
    "TRUSTAGR": ("estate",      "Trust Agreement"),
    "POA":      ("estate",      "Power of Attorney"),
    "REVPOA":   ("estate",      "Revocation of Power of Attorney"),
    # transfer
    "DEED":     ("transfer",    "Deed"),
    "BILLSALE": ("transfer",    "Bill of Sale"),
    "CONTSALE": ("transfer",    "Contract of Sale"),
}

# Negative doc types — discharges/releases, recorded for completeness, do not
# fire pattern (and ideally de-escalate, but de-escalation requires clerk
# unblock).
NEGATIVE_DOCTYPES = {
    "DISCHLIS", "DISTSC", "RLFESLEN",
    "DSCOLIEN", "DSMELIEN", "DMECHNOI", "DPHYLIEN", "DSJUDLIEN",
    "WARSATFN",
}

# ---------------- GIS deep link template (Rule 22) ----------------------------

GIS_DEEP_LINK_TEMPLATE = (
    "https://maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/"
    "MapServer/2/query?where=PAMS_PIN%3D%27{pid}%27&outFields=*&returnGeometry=false&f=html"
)


# ============================== IO ============================================

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def git_short_sha() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii").strip() or None
    except Exception:
        return None


# =========================== Helpers ==========================================

def make_pid(parcel: dict) -> str | None:
    pid = parcel.get("pid")
    if pid:
        return pid
    block = parcel.get("block")
    lot = parcel.get("lot")
    if not (block and lot):
        return None
    qual = parcel.get("qualifier") or ""
    if qual:
        return f"{block}-{lot}-{qual}"
    return f"{block}-{lot}"


_MUN_NORMALIZE_TOKENS = (
    " TOWNSHIP", " TWP", " BOROUGH", " BORO", " CITY", " VILLAGE",
    " (LBI)", " (NB)", " (SB)",
)

# Postal/CDP names that must be mapped to legal munis for parcel joins.
CDP_TO_MUN = {
    "WHITING": "MANCHESTER",
    "CRESTWOOD VILLAGE": "MANCHESTER",
    "BAYVILLE": "BERKELEY",
    "FORKED RIVER": "LACEY",
    "MANAHAWKIN": "STAFFORD",
    "NEW EGYPT": "PLUMSTED",
    "ROOSEVELT CITY": "BARNEGAT",  # legacy
    "LITTLE EGG HARB": "LITTLE EGG HARBOR",
}


def normalize_mun(s: str | None) -> str:
    if not s:
        return ""
    out = " " + s.upper().strip() + " "
    for tok in _MUN_NORMALIZE_TOKENS:
        out = out.replace(tok + " ", " ")
    out = out.replace(".", " ").replace(",", " ")
    out = " ".join(out.split())
    # Remap CDP → legal mun
    return CDP_TO_MUN.get(out, out)


def normalize_address(s: str | None) -> str:
    if not s:
        return ""
    return " ".join(s.upper().split())


def deed_date_to_iso(yymmdd: str | None) -> str | None:
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    yy, mm, dd = yymmdd[:2], yymmdd[2:4], yymmdd[4:6]
    yyyy = ("19" + yy) if int(yy) >= 30 else ("20" + yy)
    if not (1 <= int(mm) <= 12 and 1 <= int(dd) <= 31):
        return None
    return f"{yyyy}-{mm}-{dd}"


def parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def parse_mdy(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%m/%d/%Y").date()
    except Exception:
        return None


def years_owned(deed_iso: str | None) -> int | None:
    d = parse_iso(deed_iso)
    if not d:
        return None
    return max(0, (date.today() - d).days // 365)


# =========================== Parcel attributes ================================

def detect_attributes(parcel: dict) -> dict[str, bool]:
    attrs = {a: False for a in ATTRIBUTE_NAMES}

    situs_mun = normalize_mun(parcel.get("mun_name"))
    mail_city_state = (parcel.get("city_state") or "").upper().strip()

    # absentee — mailing city different from situs municipality
    if situs_mun and mail_city_state:
        mail_city = re.split(r"[,\s]+(NJ|FL|NY|PA|CA|TX|GA)\b", mail_city_state, maxsplit=1)[0].strip(", ")
        attrs["absentee"] = bool(mail_city) and mail_city.upper() not in situs_mun and situs_mun not in mail_city.upper()

    # out_of_state — mailing state explicitly not NJ
    if mail_city_state:
        # Look for the state token (last 2-letter token at end or " STATE " in middle)
        state_match = re.search(r"\b([A-Z]{2})\b\s*$", mail_city_state) or \
                      re.search(r",\s*([A-Z]{2})\b", mail_city_state)
        if state_match:
            attrs["out_of_state"] = state_match.group(1) != "NJ"

    # long_term_owned — 15+ years from DEED_DATE
    yo = years_owned(deed_date_to_iso(parcel.get("deed_date_yymmdd")))
    if yo is not None and yo >= 15:
        attrs["long_term_owned"] = True

    # senior_owner — proxy until full MOD-IV deductions field is wired:
    # long-term-owned AND tax bill < 50% of expected (~1.4% of net_value).
    nv = parcel.get("net_value") or 0
    tx = parcel.get("last_yr_tx") or 0
    if attrs["long_term_owned"] and nv > 0 and tx > 0:
        expected = nv * 0.014
        if tx < expected * 0.5:
            attrs["senior_owner"] = True

    # high_equity — proxy: assessed_value >= 2 * last_sale_price AND
    # owned 5+ yrs.
    sp = parcel.get("sale_price") or 0
    if sp > 0 and nv >= 2 * sp and yo is not None and yo >= 5:
        attrs["high_equity"] = True

    # entity_owned — owner regex (currently OWNER_NAME is redacted; fallback
    # is the mailing-address line which often has the entity)
    entity_re = re.compile(
        r"\b(LLC|INC|CORP|TRUST|LP|LTD|CO|COMPANY|HOLDINGS|ASSOCIATES|PARTNERS)\b",
        re.I,
    )
    blob = " ".join(filter(None, [
        parcel.get("owner_name"),
        parcel.get("st_address"),
        parcel.get("fac_name"),
    ]))
    if entity_re.search(blob):
        attrs["entity_owned"] = True

    # vacant, free_and_clear, multiple_properties — require external sources
    # not currently wired. Default false; pipeline computes multiple_properties
    # in a second pass below if owner_name is populated.
    return attrs


# =========================== Signal extractors ================================

def signal_for_sheriff(r: dict) -> dict:
    sale = r.get("sale_date")
    d = parse_mdy(sale)
    pattern = "foreclosure"
    subtype = "Sheriff Sale"
    # Edge: sheriff PDF "BANKRUPTCY" status fires bankruptcy too. Two signals
    # emitted by signals_from_sheriff() in that case.
    return {
        "_signal_id": r.get("_key"),
        "pattern": pattern,
        "subtype": subtype,
        "doc_type": "WRIT_OF_SALE",
        "source": "sheriff_foreclosures",
        "date": d.isoformat() if d else None,
        "block": r.get("block"),
        "lot": r.get("lot"),
        "mun_name": r.get("mun_name"),
        "case_no": r.get("case_no"),
        "case_type": r.get("case_type"),
        "foreclosure_no": r.get("foreclosure_no"),
        "plaintiff": r.get("plaintiff"),
        "defendant": r.get("defendant"),
        "amount": r.get("judgment_amount"),
        "status": r.get("status"),
        "situs_address": r.get("situs_address"),
        "zip5": r.get("zip5"),
        "payload": {
            "sale_date": sale, "case_no": r.get("case_no"),
            "plaintiff": r.get("plaintiff"), "defendant": r.get("defendant"),
            "amount": r.get("judgment_amount"), "status": r.get("status"),
            "situs": r.get("situs_address"),
            "case_type": r.get("case_type"),
        },
    }


def signals_from_sheriff(records: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        if r.get("doc_type") != "writ_of_sale":
            continue
        primary = signal_for_sheriff(r)
        out.append(primary)
        # Bankruptcy stay → also fire bankruptcy pattern
        if (r.get("status") or "").lower() == "bankruptcy":
            bk = dict(primary)
            bk["pattern"] = "bankruptcy"
            bk["subtype"] = "Foreclosure-Stay Bankruptcy"
            bk["_signal_id"] = (r.get("_key") or "") + ":bk"
            out.append(bk)
    return out


def signals_from_civilview(records: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for r in records:
        out.append({
            "_signal_id": r.get("_key"),
            "pattern": "foreclosure",
            "subtype": "Sheriff Sale (CivilView)",
            "doc_type": "CIVILVIEW_SALE",
            "source": "civilview_sales",
            "date": None,
            "case_no": r.get("sheriff_no") or r.get("col_0"),
            "payload": {k: v for k, v in r.items() if not k.startswith("_")},
        })
    return out


# NOTE: signals_from_parcel_self() was removed in the v2.1 reset.
# MOD-IV is enrichment, never a lead source. Nominal-consideration deed
# sub-types fire only from clerk DEED records (with full grantor/grantee/
# consideration metadata available to classify quitclaim / sheriff /
# executor / administrator / deed-in-lieu sub-types).


# =========================== Joining ==========================================

def attach_signals(parcels: dict[str, dict], external_signals: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    parcel_signals: dict[str, list[dict]] = defaultdict(list)
    orphans: list[dict] = []

    by_mun_block_lot: dict[tuple, str] = {}
    by_block_lot: dict[tuple, list[str]] = defaultdict(list)
    by_address: dict[str, str] = {}
    for pid, parcel in parcels.items():
        mun = normalize_mun(parcel.get("mun_name"))
        block = (parcel.get("block") or "").strip()
        lot = (parcel.get("lot") or "").strip()
        if mun and block and lot:
            by_mun_block_lot[(mun, block, lot)] = pid
        if block and lot:
            by_block_lot[(block, lot)].append(pid)
        addr = normalize_address(parcel.get("prop_loc"))
        if addr:
            by_address[addr] = pid

    for sig in external_signals:
        block = (sig.get("block") or "").strip()
        lot = (sig.get("lot") or "").strip()
        mun = normalize_mun(sig.get("mun_name"))
        # Some sheriff lots are multi-token ("1 & 6", "47, 48, 19, 50") —
        # try the first token as the canonical lot.
        first_lot_token = lot.split()[0].rstrip(",") if lot else lot
        pid = None
        if mun and block and first_lot_token:
            pid = by_mun_block_lot.get((mun, block, first_lot_token))
        if pid is None and block and first_lot_token:
            cands = by_block_lot.get((block, first_lot_token)) or []
            if len(cands) == 1:
                pid = cands[0]
        if pid is None:
            sit = normalize_address(sig.get("situs_address") or "")
            if sit and sit in by_address:
                pid = by_address[sit]
        if pid:
            parcel_signals[pid].append(sig)
            sig["parcel_match"] = pid
        else:
            sig["parcel_match"] = None
            orphans.append(sig)
    return parcel_signals, orphans


# =========================== Lead build =======================================

def cap_signals_per_pattern(signals: list[dict], cap: int = 3) -> list[dict]:
    """Keep at most `cap` signals per pattern, sorted desc by date."""
    by_pat: dict[str, list[dict]] = defaultdict(list)
    for s in signals:
        by_pat[s.get("pattern", "?")].append(s)
    out: list[dict] = []
    for p, items in by_pat.items():
        items.sort(key=lambda x: x.get("date") or "", reverse=True)
        out.extend(items[:cap])
    return out


def filter_expired(signals: list[dict]) -> list[dict]:
    today = date.today()
    keep: list[dict] = []
    for s in signals:
        ttl = SIGNAL_TTL_DAYS.get(s.get("pattern"))
        if ttl is None:
            keep.append(s)
            continue
        d = parse_iso(s.get("date"))
        if d is None:
            # No date means we can't expire it — keep but mark no-date.
            keep.append(s)
            continue
        if (today - d).days <= ttl:
            keep.append(s)
    return keep


def build_lead(parcel: dict, signals: list[dict]) -> dict | None:
    pid = make_pid(parcel)
    if not pid:
        return None

    # MOD-IV is enrichment-only — the parcel itself does not generate signals.
    all_signals = list(signals)
    all_signals = filter_expired(all_signals)
    if not all_signals:
        return None
    capped = cap_signals_per_pattern(all_signals, cap=3)

    patterns_set = {s["pattern"] for s in capped if s.get("pattern") in PATTERN_NAMES}
    if not patterns_set:
        return None
    patterns = sorted(patterns_set)

    attrs = detect_attributes(parcel)
    attributes_list = sorted(k for k, v in attrs.items() if v)

    last_sig_date = None
    for s in capped:
        d = s.get("date")
        if d and (last_sig_date is None or d > last_sig_date):
            last_sig_date = d

    deed_iso = deed_date_to_iso(parcel.get("deed_date_yymmdd"))
    yo = years_owned(deed_iso)

    return {
        "pid": pid,
        "block": parcel.get("block"),
        "lot": parcel.get("lot"),
        "qualifier": parcel.get("qualifier"),
        "owner": parcel.get("owner_name"),
        "mun_name": parcel.get("mun_name"),
        "mun_code": parcel.get("mun_code"),
        "county": parcel.get("county") or "OCEAN",
        "prop_class": parcel.get("prop_class"),
        "bldg_class": parcel.get("bldg_class"),
        "situs_address": parcel.get("prop_loc"),
        "mailing_address": parcel.get("st_address"),
        "mailing_city_state": parcel.get("city_state"),
        "mailing_zip": parcel.get("zip_code"),
        "year_built": parcel.get("yr_constr") or None,
        "land_val": parcel.get("land_val"),
        "imprvt_val": parcel.get("imprvt_val"),
        "assessed_value": parcel.get("net_value"),
        "last_yr_tx": parcel.get("last_yr_tx"),
        "last_sale_price": parcel.get("sale_price"),
        "last_sale_date": deed_iso,
        "deed_book": parcel.get("deed_book"),
        "deed_page": parcel.get("deed_page"),
        "calc_acre": parcel.get("calc_acre"),
        "dwell": parcel.get("dwell"),
        "years_owned": yo,
        "patterns": patterns,
        "pattern_count": len(patterns),
        "attributes": attributes_list,
        "attribute_count": len(attributes_list),
        "signals": capped,
        "last_signal_date": last_sig_date,
        "is_new_in_24h": False,  # set later when previous is loaded
    }


def build_orphan_lead(orphan: dict) -> dict | None:
    sig = filter_expired([orphan])
    if not sig:
        return None
    pseudo_pid = f"orphan:{orphan.get('source','?')}:{orphan.get('case_no','?')}:{orphan.get('block','?')}-{orphan.get('lot','?')}"
    return {
        "pid": pseudo_pid,
        "block": orphan.get("block"),
        "lot": orphan.get("lot"),
        "qualifier": None,
        "owner": None,
        "mun_name": orphan.get("mun_name"),
        "mun_code": None,
        "county": "OCEAN",
        "situs_address": orphan.get("situs_address"),
        "mailing_address": None,
        "mailing_city_state": None,
        "mailing_zip": None,
        "year_built": None,
        "assessed_value": None,
        "last_sale_price": None,
        "last_sale_date": None,
        "years_owned": None,
        "patterns": [orphan.get("pattern", "foreclosure")],
        "pattern_count": 1,
        "attributes": [],
        "attribute_count": 0,
        "parcel_match": None,
        "signals": sig,
        "last_signal_date": orphan.get("date"),
        "is_new_in_24h": False,
    }


# =========================== Two-Truths + regression ==========================

def two_truths_check(header: dict, records: list[dict]) -> None:
    derived_pat: Counter = Counter()
    derived_attr: Counter = Counter()
    derived_subtype: dict[str, Counter] = defaultdict(Counter)
    derived_stack: Counter = Counter()
    derived_total_signals = 0
    most_stacked = 0
    for r in records:
        for p in r.get("patterns", []):
            derived_pat[p] += 1
        for a in r.get("attributes", []):
            derived_attr[a] += 1
        for s in r.get("signals", []):
            derived_total_signals += 1
            p, sub = s.get("pattern"), s.get("subtype")
            if p and sub:
                derived_subtype[p][sub] += 1
        d = r.get("pattern_count", 0)
        if d > most_stacked:
            most_stacked = d
        derived_stack[d] += 1

    # tier_counts no longer required; instead pattern_counts, attribute_counts,
    # lead_type_subtype_counts, stack_depth_distribution, total_signals,
    # most_stacked_count.
    h_pat = header["pattern_counts"]
    h_attr = header["attribute_counts"]
    h_sub = header["lead_type_subtype_counts"]

    for p in PATTERN_NAMES:
        if int(h_pat.get(p, 0)) != int(derived_pat.get(p, 0)):
            raise RuntimeError(
                f"Two-Truths violation: header pattern_counts.{p}={h_pat.get(p)} != derived={derived_pat.get(p)}"
            )
    for a in ATTRIBUTE_NAMES:
        if int(h_attr.get(a, 0)) != int(derived_attr.get(a, 0)):
            raise RuntimeError(
                f"Two-Truths violation: header attribute_counts.{a}={h_attr.get(a)} != derived={derived_attr.get(a)}"
            )
    for p, subs in h_sub.items():
        for sub, cnt in subs.items():
            if int(cnt) != int(derived_subtype.get(p, Counter()).get(sub, 0)):
                raise RuntimeError(
                    f"Two-Truths violation: lead_type_subtype_counts[{p}][{sub}]={cnt} != derived={derived_subtype.get(p,Counter()).get(sub,0)}"
                )
    if int(header["total_signals"]) != derived_total_signals:
        raise RuntimeError(
            f"Two-Truths violation: total_signals header={header['total_signals']} != derived={derived_total_signals}"
        )
    if int(header["most_stacked_count"]) != most_stacked:
        raise RuntimeError(
            f"Two-Truths violation: most_stacked_count header={header['most_stacked_count']} != derived={most_stacked}"
        )
    h_stack = header["stack_depth_distribution"]
    for k, v in h_stack.items():
        if k == "6+":
            real = sum(c for d, c in derived_stack.items() if d >= 6)
        else:
            real = derived_stack.get(int(k), 0)
        if int(v) != int(real):
            raise RuntimeError(
                f"Two-Truths violation: stack_depth[{k}]={v} != derived={real}"
            )


def regression_check(prev: dict | None, header: dict) -> str | None:
    """Return alert message if regression detected, else None.

    Trigger: any pattern dropping by >50% run-over-run, OR any pattern dropping
    by >75% (alarm-level).
    """
    if not prev:
        return None
    prev_pat = prev.get("pattern_counts") or {}
    cur_pat = header["pattern_counts"]
    alerts: list[str] = []
    for p in PATTERN_NAMES:
        prev_c = prev_pat.get(p, 0)
        cur_c = cur_pat.get(p, 0)
        if prev_c >= 5:  # only alert when there's enough volume to be meaningful
            drop = (prev_c - cur_c) / prev_c
            if drop > 0.50:
                level = "ALERT" if drop > 0.75 else "WARN"
                alerts.append(f"{level}: pattern_counts.{p} dropped {prev_c} -> {cur_c} ({drop * 100:.0f}% drop)")
    if alerts:
        return "Run-over-run regression detected:\n" + "\n".join(alerts)
    return None


# =========================== Main =============================================

def _load_clerk_records() -> list[dict]:
    """Load clerk records from BOTH ingest paths.

    Path A: data/raw/clerk_opra/*.jsonl (manual OPRA bulk ingest, normalized
            by scrapers/clerk_opra_ingest.py)
    Path B: data/raw/clerk_seeded_*.jsonl (operator-seeded session scraper)
    """
    out: list[dict] = []
    opra_dir = RAW / "clerk_opra"
    if opra_dir.exists():
        for p in sorted(opra_dir.glob("*.jsonl")):
            out.extend(load_jsonl(p))
    for p in sorted(RAW.glob("clerk_seeded_*.jsonl")):
        out.extend(load_jsonl(p))
    return out


def signals_from_clerk(records: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Convert clerk records (OPRA + seeded paths share schema) to signals.

    Returns (positive_signals, negative_records). Negative records (DISCHLIS,
    DISTSC, RLFESLEN, etc.) de-escalate prior positive signals attached to
    the same parcel during the build pass.
    """
    positive: list[dict] = []
    negatives: list[dict] = []
    for r in records:
        dtype = (r.get("doc_type") or "").upper()
        if not dtype:
            continue
        if dtype in NEGATIVE_DOCTYPES:
            negatives.append(r)
            continue
        mapping = DOCTYPE_TO_PATTERN_SUBTYPE.get(dtype)
        if not mapping:
            # Doc type not in our pattern map (e.g. UCC1, ASSGN, MORT) —
            # tracked in raw archive but does not generate a lead.
            continue
        pattern, subtype = mapping
        # DEED sub-type refinement: classify quitclaim / sheriff / executor /
        # administrator / deed-in-lieu when the raw payload exposes it.
        if dtype == "DEED":
            grantor = " ".join(r.get("grantor") or []).upper() if isinstance(r.get("grantor"), list) else (r.get("grantor") or "").upper()
            consid = r.get("consideration")
            if "SHERIFF" in grantor:
                subtype = "Sheriff's Deed"
            elif any(tok in grantor for tok in ("EXECUTOR", "EXEC.", "EXEC,", " EXEC ")):
                subtype = "Executor's Deed"
            elif any(tok in grantor for tok in ("ADMINISTRATOR", "ADMIN.", "ADMIN,", " ADMR ")):
                subtype = "Administrator's Deed"
            elif "ESTATE OF" in grantor:
                subtype = "Estate Deed"
            elif consid is not None and isinstance(consid, (int, float)) and consid <= 10:
                subtype = "Quitclaim/Nominal Deed"
            else:
                subtype = "Deed"
        sig = {
            "_signal_id": r.get("_key") or r.get("instrument_number"),
            "pattern": pattern,
            "subtype": subtype,
            "doc_type": dtype,
            "source": r.get("_source") or "clerk",
            "date": r.get("recording_date") or r.get("date"),
            "block": r.get("block"),
            "lot": r.get("lot"),
            "qualifier": r.get("qualifier"),
            "mun_name": r.get("mun_name"),
            "case_no": r.get("case_no"),
            "instrument_number": r.get("instrument_number"),
            "book": r.get("book"),
            "page": r.get("page"),
            "amount": r.get("lien_amount") or r.get("consideration"),
            "grantor": r.get("grantor"),
            "grantee": r.get("grantee"),
            "payload": r.get("raw_payload") or {k: v for k, v in r.items() if not k.startswith("_")},
        }
        positive.append(sig)
    return positive, negatives


def apply_clerk_negatives(parcel_signals: dict[str, list[dict]], negatives: list[dict], parcels: dict[str, dict]) -> int:
    """De-escalation pass: when a clerk DISCHLIS / DISTSC / etc. is recorded
    AFTER a matching positive signal on the same parcel, mark the positive
    discharged. Returns count of de-escalations applied."""
    applied = 0
    if not negatives:
        return 0
    # Map negative doc type → which patterns it discharges
    DISCHARGE_MAP = {
        "DISCHLIS": "foreclosure",
        "DISTSC": "tax",
        "RLFESLEN": "tax",
        "DSCOLIEN": "lien",
        "DSMELIEN": "lien",
        "DMECHNOI": "lien",
        "DPHYLIEN": "lien",
        "DSJUDLIEN": "lien",
        "WARSATFN": "lien",
    }
    # Build lookup: (mun, block, lot) -> pid (same as attach_signals)
    by_mbl: dict[tuple, str] = {}
    for pid, parcel in parcels.items():
        mun = normalize_mun(parcel.get("mun_name"))
        block = (parcel.get("block") or "").strip()
        lot = (parcel.get("lot") or "").strip()
        if mun and block and lot:
            by_mbl[(mun, block, lot)] = pid
    for n in negatives:
        dtype = (n.get("doc_type") or "").upper()
        target_pat = DISCHARGE_MAP.get(dtype)
        if not target_pat:
            continue
        mun = normalize_mun(n.get("mun_name"))
        block = (n.get("block") or "").strip()
        lot = (n.get("lot") or "").strip()
        first_lot = lot.split()[0].rstrip(",") if lot else lot
        pid = by_mbl.get((mun, block, first_lot))
        if not pid or pid not in parcel_signals:
            continue
        n_date = n.get("recording_date") or n.get("date") or ""
        for s in parcel_signals[pid]:
            if s.get("pattern") != target_pat:
                continue
            s_date = s.get("date") or ""
            if n_date and s_date and n_date >= s_date:
                s["discharged"] = True
                s["discharged_date"] = n_date
                s["discharged_doc_type"] = dtype
                applied += 1
    return applied


def build(out_path: Path = LEADS_PATH, fail_on_regression: bool = True) -> dict:
    parcels_raw = load_jsonl(RAW / "nj_modiv_parcels.jsonl")
    sheriff_raw = load_jsonl(RAW / "sheriff_foreclosures_writ_of_sale.jsonl")
    civilview_raw = load_jsonl(RAW / "civilview_sales_sheriff_sale_listing.jsonl")
    clerk_meta = load_jsonl(RAW / "clerk_metadata_heartbeat.jsonl")
    clerk_records = _load_clerk_records()

    print(f"  parcels:    {len(parcels_raw):,}", flush=True)
    print(f"  sheriff:    {len(sheriff_raw):,}", flush=True)
    print(f"  civilview:  {len(civilview_raw):,}", flush=True)
    print(f"  clerk recs: {len(clerk_records):,}", flush=True)
    print(f"  clerk meta: {len(clerk_meta):,}", flush=True)

    parcels: dict[str, dict] = {}
    for p in parcels_raw:
        pid = make_pid(p)
        if pid:
            parcels[pid] = p

    ext: list[dict] = []
    ext.extend(signals_from_sheriff(sheriff_raw))
    ext.extend(signals_from_civilview(civilview_raw))
    clerk_pos, clerk_neg = signals_from_clerk(clerk_records)
    ext.extend(clerk_pos)

    parcel_signals, orphans = attach_signals(parcels, ext)
    discharged = apply_clerk_negatives(parcel_signals, clerk_neg, parcels)
    print(f"  attached signals: {sum(len(v) for v in parcel_signals.values()):,} on {len(parcel_signals):,} parcels", flush=True)
    print(f"  orphan signals:   {len(orphans):,}", flush=True)
    print(f"  clerk negatives:  {len(clerk_neg):,} (de-escalations applied: {discharged})", flush=True)

    records: list[dict] = []
    # Pass 1: signal-bearing parcels (sheriff + clerk events, all enriched with MOD-IV)
    for pid in parcel_signals:
        parcel = parcels.get(pid)
        if not parcel:
            continue
        lead = build_lead(parcel, parcel_signals[pid])
        if lead:
            records.append(lead)
    # NOTE (v2.1 reset): the v2.0 "Pass 2" generated leads from MOD-IV alone
    # (nominal-consideration deeds). That was wrong — MOD-IV is enrichment,
    # not a lead source. Pass 2 is removed.
    # Pass 3: orphan signals (events that couldn't join to a parcel)
    for o in orphans:
        l = build_orphan_lead(o)
        if l:
            records.append(l)

    # Stale GC: build_lead drops leads with zero remaining signals already.

    # Mark new in 24h via leads.previous.json comparison
    prev_path = LEADS_PREV
    prev_doc: dict | None = None
    if prev_path.exists():
        try:
            prev_doc = json.loads(prev_path.read_text(encoding="utf-8"))
        except Exception:
            prev_doc = None
    prev_pids = set()
    if prev_doc:
        prev_pids = {r.get("pid") for r in prev_doc.get("records", []) if r.get("pid")}
    if prev_pids:
        for r in records:
            r["is_new_in_24h"] = r["pid"] not in prev_pids

    # Compute multiple_properties attribute via owner-name index pass.
    # Owner names are mostly redacted upstream, so this typically fires 0;
    # documented in methodology. Still wired so it activates the moment
    # owner data lands.
    by_owner: Counter = Counter()
    for r in records:
        if r.get("owner"):
            by_owner[r["owner"].strip().upper()] += 1
    for r in records:
        own = (r.get("owner") or "").strip().upper()
        if own and by_owner[own] >= 3 and "multiple_properties" not in r["attributes"]:
            r["attributes"] = sorted(set(r["attributes"]) | {"multiple_properties"})
            r["attribute_count"] = len(r["attributes"])

    # Header counts
    pattern_counts = Counter()
    attr_counts = Counter()
    subtype_counts: dict[str, Counter] = defaultdict(Counter)
    source_attach: Counter = Counter()
    stack_dist: Counter = Counter()
    total_signals = 0
    most_stacked = 0
    new_in_24h = 0
    for r in records:
        for p in r["patterns"]:
            pattern_counts[p] += 1
        for a in r["attributes"]:
            attr_counts[a] += 1
        for s in r["signals"]:
            total_signals += 1
            src = s.get("source")
            if src:
                source_attach[src] += 1
            p, sub = s.get("pattern"), s.get("subtype")
            if p and sub:
                subtype_counts[p][sub] += 1
        d = r["pattern_count"]
        if d > most_stacked:
            most_stacked = d
        stack_dist[d] += 1
        if r.get("is_new_in_24h"):
            new_in_24h += 1

    stack_dist_out = {str(i): int(stack_dist.get(i, 0)) for i in (1, 2, 3, 4, 5)}
    stack_dist_out["6+"] = sum(c for d, c in stack_dist.items() if d >= 6)

    clerk_heartbeat = (clerk_meta or [{}])[-1] if clerk_meta else {}

    # Clerk ingest status (Phase 1 deliverable — visibility into both unblock paths)
    opra_dir = RAW / "clerk_opra"
    opra_files = sorted(opra_dir.glob("*.jsonl")) if opra_dir.exists() else []
    seeded_files = sorted(RAW.glob("clerk_seeded_*.jsonl"))
    opra_total = sum(1 for p in opra_files for _ in p.read_text(encoding="utf-8").splitlines() if _) if opra_files else 0
    seeded_total = sum(1 for p in seeded_files for _ in p.read_text(encoding="utf-8").splitlines() if _) if seeded_files else 0

    def _newest_mtime(paths):
        if not paths:
            return None
        return datetime.fromtimestamp(max(p.stat().st_mtime for p in paths), tz=timezone.utc).isoformat()

    seeded_seeded_at = os.environ.get("CLERK_SESSION_SEEDED_AT") or ""
    seeded_age_h = None
    if seeded_seeded_at:
        try:
            seeded_age_h = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(seeded_seeded_at.replace("Z", "+00:00"))).total_seconds() / 3600.0,
                1,
            )
        except Exception:
            seeded_age_h = None

    clerk_ingest_status = {
        "opra_last_received": _newest_mtime(opra_files),
        "opra_records_total": opra_total,
        "seeded_last_run": _newest_mtime(seeded_files),
        "seeded_records_last_run": seeded_total,
        "seeded_session_age_hours": seeded_age_h,
        "configured": bool(opra_files or seeded_files or seeded_seeded_at),
    }

    header = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_commit": git_short_sha(),
        "county": "Ocean",
        "state": "NJ",
        "gis_deep_link_template": GIS_DEEP_LINK_TEMPLATE,
        "pattern_display": PATTERN_DISPLAY,
        "attribute_display": ATTRIBUTE_DISPLAY,
        "signal_ttl_days": SIGNAL_TTL_DAYS,
        "pattern_counts": {p: int(pattern_counts.get(p, 0)) for p in PATTERN_NAMES},
        "attribute_counts": {a: int(attr_counts.get(a, 0)) for a in ATTRIBUTE_NAMES},
        "lead_type_subtype_counts": {p: dict(subs) for p, subs in subtype_counts.items()},
        "source_attach_counts": dict(source_attach),
        "stack_depth_distribution": stack_dist_out,
        "total_signals": total_signals,
        "new_in_24h": new_in_24h,
        "most_stacked_count": most_stacked,
        "parcel_total": len(parcels),
        "lead_total": len(records),
        "clerk_heartbeat": {
            "verifyDate": clerk_heartbeat.get("verifyDate"),
            "lastDocumentRecordedDateTime": clerk_heartbeat.get("lastDocumentRecordedDateTime"),
            "lastDocumentRecordedInfo": clerk_heartbeat.get("lastDocumentRecordedInfo"),
            "fetched_at": clerk_heartbeat.get("fetched_at"),
        },
        "clerk_ingest_status": clerk_ingest_status,
    }

    # Two-Truths
    two_truths_check(header, records)
    print("  Two-Truths check: PASS", flush=True)

    # Run-over-run regression
    regression_msg = regression_check(prev_doc, header)
    if regression_msg:
        STALE_ALERT.write_text(
            f"[{datetime.now(timezone.utc).isoformat()}]\n{regression_msg}\n",
            encoding="utf-8",
        )
        print(f"  REGRESSION: {regression_msg}", flush=True)
        if fail_on_regression:
            return {"_regression": regression_msg, **header}

    out = {**header, "records": records}

    # Rotate previous → leads.previous.json (do this BEFORE writing the new
    # leads.json so the previous version is preserved).
    if out_path.exists():
        try:
            LEADS_PREV.write_bytes(out_path.read_bytes())
        except Exception:
            pass
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB)", flush=True)
    return header


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(LEADS_PATH))
    ap.add_argument("--no-fail-on-regression", action="store_true",
                    help="ignore run-over-run regression and write leads.json anyway")
    args = ap.parse_args()
    res = build(Path(args.out), fail_on_regression=not args.no_fail_on_regression)
    if "_regression" in res:
        sys.exit(3)
    sys.exit(0)
