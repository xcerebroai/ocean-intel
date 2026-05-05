"""
NJ MOD-IV statewide parcel layer — pulls all Ocean County parcels from the NJ
Office of Information Technology ArcGIS REST endpoint.

Endpoint:
  https://maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2/query

Filter: COUNTY = 'OCEAN'. Returns ~298k parcels. Paged 1000 records per request
(server hard-cap). Resume-safe via OBJECTID watermark in state.json.

Schema canonicalized to:
  pid, block, lot, qualifier, owner_name (NULL — redacted upstream),
  prop_loc (situs), st_address (mailing), city_state, zip_code,
  land_val, imprvt_val, net_value, last_yr_tx,
  bldg_desc, land_desc, calc_acre,
  prop_class, bldg_class, mun_name, county,
  deed_book, deed_page, deed_date (YYMMDD),
  yr_constr, sale_price, sales_code, dwell, comm_dwell, prop_use,
  pcl_lastupd_ms, pcl_pbdate_ms

Run:
  py -3.12 scrapers/nj_modiv_parcels.py [--limit N] [--reset] [--since YYYY-MM-DD]

Daily refresh strategy:
  --since YYYY-MM-DD applies a PCLLASTUPD watermark filter. **However, recon
  (Phase 1) found that PCLLASTUPD is populated on only 14% of records and
  the max value is 4 months stale — i.e. this layer is not maintained for
  per-record incremental updates.** The harness therefore runs the parcel
  scraper **weekly on Mondays only** (full re-pull, idempotent on PAMS_PIN).
  Tuesday–Sunday refreshes skip this scraper entirely.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import (  # noqa: E402
    RAW_DIR, UA, RateLimit, RetryWithBackoff, StopFlag, append_jsonl,
    err, jsonl_path, load_state, log, reset_source, save_state,
)

import requests  # noqa: E402

SOURCE = "nj_modiv_parcels"
ENDPOINT = "https://maps.nj.gov/arcgis/rest/services/Applications/NJ_TaxListSearch/MapServer/2/query"
OUTPUT = jsonl_path(SOURCE)

OUT_FIELDS = (
    "OBJECTID,PAMS_PIN,PCL_MUN,PCLBLOCK,PCLLOT,PCLQCODE,PCLLASTUPD,"
    "CD_CODE,PROP_CLASS,COUNTY,MUN_NAME,PROP_LOC,OWNER_NAME,ST_ADDRESS,"
    "CITY_STATE,ZIP_CODE,LAND_VAL,IMPRVT_VAL,NET_VALUE,LAST_YR_TX,"
    "BLDG_DESC,LAND_DESC,CALC_ACRE,FAC_NAME,PROP_USE,BLDG_CLASS,"
    "DEED_BOOK,DEED_PAGE,DEED_DATE,YR_CONSTR,SALES_CODE,SALE_PRICE,"
    "DWELL,COMM_DWELL,ZIP5,ZIP_PLUS4,PCL_PBDATE"
)

PAGE_SIZE = 1000


def canonicalize(attrs: dict) -> dict:
    """Map ArcGIS uppercase fields to canonical lowercase, key by PAMS_PIN."""
    pid = attrs.get("PAMS_PIN") or ""
    out = {
        "_key": pid,
        "_source": SOURCE,
        "pid": pid,
        "block": attrs.get("PCLBLOCK"),
        "lot": attrs.get("PCLLOT"),
        "qualifier": attrs.get("PCLQCODE"),
        "owner_name": (attrs.get("OWNER_NAME") or "").strip() or None,
        "prop_loc": attrs.get("PROP_LOC"),
        "st_address": attrs.get("ST_ADDRESS"),
        "city_state": attrs.get("CITY_STATE"),
        "zip_code": attrs.get("ZIP_CODE"),
        "zip5": attrs.get("ZIP5"),
        "zip_plus4": attrs.get("ZIP_PLUS4"),
        "land_val": attrs.get("LAND_VAL"),
        "imprvt_val": attrs.get("IMPRVT_VAL"),
        "net_value": attrs.get("NET_VALUE"),
        "last_yr_tx": attrs.get("LAST_YR_TX"),
        "bldg_desc": attrs.get("BLDG_DESC"),
        "land_desc": attrs.get("LAND_DESC"),
        "calc_acre": attrs.get("CALC_ACRE"),
        "fac_name": attrs.get("FAC_NAME"),
        "prop_use": attrs.get("PROP_USE"),
        "bldg_class": attrs.get("BLDG_CLASS"),
        "prop_class": attrs.get("PROP_CLASS"),
        "mun_name": attrs.get("MUN_NAME"),
        "mun_code": attrs.get("PCL_MUN") or attrs.get("CD_CODE"),
        "county": attrs.get("COUNTY"),
        "deed_book": attrs.get("DEED_BOOK"),
        "deed_page": attrs.get("DEED_PAGE"),
        "deed_date_yymmdd": attrs.get("DEED_DATE"),
        "yr_constr": attrs.get("YR_CONSTR"),
        "sales_code": attrs.get("SALES_CODE"),
        "sale_price": attrs.get("SALE_PRICE"),
        "dwell": attrs.get("DWELL"),
        "comm_dwell": attrs.get("COMM_DWELL"),
        "objectid": attrs.get("OBJECTID"),
        "pcl_lastupd_ms": attrs.get("PCLLASTUPD"),
        "pcl_pbdate_ms": attrs.get("PCL_PBDATE"),
    }
    return out


def build_where(since: str | None) -> str:
    where = "COUNTY='OCEAN'"
    if since:
        # PCLLASTUPD is a date field — ArcGIS date literal syntax
        where += f" AND PCLLASTUPD >= TIMESTAMP '{since} 00:00:00'"
    return where


def fetch_page(sess: requests.Session, retry: RetryWithBackoff, where: str, offset: int) -> list[dict]:
    params = {
        "where": where,
        "outFields": OUT_FIELDS,
        "returnGeometry": "false",
        "orderByFields": "OBJECTID ASC",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
        "f": "json",
    }
    r = retry.call(lambda: sess.get(ENDPOINT, params=params, timeout=60))
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(f"ArcGIS error: {j['error']}")
    return [ft.get("attributes", {}) for ft in j.get("features", [])]


def fetch_count(sess: requests.Session, retry: RetryWithBackoff, where: str) -> int:
    params = {"where": where, "returnCountOnly": "true", "f": "json"}
    r = retry.call(lambda: sess.get(ENDPOINT, params=params, timeout=30))
    r.raise_for_status()
    return int(r.json().get("count", 0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="cap total records (0 = no cap)")
    ap.add_argument("--reset", action="store_true", help="wipe state + jsonl and start over")
    ap.add_argument("--since", default="", help="YYYY-MM-DD watermark on PCLLASTUPD")
    args = ap.parse_args()

    if args.reset:
        log("--reset: wiping state and jsonl")
        reset_source(SOURCE)

    state = load_state(SOURCE)
    where = build_where(args.since or None)
    log(f"WHERE: {where}")

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json"})

    rl = RateLimit(min_seconds=2.0)  # ArcGIS handles bursts fine; keep modest
    retry = RetryWithBackoff(max_attempts=5, base_delay=1.0)
    flag = StopFlag()
    flag.install()

    total = fetch_count(sess, retry, where)
    log(f"Total matching parcels: {total:,}")

    offset = state.get("offset", 0)
    if offset and not args.reset:
        log(f"Resuming from offset {offset:,}")

    written_total = state.get("written_total", 0)
    cap = args.limit if args.limit > 0 else total

    while offset < total and written_total < cap:
        if flag.stopped:
            log("Stopped by signal — flushing state.")
            break
        rl.wait()
        try:
            page = fetch_page(sess, retry, where, offset)
        except Exception as e:
            err(f"page fetch failed at offset {offset}: {e}")
            time.sleep(10)
            continue
        if not page:
            log(f"No more rows at offset {offset:,}")
            break
        records = [canonicalize(a) for a in page if a.get("PAMS_PIN")]
        wrote = append_jsonl(OUTPUT, records, key_field="_key")
        written_total += wrote
        offset += len(page)
        log(f"  offset={offset:,}/{total:,}  page={len(page)}  wrote={wrote}  total_written={written_total:,}")
        save_state(SOURCE, {"offset": offset, "written_total": written_total, "where": where})
        if args.limit and written_total >= args.limit:
            break

    save_state(SOURCE, {"offset": offset, "written_total": written_total, "where": where, "completed_at": datetime.now(timezone.utc).isoformat()})
    log(f"Done. {written_total:,} records written to {OUTPUT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
