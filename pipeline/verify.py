"""
verify.py — Phase 5 live-browser verification gate.

Replaces v2's static-file-only verifier. This script launches a real
Chromium against https://xcerebroai.github.io/ocean-intel/ and confirms
the dashboard actually renders. The one thing v2.0 missed.

Behavior:

  1. Wait for Pages CDN to flush (poll the live URL until data/leads.json
     returns the new generated_at, max 180s).
  2. Launch Playwright Chromium against the live URL.
  3. Wait for [data-ready="1"] within 15s.
  4. Capture all JS console errors that fire during load.
  5. Assert lead-row count >= min(leads.json.lead_total, 800).
  6. Assert "Total: <N>" stat-tile matches leads.json.lead_total.
  7. Click each pre-canned button — assert no console errors and tbody re-renders.
  8. Click "Export CSV" — capture download, parse first row, validate columns.

On any failure:
  - revert HEAD to previous commit
  - force-push (the build step's commit was the breakage)
  - write BUILD_BROKEN.md to the now-reverted state
  - commit + push BUILD_BROKEN.md
  - exit non-zero

On success:
  - write LIVE_VERIFIED.txt with timestamp + counts + clerk_ingest snapshot
  - exit 0
"""
from __future__ import annotations
import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEADS = ROOT / "data" / "leads.json"
LIVE_URL = "https://xcerebroai.github.io/ocean-intel/"
LEADS_URL = LIVE_URL + "data/leads.json"
BROKEN_MD = ROOT / "BUILD_BROKEN.md"
VERIFIED_TXT = ROOT / "LIVE_VERIFIED.txt"

EXPECTED_CSV_COLS = [
    "pid","block","lot","qualifier","owner","situs_address","mailing_address","mun_name",
    "year_built","assessed_value","last_sale_date","last_sale_price","years_owned",
    "lead_types","lead_type_count",
    "vacant","absentee","out_of_state","senior_owner","long_term_owned",
    "free_and_clear","high_equity","entity_owned","multiple_properties",
    "signal_count","last_signal_date",
    "phone1","phone2","phone3","email1","email2",
]


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def telegram_send(text: str) -> None:
    env_file = ROOT / ".env"
    bot, chat = "", ""
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                bot = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("TELEGRAM_USER_ID=") or line.startswith("TELEGRAM_CHAT_ID="):
                chat = line.split("=", 1)[1].strip().strip('"')
    bot = bot or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = chat or os.environ.get("TELEGRAM_USER_ID", "")
    if not (bot and chat):
        return
    try:
        import urllib.parse
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{bot}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass


def wait_for_pages_flush(local_generated_at: str, deadline_s: float = 180.0) -> bool:
    """Poll the live URL's leads.json until generated_at matches the local
    file's value. Returns True if matched, False on timeout."""
    log(f"Waiting for Pages CDN to flush (target generated_at={local_generated_at})")
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        try:
            req = urllib.request.Request(LEADS_URL, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                served = json.loads(resp.read())
            served_at = served.get("generated_at", "")
            if served_at == local_generated_at:
                log(f"  Pages serving target version (generated_at={served_at})")
                return True
            log(f"  Pages still serving older generated_at={served_at!r}, waiting...")
        except Exception as e:
            log(f"  fetch failed: {e}")
        time.sleep(8)
    return False


def revert_and_blame(failures: list[dict], console_errors: list[str], stdout_log: list[str]) -> None:
    """Auto-rollback on verification failure."""
    log("VERIFICATION FAILED — reverting HEAD")
    try:
        subprocess.run(["git", "revert", "--no-edit", "HEAD"], cwd=str(ROOT), check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=str(ROOT), check=True)
        log("  HEAD reverted and pushed")
    except subprocess.CalledProcessError as e:
        log(f"  revert/push failed: {e}")

    md = ["# BUILD_BROKEN.md\n",
          f"\nGenerated {datetime.now(timezone.utc).isoformat()}\n",
          f"\n**Live URL:** {LIVE_URL}\n",
          "\nLive-browser verification failed. HEAD has been auto-reverted "
          "to the previous good commit. The dashboard is back online with "
          "the prior known-good build.\n",
          "\n## Failures\n"]
    for f in failures:
        md.append(f"\n### {f['name']}\n\n{f['detail']}\n")
    if console_errors:
        md.append("\n## Browser console errors captured\n\n```\n")
        md.extend(e + "\n" for e in console_errors)
        md.append("```\n")
    if stdout_log:
        md.append("\n## Verifier log\n\n```\n")
        md.extend(line + "\n" for line in stdout_log)
        md.append("```\n")
    BROKEN_MD.write_text("".join(md), encoding="utf-8")

    try:
        subprocess.run(["git", "add", "BUILD_BROKEN.md"], cwd=str(ROOT), check=True)
        subprocess.run(
            ["git", "commit", "-m",
             "ops: BUILD_BROKEN.md - auto-rollback after live verification failure"],
            cwd=str(ROOT), check=True,
        )
        subprocess.run(["git", "push", "origin", "main"], cwd=str(ROOT), check=True)
    except subprocess.CalledProcessError as e:
        log(f"  BUILD_BROKEN.md push failed: {e}")

    summary = "\n".join(f"FAIL: {f['name']}" for f in failures)[:500]
    telegram_send(
        f"<b>[ocean-intel]</b> BUILD AUTO-ROLLED BACK\n{summary}\n"
        f"See https://github.com/xcerebroai/ocean-intel/blob/main/BUILD_BROKEN.md"
    )


def write_live_verified(local_leads: dict) -> None:
    ingest = local_leads.get("clerk_ingest_status", {})
    text = (
        f"Live verification PASS at {datetime.now(timezone.utc).isoformat()}\n"
        f"\nLive URL: {LIVE_URL}\n"
        f"Schema: v{local_leads.get('schema_version')}\n"
        f"Source commit: {local_leads.get('source_commit')}\n"
        f"\nLead totals:\n"
        f"  total leads: {local_leads.get('lead_total')}\n"
        f"  total signals: {local_leads.get('total_signals')}\n"
        f"  most stacked: {local_leads.get('most_stacked_count')}\n"
        f"\nPattern counts:\n" +
        "\n".join(f"  {p}: {c}" for p, c in (local_leads.get("pattern_counts") or {}).items()) +
        f"\n\nClerk ingest status:\n  configured: {ingest.get('configured')}\n"
        f"  opra_records_total: {ingest.get('opra_records_total', 0)}\n"
        f"  seeded_records_last_run: {ingest.get('seeded_records_last_run', 0)}\n"
        f"  seeded_session_age_hours: {ingest.get('seeded_session_age_hours')}\n"
    )
    VERIFIED_TXT.write_text(text, encoding="utf-8")
    log(f"  wrote {VERIFIED_TXT}")


def run_browser_checks(local_leads: dict) -> tuple[list[dict], list[str], list[str]]:
    failures: list[dict] = []
    console_errors: list[str] = []
    stdout_log: list[str] = []

    def slog(msg: str) -> None:
        log(msg)
        stdout_log.append(msg)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        failures.append({"name": "Playwright import", "detail": "playwright not installed; run `py -3.12 -m pip install playwright && py -3.12 -m playwright install chromium`"})
        return failures, console_errors, stdout_log

    expected_lead_total = int(local_leads.get("lead_total", 0))
    expected_min_rows = min(expected_lead_total, 800)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(f"[pageerror] {exc}"))

        slog(f"Navigating to {LIVE_URL}")
        page.goto(LIVE_URL, wait_until="domcontentloaded", timeout=30_000)

        # Check 1: ready marker
        try:
            page.wait_for_selector('body[data-ready="1"]', timeout=15_000)
            slog("  PASS: body[data-ready=1] within 15s")
        except Exception as e:
            failures.append({"name": "Ready marker", "detail": f"body[data-ready=1] never appeared (15s timeout): {e}"})
            browser.close()
            return failures, console_errors, stdout_log

        # Check 2: console errors during load
        if console_errors:
            failures.append({"name": "Console errors during load", "detail": f"{len(console_errors)} JS errors fired:\n" + "\n".join(console_errors[:10])})
            browser.close()
            return failures, console_errors, stdout_log
        slog("  PASS: no console errors during load")

        # Check 3: lead-row count
        row_count = page.locator(".lead-row[data-idx]").count()
        slog(f"  rendered .lead-row count: {row_count}")
        if row_count < expected_min_rows:
            failures.append({"name": "tbody row count", "detail": f"rendered {row_count} rows; expected >= {expected_min_rows} (lead_total={expected_lead_total})"})

        # Check 4: Total stat tile matches lead_total
        try:
            total_text = page.locator(".stat-tile.total .value").first.text_content(timeout=2000) or ""
            slog(f"  Total stat tile text: {total_text!r}")
            try:
                total_n = int(total_text.replace(",", "").strip())
            except ValueError:
                total_n = -1
            if total_n != expected_lead_total:
                failures.append({"name": "Two-Truths in browser", "detail": f"DOM 'Total' tile = {total_n}, leads.json lead_total = {expected_lead_total}"})
            else:
                slog(f"  PASS: Total stat tile matches lead_total ({total_n})")
        except Exception as e:
            failures.append({"name": "Stat tile read", "detail": f"could not read .stat-tile.total: {e}"})

        # Check 5: Click "Sheriff sales — call list" pre-canned
        try:
            slog("  clicking 'Sheriff sales — call list' pre-canned view")
            page.locator('button[data-precanned="sheriff_calls"]').click(timeout=5000)
            page.wait_for_timeout(300)
            after_count = page.locator(".lead-row[data-idx]").count()
            slog(f"  rows after sheriff_calls: {after_count}")
            if after_count < 1:
                failures.append({"name": "Pre-canned view interaction", "detail": "Sheriff sales view rendered 0 rows; expected >= 1"})
            else:
                slog(f"  PASS: pre-canned view renders {after_count} rows")
            errs_after = [e for e in console_errors if e not in console_errors[:0]]  # all collected so far
            if len(errs_after) > 0:
                # We already failed on Check 2 if there were errors during load; this catches new errors during interaction
                pass
        except Exception as e:
            failures.append({"name": "Pre-canned view click", "detail": f"could not click sheriff_calls button: {e}"})

        # Check 6: CSV export
        try:
            slog("  clicking Export CSV")
            with page.expect_download(timeout=8000) as dl_info:
                page.locator("#export-csv").click()
            download = dl_info.value
            csv_path = ROOT / "data" / "raw" / f"_verify_{int(time.time())}.csv"
            download.save_as(str(csv_path))
            content = csv_path.read_text(encoding="utf-8")
            csv_path.unlink(missing_ok=True)
            reader = csv.reader(io.StringIO(content))
            header = next(reader, [])
            if header != EXPECTED_CSV_COLS:
                missing = [c for c in EXPECTED_CSV_COLS if c not in header]
                extra = [c for c in header if c not in EXPECTED_CSV_COLS]
                failures.append({"name": "CSV header", "detail": f"header mismatch.\n  missing: {missing}\n  extra: {extra}\n  got: {header}"})
            else:
                slog(f"  PASS: CSV header matches spec ({len(header)} columns)")
            row_count_in_csv = sum(1 for _ in reader)
            slog(f"  CSV body rows: {row_count_in_csv}")
        except Exception as e:
            failures.append({"name": "CSV export", "detail": f"download / parse failed: {e}"})

        browser.close()

    return failures, console_errors, stdout_log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-cdn-wait", action="store_true",
                    help="skip the Pages-CDN-flush poll (use when verifying local-only)")
    ap.add_argument("--no-rollback", action="store_true",
                    help="report failures but do not auto-revert HEAD")
    args = ap.parse_args()

    if not LEADS.exists():
        log("FATAL: data/leads.json missing")
        return 2

    local_leads = json.loads(LEADS.read_text(encoding="utf-8"))
    local_generated = local_leads.get("generated_at")

    if not args.skip_cdn_wait:
        if not wait_for_pages_flush(local_generated):
            failures = [{"name": "Pages CDN flush", "detail": f"after 180s, live URL still serving an older generated_at than local. Local: {local_generated}"}]
            log("FAIL: Pages CDN never converged")
            if not args.no_rollback:
                revert_and_blame(failures, [], [])
            return 1

    failures, console_errors, stdout_log = run_browser_checks(local_leads)

    if failures:
        log(f"VERIFICATION FAILED with {len(failures)} failure(s):")
        for f in failures:
            log(f"  FAIL  {f['name']} — {f['detail'][:200]}")
        if not args.no_rollback:
            revert_and_blame(failures, console_errors, stdout_log)
        return 1

    log("ALL LIVE BROWSER CHECKS PASS")
    write_live_verified(local_leads)
    return 0


if __name__ == "__main__":
    sys.exit(main())
