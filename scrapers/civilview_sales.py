"""
Tyler CivilView sheriff sales — Ocean (countyId=85). Currently empty for Ocean
(0 rows on probe), but the scraper is portable: change countyId for any of NJ's
21 counties. Kept in place because (a) Ocean has historically used CivilView,
(b) the platform is the standard NJ pattern.

Output schema:
  {
    _key: "civilview:<countyId>:<property_id>",
    _source: "civilview_sales",
    doc_type: "sheriff_sale_listing",
    sheriff_no, sale_date, plaintiff, defendant, address, city, status, ...
  }

Run:
  py -3.12 scrapers/civilview_sales.py [--limit N] [--reset] [--since YYYY-MM-DD] [--county-id 85] [--include-sold]
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
    UA, RateLimit, RetryWithBackoff, StopFlag, append_jsonl,
    err, jsonl_path, load_state, log, reset_source, save_state,
)

from bs4 import BeautifulSoup  # noqa: E402
import requests  # noqa: E402

SOURCE = "civilview_sales"
DOCTYPE = "sheriff_sale_listing"
OUTPUT = jsonl_path(SOURCE, DOCTYPE)

BASE = "https://salesweb.civilview.com"


def submit_search(sess: requests.Session, retry: RetryWithBackoff, county_id: str, is_open: bool) -> str:
    url = f"{BASE}/Sales/SalesSearch?countyId={county_id}"
    retry.call(lambda: sess.get(url, timeout=30)).raise_for_status()
    payload = {
        "PropertyStatusDate": "",
        "MonthNumber": "0",
        "IsOpen": "true" if is_open else "false",
        "SheriffNumber": "",
        "PlaintiffTitle": "",
        "DefendantTitle": "",
        "Address": "",
        "CityDesc": "",
        "countyId": county_id,
    }
    r = retry.call(lambda: sess.post(url, data=payload, headers={"Referer": url}, timeout=60))
    r.raise_for_status()
    return r.text


def parse_results(html: str, county_id: str, status: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id=lambda x: x and "SalesResults" in x) or soup.find("table", class_=lambda x: x and "table-striped" in x)
    if not table:
        return []
    rows = []
    headers = []
    thead = table.find("thead")
    if thead:
        headers = [th.get_text(strip=True).lower().replace(" ", "_") for th in thead.find_all("th")]
    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cells = [td.get_text(" ", strip=True) for td in tds]
        # Property id from the SaleDetails link if present
        link = tr.find("a", href=re.compile(r"SaleDetails", re.I))
        prop_id = None
        if link:
            m = re.search(r"PropertyId=([^&]+)", link.get("href", ""))
            if m:
                prop_id = m.group(1)
        rec = {}
        if headers and len(headers) == len(cells):
            rec = dict(zip(headers, cells))
        else:
            rec = {f"col_{i}": v for i, v in enumerate(cells)}
        rec.setdefault("property_id", prop_id)
        rec["_source"] = SOURCE
        rec["_key"] = f"civilview:{county_id}:{prop_id or '|'.join(cells)[:80]}"
        rec["doc_type"] = DOCTYPE
        rec["county_id"] = county_id
        rec["civilview_status"] = status
        rows.append(rec)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--since", default="")
    ap.add_argument("--county-id", default="85")
    ap.add_argument("--include-sold", action="store_true")
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

    all_records: list[dict] = []
    statuses = ["open"] + (["sold"] if args.include_sold else [])
    for status in statuses:
        if flag.stopped:
            break
        rl.wait()
        log(f"Querying CivilView countyId={args.county_id} status={status}")
        try:
            html = submit_search(sess, retry, args.county_id, is_open=(status == "open"))
        except Exception as e:
            err(f"submit failed: {e}")
            continue
        rows = parse_results(html, args.county_id, status)
        log(f"  parsed {len(rows)} rows")
        all_records.extend(rows)

    if args.limit:
        all_records = all_records[: args.limit]

    wrote = append_jsonl(OUTPUT, all_records, key_field="_key")
    log(f"Wrote {wrote} new records to {OUTPUT.name}")

    save_state(SOURCE, {
        "county_id": args.county_id,
        "parsed_total": len(all_records),
        "written_total": wrote,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
