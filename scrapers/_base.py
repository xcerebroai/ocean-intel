"""
Shared scraper utilities. Polite UA, rate limiting, JSONL append + dedupe,
checkpoint persistence, signal-trapped exit.

Every scraper must satisfy the contract documented in scrapers/README.md and
described at the top of OPERATIONS.md. This module enforces it.
"""
from __future__ import annotations
import json
import os
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

CONTACT_EMAIL = os.environ.get("SCRAPER_CONTACT_EMAIL", "infinitygauntletllc@gmail.com")

UA = (
    f"OceanIntel/0.1 (+contact {CONTACT_EMAIL}) "
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# Signal-handling / interrupt management ---------------------------------------

class StopFlag:
    """Set on SIGINT/SIGTERM. Scrapers poll this between requests for a clean
    Ctrl+C. State is checkpointed; partial JSONL is flushed line-by-line so no
    record is left half-written."""

    def __init__(self) -> None:
        self.stopped = False

    def install(self) -> None:
        def handler(signum, frame):
            sys.stderr.write(f"\n[scraper] caught signal {signum}, flushing checkpoint...\n")
            sys.stderr.flush()
            self.stopped = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, AttributeError):
                pass


# State checkpoint -------------------------------------------------------------

def state_path(source: str) -> Path:
    return RAW_DIR / f"{source}.state.json"


def load_state(source: str) -> dict:
    p = state_path(source)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(source: str, state: dict) -> None:
    p = state_path(source)
    state = dict(state)
    state["_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


# JSONL append with idempotent dedupe ------------------------------------------

def jsonl_path(source: str, doctype: str | None = None) -> Path:
    if doctype:
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in doctype.lower())
        return RAW_DIR / f"{source}_{safe}.jsonl"
    return RAW_DIR / f"{source}.jsonl"


def load_existing_keys(path: Path, key_field: str = "_key") -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                k = rec.get(key_field)
                if k:
                    keys.add(str(k))
            except Exception:
                continue
    return keys


def append_jsonl(path: Path, records: Iterable[dict], key_field: str = "_key") -> int:
    """Append records to JSONL, deduping against existing keys. Returns count
    actually written. Each record must have key_field populated by the caller."""
    existing = load_existing_keys(path, key_field=key_field)
    written = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            key = rec.get(key_field)
            if key is None:
                continue
            key = str(key)
            if key in existing:
                continue
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            existing.add(key)
            written += 1
    return written


# Polite rate limit ------------------------------------------------------------

class RateLimit:
    def __init__(self, min_seconds: float = 3.0) -> None:
        self.min = min_seconds
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed < self.min:
            time.sleep(self.min - elapsed)
        self._last = time.monotonic()


# HTTP retry with exponential backoff (Rule 24) -------------------------------

class RetryWithBackoff:
    """Wraps an HTTP call. Retries on 429 / 503 / network errors with backoff
    1s, 2s, 4s, 8s, 16s. After 5 failures, raises the last exception.

    Usage:
        retry = RetryWithBackoff()
        r = retry.call(lambda: requests.get(url, timeout=30))
        r.raise_for_status()
    """

    def __init__(self, max_attempts: int = 5, base_delay: float = 1.0) -> None:
        self.max_attempts = max_attempts
        self.base_delay = base_delay

    def call(self, fn):
        last_exc: BaseException | None = None
        for attempt in range(self.max_attempts):
            try:
                resp = fn()
                # Treat 429/503 as retryable
                status = getattr(resp, "status_code", None)
                if status in (429, 503):
                    raise RuntimeError(f"retryable HTTP {status}")
                return resp
            except Exception as exc:
                last_exc = exc
                if attempt == self.max_attempts - 1:
                    break
                delay = self.base_delay * (2 ** attempt)
                err(f"attempt {attempt + 1}/{self.max_attempts} failed ({exc}); sleeping {delay:.0f}s")
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc


# Reset support ----------------------------------------------------------------

def reset_source(source: str, doctypes: list[str] | None = None) -> None:
    """Wipe state + jsonl for a source. Used by --reset."""
    sp = state_path(source)
    if sp.exists():
        sp.unlink()
    if doctypes:
        for dt in doctypes:
            p = jsonl_path(source, dt)
            if p.exists():
                p.unlink()
    p = jsonl_path(source)
    if p.exists():
        p.unlink()


# Logging ----------------------------------------------------------------------

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.localtime())
    sys.stdout.write(f"[{ts}] {msg}\n")
    sys.stdout.flush()


def err(msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.localtime())
    sys.stderr.write(f"[{ts}] ERR {msg}\n")
    sys.stderr.flush()
