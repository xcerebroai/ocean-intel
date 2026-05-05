"""
refresh.py (v2.0) — daily orchestrator. Runs each scraper as a subprocess in
dependency order, then build_leads.py, then build_methodology.py, then
optionally commits + pushes.

v2.0 additions:
  - Heartbeat staleness check (Rule 16): if HEARTBEAT.json.last_success_timestamp
    is older than 36h, write STALE_ALERT.txt and try Telegram.
  - Parcel master scheduled weekly (Rule 25 enforcement): nj_modiv_parcels
    runs only on Monday (or with --force-parcels).
  - Telegram new-high-signal-lead alert (Rule 17): after pipeline succeeds,
    diff records vs leads.previous.json. Each new lead with
    pattern_count >= 3 → individual Telegram message.
  - Run-over-run regression alert (Rule 19): pipeline exit 3 → log + Telegram.

CLI:
  python pipeline/refresh.py [--push] [--since-days N] [--source NAME]
                             [--dry-run] [--no-pipeline] [--force-parcels]
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "raw" / "refresh.log"
HEARTBEAT = ROOT / "HEARTBEAT.json"
STALE_ALERT = ROOT / "STALE_ALERT.txt"
LEADS = ROOT / "data" / "leads.json"
LEADS_PREV = ROOT / "data" / "leads.previous.json"
ENV_FILE = ROOT / ".env"
PYTHON = sys.executable

DASHBOARD_URL = "https://xcerebroai.github.io/ocean-intel/"

SOURCES: list[dict] = [
    {
        "name": "nj_modiv_parcels",
        "script": "scrapers/nj_modiv_parcels.py",
        "supports_since": True,
        "weekly_only_dow": 0,  # Monday — see Rule 25; skip the rest of the week
    },
    {
        "name": "sheriff_foreclosures",
        "script": "scrapers/sheriff_foreclosures.py",
        "supports_since": True,
    },
    {
        "name": "civilview_sales",
        "script": "scrapers/civilview_sales.py",
        "supports_since": False,
    },
    {
        "name": "clerk_metadata",
        "script": "scrapers/clerk_metadata.py",
        "supports_since": False,
    },
    {
        "name": "clerk_seeded",
        "script": "scrapers/clerk_seeded.py",
        "supports_since": True,
        # Skip silently if CLERK_SESSION_COOKIES not set in .env
        "needs_env": "CLERK_SESSION_COOKIES",
    },
    {
        "name": "clerk_opra_ingest",
        "script": "scrapers/clerk_opra_ingest.py",
        "supports_since": False,
    },
]


# ----- .env / Telegram --------------------------------------------------------

def load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def telegram_send(text: str) -> bool:
    """Send a Telegram message. Returns True on success. Logs and returns
    False if no creds OR if the request fails. Never raises."""
    env = load_env()
    bot = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = env.get("TELEGRAM_USER_ID") or env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_USER_ID")
    if not (bot and chat):
        log_line(f"  telegram: SKIP (no creds in .env)")
        return False
    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{bot}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status == 200
        log_line(f"  telegram: {'OK' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        log_line(f"  telegram: ERR {e}")
        return False


# ----- Logging / heartbeat ----------------------------------------------------

def log_line(msg: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_heartbeat() -> dict:
    if not HEARTBEAT.exists():
        return {"last_success_timestamp": None, "sources": {}, "failed_sources": []}
    try:
        return json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except Exception:
        return {"last_success_timestamp": None, "sources": {}, "failed_sources": []}


def save_heartbeat(hb: dict) -> None:
    HEARTBEAT.write_text(json.dumps(hb, indent=2), encoding="utf-8")


# ----- Staleness check (Rule 16) ----------------------------------------------

def check_heartbeat_staleness() -> None:
    hb = load_heartbeat()
    last_ts = hb.get("last_success_timestamp")
    if not last_ts:
        return
    try:
        last = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except Exception:
        return
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    if age_h > 36:
        msg = (
            f"<b>[ocean-intel]</b> heartbeat stale\n"
            f"last_success_timestamp = {last_ts}\n"
            f"age = {age_h:.1f}h > 36h threshold"
        )
        STALE_ALERT.write_text(
            f"[{datetime.now(timezone.utc).isoformat()}]\n{msg}\n",
            encoding="utf-8",
        )
        log_line(f"  STALE ALERT: heartbeat {age_h:.1f}h old")
        telegram_send(msg)


def check_clerk_session_age() -> None:
    """Warn when seeded clerk session is 72-96h old (still valid but
    re-seeding is recommended)."""
    env = load_env()
    seeded_at = env.get("CLERK_SESSION_SEEDED_AT") or os.environ.get("CLERK_SESSION_SEEDED_AT")
    if not seeded_at:
        return
    try:
        seeded = datetime.fromisoformat(seeded_at.replace("Z", "+00:00"))
    except Exception:
        return
    age_h = (datetime.now(timezone.utc) - seeded).total_seconds() / 3600.0
    if 72 <= age_h <= 96:
        log_line(f"  clerk session age {age_h:.1f}h — re-seed recommended within 24h")
        telegram_send(
            f"<b>[ocean-intel]</b> Clerk seeded-session is {age_h:.1f}h old\n"
            f"Re-seed within 24h to avoid expired pulls. See methodology -> "
            f"\"Seeding the clerk session\"."
        )


# ----- Scraper subprocess wrapper ---------------------------------------------

def run_scraper(src: dict, since_days: int | None, dry_run: bool, force_parcels: bool) -> tuple[bool, dict]:
    name = src["name"]
    script = ROOT / src["script"]
    if not script.exists():
        log_line(f"  [{name}] SKIP — script missing: {script}")
        return False, {"error": "script missing"}

    # Rule 25: parcel scraper runs Mondays only unless forced.
    weekly_only_dow = src.get("weekly_only_dow")
    if weekly_only_dow is not None and not force_parcels:
        if date.today().weekday() != weekly_only_dow:
            log_line(f"  [{name}] SKIP (weekly cadence — Monday only)")
            return True, {"skipped": "weekly_cadence"}

    # Skip if a required .env var is missing (clerk_seeded, etc.)
    needs_env = src.get("needs_env")
    if needs_env:
        env = load_env()
        if not (env.get(needs_env) or os.environ.get(needs_env)):
            log_line(f"  [{name}] SKIP (env var {needs_env} not set)")
            return True, {"skipped": "env_not_configured"}

    cmd = [PYTHON, str(script)]
    if src.get("supports_since") and since_days is not None and weekly_only_dow is None:
        # Daily-source incremental window — convert to --since-days for
        # scrapers that take that arg, otherwise --since YYYY-MM-DD.
        if name == "clerk_seeded":
            cmd.extend(["--since-days", str(since_days)])
        else:
            since = (date.today() - timedelta(days=since_days)).isoformat()
            cmd.extend(["--since", since])

    log_line(f"  [{name}] $ {' '.join(cmd[1:])}")
    if dry_run:
        return True, {"dry_run": True}

    started = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT),
            capture_output=True, text=True, timeout=60 * 60,
        )
    except subprocess.TimeoutExpired:
        log_line(f"  [{name}] FAIL — timeout (60min)")
        return False, {"error": "timeout"}

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    if proc.stdout:
        for line in proc.stdout.splitlines()[-8:]:
            log_line(f"    | {line}")
    if proc.returncode != 0:
        log_line(f"  [{name}] FAIL exit={proc.returncode}  ({elapsed:.0f}s)")
        if proc.stderr:
            for line in proc.stderr.splitlines()[-5:]:
                log_line(f"    ! {line}")
        # Special case: clerk_seeded exits 4 when the session expires.
        # Notify the operator with the re-seed instructions.
        if name == "clerk_seeded" and proc.returncode == 4:
            telegram_send(
                "<b>[ocean-intel]</b> Clerk session EXPIRED\n"
                "Re-seed in &lt;24h:\n"
                "1) Open https://sng.co.ocean.nj.us/publicsearch/ in Chrome\n"
                "2) Run any search to clear reCAPTCHA v3\n"
                "3) DevTools -> Application -> Cookies -> copy ALL cookies as a single string\n"
                "4) Paste into <code>.env</code> CLERK_SESSION_COOKIES=<i>...</i>\n"
                "5) Update CLERK_SESSION_SEEDED_AT=<i>now</i>"
            )
            return False, {"error": "session_expired", "elapsed_s": elapsed}
        return False, {"error": f"exit {proc.returncode}", "elapsed_s": elapsed}
    log_line(f"  [{name}] OK ({elapsed:.0f}s)")
    return True, {"elapsed_s": elapsed}


# ----- Pipeline + methodology -------------------------------------------------

def run_pipeline(dry_run: bool) -> int:
    """Returns the build_leads.py exit code."""
    log_line("Pipeline: build_leads.py")
    if dry_run:
        return 0
    try:
        proc = subprocess.run(
            [PYTHON, str(ROOT / "pipeline" / "build_leads.py")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60 * 30,
        )
    except subprocess.TimeoutExpired:
        log_line("  pipeline timeout")
        return 99
    for line in proc.stdout.splitlines():
        log_line(f"    | {line}")
    if proc.returncode != 0:
        for line in proc.stderr.splitlines()[-10:]:
            log_line(f"    ! {line}")
        log_line(f"  pipeline FAIL exit={proc.returncode}")
        if proc.returncode == 3:
            telegram_send(
                "<b>[ocean-intel]</b> Run-over-run regression detected. "
                f"Pipeline exited 3. See <code>STALE_ALERT.txt</code>."
            )
        return proc.returncode
    log_line("  pipeline OK")
    return 0


def run_methodology(dry_run: bool) -> bool:
    log_line("Methodology: build_methodology.py")
    script = ROOT / "pipeline" / "build_methodology.py"
    if not script.exists():
        log_line("  build_methodology.py missing — skipping")
        return True
    if dry_run:
        return True
    try:
        proc = subprocess.run(
            [PYTHON, str(script)], cwd=str(ROOT),
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        log_line("  methodology timeout")
        return False
    if proc.returncode != 0:
        log_line(f"  methodology FAIL exit={proc.returncode}")
        for line in proc.stderr.splitlines()[-5:]:
            log_line(f"    ! {line}")
        return False
    log_line("  methodology OK")
    return True


# ----- New high-signal lead diff (Rule 17) ------------------------------------

def alert_new_high_signal_leads() -> int:
    """After the pipeline writes leads.json, diff vs leads.previous.json. New
    leads (by pid) with pattern_count >= 3 get individual Telegram messages.
    Returns count of alerts dispatched."""
    if not LEADS.exists():
        return 0
    try:
        cur = json.loads(LEADS.read_text(encoding="utf-8"))
    except Exception:
        return 0
    prev_pids: set[str] = set()
    if LEADS_PREV.exists():
        try:
            prev = json.loads(LEADS_PREV.read_text(encoding="utf-8"))
            prev_pids = {r.get("pid") for r in prev.get("records", []) if r.get("pid")}
        except Exception:
            pass
    pat_disp = cur.get("pattern_display", {})
    sent = 0
    for r in cur.get("records", []):
        pid = r.get("pid")
        if not pid or pid in prev_pids:
            continue
        if r.get("pattern_count", 0) < 3:
            continue
        chips = [pat_disp.get(p, p) for p in r.get("patterns", [])]
        msg = (
            f"<b>[ocean-intel]</b> NEW {r['pattern_count']}+ stack lead\n"
            f"<b>{pid}</b> — {r.get('situs_address') or ''}, {r.get('mun_name') or ''}\n"
            f"Lead types: {', '.join(chips)}\n"
            f'<a href="{DASHBOARD_URL}#pid={pid}">Open in dashboard</a>'
        )
        if telegram_send(msg):
            sent += 1
    if sent:
        log_line(f"  alerted {sent} new high-signal lead(s)")
    return sent


# ----- Git push ---------------------------------------------------------------

def run_git_push(commit_msg: str, dry_run: bool) -> bool:
    log_line(f"git: stage + commit + push  ({commit_msg!r})")
    if dry_run:
        return True
    files = ["data/leads.json", "data/leads.previous.json", "HEARTBEAT.json", "methodology.html"]
    if STALE_ALERT.exists():
        files.append(STALE_ALERT.name)
    for f in files:
        subprocess.run(["git", "add", f], cwd=str(ROOT), check=False)
    diff_proc = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=str(ROOT)
    )
    if diff_proc.returncode == 0:
        log_line("  git: no staged changes — skipping commit")
        return True
    rc = subprocess.call(["git", "commit", "-m", commit_msg], cwd=str(ROOT))
    if rc != 0:
        log_line(f"  git commit failed (exit {rc})")
        return False
    rc = subprocess.call(["git", "push"], cwd=str(ROOT))
    if rc != 0:
        log_line(f"  git push failed (exit {rc})")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--since-days", type=int, default=7)
    ap.add_argument("--source", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-pipeline", action="store_true")
    ap.add_argument("--force-parcels", action="store_true",
                    help="ignore Monday-only weekly cadence and run nj_modiv_parcels")
    args = ap.parse_args()

    log_line("=" * 60)
    log_line(f"refresh.py start  push={args.push}  since_days={args.since_days}  "
             f"source={args.source!r}  dry_run={args.dry_run}  force_parcels={args.force_parcels}")

    # Rule 16: heartbeat staleness check at start
    check_heartbeat_staleness()
    check_clerk_session_age()

    # First-of-month OPRA reminder (regenerate the request PDF and prompt the
    # operator to sign + send). Best-effort — never fails the run.
    if date.today().day == 1:
        try:
            opra_proc = subprocess.run(
                [PYTHON, str(ROOT / "pipeline" / "opra_request.py"), "--standing"],
                cwd=str(ROOT), capture_output=True, text=True, timeout=60,
            )
            if opra_proc.returncode == 0:
                log_line("  OPRA monthly request PDF regenerated (day 1 of month)")
                telegram_send(
                    "<b>[ocean-intel]</b> Monthly OPRA reminder\n"
                    "A fresh request PDF has been generated for last month's "
                    "clerk records. Sign and email to John P. Kelly's office. "
                    "See <code>opra_requests/</code>."
                )
        except Exception as e:
            log_line(f"  OPRA reminder skipped: {e}")

    hb = load_heartbeat()
    failed: list[str] = []

    sources = SOURCES if not args.source else [s for s in SOURCES if s["name"] == args.source]
    for src in sources:
        ok, meta = run_scraper(src, args.since_days, args.dry_run, args.force_parcels)
        rec = hb["sources"].setdefault(src["name"], {})
        if ok:
            rec["last_success"] = datetime.now(timezone.utc).isoformat()
            rec["last_status"] = "ok" if not meta.get("skipped") else "skipped"
        else:
            rec["last_status"] = "fail"
            rec["last_error"] = meta.get("error")
            failed.append(src["name"])
            telegram_send(
                f"<b>[ocean-intel]</b> source FAIL: <code>{src['name']}</code>\n"
                f"error: {meta.get('error')}"
            )
        rec.update({k: v for k, v in meta.items() if k != "error"})

    hb["failed_sources"] = failed

    pipeline_exit = 0
    if not args.no_pipeline:
        pipeline_exit = run_pipeline(args.dry_run)
        if pipeline_exit not in (0, 3):
            hb["pipeline_status"] = "fail"
            save_heartbeat(hb)
            log_line("FAIL: pipeline did not produce a valid leads.json")
            return 2
        hb["pipeline_status"] = "ok" if pipeline_exit == 0 else "regression"
        run_methodology(args.dry_run)
    else:
        hb["pipeline_status"] = "skipped"

    # Mark heartbeat success if we wrote leads.json this run
    if pipeline_exit == 0:
        hb["last_success_timestamp"] = datetime.now(timezone.utc).isoformat()
    save_heartbeat(hb)

    # Rule 17: new high-signal-lead alert
    if pipeline_exit == 0 and not args.dry_run:
        alert_new_high_signal_leads()

    if args.push and not args.dry_run and pipeline_exit == 0:
        try:
            data = json.loads(LEADS.read_text(encoding="utf-8"))
            tc = data.get("pattern_counts", {})
            today = date.today().isoformat()
            msg = (
                f"daily refresh {today} — "
                f"foreclosure {tc.get('foreclosure',0)}, transfer {tc.get('transfer',0)}, "
                f"signals {data.get('total_signals',0)}, leads {data.get('lead_total',0)}"
            )
        except Exception:
            msg = f"daily refresh {date.today().isoformat()}"
        run_git_push(msg, args.dry_run)

    log_line(f"refresh.py done  failed_sources={failed}  pipeline_exit={pipeline_exit}")
    if pipeline_exit == 3:
        return 3
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
