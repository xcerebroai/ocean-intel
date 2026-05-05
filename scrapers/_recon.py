"""
Phase 1 recon harness.

Probes each public source, captures headers + body excerpt, classifies access
method (REST, ASP WebForms, JSF, login wall, CAPTCHA, blocked). Writes one
JSON dump per source to data/raw/recon_<key>.json so the recon results are
inspectable and resumable.

Run: py -3.12 scrapers/_recon.py
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

UA = (
    "OceanIntel-Recon/0.1 (+contact infinitygauntletllc@gmail.com) "
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
RAW.mkdir(parents=True, exist_ok=True)

SOURCES = {
    "clerk_official_records": "https://sng.co.ocean.nj.us/publicsearch/",
    "clerk_records_forms": "https://oceancountyclerk.com/frmRecordsForms",
    "tax_board_landing": "https://tax.co.ocean.nj.us/",
    "tax_board_search": "https://tax.co.ocean.nj.us/frmtaxboardtaxlistsearch",
    "nj_courts_find_case": "https://www.njcourts.gov/public/find-a-case",
    "nj_judgment_search": "https://portal.njcourts.gov/webe40/JudgmentWeb/jsp/judgmentSearch.faces",
    "nj_judgment_lien_pa": "https://www.njcourts.gov/public/find-a-case/judgment-lien-public-access",
    "nj_foreclosure_info": "https://www.njcourts.gov/courts/superior-court-clerks-office/foreclosure",
    "sheriff_foreclosures": "https://sheriff.co.ocean.nj.us/frmForeclosures",
    "sheriff_civilview": "https://salesweb.civilview.com/Sales/SalesSearch?countyId=85",
    "surrogate": "http://co.ocean.nj.us/OC/surrogate/",
    "property_alert": "https://countyclerkpas.co.ocean.nj.us/PropertyAlert/",
    "ocean_gis_portal": "https://www.co.ocean.nj.us/gis",
    "ocean_arcgis_services": "https://gis.co.ocean.nj.us/arcgis/rest/services",
}

TIMEOUT = 25


def fetch_plain(url: str) -> dict:
    import requests
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
    return {
        "engine": "requests",
        "status": r.status_code,
        "url_final": r.url,
        "headers": dict(r.headers),
        "len": len(r.content),
        "body_excerpt": r.text[:8000],
    }


def fetch_cffi(url: str) -> dict:
    from curl_cffi import requests as cffi
    r = cffi.get(url, headers={"User-Agent": UA}, impersonate="chrome120", timeout=TIMEOUT, allow_redirects=True)
    return {
        "engine": "curl_cffi/chrome120",
        "status": r.status_code,
        "url_final": r.url,
        "headers": dict(r.headers),
        "len": len(r.content),
        "body_excerpt": r.text[:8000],
    }


def classify(body: str, status: int) -> dict:
    body_l = body.lower()
    flags = {
        "asp_webforms": "__viewstate" in body_l,
        "jsf": "javax.faces.viewstate" in body_l,
        "captcha": ("captcha" in body_l) or ("recaptcha" in body_l) or ("hcaptcha" in body_l),
        "login_form": ("password" in body_l and ("login" in body_l or "signin" in body_l or "username" in body_l)),
        "cloudflare": "cloudflare" in body_l or "cf-ray" in body_l,
        "esri_arcgis": "arcgis" in body_l or "/rest/services" in body_l,
        "blocked_403": status in (401, 403),
        "rate_limited": status == 429,
        "server_error": status >= 500,
    }
    return flags


def probe(key: str, url: str) -> dict:
    print(f"[recon] {key}  {url}")
    out = {"key": key, "url": url, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        r = fetch_plain(url)
        out["plain"] = r
        out["plain"]["flags"] = classify(r["body_excerpt"], r["status"])
        if r["status"] in (403, 429, 503) or r["status"] >= 500:
            try:
                r2 = fetch_cffi(url)
                out["cffi"] = r2
                out["cffi"]["flags"] = classify(r2["body_excerpt"], r2["status"])
            except Exception as e:
                out["cffi_error"] = str(e)
    except Exception as e:
        out["plain_error"] = str(e)
        try:
            r2 = fetch_cffi(url)
            out["cffi"] = r2
            out["cffi"]["flags"] = classify(r2["body_excerpt"], r2["status"])
        except Exception as e2:
            out["cffi_error"] = str(e2)
    return out


def main():
    keys = sys.argv[1:] or list(SOURCES.keys())
    for key in keys:
        url = SOURCES[key]
        result = probe(key, url)
        path = RAW / f"recon_{key}.json"
        path.write_text(json.dumps(result, indent=2)[:600_000])
        print(f"  -> {path}")
        time.sleep(2)


if __name__ == "__main__":
    main()
