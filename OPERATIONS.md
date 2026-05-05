# OPERATIONS — Ocean Intel

How the daily refresh runs in production, where logs land, what alerts fire,
and how OpenClaw can take over orchestration.

---

## Daily refresh contract

`pipeline/refresh.py` is the one entry point. It is idempotent, safe to
re-run, and never aborts on a single source failure.

```
python pipeline/refresh.py [--push] [--since-days N] [--source NAME]
                           [--dry-run] [--no-pipeline]
```

Default: pull the last 7 days of incremental data per source, build leads,
write `data/leads.json`. With `--push`, commit + push.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | All sources OK, pipeline OK, push OK (if requested) |
| 1 | Some sources failed, but the pipeline still produced a valid `leads.json` |
| 2 | Pipeline / Two-Truths failed — `leads.json` was NOT written or was rejected. Treat as page-worthy. |

Per-source failure is logged and recorded in `HEARTBEAT.json.failed_sources[]`
but does **not** prevent the run from completing. Stale data is acceptable;
incorrect data is not.

---

## Scheduling

### Option A — Windows Task Scheduler (default)

A Task Scheduler XML lives at `scripts/daily_refresh.xml`. Register it:

```powershell
schtasks /create /xml scripts\daily_refresh.xml /tn "ocean-intel-refresh" /f
```

Runs daily at 4:00 AM local time (configured to system locale). Verify:

```powershell
schtasks /query /tn "ocean-intel-refresh" /v /fo list
```

To run on demand:

```powershell
schtasks /run /tn "ocean-intel-refresh"
```

To remove:

```powershell
schtasks /delete /tn "ocean-intel-refresh" /f
```

### Option B — OpenClaw orchestration

Treat refresh.py as a black-box that emits exit codes and updates
`HEARTBEAT.json`. From an OpenClaw routine:

```powershell
& "C:\Users\Owner\AppData\Local\Programs\Python\Python312\python.exe" `
  C:\Dev\xcerebro-builds\projects\ocean-intel\pipeline\refresh.py --push
```

Working directory: `C:\Dev\xcerebro-builds\projects\ocean-intel`.

OpenClaw is the better fit when this scaffold is ported to a second NJ county
(or moved off Quentin's workstation). The XML is the local-first default.

---

## Heartbeat

`HEARTBEAT.json` is the single source of truth on whether the pipeline is
alive. Schema:

```json
{
  "last_success_timestamp": "2026-05-05T08:30:00+00:00",
  "pipeline_status": "ok",
  "sources": {
    "nj_modiv_parcels":     { "last_success": "...", "last_status": "ok",   "elapsed_s": 920.0 },
    "sheriff_foreclosures": { "last_success": "...", "last_status": "ok",   "elapsed_s": 7.5 },
    "civilview_sales":      { "last_success": "...", "last_status": "ok",   "elapsed_s": 3.0 },
    "clerk_metadata":       { "last_success": "...", "last_status": "ok",   "elapsed_s": 2.0 }
  },
  "failed_sources": []
}
```

Staleness alert: if `last_success_timestamp` is older than 36 hours, alert.

---

## Alerting (Telegram)

When `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env`, the
refresh harness sends:

1. **Failure alert** — any source that fell from `last_success < 48h ago` into
   `failed_sources[]` triggers a single message:
   `[ocean-intel] FAIL <source>: <error excerpt>`.
2. **High-tier delta alert** — when a Hot or Warm lead appears in the new
   `leads.json` that was not in `leads.previous.json` (compared on `pid`):
   `[ocean-intel] NEW <tier> lead: <pid> <address> patterns=[...] dashboard=<url>`.

Bot: `@Xcerebrobot` (token in `.env`). Chat: `6004053137`. (Implementation is
a TODO pending the OpenClaw notification wiring; the dispatch points are
flagged in `refresh.py` with comments at the failure / new-lead detection
sites.)

---

## Logs

`data/raw/refresh.log` — append-only ISO-timestamped log of every refresh run
and every per-source result. Rotate via Windows log retention if it grows.
Gitignored.

`data/raw/<source>.state.json` — per-source resume checkpoint (offset, last
processed ID, etc.). Gitignored.

---

## Gotchas

- The parcel scraper's full-county pull takes ~17 minutes (NJ ArcGIS rate
  limit + 1000-record paging × 298 pages). Daily incremental (last-7-day
  PCLLASTUPD) takes seconds. Force the full pull weekly (Mondays) to catch
  records the incremental misses.
- The sheriff PDF URL is a stable GUID that has not rotated since the page
  was last published. If it does rotate, `discover_pdf_url()` re-resolves it
  by re-parsing the landing page — no manual intervention needed.
- The clerk-search reCAPTCHA v3 block can re-open without warning if NewVision
  changes their site config. The `clerk_metadata` heartbeat tracks
  `verifyDate` daily — if the search API unblocks, that's the canary.
- Leads file size: today ~20 MB at full-county pull (uncompressed). GitHub's
  hard cap is 50 MB per file. Monitor; if it crosses 35 MB, prune
  signals-per-pattern down from 3 to 2 in `pipeline/build_leads.py`.

---

## Manual recovery

If `leads.json` is bad:

```powershell
# Re-run pipeline only (skip scrapers; use existing JSONL)
py -3.12 pipeline\build_leads.py
```

If a single scraper is wedged:

```powershell
py -3.12 pipeline\refresh.py --source <name>
```

If state is corrupted:

```powershell
py -3.12 scrapers\<scraper>.py --reset
```

Reset wipes both `state.json` and the JSONL. Next run starts from offset 0.
