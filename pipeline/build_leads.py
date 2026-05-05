"""
build_leads.py — joins raw scraper output, derives the 6-pattern stack
(jfc, tax, estate, code, lien, transfer), assigns a tier from STACK DEPTH (not
score sum), and emits a single static `data/leads.json` for the dashboard.

Contract enforced:
  - Tier comes from stack depth.
  - One `matches()` function would be in the dashboard; here we mirror the
    pattern-build path: every `pattern_counts` entry in the header is derived
    by walking `records[]`, never from a separate counter.
  - Two-Truths invariant runs before write: header `tier_counts` and
    `pattern_counts` must equal counts re-derived from the records list. If
    not equal, raises and exits with code != 0.
  - Per-pattern signal cap = 3 most recent.
  - No mock data — fields default to None when not derivable.
"""
from __future__ import annotations
import argparse
import json
import os
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


PATTERN_NAMES = ("jfc", "tax", "estate", "code", "lien", "transfer")


# Doc-type → pattern map (NJ-specific; sourced from RECON.md)
DOCTYPE_PATTERNS: dict[str, list[str]] = {
    # jfc
    "LISPEN": ["jfc"], "NOTLIS": ["jfc"], "NTCELIS": ["jfc"],
    # tax
    "INREM": ["tax"], "MTSC": ["tax"], "TSC": ["tax"], "FEDLIEN": ["tax"],
    "TAXWAIVE": ["estate", "tax"],
    # estate
    "DISCLAIM": ["estate"], "TRUSTAGR": ["estate"],
    "POA": ["estate"], "REVPOA": ["estate"],
    # code (sparse — only fires when a clerk-recorded municipal lien appears)
    "INSTLIEN": ["code", "lien"],
    # lien
    "CONLIEN": ["lien"], "MECHLIEN": ["lien"], "MECHNOI": ["lien"],
    "PHYSLIEN": ["lien"], "BRTYLIEN": ["lien"], "ARCLIEN": ["lien"],
    "WAGECLM": ["lien"], "WAREXEC": ["lien"], "WRITEXEC": ["lien"],
    "STOPNOT": ["lien"],
    # transfer (deed-level signals; nominal-consideration heuristic applied
    # downstream of doctype assignment)
    "DEED": ["transfer"], "BILLSALE": ["transfer"], "CONTSALE": ["transfer"],
    "FINJUDGE": ["transfer"],
}

NEGATIVE_DOCTYPES = {  # discharges/releases — record the event but do NOT fire pattern
    "DISCHLIS", "DISTSC", "RLFESLEN",
    "DSCOLIEN", "DSMELIEN", "DMECHNOI", "DPHYLIEN", "DSJUDLIEN",
    "WARSATFN",
}


# -----------------------------------------------------------------------------
# IO

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
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL)
        return out.decode("ascii").strip() or None
    except Exception:
        return None


# -----------------------------------------------------------------------------
# PID and join helpers

def make_pid(parcel: dict) -> str | None:
    """NJ canonical PID = block-lot[-qualifier]. We use PAMS_PIN directly when
    available (CD_CODE_BLOCK_LOT format), since that's the join key NJ uses."""
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


def normalize_address(s: str | None) -> str:
    if not s:
        return ""
    return " ".join(s.upper().split())


_MUN_NORMALIZE_TOKENS = (
    " TOWNSHIP", " TWP", " BOROUGH", " BORO", " CITY", " VILLAGE",
    " (LBI)", " (NB)", " (SB)",
)


def normalize_mun(s: str | None) -> str:
    """Strip suffixes ('TOWNSHIP'/'TWP'/'BORO') and parenthetical qualifiers
    so 'LACEY TOWNSHIP' (sheriff) matches 'LACEY TWP' (parcels)."""
    if not s:
        return ""
    out = " " + s.upper().strip() + " "
    for tok in _MUN_NORMALIZE_TOKENS:
        out = out.replace(tok + " ", " ")
    out = out.replace(".", " ").replace(",", " ")
    return " ".join(out.split())


def deed_date_to_iso(yymmdd: str | None) -> str | None:
    if not yymmdd or len(yymmdd) != 6 or not yymmdd.isdigit():
        return None
    yy, mm, dd = yymmdd[:2], yymmdd[2:4], yymmdd[4:6]
    yyyy = ("19" + yy) if int(yy) >= 30 else ("20" + yy)
    try:
        return f"{yyyy}-{mm}-{dd}"
    except Exception:
        return None


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


# -----------------------------------------------------------------------------
# Signal extractors per source

def signals_from_sheriff(records: Iterable[dict]) -> list[dict]:
    """Each sheriff PDF row → one `jfc` signal."""
    out = []
    for r in records:
        if r.get("doc_type") != "writ_of_sale":
            continue
        sale = r.get("sale_date")
        d = parse_mdy(sale)
        out.append({
            "_signal_id": r.get("_key"),
            "pattern": "jfc",
            "doc_type": "WRIT_OF_SALE",
            "source": "sheriff_foreclosures",
            "date": d.isoformat() if d else None,
            "block": r.get("block"),
            "lot": r.get("lot"),
            "mun_name": r.get("mun_name"),
            "case_no": r.get("case_no"),
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
            },
        })
    return out


def signals_from_civilview(records: Iterable[dict]) -> list[dict]:
    out = []
    for r in records:
        out.append({
            "_signal_id": r.get("_key"),
            "pattern": "jfc",
            "doc_type": "CIVILVIEW_SALE",
            "source": "civilview_sales",
            "date": None,
            "case_no": r.get("sheriff_no") or r.get("col_0"),
            "payload": {k: v for k, v in r.items() if not k.startswith("_")},
        })
    return out


def signals_from_parcel_self(parcel: dict) -> list[dict]:
    """Derive parcel-internal signals: nominal-consideration recent deed
    (transfer pattern), tax delinquency proxy (none reliable from this layer
    alone), absentee ownership flag (computed in the lead, not a signal)."""
    out = []
    sale_price = parcel.get("sale_price")
    deed_iso = deed_date_to_iso(parcel.get("deed_date_yymmdd"))
    # Nominal consideration deed within 3 years (and only if we have a real
    # deed date — undated nominal sales are usually historical artifacts and
    # would inflate the file past GitHub's 50MB cap if surfaced as leads).
    if sale_price is not None and isinstance(sale_price, (int, float)) and sale_price <= 10:
        try:
            d = parse_iso(deed_iso) if deed_iso else None
        except Exception:
            d = None
        if d is not None and (date.today() - d) <= timedelta(days=3 * 365):
            out.append({
                "_signal_id": f"parcel-nominal:{parcel.get('pid')}",
                "pattern": "transfer",
                "doc_type": "DEED_NOMINAL",
                "source": "nj_modiv_parcels",
                "date": deed_iso,
                "payload": {
                    "sale_price": sale_price,
                    "deed_date": deed_iso,
                    "deed_book": parcel.get("deed_book"),
                    "deed_page": parcel.get("deed_page"),
                    "sales_code": parcel.get("sales_code"),
                },
            })
    return out


# -----------------------------------------------------------------------------
# Lead build

def years_owned(deed_iso: str | None) -> int | None:
    d = parse_iso(deed_iso)
    if not d:
        return None
    return max(0, (date.today() - d).days // 365)


def is_absentee(parcel: dict) -> bool:
    """Mailing address differs from situs city (or is out-of-state)."""
    situs_mun = (parcel.get("mun_name") or "").upper().strip()
    mail_city_state = (parcel.get("city_state") or "").upper().strip()
    if not (situs_mun and mail_city_state):
        return False
    # Pull state out of "CITY, ST" or "CITY ST"
    if " NJ" not in mail_city_state and ", NJ" not in mail_city_state:
        return True
    # If NJ but city differs from situs municipality
    mail_city = mail_city_state.split(",")[0].split(" NJ")[0].strip()
    return bool(mail_city) and mail_city not in situs_mun and situs_mun not in mail_city


def is_entity_owner(parcel: dict) -> bool:
    """OWNER_NAME is redacted upstream — fall back to mailing-address signals."""
    nm = (parcel.get("owner_name") or "").upper()
    mail = (parcel.get("st_address") or "").upper()
    blob = nm + " " + mail
    if not blob.strip():
        return False
    return any(tok in blob for tok in (" LLC", " LP", " INC", " CORP", " TRUST", " ASSOC", " HOLDINGS", " PARTNERS"))


def attach_signals(parcels: dict[str, dict], external_signals: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """Returns (parcel_id → list of signals) and orphans list.

    Sheriff signals attach by mun_name + block + lot when available, fallback
    to address contains."""
    parcel_signals: dict[str, list[dict]] = defaultdict(list)
    orphans: list[dict] = []

    # Build lookup tables
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
        pid = None
        if mun and block and lot:
            pid = by_mun_block_lot.get((mun, block, lot))
        if pid is None and block and lot:
            cands = by_block_lot.get((block, lot)) or []
            if len(cands) == 1:
                pid = cands[0]
        if pid is None:
            # Address fuzzy — situs_address minus number → contains street name
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


def build_lead(parcel: dict, signals: list[dict]) -> dict | None:
    """Combine parcel + attached signals into a lead. Drop parcels with no
    pattern-firing signals."""
    pid = make_pid(parcel)
    if not pid:
        return None

    # Parcel-internal signals (nominal deeds, etc.)
    sigs = list(signals) + signals_from_parcel_self(parcel)

    # Group by pattern, sort newest first, cap at 3 per pattern
    by_pattern: dict[str, list[dict]] = defaultdict(list)
    for s in sigs:
        p = s.get("pattern")
        if p in PATTERN_NAMES:
            by_pattern[p].append(s)

    if not by_pattern:
        return None

    capped: list[dict] = []
    for p, items in by_pattern.items():
        items.sort(key=lambda x: x.get("date") or "", reverse=True)
        capped.extend(items[:3])

    patterns = sorted(by_pattern.keys())
    n = len(patterns)
    if n >= 3:
        tier = "hot"
    elif n == 2:
        tier = "warm"
    elif n == 1:
        tier = "active"
    else:
        return None

    deed_iso = deed_date_to_iso(parcel.get("deed_date_yymmdd"))
    yo = years_owned(deed_iso)

    lead = {
        "pid": pid,
        "block": parcel.get("block"),
        "lot": parcel.get("lot"),
        "qualifier": parcel.get("qualifier"),
        "owner_name": parcel.get("owner_name"),
        "mun_name": parcel.get("mun_name"),
        "mun_code": parcel.get("mun_code"),
        "county": parcel.get("county") or "OCEAN",
        "prop_class": parcel.get("prop_class"),
        "bldg_class": parcel.get("bldg_class"),
        "situs_address": parcel.get("prop_loc"),
        "mailing_address": parcel.get("st_address"),
        "mailing_city_state": parcel.get("city_state"),
        "mailing_zip": parcel.get("zip_code"),
        "absentee": is_absentee(parcel),
        "entity_owner": is_entity_owner(parcel),
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
        "tier": tier,
        "patterns": patterns,
        "stack_depth": n,
        "signals": capped,
        "flags": {
            "demolition_order": False,        # not derivable from current sources
            "senior_exemption": False,        # ditto
        },
    }
    return lead


def build_orphan_lead(orphan: dict) -> dict:
    """When a sheriff-PDF signal can't be joined to a parcel, surface it
    anyway as a synthetic lead so it's not lost."""
    pseudo_pid = f"orphan:{orphan.get('source','?')}:{orphan.get('case_no','?')}:{orphan.get('block','?')}-{orphan.get('lot','?')}"
    return {
        "pid": pseudo_pid,
        "block": orphan.get("block"),
        "lot": orphan.get("lot"),
        "qualifier": None,
        "owner_name": None,
        "mun_name": orphan.get("mun_name"),
        "county": "OCEAN",
        "situs_address": orphan.get("situs_address"),
        "mailing_address": None,
        "absentee": False,
        "entity_owner": False,
        "year_built": None,
        "assessed_value": None,
        "last_sale_price": None,
        "last_sale_date": None,
        "tier": "active",
        "patterns": [orphan.get("pattern", "jfc")],
        "stack_depth": 1,
        "parcel_match": None,
        "signals": [orphan],
        "flags": {"demolition_order": False, "senior_exemption": False},
    }


# -----------------------------------------------------------------------------
# Two-Truths

def two_truths_check(header: dict, records: list[dict]) -> None:
    """Recompute counts from records[]. Raise if header drifted."""
    derived_tier = Counter(r["tier"] for r in records)
    derived_pattern: Counter = Counter()
    for r in records:
        for p in r.get("patterns", []):
            derived_pattern[p] += 1

    h_tier = header["tier_counts"]
    h_pattern = header["pattern_counts"]

    # Compare
    for k in ("hot", "warm", "active"):
        if int(h_tier.get(k, 0)) != int(derived_tier.get(k, 0)):
            raise RuntimeError(
                f"Two-Truths violation: header tier_counts.{k}={h_tier.get(k)} != "
                f"records-derived={derived_tier.get(k)}"
            )
    for p in PATTERN_NAMES:
        if int(h_pattern.get(p, 0)) != int(derived_pattern.get(p, 0)):
            raise RuntimeError(
                f"Two-Truths violation: header pattern_counts.{p}={h_pattern.get(p)} != "
                f"records-derived={derived_pattern.get(p)}"
            )


# -----------------------------------------------------------------------------
# Main

def build(out_path: Path = LEADS_PATH) -> dict:
    parcels_raw = load_jsonl(RAW / "nj_modiv_parcels.jsonl")
    sheriff_raw = load_jsonl(RAW / "sheriff_foreclosures_writ_of_sale.jsonl")
    civilview_raw = load_jsonl(RAW / "civilview_sales_sheriff_sale_listing.jsonl")
    clerk_meta = load_jsonl(RAW / "clerk_metadata_heartbeat.jsonl")

    print(f"  parcels:    {len(parcels_raw):,}", flush=True)
    print(f"  sheriff:    {len(sheriff_raw):,}", flush=True)
    print(f"  civilview:  {len(civilview_raw):,}", flush=True)
    print(f"  clerk meta: {len(clerk_meta):,}", flush=True)

    # Index parcels by pid
    parcels: dict[str, dict] = {}
    for p in parcels_raw:
        pid = make_pid(p)
        if pid:
            parcels[pid] = p

    # Build external signal pool
    ext: list[dict] = []
    ext.extend(signals_from_sheriff(sheriff_raw))
    ext.extend(signals_from_civilview(civilview_raw))

    # Attach
    parcel_signals, orphans = attach_signals(parcels, ext)
    print(f"  attached signals: {sum(len(v) for v in parcel_signals.values()):,} on {len(parcel_signals):,} parcels", flush=True)
    print(f"  orphan signals:   {len(orphans):,}", flush=True)

    # Build leads
    records: list[dict] = []
    # Pass 1: signal-bearing parcels
    for pid in parcel_signals:
        parcel = parcels.get(pid)
        if not parcel:
            continue
        lead = build_lead(parcel, parcel_signals[pid])
        if lead:
            records.append(lead)
    # Pass 2: parcels with no external signal but whose own attributes fire
    # patterns (e.g., nominal-consideration recent deed → transfer)
    seen_pids = {r["pid"] for r in records}
    for pid, parcel in parcels.items():
        if pid in seen_pids:
            continue
        lead = build_lead(parcel, [])
        if lead:
            records.append(lead)
    # Pass 3: orphan signals → synthetic leads
    for o in orphans:
        records.append(build_orphan_lead(o))

    # ------- Header metrics -------
    tier_counts = Counter(r["tier"] for r in records)
    pattern_counts: Counter = Counter()
    doc_type_counts: Counter = Counter()
    source_attach: Counter = Counter()
    transfer_rule_counts = Counter()

    warm_total = 0
    warm_high_conf = 0
    pattern_combo_counts: Counter = Counter()

    for r in records:
        for p in r.get("patterns", []):
            pattern_counts[p] += 1
        for s in r.get("signals", []):
            dt = s.get("doc_type")
            if dt:
                doc_type_counts[dt] += 1
            src = s.get("source")
            if src:
                source_attach[src] += 1
            if s.get("doc_type") == "DEED_NOMINAL":
                transfer_rule_counts["nominal_consideration"] += 1
            if s.get("doc_type") == "WRIT_OF_SALE":
                transfer_rule_counts["sheriff_sale_pending"] += 1
        if r["tier"] == "warm":
            warm_total += 1
            # "high confidence" warm: both signals are recent (<= 365d)
            recents = []
            for s in r.get("signals", []):
                d = parse_iso(s.get("date"))
                if d and (date.today() - d).days <= 365:
                    recents.append(s.get("pattern"))
            if len(set(recents)) >= 2:
                warm_high_conf += 1
        combo = tuple(sorted(set(r.get("patterns", []))))
        if combo:
            pattern_combo_counts[combo] += 1

    warm_pct = round(100 * warm_high_conf / warm_total, 1) if warm_total else 0.0
    top_combos = [(list(k), v) for k, v in pattern_combo_counts.most_common(10)]

    clerk_heartbeat = (clerk_meta or [{}])[-1] if clerk_meta else {}

    header = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_commit": git_short_sha(),
        "county": "Ocean",
        "state": "NJ",
        "tier_counts": {
            "hot": int(tier_counts.get("hot", 0)),
            "warm": int(tier_counts.get("warm", 0)),
            "active": int(tier_counts.get("active", 0)),
        },
        "pattern_counts": {p: int(pattern_counts.get(p, 0)) for p in PATTERN_NAMES},
        "source_attach_counts": dict(source_attach),
        "doc_type_counts": dict(doc_type_counts),
        "transfer_rule_counts": dict(transfer_rule_counts),
        "warm_tier_high_confidence_pct": warm_pct,
        "top_pattern_combos": top_combos,
        "clerk_heartbeat": {
            "verifyDate": clerk_heartbeat.get("verifyDate"),
            "lastDocumentRecordedDateTime": clerk_heartbeat.get("lastDocumentRecordedDateTime"),
            "lastDocumentRecordedInfo": clerk_heartbeat.get("lastDocumentRecordedInfo"),
            "fetched_at": clerk_heartbeat.get("fetched_at"),
        },
        "parcel_total": len(parcels),
        "lead_total": len(records),
    }

    # Two-Truths
    two_truths_check(header, records)
    print(f"  Two-Truths check: PASS", flush=True)

    out = {**header, "records": records}

    # Rotate previous
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
    args = ap.parse_args()
    build(Path(args.out))
