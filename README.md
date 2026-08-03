# Russo-Ukrainian War Timeline

A curated, autonomously-fed interactive timeline with a human-in-the-loop review
queue. Scrapers propose; you decide; every decision is logged.

```
                 ┌──────────────────────────────────────────┐
  scrapers ─────▶│ POST /api/v1/ingest   (INGEST_API_KEY)   │
  (many, cron)   └────────────────┬─────────────────────────┘
                                  │ validate → dedup → queue
                                  ▼
                        ┌───────────────────────┐
                        │  pending_submissions  │  ◀── invisible to the public
                        └──────────┬────────────┘
                                   │  approve / reject / edit-then-approve
                    ┌──────────────┴──────────────┐  (REVIEW_API_KEY)
                    │  /review dashboard          │
                    └──────────────┬──────────────┘
                                   ▼
                          ┌─────────────────┐        ┌───────────┐
                          │  events         │───────▶│ audit_log │
                          │  (published)    │        │ append-   │
                          └────────┬────────┘        │ only      │
                                   │                 └───────────┘
                                   ▼
                     GET /api/v1/events  →  public timeline at /
```

**The one-way rule:** ingest can only write to `pending_submissions`. The only
code path that writes to `events` is an approved review decision. The public read
API filters on `status='published'` in every query.

---

## Quickstart

```bash
make install     # creates .venv, installs deps, copies .env.example -> .env
make migrate     # creates data/timeline.db
make seed        # imports the 216 events from the seed JSON as published
make dev         # http://127.0.0.1:8000
```

| URL | What |
|---|---|
| <http://127.0.0.1:8000/> | Public interactive timeline |
| <http://127.0.0.1:8000/review> | Review dashboard (needs `REVIEW_API_KEY`) |
| <http://127.0.0.1:8000/docs> | Interactive API docs |
| <http://127.0.0.1:8000/openapi.json> | Machine-readable schema for your scrapers |
| <http://127.0.0.1:8000/api/v1/ingest/contract> | Ingest contract + dedup algorithm |

`make test` runs 95 tests. `make reset` rebuilds the DB from scratch.

**Before exposing this to anything,** put real keys in `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## The ingest contract

This is what you build your scrapers against. It is enforced by Pydantic models
in [app/schemas.py](app/schemas.py) and published as JSON Schema at
`/openapi.json` — `make openapi` dumps it to a file you can vendor into the
scraper repo. A working reference client is
[scripts/example_submit.py](scripts/example_submit.py) (standard library only).

```
POST /api/v1/ingest
X-API-Key: <INGEST_API_KEY>
X-Scraper-Agent: isw-daily        (optional, recorded in the audit log)
Content-Type: application/json
```

### Proposing a new event

```json
{
  "submission_type": "new",
  "scraper": "isw-daily",
  "scraper_run_id": "2026-07-30T06:00Z",
  "confidence": 0.85,
  "notes": "Free text for the reviewer.",
  "external_id": "isw-2026-08-12-volgograd",
  "event": {
    "date_text": "12 August 2026",
    "date_start": "2026-08-12",
    "date_end": "2026-08-12",
    "date_precision": "day",
    "section": "2026: Ceasefire Attempts, Continued Strikes, and Ukraine Regains the Initiative",
    "subsection": null,
    "body": "Ukrainian long-range drones strike an oil refinery in Volgograd…",
    "tags": ["Warfare Shift"],
    "research_categories": ["Fire/Maneuver"],
    "sources": [
      {
        "name": "ISW",
        "url": "https://example.org/isw/2026-08-12",
        "title": "Russian Offensive Campaign Assessment",
        "accessed_at": "2026-08-13"
      }
    ]
  }
}
```

Required: `submission_type`, `scraper`, `event.date_precision`, `event.body`
(≥ 10 chars). Everything else is optional.

`sources` accepts a **bare string or an object** — `"ISW"` and `{"name": "ISW"}`
are equivalent. Send the object form when you have a URL, which you usually will.

`external_id` is optional but recommended: it's *your* stable id for the event.
When present it takes precedence over the content hash when matching, so you can
re-submit a corrected version of your own event and have it recognised as the
same one.

### Proposing an edit

Send **only the fields you want to change.** The server reads the current
published values and stores a field-level before/after diff.

```json
{
  "submission_type": "edit",
  "scraper": "date-refiner",
  "target_event_id": 1,
  "notes": "CFR gives an exact date for the crackdown.",
  "patch": {
    "date_start": "2013-11-21",
    "date_end": "2013-11-21",
    "date_precision": "day",
    "date_text": "21 November 2013"
  }
}
```

Use `target_external_id` instead of `target_event_id` to address the event by
your own id. Editable fields: `date_text`, `date_start`, `date_end`,
`date_precision`, `section`, `subsection`, `body`, `tags`,
`research_categories`, `sources`. An explicit `null` clears a nullable field.
Anything else (`id`, `version`, `status`, `dedup_key`) is rejected with a 422.

Two things happen server-side that are worth knowing about:

* **The patch is re-validated against the live event.** A patch that is
  well-formed on its own but contradictory in context — `date_precision: "day"`
  on an event with no `date_start` — fails with a 422 at submission time, not at
  approval time.
* **No-op patches don't create review work.** If your patch restates values the
  event already has, you get `duplicate_of_published` and nothing is queued.

### Date rules

| `date_precision` | Meaning |
|---|---|
| `day` | A specific calendar day |
| `month` | Month known, day not |
| `month-range` | e.g. "March–May 2022" |
| `season` | e.g. "summer 2023" |
| `year` | Year only |
| `year-range` | e.g. "2014–2022" |
| `range` | An explicit span that isn't month/year aligned |
| `approx` | Best-effort anchor date; treat as soft |
| `undated` | No date could be assigned |

* `date_start` is **required** unless `date_precision` is `undated`.
* `date_precision: "undated"` must **not** carry dates — sending them is a 422.
  (Use `approx` or `year` if you have an anchor.)
* `date_end` defaults to `date_start`, and must not precede it.
* Dates are ISO 8601 `YYYY-MM-DD`.

Everything except `day` sets `is_imprecise: true` in read responses, which is
what the timeline uses to draw the event as a soft span rather than a point.

### Responses

| HTTP | `outcome` | Meaning |
|---|---|---|
| 201 | `queued` | A new pending item was created. |
| 200 | `corroborated` | An equivalent item was already open. Your report was attached to it as evidence and `corroboration_count` bumped. |
| 200 | `duplicate_of_published` | Already in the published dataset (or a no-op edit). Nothing to review. `event_id` tells you which event. |
| 401 | — | Bad or missing key. |
| 404 | — | Edit target doesn't exist. |
| 422 | — | Validation failed. **Nothing was stored.** The message names the offending field path. |

```json
{
  "outcome": "queued",
  "submission_id": 1,
  "status": "pending",
  "dedup_key": "1723a4e867404d73…",
  "corroboration_count": 1,
  "event_id": null,
  "warnings": ["introduces a new tag: 'Sabotage'"]
}
```

`warnings` are **non-blocking**. The main one you'll see is new-vocabulary
notices: unknown tags, sections and sources are accepted into the queue and
flagged for the reviewer, but the vocabulary term itself is only created on
approval. That way one scraper typo can't pollute the public filter lists.

**Ingest is effectively idempotent.** Re-posting an identical payload will never
create a second queue item or a duplicate event, so a scraper that re-runs over
the same window is safe.

---

## Dedup keys

The natural key is a content hash, so it's stable across runs *and* across
scrapers with no coordination between them.

```
dedup_key = sha256("v1|" + (date_start or "undated") + "|" + normalized_body_excerpt)
```

Normalization: NFKD-decompose → drop combining marks → lowercase → replace runs
of non-alphanumerics with a single space → strip → truncate to **120 chars**.
With `external_id`, the key is `sha256("v1|external|" + external_id)` instead.

Two consequences you can rely on:

* Cosmetic rewording — case, punctuation, en-dashes, accents, whitespace, or
  **anything appended past the first 120 normalized characters** — does not fork
  an event.
* Same opening text on a different date *is* a different event.

The algorithm is versioned by the `v1` prefix and is reproduced in
[scripts/example_submit.py](scripts/example_submit.py) so a scraper can check
"did I already submit this?" without a network call:

```bash
python -m scripts.example_submit --dedup-key 2026-08-12 "Ukrainian long-range drones strike…"
```

Verified against the current 216-event dataset: **zero collisions.**

### Duplicate handling: corroborate, not ignore

When a submission's key matches one that's already open, the new report is
attached as **corroborating evidence** (a `submission_evidence` row) and
`corroboration_count` is bumped, rather than being dropped.

**Why corroborate:**
- Independent agreement is exactly the signal a reviewer wants. "ISW *and* UK
  Parliament both report this" is a stronger basis for publishing than one
  scraper's word — and it's only visible if the second report is kept.
- The second report usually carries a *different citation*. Ignoring it throws
  away a source URL you'd otherwise have to re-find by hand.
- The queue stays at one row per real-world claim, so review effort scales with
  distinct claims, not with scraper runs.

**The cost, and how it's contained:**
- A queue item is no longer a single immutable request, which could muddy the
  audit story. Contained by never letting corroboration touch the proposal:
  `proposed` and `diff` are written once at ingest and never rewritten, and each
  report's verbatim payload is preserved in its own evidence row. Corroboration
  is strictly *additive metadata*. There's a test asserting this
  (`test_corroboration_preserves_the_original_proposal`).
- A looping scraper inflates the count. Contained by recording `scraper` and
  `scraper_run_id` per evidence row, so the dashboard shows **distinct scraper
  count** next to the raw total — 5 reports from one scraper reads very
  differently from 2 from two independent ones.

The alternative (ignore, return 200, discard the payload) is simpler and
stateless, but makes corroboration invisible and silently drops source URLs. For
a research artifact that has to be defensible, keeping the evidence wins.

Note that dedup applies only across **open** submissions (`pending`,
`auto_closed`), enforced by a partial unique index. Once an item is approved or
rejected the key is free again — a rejected event can legitimately come back
later with better sourcing.

---

## Data model

SQLite by default, written to be Postgres-portable. See [app/models.py](app/models.py).

```
sections ──┐ (self-referential: parent_id gives subsections)
           ├──< events >──< event_tags >── tags   (kind: tag | research_category)
sources ──────< event_sources (citations: url, title, quote, accessed_at)
                    │
pending_submissions ┴──< submission_evidence
                    │
audit_log ──────────┘
```

Design notes:

* **`events` is the published dataset.** `status` is `published` or `retracted`;
  the read API only ever serves `published`. `version` increments on every
  applied edit and drives stale-edit detection.
* **Tags and research categories share one table** with a `kind` discriminator.
  They're two separate vocabularies but structurally identical, and one join
  keeps filtering uniform. Unique on `(kind, slug)`.
* **Sections are self-referential.** A subsection is a section with a
  `parent_id`; an event carries a direct FK to each level. One table instead of
  two near-identical ones.
* **Citations carry the URL.** `event_sources.url` is `NOT NULL DEFAULT ''`
  rather than nullable, so `UNIQUE(event_id, source_id, url)` behaves the same on
  both backends (both treat NULLs as distinct, which would permit unlimited
  duplicate un-URL'd citations). The API serializes `''` back to `null`.
* **JSON is used only for audit artifacts and open-ended proposals** —
  `payload`, `proposed`, `diff`, `changes`, `detail`. Everything queryable is a
  real column or relation. On Postgres these become `JSONB` automatically.
* **Enums are VARCHAR + CHECK**, not native DB enums: identical behaviour on both
  backends, and adding a value later is a data migration rather than an
  `ALTER TYPE`.

### Postgres migration path

Nothing in the app is SQLite-specific except two pragmas in
[app/db.py](app/db.py). To move:

```bash
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/ukr_timeline
alembic upgrade head
python -m scripts.seed Russo-Ukrainian_War_Timeline_Dates.json
```

The partial unique index, all CHECK constraints, and the JSONB variants are
already in the migration. Foreign keys are enforced on SQLite too (via
`PRAGMA foreign_keys=ON`) so behaviour doesn't change under you on the way over.

---

## Audit trail

Append-only. Nothing in the app updates or deletes an `audit_log` row.

| Action | Written when |
|---|---|
| `seed.import` | The one-time seed import |
| `submission.received` | A scraper queues something |
| `submission.corroborated` | A duplicate report is attached as evidence |
| `submission.auto_closed` | Ingest finds it already published, or a no-op edit |
| `submission.approved` / `submission.rejected` | You decide |
| `event.created` / `event.updated` / `event.retracted` | The published dataset changes |

Each entry carries `actor`, `occurred_at`, the originating `submission_id`, the
resulting `event_version`, and a `{field: {before, after}}` change map.
`event.created` logs a **full field snapshot** in the same shape as an update, so
replays are uniform.

**The reconstructability guarantee is testable, not just claimed.**
`GET /api/v1/events/{id}/history?verify=true` replays the log from scratch and
reports whether it reproduces the live row:

```json
"verification": {
  "entries_applied": 4,
  "reconstructs_current_state": true,
  "mismatched_fields": []
}
```

If that ever comes back `false`, something wrote to `events` outside the review
path and the provenance is suspect. The detail drawer on the public timeline
surfaces this check per event, and
`test_audit_log_reconstructs_current_state` asserts it across a 3-edit chain.

Seed events are on the same footing as scraper-contributed ones: all 216 got an
`event.created` entry with actor `system:seed`, so `/history` works for event 1
exactly as it does for event 900.

---

## API

Read endpoints are open by default (set `REQUIRE_KEY_FOR_READS=true` if the
dataset is embargoed).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/v1/health` | — | Liveness + counts |
| GET | `/api/v1/events` | — | List published events (filters below) |
| GET | `/api/v1/events/{id}` | — | One published event |
| GET | `/api/v1/events/{id}/history` | — | Full provenance for one event |
| GET | `/api/v1/changelog` | — | Dataset-wide audit feed |
| GET | `/api/v1/meta` | — | Vocabularies, counts, date bounds (drives the filter UI) |
| POST | `/api/v1/ingest` | ingest | Submit a candidate |
| GET | `/api/v1/ingest/contract` | — | Contract + dedup algorithm |
| GET | `/api/v1/review/submissions` | review | The queue |
| GET | `/api/v1/review/submissions/{id}` | review | One submission + raw payload |
| POST | `/api/v1/review/submissions/{id}/approve` | review | Approve / edit-then-approve |
| POST | `/api/v1/review/submissions/{id}/reject` | review | Reject with a reason |
| GET | `/api/v1/review/stats` | review | Queue health, per-scraper volume |
| GET | `/api/v1/review/audit` | review | Audit log, filterable by submission |

`GET /api/v1/events` filters: `tag` (repeatable), `research_category`
(repeatable), `tag_mode=any|all`, `section`, `subsection`, `source`, `date_from`,
`date_to`, `date_precision` (repeatable), `include_undated`, `q`, `order`,
`limit`, `offset`. All names accept either the display name or the slug.

**Date filtering is overlap-based:** an event whose span touches the window
matches, so a month- or year-precision event surfaces for any query that
intersects it. Undated events are kept by default rather than silently dropped
from a date-filtered view; pass `include_undated=false` to exclude them.

**`order=narrative` is the default.** The dataset's sections are the source
document's chapters and they are *not* date-contiguous — a "2021–2025 five-year
total" event belongs to the 2025 chapter but starts in 2021. Sorting purely by
date interleaves chapters and makes the same heading appear repeatedly. Narrative
order sorts by section position, then date within the section. `order=asc|desc`
gives strict chronological order when you want one continuous sequence.

### Auth

Two keys, because the audiences differ:

* `INGEST_API_KEY` — scrapers. Can only add to the queue. **A leaked scraper key
  cannot publish anything.** (Asserted by
  `test_ingest_key_cannot_reach_the_review_api`.)
* `REVIEW_API_KEY` — you. Can change the published dataset.

Send either as `X-API-Key: <key>` or `Authorization: Bearer <key>`. Compared with
`hmac.compare_digest`. `X-Actor` on review requests is recorded as the deciding
actor in the audit log. The review dashboard keeps the key in **sessionStorage**
only, so it clears when the tab closes.

### Why REST rather than GraphQL

The read surface is small and fixed — a filtered list, one event, some metadata —
and there's no client-shaped over-fetching problem to solve: the timeline wants
whole events. Meanwhile the write surface is where all the complexity lives, and
it isn't CRUD at all. `approve`, `reject`, and `edit-then-approve` are *state
transitions with side effects and conflict semantics*, which map naturally onto
distinct POST endpoints and awkwardly onto GraphQL mutations. REST also gives you
the thing that matters most here for free: HTTP status codes as the contract
(`201` queued vs `200` corroborated vs `409` conflict), which a cron-driven
scraper can branch on without parsing a response body. GraphQL would return
`200 OK` with an errors array for all of it.

The one GraphQL advantage that would apply — a self-describing schema for you to
build scrapers against — is already covered by OpenAPI at `/openapi.json`.

---

## Reviewing

The dashboard at `/review` shows, per submission: which scraper produced it, how
many distinct scrapers corroborate it, the scraper's own confidence, any
vocabulary warnings, and either a **full preview** (new events) or a **field-level
diff** (edits).

* **Approve** — publishes as proposed.
* **Edit, then approve** — opens the fields for correction. Only fields you
  actually change are sent as `overrides`; the scraper's original proposal is
  preserved unmodified and the audit entry records both, so the log stays honest
  about what was reviewer-authored.
* **Reject** — archives with an optional reason. **Nothing is deleted**; the row,
  its evidence, and its proposal are retained and stay queryable at
  `?status=rejected`.

**Stale-edit protection.** Each pending edit records the target's `version` at
submission time. If the event moved on, the diff shows a per-field conflict
banner with the current value, and approving returns `409` instead of silently
overwriting newer data. Re-send with `force: true` once you've read it; the
audit entry records `forced_over_conflict: true`.

Approving is also blocked with a `409` if it would make two events share a
dedup key — you're told which event it would collide with, so you can merge
instead of duplicating.

### Diff readability

Added/removed use green/red, but that pair measures ΔE 4.1 under deuteranopia —
effectively identical for a large minority of readers. So every diff row also
carries a `− WAS:` / `+ NOW:` label and strikes through the removed value. Colour
is only ever reinforcement, never the sole carrier of meaning. Same rule applies
to the conflict banner (⚠ + the word "Conflict") and the date-precision encoding
on the timeline, which uses **shape and an explicit precision word**, not hue.

---

## A note on the seed data

One data-quality issue surfaced during import, worth a scraper edit rather than a
code change: event 5's `date_text` reads **"2014–2021"** but its stored span is
`2014-01-01 → 2014-12-31`, so the original extraction only captured the first
year. It shows up as a gap in the events-per-year chart for 2015–2020. Ten other
events do have correct multi-year spans, so the range handling itself is fine.
This is exactly the case an `edit` submission is for.

## Layout

```
app/
  main.py          FastAPI app, static file serving
  config.py        env-driven settings
  db.py            engine/session; the only SQLite-specific code
  models.py        ORM models
  schemas.py       request/response models — the scraper contract
  enums.py         controlled vocabularies shared by models and schemas
  dedup.py         the natural-key algorithm
  security.py      two-key shared-secret auth
  routers/         public.py · ingest.py · review.py
  services/        events.py · submissions.py · audit.py · vocab.py
migrations/        Alembic
scripts/
  seed.py          one-time JSON import (idempotent, --dry-run, --force)
  example_submit.py  reference scraper client, stdlib only
static/            timeline + review dashboard (no build step)
tests/             95 tests
```
