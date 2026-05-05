"""
Ocean County Clerk official records — metadata-only scraper.

The full search API at sng.co.ocean.nj.us/publicsearch/api/search is server-
enforced reCAPTCHA v3 (no public bypass), so this scraper pulls the open
metadata endpoints only:

  - /api/document/doctypes  → 168 doc-type abbreviations
  - /api/document/commonTowns → 35 common towns
  - /api/search/clientinfo  → verifyDate, lastDocumentRecordedDateTime,
                              lastDocumentRecordedInfo (heartbeat)

These power:
  - the per-doc-type filter pills in the dashboard
  - the "Clerk index freshness" indicator in the dashboard header
  - regression detection when verifyDate or lastDocumentRecordedInfo stops
    advancing day-over-day

Output: a single JSONL with one record per refresh.
  data/raw/clerk_metadata.jsonl

Run:
  py -3.12 scrapers/clerk_metadata.py [--reset]
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import (  # noqa: E402
    UA, RateLimit, RetryWithBackoff, StopFlag, append_jsonl,
    err, jsonl_path, log, reset_source, save_state,
)

import requests  # noqa: E402

SOURCE = "clerk_metadata"
DOCTYPE = "heartbeat"
OUTPUT = jsonl_path(SOURCE, DOCTYPE)
BASE = "https://sng.co.ocean.nj.us/publicsearch"


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
    sess.headers.update({"User-Agent": UA, "Accept": "application/json"})

    rl = RateLimit(min_seconds=2.0)
    retry = RetryWithBackoff(max_attempts=5, base_delay=1.0)
    flag = StopFlag()
    flag.install()

    rl.wait()
    rec: dict = {
        "_source": SOURCE,
        "doc_type": DOCTYPE,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        rl.wait()
        ci = retry.call(lambda: sess.get(f"{BASE}/api/search/clientinfo", timeout=20)).json()
        rec["client_info"] = ci
        rec["verifyDate"] = ci.get("verifyDate")
        rec["lastDocumentRecordedDateTime"] = ci.get("lastDocumentRecordedDateTime")
        rec["lastDocumentRecordedInfo"] = ci.get("lastDocumentRecordedInfo")
    except Exception as e:
        err(f"clientinfo failed: {e}")
    try:
        rl.wait()
        dt = retry.call(lambda: sess.get(f"{BASE}/api/document/doctypes", timeout=20)).json()
        rec["doctypes"] = dt
        rec["doctypes_count"] = sum(len(g.get("children", [])) for g in dt if g.get("name") == "ALL")
    except Exception as e:
        err(f"doctypes failed: {e}")
    try:
        rl.wait()
        ct = retry.call(lambda: sess.get(f"{BASE}/api/document/commonTowns", timeout=20)).json()
        rec["common_towns"] = ct
        rec["common_towns_count"] = len(ct)
    except Exception as e:
        err(f"commonTowns failed: {e}")

    rec["_key"] = f"{SOURCE}:{rec['fetched_at']}"
    wrote = append_jsonl(OUTPUT, [rec], key_field="_key")
    log(
        f"Wrote {wrote} record. doctypes={rec.get('doctypes_count')} "
        f"towns={rec.get('common_towns_count')} "
        f"last_recorded={rec.get('lastDocumentRecordedInfo')}"
    )
    save_state(SOURCE, {
        "last_record": rec.get("lastDocumentRecordedInfo"),
        "verifyDate": rec.get("verifyDate"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
