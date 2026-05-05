"""
refresh.py — daily orchestrator. Runs each scraper as a subprocess in
dependency order, then build_leads.py, then optionally commits + pushes.

Design rules:
  - One per-source failure does not abort the whole run.
  - Heartbeat tracks per-source last_success / failed_sources.
  - Pipeline / Two-Truths failure is a hard exit (code 2). Some sources can be
    stale; the pipeline cannot.
  - Logs to data/raw/refresh.log (append-mode, ISO timestamps).
  - --push commits leads.json + leads.previous.json + HEARTBEAT.json and
    pushes to origin/main.

CLI:
  python pipeline/refresh.py [--push] [--since-days N] [--source NAME]
                             [--dry-run] [--no-pipeline]
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
PYTHON = sys.executable  # whichever interpreter is running this is what we use

# Dependency order: parcels first (signal sources may attach to parcels later)
SOURCES: list[dict] = [
    {
        "name": "nj_modiv_parcels",
        "script": "scrapers/nj_modiv_parcels.py",
        "supports_since": True,
        # Force a full county pull weekly (Mondays). Otherwise incremental on PCLLASTUPD.
        "weekly_full_pull_dow": 0,  # Monday=0
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
]


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


def run_scraper(src: dict, since_days: int | None, dry_run: bool) -> tuple[bool, dict]:
    name = src["name"]
    script = ROOT / src["script"]
    if not script.exists():
        log_line(f"  [{name}] SKIP — script missing: {script}")
        return False, {"error": "script missing"}

    cmd = [PYTHON, str(script)]
    if src.get("supports_since") and since_days is not None:
        # Force a full re-pull on configured weekday for parcels
        full_pull_dow = src.get("weekly_full_pull_dow")
        if full_pull_dow is not None and date.today().weekday() == full_pull_dow:
            log_line(f"  [{name}] weekly full pull (Monday)")
            cmd.append("--reset")
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
            capture_output=True, text=True, timeout=60 * 60,  # 1h max per scraper
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
        return False, {"error": f"exit {proc.returncode}", "elapsed_s": elapsed}
    log_line(f"  [{name}] OK ({elapsed:.0f}s)")
    return True, {"elapsed_s": elapsed}


def run_pipeline(dry_run: bool) -> bool:
    log_line("Pipeline: build_leads.py")
    if dry_run:
        return True
    try:
        proc = subprocess.run(
            [PYTHON, str(ROOT / "pipeline" / "build_leads.py")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60 * 30,
        )
    except subprocess.TimeoutExpired:
        log_line("  pipeline timeout")
        return False
    for line in proc.stdout.splitlines():
        log_line(f"    | {line}")
    if proc.returncode != 0:
        for line in proc.stderr.splitlines()[-10:]:
            log_line(f"    ! {line}")
        log_line(f"  pipeline FAIL exit={proc.returncode}")
        return False
    log_line("  pipeline OK")
    return True


def run_git_push(commit_msg: str, dry_run: bool) -> bool:
    log_line(f"git: stage + commit + push  ({commit_msg!r})")
    if dry_run:
        return True
    try:
        subprocess.check_call(
            ["git", "add", "data/leads.json", "data/leads.previous.json", "HEARTBEAT.json"],
            cwd=str(ROOT),
        )
    except subprocess.CalledProcessError:
        # leads.previous.json may not exist on first run
        subprocess.run(
            ["git", "add", "data/leads.json", "HEARTBEAT.json"],
            cwd=str(ROOT), check=False,
        )
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
    ap.add_argument("--push", action="store_true", help="commit + push leads.json after pipeline")
    ap.add_argument("--since-days", type=int, default=7)
    ap.add_argument("--source", default="", help="run only this source by name")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-pipeline", action="store_true", help="skip the build_leads step (scrapers only)")
    args = ap.parse_args()

    log_line("=" * 60)
    log_line(f"refresh.py start  push={args.push}  since_days={args.since_days}  source={args.source!r}  dry_run={args.dry_run}")

    hb = load_heartbeat()
    failed: list[str] = []

    sources = SOURCES if not args.source else [s for s in SOURCES if s["name"] == args.source]
    for src in sources:
        ok, meta = run_scraper(src, args.since_days, args.dry_run)
        rec = hb["sources"].setdefault(src["name"], {})
        if ok:
            rec["last_success"] = datetime.now(timezone.utc).isoformat()
            rec["last_status"] = "ok"
        else:
            rec["last_status"] = "fail"
            rec["last_error"] = meta.get("error")
            failed.append(src["name"])
        rec.update({k: v for k, v in meta.items() if k != "error"})

    hb["failed_sources"] = failed

    if not args.no_pipeline:
        ok = run_pipeline(args.dry_run)
        if not ok:
            hb["pipeline_status"] = "fail"
            save_heartbeat(hb)
            log_line("FAIL: pipeline did not produce a valid leads.json")
            return 2

    hb["pipeline_status"] = "ok"
    hb["last_success_timestamp"] = datetime.now(timezone.utc).isoformat()
    save_heartbeat(hb)

    if args.push and not args.dry_run:
        # Read tier counts from the just-built leads.json for the commit msg
        try:
            data = json.loads((ROOT / "data" / "leads.json").read_text(encoding="utf-8"))
            tc = data.get("tier_counts", {})
            today = date.today().isoformat()
            msg = f"daily refresh {today} — {tc.get('hot',0)} hot / {tc.get('warm',0)} warm / {tc.get('active',0)} active"
        except Exception:
            msg = f"daily refresh {date.today().isoformat()}"
        run_git_push(msg, args.dry_run)

    log_line(f"refresh.py done  failed_sources={failed}")
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
