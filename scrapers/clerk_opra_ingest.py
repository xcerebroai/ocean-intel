"""
clerk_opra_ingest.py — ingests CSV/Excel files the operator drops into
data/raw/clerk_opra/incoming/ after receiving an OPRA bulk extract from
the Ocean County Clerk's office.

Maps the operator's CSV → pipeline's normalized JSONL schema, written to
data/raw/clerk_opra/<source-stem>.jsonl. The pipeline (pipeline/build_leads.py)
auto-loads any *.jsonl in data/raw/clerk_opra/ and converts to leads.

Idempotent: re-running with the same input file overwrites the same output.
Multiple input files OK — one JSONL per CSV.

Run:
  py -3.12 scrapers/clerk_opra_ingest.py [--reset]

Schema mapping (heuristic — the operator's CSV column names will vary by
how the Clerk's office exports). The script scans the CSV header and maps
common synonyms to canonical fields. Unknown columns are preserved in
raw_payload for audit.
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import (  # noqa: E402
    RAW_DIR, log, err,
)

SOURCE = "clerk_opra"
INCOMING = RAW_DIR / "clerk_opra" / "incoming"
OUT_DIR = RAW_DIR / "clerk_opra"

# Canonical field → list of accepted source-column-name patterns (lowercased,
# matched via "in" against the lowercased header).
FIELD_MAP: dict[str, list[str]] = {
    "instrument_number": ["instrument", "instr no", "instr_no", "doc number", "document number", "instrument#", "instr#"],
    "recording_date": ["recording date", "rec date", "recorded date", "date recorded", "filed date", "file date"],
    "doc_type": ["doc type", "document type", "doctype", "type code", "instrument type"],
    "doc_type_desc": ["type description", "doc desc", "description", "document description"],
    "grantor": ["grantor", "from", "party 1", "party1", "party from", "from party"],
    "grantee": ["grantee", "to", "party 2", "party2", "party to", "to party"],
    "consideration": ["consideration", "amount", "consid", "price", "transfer amount"],
    "lien_amount": ["lien amount", "judgment amount", "principal amount", "amount of lien"],
    "book": ["book", "book#"],
    "page": ["page", "page#"],
    "case_no": ["case number", "case no", "docket", "docket number"],
    "block": ["block", "blk"],
    "lot": ["lot"],
    "qualifier": ["qualifier", "qual"],
    "mun_name": ["municipality", "town", "city", "mun", "muni"],
    "pid": ["pams_pin", "pams pin", "pin", "parcel id", "parcel pin", "parcel"],
}


def detect_columns(header: list[str]) -> dict[str, str]:
    """Return canonical → source-header-name mapping."""
    lower_to_orig = {h.lower().strip(): h for h in header}
    mapping: dict[str, str] = {}
    for canonical, candidates in FIELD_MAP.items():
        for cand in candidates:
            for low, orig in lower_to_orig.items():
                if cand in low:
                    mapping[canonical] = orig
                    break
            if canonical in mapping:
                break
    return mapping


def normalize_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(raw) -> float | None:
    if raw is None or raw == "":
        return None
    s = str(raw).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def split_parties(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[;|]|\s+&\s+|\s+AND\s+", str(raw).strip())
    return [p.strip() for p in parts if p.strip()]


def ingest_file(path: Path, out_jsonl: Path) -> int:
    """Read one CSV. Write normalized JSONL. Return record count."""
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return 0
        col_map = detect_columns(header)
        if "doc_type" not in col_map:
            err(f"  {path.name}: no doc_type column detected. Header: {header}")
            return 0
        if "instrument_number" not in col_map and "case_no" not in col_map:
            err(f"  {path.name}: no instrument_number or case_no column — cannot key. Skipping.")
            return 0
        idx = {k: header.index(v) for k, v in col_map.items()}
        for r in reader:
            if not r:
                continue
            def get(k: str) -> str | None:
                i = idx.get(k)
                if i is None or i >= len(r):
                    return None
                return r[i]
            doc_type = (get("doc_type") or "").strip().upper()
            if not doc_type:
                continue
            instr = (get("instrument_number") or get("case_no") or "").strip()
            key = f"opra:{path.stem}:{doc_type}:{instr}" if instr else f"opra:{path.stem}:{doc_type}:row{len(rows)}"
            rec = {
                "_key": key,
                "_source": SOURCE,
                "doc_type": doc_type,
                "doc_type_desc": (get("doc_type_desc") or "").strip() or None,
                "instrument_number": instr or None,
                "recording_date": normalize_date(get("recording_date")),
                "book": (get("book") or "").strip() or None,
                "page": (get("page") or "").strip() or None,
                "grantor": split_parties(get("grantor")),
                "grantee": split_parties(get("grantee")),
                "consideration": parse_amount(get("consideration")),
                "lien_amount": parse_amount(get("lien_amount")),
                "case_no": (get("case_no") or "").strip() or None,
                "block": (get("block") or "").strip() or None,
                "lot": (get("lot") or "").strip() or None,
                "qualifier": (get("qualifier") or "").strip() or None,
                "mun_name": (get("mun_name") or "").strip() or None,
                "pid": (get("pid") or "").strip() or None,
                "raw_payload": dict(zip(header, r)),
            }
            rows.append(rec)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with out_jsonl.open("w", encoding="utf-8") as f:
        for rec in rows:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true",
                    help="delete existing data/raw/clerk_opra/*.jsonl before re-ingesting")
    args = ap.parse_args()

    INCOMING.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.reset:
        for p in OUT_DIR.glob("*.jsonl"):
            log(f"  --reset: removing {p.name}")
            p.unlink()

    csvs = sorted(INCOMING.glob("*.csv"))
    if not csvs:
        log(f"No CSV files in {INCOMING}. Drop OPRA-delivered CSVs there and re-run.")
        log(f"  Expected delivery from John P. Kelly's office per opra_requests/log.csv.")
        return 0

    total = 0
    for csv_path in csvs:
        out = OUT_DIR / (csv_path.stem + ".jsonl")
        n = ingest_file(csv_path, out)
        log(f"  {csv_path.name}: ingested {n} records -> {out.name}")
        total += n
    log(f"Ingested {total} clerk OPRA records across {len(csvs)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
