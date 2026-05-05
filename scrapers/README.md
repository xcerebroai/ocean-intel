# scrapers/

One scraper per source. Pattern is consistent: fetch only, no scoring, write
JSONL to `data/raw/`, persist a checkpoint for resume, retry/backoff, graceful
Ctrl+C. Joins, normalization, scoring all happen in `pipeline/build_leads.py`.

Phase 1 recon (see `../RECON.md`) determines which sources are accessible and
documents the per-source doc type taxonomy. Phase 2 builds one scraper per
viable source.

## Scraper contract

Every scraper in this folder must:

1. Be a single self-contained file under `scrapers/`.
2. Accept `--limit N`, `--reset`, `--since YYYY-MM-DD` flags.
3. Write JSONL to `data/raw/<source>_<doctype>.jsonl` (one file per doc type
   when applicable).
4. Persist a checkpoint to `data/raw/<source>.state.json`.
5. Use `Path(__file__).resolve().parents[1]` to locate the project root —
   never hardcode paths.
6. Apply ≥3 second rate limit between requests baseline.
7. Identify with a real User-Agent including contact email
   `infinitygauntletllc@gmail.com`.
8. Trap SIGINT/SIGTERM and flush a clean checkpoint on Ctrl+C.
9. Tag each output row with `_source` so the pipeline can disambiguate.
10. Field-canonicalize only — never score, score, classify, or join.
11. Be idempotent — re-running with same parameters does not duplicate rows.

## Operational notes — sources with friction

(Populated during Phase 2 as blockers are encountered. See `RECON.md` for the
upstream access-method assessment.)
