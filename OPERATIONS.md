# OPERATIONS — Ocean Intel (v2.0)

How the daily refresh runs in production, where logs land, what alerts fire,
and how OpenClaw can take over orchestration.

---

## Daily refresh contract

`pipeline/refresh.py` is the one entry point. It is idempotent, safe to
re-run, and never aborts on a single source failure.

```
python pipeline/refresh.py [--push] [--since-days N] [--source NAME]
                           [--dry-run] [--no-pipeline] [--force-parcels]
```

Default: pull the last 7 days of incremental data per source (where
supported), build leads, generate methodology, write `data/leads.json`. With
`--push`, commit + push.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | All sources OK, pipeline OK, push OK (if requested) |
| 1 | Some sources failed; pipeline still produced a valid `leads.json` |
| 2 | Pipeline pre-existing failure (Two-Truths, IO, etc.) — `leads.json` was NOT written. Page-worthy. |
| 3 | Run-over-run regression detected. Pipeline did not write `leads.json`. STALE_ALERT.txt written. |

---

## Scheduling

### Default — Windows Task Scheduler

Task XML at `scripts/daily_refresh.xml`. Per Rule 15 the XML uses
`LogonType=Password` (stored credentials) so the task runs whether the
operator is logged in or not.

**Registration requires the operator's password.** This cannot be done
autonomously. Operator runs:

```powershell
schtasks /create /xml scripts\daily_refresh.xml `
  /tn "ocean-intel-refresh" `
  /ru DESKTOP-3GOQFT6\Owner /rp <password> /f
```

Verify:

```powershell
schtasks /query /tn "ocean-intel-refresh" /v /fo list
```

Run on demand (auth not needed):

```powershell
schtasks /run /tn "ocean-intel-refresh"
```

Remove:

```powershell
schtasks /delete /tn "ocean-intel-refresh" /f
```

If the password is unavailable at build time, the autonomous build leaves
the v1 InteractiveToken registration in place (which only runs while the
operator is logged in) and documents this as an open item in
`BUILD_SUMMARY.md`.

### Alternative — OpenClaw

```powershell
& "C:\Users\Owner\AppData\Local\Programs\Python\Python312\python.exe" `
  C:\Dev\xcerebro-builds\projects\ocean-intel\pipeline\refresh.py --push
```

Working directory: `C:\Dev\xcerebro-builds\projects\ocean-intel`.

---

## Heartbeat

`HEARTBEAT.json` schema:

```json
{
  "last_success_timestamp": "2026-05-05T08:30:00+00:00",
  "pipeline_status": "ok",
  "sources": {
    "nj_modiv_parcels":     { "last_success": "...", "last_status": "ok" },
    "sheriff_foreclosures": { "last_success": "...", "last_status": "ok" },
    "civilview_sales":      { "last_success": "...", "last_status": "ok" },
    "clerk_metadata":       { "last_success": "...", "last_status": "ok" }
  },
  "failed_sources": []
}
```

`pipeline_status` enum: `ok` | `regression` | `fail` | `skipped`.
`sources[].last_status` enum: `ok` | `skipped` | `fail`.

---

## Alerts (Telegram)

Configured via `.env` — copy `.env.example` and set `TELEGRAM_BOT_TOKEN` +
`TELEGRAM_USER_ID`. Bot: `@Xcerebrobot`. When `.env` is missing or empty,
alerts log to `data/raw/refresh.log` only.

### 4 alert types

| Type | Trigger | Where |
|---|---|---|
| **Heartbeat stale** (Rule 16) | `last_success_timestamp` > 36h old at start of refresh | Telegram + `STALE_ALERT.txt` |
| **Source failed** (Rule 24) | Any scraper exits non-zero after 5 retry attempts | Telegram |
| **Regression detected** (Rule 19) | Run-over-run pattern_count drop >50% (or any count drop >75%) | Telegram + `STALE_ALERT.txt` |
| **New high-signal lead** (Rule 17) | New lead (by pid) with `pattern_count >= 3` appearing in current `leads.json` but not `leads.previous.json` | Telegram (one message per lead) |

---

## Logs

`data/raw/refresh.log` — append-only ISO-timestamped log of every refresh
run and every per-source result. Gitignored.

`data/raw/<source>.state.json` — per-source resume checkpoint. Gitignored.

`data/raw/sheriff_foreclosures.qa.json` — QA report from the sheriff parser.
`valid_pct < 90%` causes the scraper to exit non-zero (Rule 20). Gitignored.

`STALE_ALERT.txt` — written when heartbeat staleness or run-over-run
regression triggers. **Committed to repo**, visible on GitHub.

---

## Cadence rules

- **Parcel master (`nj_modiv_parcels`)** — weekly, Monday only (Rule 25). The
  NJ MOD-IV public ArcGIS layer publishes `PCLLASTUPD` on only ~14% of records
  with the max value 4 months stale, so per-record incrementals are not
  viable. Override with `--force-parcels`.
- **Sheriff PDF** — every refresh. The PDF re-publishes weekly (Tuesday sales
  cycle) and we dedupe on case_no + seq.
- **CivilView** — every refresh. Currently empty for Ocean (countyId=85), kept
  for cross-county portability.
- **Clerk metadata** — every refresh. Powers the dashboard's per-doc-type
  filter pills and the heartbeat staleness canary.

---

## Manual recovery

If `leads.json` is bad:

```powershell
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

If the regression detection is firing but the operator wants to ship anyway:

```powershell
py -3.12 pipeline\build_leads.py --no-fail-on-regression
```

---

## Gotchas

- **Sheriff PDF parser quarantine.** Rule 20 enforces ≥90% of parsed rows
  have block + lot + mun_name. Below that, scraper exits 1. v1 had a 47%
  miss rate due to mis-matched CH-only case-prefix regex; v2 matches all
  case types (CH/L/DJ/F/JL) and ships at 100% on the current PDF.
- **Leads file size.** Currently ~10 MB. GitHub's hard cap is 50 MB. At
  3-year transfer-pattern window with 298k parcels, the file fluctuates with
  recent-deed volume. If it crosses 35 MB, prune signals-per-pattern from 3
  to 2 in `pipeline/build_leads.py`.
- **Pages CDN lag.** GitHub Pages typically deploys within 60s of push, but
  the CDN can take longer to flush. The verification gate retries for 90s
  before logging a soft warning.
- **Owner names are redacted.** `multiple_properties` and `entity_owned`
  attributes fire only via mailing-line fallback today. Real fix: OPRA bulk
  MOD-IV access.
