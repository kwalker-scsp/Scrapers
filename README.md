# Russo-Ukrainian War Timeline

Automated intelligence aggregation, deduplication, and Human-in-the-Loop review
for the Russo-Ukrainian War. Scrapes ISW / CFR / Covert Shores, extracts
structured events with Gemini, deduplicates them with a local ONNX embedding
model, and serves a public timeline plus an analyst review queue.

**Almost nothing self-installs.** The only thing that sets itself up is the
database schema — `init_db()` creates the tables, indexes, and column
migrations on first run. Everything below has to be done by hand.

---

## Setup

### 1. Prerequisites

- **Python 3.10 or newer.** `pipeline.py` uses `str | None` union syntax, which
  is a syntax error on 3.9. **3.11 is the verified version.**
- **PostgreSQL**, running, with a database created for this project.
- **~500MB free disk** for the Chromium build Crawl4AI needs.

### 2. Virtual environment

Any `.venv` already in the repo is stale — it predates the web app and is
missing `fastapi`, `uvicorn`, and `psycopg2`. Delete and rebuild it so a single
interpreter runs both the scraper and the app:

```bash
rm -rf .venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Verify in one shot — this is exactly the check the old `.venv` fails:

```bash
python -c "import crawl4ai, fastapi, uvicorn, psycopg2, onnxruntime, tokenizers, jinja2, multipart, numpy, pydantic, dotenv; from google import genai; print('all imports OK')"
```

### 3. Browser binaries (scraper only)

```bash
crawl4ai-setup          # or: playwright install chromium
```

Only `scraping.py` needs this. **The web app runs fine without it** — if you
are only working on the UI, you can skip this step.

### 4. `model.onnx` — copy it manually

The 86MB embedding model (`all-MiniLM-L6-v2`) is **deliberately not in git**.
`tokenizer.json` *is* tracked, so you only need the one file. Copy it into the
repository root from an existing machine.

If it is missing, embedding fails loudly rather than silently returning zero
vectors, so you will see:

```
RuntimeError: ONNX embedding model not found at /path/to/model.onnx.
Deduplication and search cannot run without it.
```

This stops **deduplication and search** — the scraper's extraction and the
timeline views still work. Both paths are overridable via
`LOCAL_ONNX_MODEL_PATH` / `LOCAL_TOKENIZER_PATH`; relative values resolve
against the repo root (`embedding.py`), not the working directory.

### 5. `.env`

Gitignored, so it must be recreated on every machine. Create it in the repo
root:

| Key | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | one of the two | Remote Postgres DSN. Tried **first** — see Gotchas. |
| `DATABASE_URL_LOCAL` | one of the two | Local Postgres DSN. Used if the remote is unreachable. |
| `GEMINI_API_KEY` | for scraping | Event extraction in `pipeline.py`. |
| `GEMINI_API_KEY_2` | for AI search | Search re-ranking. **Separate key on purpose** so search cannot exhaust the scraper's quota. |
| `ADMIN_PASSWORD` | for AI search | Gates the "Refine with AI" button. A spend control, not authentication. |
| `LOCAL_ONNX_MODEL_PATH` | no | Defaults to `model.onnx` in the repo root. |
| `LOCAL_TOKENIZER_PATH` | no | Defaults to `tokenizer.json` in the repo root. |
| `THRESHOLD_AUTO_MERGE` | no | Default `0.95`. Cosine ≥ this auto-merges as a duplicate. |
| `THRESHOLD_DUP_REVIEW` | no | Default `0.90`. Cosine ≥ this goes to the duplicate queue. |
| `SEARCH_MODEL` | no | Default `gemini-flash-lite-latest`. Pin a version here if ranking shifts. |
| `SEARCH_TIMEOUT_MS` | no | Default `20000`. Re-rank falls back to local results on timeout. |

At least one of `DATABASE_URL` / `DATABASE_URL_LOCAL` must be set, or startup
raises `ValueError: No database URL configured`.

### 6. Database schema

Nothing to run explicitly — both entry points call `init_db()` on startup,
which creates the four tables, their indexes, and any missing columns. To do it
ahead of time:

```bash
python -c "import database; database.init_db()"
```

### 7. Data (optional but recommended)

A fresh database is empty; the timeline renders its empty state until records
are approved.

```bash
python seed_from_json.py     # 216 demo records from the bundled JSON
python backfill.py all       # repair truncated titles, compute embeddings
```

**Search returns nothing until embeddings exist**, so run `backfill.py` after
any bulk insert. Both scripts are idempotent. `seed_from_json.py --reset`
deletes all rows first and prompts for confirmation.

### 8. Run it

```bash
# Review + timeline app -> http://127.0.0.1:8080
cd "Application Interface" && python app.py

# One scrape pass (needs step 3)
python scraping.py
```

`scraping.py` exits non-zero if any source fails, so it is safe to schedule.

---

## Operational gotchas

**The database can silently switch under you.** `get_db_connection()` tries
`DATABASE_URL` first and falls back to `DATABASE_URL_LOCAL` when the remote is
unreachable. If your data lives in one and not the other, the UI will look
empty even though nothing was deleted. Startup prints which DSN won:

```
[Database] Connected via: local postgresql://***@localhost:5432/scsp_scraper
```

Read that line before debugging missing data. To force local, unset
`DATABASE_URL` for the process:

```bash
DATABASE_URL= python app.py
```

**Environment overrides must be set before import.** `database.py` and
`embedding.py` call `load_dotenv()` at import time, and `load_dotenv` defaults
to `override=False` — so a value already in the environment wins, but one you
set *after* importing does not. Export it in the shell (`DATABASE_URL= python
app.py`) rather than assigning `os.environ[...]` mid-script, or `.env` will
quietly win and you will connect somewhere unexpected.

**Failover is sticky.** Once the remote times out, it is not probed again for
the life of the process — otherwise every write would pay the 3s timeout, which
made a 216-row seed take ~11 minutes. Restart to retry, or call
`database.reset_remote_probe()`.

**AI search costs a call every time.** Ordinary search is 100% local: ONNX
vectors plus lexical matching, no API call, no tokens. Only the password-gated
"Refine with AI" button spends quota, and it re-prompts on every use by design.
`GET /search` can never spend a call.

**Undated events are excluded from charts.** Records whose `event_date` is not
a strict `YYYY-MM-DD` (e.g. the `2025-08-XX` placeholders the extractor
sometimes emits) are counted and reported under the chart rather than plotted,
since they cannot be placed on a time axis honestly.

---

## Layout

```
Scrapers/
├── requirements.txt
├── database.py              # schema, migrations, connection routing, queries
├── embedding.py             # local ONNX embeddings (shared by pipeline + app)
├── pipeline.py              # crawl -> Gemini extraction -> dedup -> routing
├── scraping.py              # CLI entry point for a scrape run
├── seed_from_json.py        # load the bundled demo dataset
├── backfill.py              # repair titles / compute missing embeddings
├── model.onnx               # NOT in git — copy manually (86MB)
├── tokenizer.json           # tracked
└── Application Interface/
    ├── app.py               # FastAPI: /, /daily, /admin, /search
    ├── static/style.css     # brand palette — do not redefine its tokens
    └── templates/           # _layout, home, daily, queue, merge_resolve
```
