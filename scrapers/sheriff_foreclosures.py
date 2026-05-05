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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import (  # noqa: E402
    RAW_DIR, UA, RateLimit, StopFlag, append_jsonl,
    err, jsonl_path, load_state, log, reset_source, save_state,
)

import pdfplumber  # noqa: E402
import requests  # noqa: E402

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

CH_RE = re.compile(r"^CH\s+(\d+)\s*$")
FORE_RE = re.compile(r"^([F]\d{8,})\s+DEFENDANT\s+(.*)$")
PLAINTIFF_RE = re.compile(r"^PLAINTIFF\s+(.*?)\s+\$([\d,]+\.\d{2})\s*(.*)$")
ATTORNEY_RE = re.compile(r"^ATTORNEY\s+(.*)$")
SEQ_RE = re.compile(r"^SEQ\s+(\d{3})\s+(.*)$")
LOT_RE = re.compile(r"^Lot:\s*([^\s].*?)\s+Block:\s*(\S+)\s*$", re.I)
ADJ_RE = re.compile(r"ADJOURNED UNTIL\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
SALE_DATE_RE = re.compile(r"REAL ESTATE LISTING FOR\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)


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
                    current = {
                        "case_no": "CH " + m.group(1),
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
                    # situs is everything before NJ + zip; mun is the last
                    # uppercase tokens before NJ. Coarse parse — just keep raw
                    # address line; pipeline can refine.
                    current_seq = {
                        "_key": f"{current['case_no']}:{seq_num}",
                        "_source": SOURCE,
                        "doc_type": DOCTYPE,
                        "sale_date": sale_date_str,
                        "case_no": current["case_no"],
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
                    }
                    # Split "<street> <municipality> NJ <zip>". Strategy:
                    # find the street suffix (RD, BLVD, ST, AVE, etc.) — the
                    # token after it through "NJ" is the municipality.
                    am = re.match(r"^(.*?\b(?:RD|ROAD|BLVD|BOULEVARD|ST|STREET|AVE|AVENUE|DR|DRIVE|LN|LANE|CT|COURT|CIR|CIRCLE|WAY|PL|PLACE|PKWY|PARKWAY|HWY|HIGHWAY|TER|TERRACE|TRL|TRAIL|CRES|CRESCENT|LOOP|RUN|XING|ALLEY|ROW|VACANT|PT|POINT|VIEW|RDG|RIDGE|RT|ROUTE|US\s*\d+|HEIGHTS|HTS)\b\.?)\s+(.+?)\s+NJ\s+(\d{5})\s*$",
                                  addr_text, re.I)
                    if am:
                        current_seq["situs_address"] = am.group(1).strip()
                        current_seq["mun_name"] = am.group(2).strip()
                        current_seq["zip5"] = am.group(3)
                    else:
                        # Fallback: strip "NJ ZIP" from the end and keep everything as situs
                        m2 = re.match(r"^(.*?)\s+NJ\s+(\d{5})\s*$", addr_text)
                        if m2:
                            current_seq["situs_address"] = m2.group(1).strip()
                            current_seq["zip5"] = m2.group(2)
                        else:
                            current_seq["situs_address"] = addr_text
                    continue

                m = LOT_RE.match(raw_line)
                if m and current_seq:
                    current_seq["lot"] = m.group(1).strip()
                    current_seq["block"] = m.group(2).strip()
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
    flag = StopFlag()
    flag.install()

    rl.wait()
    pdf_url = discover_pdf_url(sess)
    log(f"PDF URL: {pdf_url}")
    rl.wait()
    pdf_path = fetch_pdf(sess, pdf_url)
    log(f"PDF: {pdf_path.name}  ({pdf_path.stat().st_size / 1024:.1f} KB)")

    records = parse_pdf(pdf_path)
    log(f"Parsed {len(records)} SEQ rows")

    if args.since:
        watermark = datetime.strptime(args.since, "%Y-%m-%d")
        before = len(records)
        records = [r for r in records if (parse_sale_date(r.get("sale_date")) or datetime.min) >= watermark]
        log(f"--since {args.since}: kept {len(records)}/{before}")

    if args.limit:
        records = records[: args.limit]

    wrote = append_jsonl(OUTPUT, records, key_field="_key")
    log(f"Wrote {wrote} new records to {OUTPUT.name}")

    save_state(SOURCE, {
        "pdf_url": pdf_url,
        "parsed_total": len(records),
        "written_total": wrote,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
