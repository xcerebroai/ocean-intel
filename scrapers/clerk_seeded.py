"""
clerk_seeded.py — operator-seeded clerk session replay.

The Ocean County Clerk's NewVision publicsearch API is server-enforced
reCAPTCHA v3. We do not solve CAPTCHA. Instead, the operator opens the
clerk site in their real Chrome, completes one search to seed the session,
copies cookies + the X-RequestVerificationToken header into .env, and this
scraper replays them on automated calls.

Mechanic:
  1. Operator visits https://sng.co.ocean.nj.us/publicsearch/ in Chrome.
  2. Submits any doc-type search (e.g. DEED, last 7 days).
  3. DevTools -> Application -> Cookies -> copy ALL cookies for
     sng.co.ocean.nj.us as a single semicolon-joined string into .env:
        CLERK_SESSION_COOKIES=ASP.NET_SessionId=...; <other>=...
        CLERK_SESSION_TOKEN=<value of X-RequestVerificationToken header>
        CLERK_SESSION_SEEDED_AT=<ISO timestamp when seeded, e.g. 2026-05-05T14:00:00Z>

This scraper:
  - Validates the session via /api/search/clientinfo.
  - For each doc-type-firing pattern, posts an /api/search query for the
    last `--since-days` window (default 7).
  - Writes one JSONL per doc type to data/raw/clerk_seeded_<doctype>.jsonl.
  - On 400/403 ("No V3 token" or recaptcha challenge), exits with
    CLERK_SESSION_EXPIRED so refresh.py can alert the operator.

Output schema matches scrapers/clerk_opra_ingest.py — pipeline doesn't care
which path the data came from.

Run:
  py -3.12 scrapers/clerk_seeded.py [--since-days N] [--limit N] [--reset]
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import (  # noqa: E402
    RAW_DIR, UA, RateLimit, RetryWithBackoff, StopFlag, append_jsonl,
    err, jsonl_path, load_state, log, save_state,
)

import requests  # noqa: E402

SOURCE = "clerk_seeded"
BASE = "https://sng.co.ocean.nj.us/publicsearch"

EXPIRED_EXIT = 4

# Doc types to pull. Keep in sync with the OPRA request body.
DOC_TYPES_TO_PULL = [
    # foreclosure
    "LISPEN", "NOTLIS", "NTCELIS", "FINJUDGE",
    # tax
    "INREM", "MTSC", "TSC", "FEDLIEN",
    # lien
    "CONLIEN", "MECHLIEN", "MECHNOI", "PHYSLIEN", "BRTYLIEN",
    "WAREXEC", "WRITEXEC", "STOPNOT", "INSTLIEN", "WAGECLM",
    # estate
    "TAXWAIVE", "DISCLAIM", "TRUSTAGR",
    # transfer
    "DEED",
    # negative / de-escalation
    "DISCHLIS", "DISTSC", "RLFESLEN",
    "DSCOLIEN", "DSMELIEN", "DMECHNOI", "DPHYLIEN", "DSJUDLIEN", "WARSATFN",
]


def load_env() -> dict[str, str]:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def session_from_env(env: dict[str, str]) -> requests.Session | None:
    cookies = env.get("CLERK_SESSION_COOKIES") or os.environ.get("CLERK_SESSION_COOKIES")
    token = env.get("CLERK_SESSION_TOKEN") or os.environ.get("CLERK_SESSION_TOKEN")
    if not cookies:
        return None
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://sng.co.ocean.nj.us",
        "Referer": f"{BASE}/",
        "X-Requested-With": "XMLHttpRequest",
    })
    if token:
        sess.headers["X-RequestVerificationToken"] = token
    # Cookie header: replay the entire string verbatim so we don't accidentally
    # drop encoding-sensitive cookies (Newvision's session token is base64).
    sess.headers["Cookie"] = cookies
    return sess


def validate_session(sess: requests.Session, retry: RetryWithBackoff) -> bool:
    try:
        r = retry.call(lambda: sess.get(f"{BASE}/api/search/clientinfo", timeout=15))
        if r.status_code != 200:
            err(f"  clientinfo HTTP {r.status_code}: session likely invalid")
            return False
        # An invalid session typically returns HTML (the recaptcha challenge
        # page) rather than JSON.
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype.lower():
            err(f"  clientinfo non-JSON content-type: {ctype}")
            return False
        return True
    except Exception as e:
        err(f"  clientinfo failed: {e}")
        return False


def search_one(sess: requests.Session, retry: RetryWithBackoff, doc_type: str, since: str, until: str) -> list[dict]:
    """Post one /api/search query for the given doctype + window."""
    payload = {
        "DocTypes": doc_type,
        "FromDate": since.replace("-", ""),
        "ToDate": until.replace("-", ""),
        "MaxRows": 500,
        "RowsPerPage": 500,
        "StartRow": 0,
        "token": "",
        "recaptchaResponse": "",
    }
    r = retry.call(lambda: sess.post(f"{BASE}/api/search", json=payload, timeout=30))
    if r.status_code in (400, 401, 403):
        # Almost certainly "No V3 token found" — session expired.
        msg = ""
        try:
            msg = r.json().get("message", "")
        except Exception:
            msg = r.text[:200]
        err(f"  doc_type={doc_type}: HTTP {r.status_code} ({msg}) — session expired")
        raise SystemExit(EXPIRED_EXIT)
    if r.status_code != 200:
        err(f"  doc_type={doc_type}: unexpected HTTP {r.status_code}")
        return []
    try:
        data = r.json()
    except Exception:
        err(f"  doc_type={doc_type}: non-JSON response")
        return []
    if not isinstance(data, list):
        return []
    # First row in NewVision response carries header metadata; subsequent rows are records.
    rows = data[1:] if data and isinstance(data[0], dict) and "_total_rows" in data[0] else data
    return rows


def to_canonical(raw: dict, doc_type: str) -> dict:
    """Map NewVision response row -> pipeline canonical schema."""
    # Field names vary slightly per Newvision deployment; capture defensively.
    instr = raw.get("doc_id") or raw.get("inst_no") or raw.get("doc_num")
    rec_date = raw.get("rec_date") or raw.get("recording_date")
    if rec_date and len(rec_date) == 8 and rec_date.isdigit():
        rec_date = f"{rec_date[:4]}-{rec_date[4:6]}-{rec_date[6:]}"
    elif rec_date and "T" in rec_date:
        rec_date = rec_date.split("T")[0]
    return {
        "_key": f"clerk:{doc_type}:{instr}" if instr else f"clerk:{doc_type}:{json.dumps(raw, sort_keys=True)[:80]}",
        "_source": SOURCE,
        "doc_type": doc_type,
        "doc_type_desc": raw.get("doc_type_desc") or raw.get("type_desc"),
        "instrument_number": instr,
        "recording_date": rec_date,
        "book": raw.get("book"),
        "page": raw.get("page"),
        "grantor": [raw.get("grantor")] if raw.get("grantor") else [],
        "grantee": [raw.get("grantee")] if raw.get("grantee") else [],
        "consideration": raw.get("consid_1"),
        "lien_amount": raw.get("lien_amount") or raw.get("amount"),
        "case_no": raw.get("case_num"),
        "block": raw.get("block"),
        "lot": raw.get("lot"),
        "qualifier": raw.get("qualifier"),
        "mun_name": raw.get("town") or raw.get("party_town"),
        "raw_payload": raw,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--reset", action="store_true",
                    help="wipe data/raw/clerk_seeded_*.jsonl before pulling")
    args = ap.parse_args()

    env = load_env()
    seeded_at = env.get("CLERK_SESSION_SEEDED_AT") or os.environ.get("CLERK_SESSION_SEEDED_AT") or ""
    sess = session_from_env(env)
    if not sess:
        log("CLERK_SESSION_COOKIES not set in .env — skipping seeded clerk pull.")
        log("To enable: open clerk site in Chrome, copy cookies into .env (see scrapers/clerk_seeded.py docstring).")
        return 0

    if seeded_at:
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(seeded_at.replace("Z", "+00:00"))).total_seconds() / 3600
            log(f"Session seeded at {seeded_at} (age {age_h:.1f}h)")
            if age_h > 96:
                err("Session is >96h old. Re-seed before running.")
                return EXPIRED_EXIT
        except Exception:
            pass

    if args.reset:
        for p in RAW_DIR.glob("clerk_seeded_*.jsonl"):
            log(f"  --reset: removing {p.name}")
            p.unlink()

    rl = RateLimit(min_seconds=2.0)
    retry = RetryWithBackoff(max_attempts=3, base_delay=2.0)
    flag = StopFlag()
    flag.install()

    rl.wait()
    if not validate_session(sess, retry):
        err("Session validation failed. Re-seed: open clerk site in Chrome, copy cookies into .env.")
        return EXPIRED_EXIT
    log("Session validated.")

    since_dt = (date.today() - timedelta(days=args.since_days)).isoformat()
    until_dt = date.today().isoformat()
    log(f"Pulling doc types {since_dt} -> {until_dt}")

    total = 0
    by_doctype: dict[str, int] = {}
    for dt in DOC_TYPES_TO_PULL:
        if flag.stopped:
            break
        rl.wait()
        try:
            rows = search_one(sess, retry, dt, since_dt, until_dt)
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else EXPIRED_EXIT
        canonical = [to_canonical(r, dt) for r in rows]
        if args.limit and len(canonical) > args.limit:
            canonical = canonical[: args.limit]
        out = jsonl_path(SOURCE, dt)
        wrote = append_jsonl(out, canonical, key_field="_key")
        log(f"  {dt:10}: pulled {len(canonical):4d}  wrote {wrote}")
        by_doctype[dt] = wrote
        total += wrote

    log(f"Total clerk records ingested: {total}")
    save_state(SOURCE, {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "since_days": args.since_days,
        "by_doctype": by_doctype,
        "session_seeded_at": seeded_at,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
