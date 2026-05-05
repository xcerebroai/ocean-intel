"""
Ocean County Sheriff foreclosure listing — parses the weekly Sheriff sales PDF.

Discovery flow:
  1) GET https://sheriff.co.ocean.nj.us/frmForeclosures
  2) Extract the "Foreclosure Listing" PDF URL from the page (rotates per
     publication cycle).
  3) Download PDF.
  4) Extract structured records via pdfplumber text layer.

Record schema (one record per SEQ row in PDF):
  {
    _key: "<case_no>:<seq>",
    _source: "sheriff_foreclosures",
    sale_date, case_no (CH), foreclosure_no (F#), plaintiff, defendant,
    attorney, judgment_amount, status (BANKRUPTCY / ADJOURNED / blank / N),
    seq, situs_address, mun_name, block, lot
  }

PDFs are republished entirely each cycle. Idempotent via case_no + seq dedupe.
On disk: data/raw/sheriff_foreclosures.jsonl (single doctype, "writ_of_sale").

Run:
  py -3.12 scrapers/sheriff_foreclosures.py [--limit N] [--reset] [--since YYYY-MM-DD]

--since is honored only loosely (we filter parsed records whose sale_date is
on/after the watermark).
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import (  # noqa: E402
    RAW_DIR, UA, RateLimit, RetryWithBackoff, StopFlag, append_jsonl,
    err, jsonl_path, load_state, log, reset_source, save_state,
)

import pdfplumber  # noqa: E402
import requests  # noqa: E402

# Rule 20: sheriff parser must validate >= 90% of parsed rows have block + lot
# + mun_name. Below that, scraper exits 1 and writes data/raw/sheriff_foreclosures.qa.json.
QA_PATH = RAW_DIR / "sheriff_foreclosures.qa.json"
QA_MIN_VALID_PCT = 90.0

SOURCE = "sheriff_foreclosures"
DOCTYPE = "writ_of_sale"
OUTPUT = jsonl_path(SOURCE, DOCTYPE)

LANDING = "https://sheriff.co.ocean.nj.us/frmForeclosures"


def discover_pdf_url(sess: requests.Session) -> str:
    r = sess.get(LANDING, timeout=30)
    r.raise_for_status()
    # Find the Foreclosure Listing button — looks like
    # <a href="http://www.co.ocean.nj.us//WebContentFiles//<guid>.pdf" class="btn btn-primary" target="_blank">Foreclosure Listing</a>
    m = re.search(r'href="(https?://[^"]+\.pdf)"[^>]*>(?:[^<]*\bForeclosure Listing\b)', r.text, re.I)
    if not m:
        # Fallback: any PDF link in the page
        m = re.search(r'href="(https?://[^"]+\.pdf)"', r.text)
    if not m:
        raise RuntimeError("Could not find Foreclosure Listing PDF link on landing page")
    return m.group(1)


def fetch_pdf(sess: requests.Session, url: str) -> Path:
    r = sess.get(url, timeout=60)
    r.raise_for_status()
    p = RAW_DIR / "sheriff_foreclosure_listing.pdf"
    p.write_bytes(r.content)
    return p


# ----- PDF parsing ------------------------------------------------------------

# NJ Superior Court has multiple case types — Chancery (CH), Law (L), and
# legacy variants. v1 only matched CH, which silently mis-routed every L-case
# SEQ row onto the previous CH case (root cause of the v1 47% miss rate).
CH_RE = re.compile(r"^(CH|L|DJ|F|JL)\s+(\d{4,})\s*$")
FORE_RE = re.compile(r"^([FL]\d{6,})(?:\s+[A-Z])?\s+DEFENDANT\s+(.*)$")
PLAINTIFF_RE = re.compile(r"^PLAINTIFF\s+(.*?)\s+\$([\d,]+\.\d{2})\s*(.*)$")
ATTORNEY_RE = re.compile(r"^ATTORNEY\s+(.*)$")
SEQ_RE = re.compile(r"^SEQ\s+(\d{3})\s+(.*)$")
# v2: greedy-but-non-greedy match on the lot value so multi-token lots
# ("Lot: 47, 48, 19, 50 Block: 351", "Lot: 1 & 6 Block: 900",
# "Lot: 20(F/K/A 21.02) Block: 12401 FKA 8") all work. End not anchored to
# tolerate column-split zip fragments trailing the line.
LOT_RE = re.compile(r"^Lot:\s*(.+?)\s+Block:\s*([0-9][0-9.]*)", re.I)
ADJ_RE = re.compile(r"ADJOURNED UNTIL\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
SALE_DATE_RE = re.compile(r"REAL ESTATE LISTING FOR\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
# Single-digit lines after a SEQ are zip-column-split fragments (PDF columnar
# rendering of "08008" as "8\n0\n0\n8" stacked in the rightmost column).
ZIP_FRAGMENT_RE = re.compile(r"^\d$")
# Known Ocean County municipality names — used to recover mun_name when the
# heuristic split fails (see normalize_situs).
OCEAN_MUNICIPALITIES = (
    "BARNEGAT LIGHT", "BARNEGAT", "BAY HEAD", "BEACH HAVEN", "BEACHWOOD",
    "BERKELEY", "BRICK", "EAGLESWOOD", "HARVEY CEDARS", "ISLAND HEIGHTS",
    "JACKSON", "LACEY", "LAKEHURST", "LAKEWOOD", "LAVALLETTE", "LITTLE EGG HARBOR",
    "LITTLE EGG HARB", "LONG BEACH", "MANCHESTER", "MANTOLOKING", "OCEAN GATE",
    "OCEAN", "PINE BEACH", "PLUMSTED", "POINT PLEASANT BEACH", "POINT PLEASANT",
    "SEASIDE HEIGHTS", "SEASIDE PARK", "SHIP BOTTOM", "SOUTH TOMS RIVER",
    "STAFFORD", "SURF CITY", "TOMS RIVER", "TUCKERTON", "WARETOWN", "NEW EGYPT",
    "FORKED RIVER", "MANAHAWKIN",
    # Postal/CDP names that the sheriff uses but aren't legal munis (mapped
    # to legal munis in the pipeline normalize_mun() pass)
    "WHITING", "CRESTWOOD VILLAGE", "BAYVILLE", "ROOSEVELT CITY",
)


def parse_situs_line(addr_text: str) -> dict:
    """Parse a SEQ address line into {situs_address, mun_name, zip5}.

    Strategy that handles both clean "<addr> <mun> NJ <zip>" lines and the
    column-split case where the trailing zip is split vertically across
    follow-up single-digit lines (Lot/Block + zip-fragment lines).
    """
    out: dict = {"situs_address": None, "mun_name": None, "zip5": None}
    cleaned = re.sub(r"\s+", " ", addr_text).strip()

    # Strip parenthetical municipality qualifiers (LBI / NB / SB) — they
    # confuse the mun-detection but aren't part of the canonical mun_name.
    cleaned_no_paren = re.sub(r"\s*\([^)]*\)\s*", " ", cleaned).strip()

    # Identify trailing "NJ <digits>" (zip may be partial — column-split case
    # may have only "NJ 0" with the rest in zip-fragment lines below).
    nj_match = re.search(r"\s+NJ\s+(\d{0,5})\s*$", cleaned_no_paren, re.I)
    if nj_match:
        out["zip5"] = nj_match.group(1) or None
        before_nj = cleaned_no_paren[: nj_match.start()].strip()
    else:
        before_nj = cleaned_no_paren

    # Strip "VACANT" trailing flag (LBI vacant lots).
    before_nj = re.sub(r"\s+VACANT\s*$", "", before_nj, flags=re.I).strip()

    # Find the longest known Ocean municipality name that ends `before_nj`.
    # Iterate from longest to shortest so multi-word muns ("LONG BEACH",
    # "POINT PLEASANT BEACH", "LITTLE EGG HARBOR") match before single-word.
    upper_before = before_nj.upper()
    best_mun = None
    for cand in sorted(OCEAN_MUNICIPALITIES, key=len, reverse=True):
        # Allow optional " TWP" suffix on cand
        for suffix in ("", " TWP", " TOWNSHIP", " BORO", " BOROUGH", " CITY"):
            target = cand + suffix
            if upper_before.endswith(" " + target) or upper_before == target:
                best_mun = target
                break
        if best_mun:
            break

    if best_mun:
        out["mun_name"] = best_mun
        end_idx = upper_before.rfind(best_mun)
        out["situs_address"] = before_nj[:end_idx].strip() or None
    else:
        # Fallback: heuristic — last 2 words before NJ are the municipality
        toks = before_nj.split()
        if len(toks) >= 3:
            out["situs_address"] = " ".join(toks[:-2])
            out["mun_name"] = " ".join(toks[-2:])
        else:
            out["situs_address"] = before_nj or None

    return out


def parse_pdf(path: Path) -> list[dict]:
    """Walk the PDF text top-to-bottom, accumulating one record per SEQ row.

    The PDF has stable structural cues:
      - "OCEAN COUNTY UPSET" + "PAGE N REAL ESTATE LISTING FOR MM/DD/YYYY AMOUNT S" header per page
      - Each foreclosure block starts with `CH <num>`
      - Followed by PLAINTIFF, F-number+DEFENDANT, ATTORNEY (and continuation), then 1+ SEQ rows
      - Each SEQ row is followed by a Lot: / Block: line
    """
    records: list[dict] = []
    sale_date_str: str | None = None

    current = None  # current foreclosure header
    current_seq = None  # current SEQ within that header

    with pdfplumber.open(str(path)) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            txt = page.extract_text() or ""
            lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            for raw_line in lines:
                m = SALE_DATE_RE.search(raw_line)
                if m:
                    sale_date_str = m.group(1)
                    continue
                m = CH_RE.match(raw_line)
                if m:
                    # finalize previous SEQ if any
                    if current_seq:
                        records.append(current_seq)
                        current_seq = None
                    case_prefix = m.group(1)
                    case_num = m.group(2)
                    current = {
                        "case_no": f"{case_prefix} {case_num}",
                        "case_type": case_prefix,
                        "plaintiff": None,
                        "judgment_amount": None,
                        "status_raw": None,
                        "foreclosure_no": None,
                        "defendant": None,
                        "attorney_lines": [],
                    }
                    continue
                if current is None:
                    continue

                m = PLAINTIFF_RE.match(raw_line)
                if m:
                    current["plaintiff"] = m.group(1).strip()
                    current["judgment_amount"] = float(m.group(2).replace(",", ""))
                    rest = m.group(3).strip()
                    current["status_raw"] = rest or None
                    continue

                m = FORE_RE.match(raw_line)
                if m:
                    current["foreclosure_no"] = m.group(1)
                    current["defendant"] = m.group(2).strip()
                    continue

                m = ADJ_RE.search(raw_line)
                if m and current.get("status_raw"):
                    current["status_raw"] = current["status_raw"] + " " + raw_line
                elif m and not current.get("status_raw"):
                    current["status_raw"] = raw_line

                m = ATTORNEY_RE.match(raw_line)
                if m:
                    current["attorney_lines"].append(m.group(1).strip())
                    continue

                m = SEQ_RE.match(raw_line)
                if m:
                    if current_seq:
                        records.append(current_seq)
                    seq_num = m.group(1)
                    addr_text = m.group(2).strip()
                    current_seq = {
                        "_key": f"{current['case_no']}:{seq_num}",
                        "_source": SOURCE,
                        "doc_type": DOCTYPE,
                        "sale_date": sale_date_str,
                        "case_no": current["case_no"],
                        "case_type": current.get("case_type"),
                        "foreclosure_no": current.get("foreclosure_no"),
                        "plaintiff": current.get("plaintiff"),
                        "defendant": current.get("defendant"),
                        "judgment_amount": current.get("judgment_amount"),
                        "status_raw": current.get("status_raw"),
                        "attorney": " | ".join(current.get("attorney_lines") or []) or None,
                        "seq": seq_num,
                        "situs_raw": addr_text,
                        "block": None,
                        "lot": None,
                        "mun_name": None,
                        "situs_address": None,
                        "zip5": None,
                    }
                    parsed = parse_situs_line(addr_text)
                    current_seq.update(parsed)
                    continue

                m = LOT_RE.match(raw_line)
                if m and current_seq:
                    current_seq["lot"] = m.group(1).strip()
                    current_seq["block"] = m.group(2).strip()
                    continue

                # Single-digit zip-fragment lines (column-split) — append to
                # the running zip5 of the current SEQ if present.
                if current_seq and ZIP_FRAGMENT_RE.match(raw_line):
                    cur_zip = current_seq.get("zip5") or ""
                    if len(cur_zip) < 5:
                        current_seq["zip5"] = cur_zip + raw_line
                    continue

    if current_seq:
        records.append(current_seq)

    # Compute derived status
    for r in records:
        sr = (r.get("status_raw") or "").upper()
        if "BANKRUPTCY" in sr:
            r["status"] = "bankruptcy"
        elif "ADJOURNED" in sr:
            r["status"] = "adjourned"
        elif "CANCEL" in sr:
            r["status"] = "cancelled"
        elif sr.strip().isdigit():
            r["status"] = "active"
            r["adjournment_count"] = int(sr.strip())
        elif sr.strip() == "":
            r["status"] = "active"
        else:
            r["status"] = "other"
    return records


def parse_sale_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y")
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--since", default="")
    args = ap.parse_args()

    if args.reset:
        log("--reset: wiping state and jsonl")
        reset_source(SOURCE, doctypes=[DOCTYPE])

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})

    rl = RateLimit(min_seconds=3.0)
    retry = RetryWithBackoff(max_attempts=5, base_delay=1.0)
    flag = StopFlag()
    flag.install()

    rl.wait()
    r0 = retry.call(lambda: sess.get(LANDING, timeout=30))
    r0.raise_for_status()
    m = re.search(r'href="(https?://[^"]+\.pdf)"[^>]*>(?:[^<]*\bForeclosure Listing\b)', r0.text, re.I) \
        or re.search(r'href="(https?://[^"]+\.pdf)"', r0.text)
    if not m:
        raise RuntimeError("Could not find Foreclosure Listing PDF link on landing page")
    pdf_url = m.group(1)
    log(f"PDF URL: {pdf_url}")

    rl.wait()
    r1 = retry.call(lambda: sess.get(pdf_url, timeout=60))
    r1.raise_for_status()
    pdf_path = RAW_DIR / "sheriff_foreclosure_listing.pdf"
    pdf_path.write_bytes(r1.content)
    log(f"PDF: {pdf_path.name}  ({pdf_path.stat().st_size / 1024:.1f} KB)")

    records = parse_pdf(pdf_path)
    log(f"Parsed {len(records)} SEQ rows")

    # QA gate (Rule 20). >= 90% of rows must have block + lot + mun_name.
    quarantine = []
    valid = []
    for r in records:
        if r.get("block") and r.get("lot") and r.get("mun_name"):
            valid.append(r)
        else:
            quarantine.append({
                "_key": r.get("_key"),
                "case_no": r.get("case_no"),
                "seq": r.get("seq"),
                "situs_raw": r.get("situs_raw"),
                "missing": [k for k in ("block", "lot", "mun_name") if not r.get(k)],
            })
    valid_pct = (100.0 * len(valid) / len(records)) if records else 100.0
    qa = {
        "parsed_total": len(records),
        "valid_count": len(valid),
        "quarantine_count": len(quarantine),
        "valid_pct": round(valid_pct, 2),
        "quarantine_pct": round(100.0 - valid_pct, 2),
        "min_valid_pct_threshold": QA_MIN_VALID_PCT,
        "quarantine": quarantine,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    QA_PATH.write_text(json.dumps(qa, indent=2), encoding="utf-8")
    log(f"QA: {len(valid)}/{len(records)} valid ({valid_pct:.1f}%)  quarantine={len(quarantine)}")

    if valid_pct < QA_MIN_VALID_PCT:
        err(f"QA FAIL: valid_pct {valid_pct:.1f}% < threshold {QA_MIN_VALID_PCT}%. See {QA_PATH}")
        return 1

    if args.since:
        watermark = datetime.strptime(args.since, "%Y-%m-%d")
        before = len(valid)
        valid = [r for r in valid if (parse_sale_date(r.get("sale_date")) or datetime.min) >= watermark]
        log(f"--since {args.since}: kept {len(valid)}/{before}")

    if args.limit:
        valid = valid[: args.limit]

    wrote = append_jsonl(OUTPUT, valid, key_field="_key")
    log(f"Wrote {wrote} new records to {OUTPUT.name}")

    save_state(SOURCE, {
        "pdf_url": pdf_url,
        "parsed_total": len(records),
        "valid_count": len(valid),
        "quarantine_count": len(quarantine),
        "valid_pct": round(valid_pct, 2),
        "written_total": wrote,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
