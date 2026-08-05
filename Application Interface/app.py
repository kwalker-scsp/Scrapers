import hmac
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

# Explicitly resolve parent directory (Scrapers/) so database.py can be found
BASE_DIR = Path(__file__).resolve().parent
sys.path.append(str(BASE_DIR.parent))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field

import database
import embedding

app = FastAPI(title="Russo-Ukrainian War Timeline Review Queue")

# Explicitly resolve templates directory relative to app.py
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# queue.html links /static/style.css; without this mount it 404s and the whole
# dashboard renders unstyled.

def safe_url(value):
    """Blocks non-http(s) hrefs.

    source_url comes from scraped pages, so a `javascript:` value would
    otherwise be rendered straight into an anchor.
    """
    first = (value or "").split(" | ")[0].strip()
    return first if first.lower().startswith(("http://", "https://")) else "#"


templates.env.filters["safe_url"] = safe_url

STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Only these values may reach a query or an interpolated table name.
QUEUE_TYPES = {
    "daily": {
        "table": '"DailyPendingEvents"',
        "date_col": "event_date",
        "summary_col": "summary",
    },
    "monthly": {
        "table": '"MonthlyMacroMilestones"',
        "date_col": "event_month",
        "summary_col": "strategic_summary",
    },
}
VALID_STATUSES = {"pending", "approved", "declined", "merged"}


def resolve_queue(queue_type):
    """Maps a queue_type to its table config, rejecting anything unrecognized.

    A two-way if/else here meant any unexpected value silently operated on the
    monthly table.
    """
    config = QUEUE_TYPES.get(queue_type)
    if config is None:
        raise ValueError(f"Unknown queue_type: {queue_type!r}")
    return config


def redirect_back(queue_type, status="pending", error=None):
    """All review actions return to the admin queue they came from."""
    url = f"/admin?queue_type={queue_type}&status={status}"
    if error:
        url += f"&error={quote(error)}"
    return RedirectResponse(url=url, status_code=303)


# Subtitles sit under the permanent title and change per section. The title
# itself is fixed in the layout and never varies.
SUBTITLES = {
    "home": "Macro view of events in Russo-Ukrainian War",
    "daily": "Granular tactical battlefield developments",
    "admin": "Human-in-the-Loop Review & System Verification",
}


@app.get("/admin", response_class=HTMLResponse)
def queue_dashboard(
    request: Request,
    queue_type: str = "daily",
    status: str = "pending",
    error: str = None,
):
    """Renders the main review queue with tab switching (Daily, Monthly, Duplicate)

    and sub-tabs (Pending, Approved, Declined).
    """
    db_error = False
    records = []
    duplicate_records = []

    if status.lower() not in VALID_STATUSES:
        status = "pending"

    conn = None
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        if queue_type == "daily":
            cursor.execute(
                """
                SELECT id, event_date, title, summary, source_name, source_url, notes, review_status
                FROM "DailyPendingEvents"
                WHERE review_status = %s
                ORDER BY created_at DESC
            """,
                (status.upper(),),
            )
            records = cursor.fetchall()
        elif queue_type == "monthly":
            cursor.execute(
                """
                SELECT id, event_month, title, strategic_summary AS summary, category, source_name, source_url, notes, review_status
                FROM "MonthlyMacroMilestones"
                WHERE review_status = %s
                ORDER BY created_at DESC
            """,
                (status.upper(),),
            )
            records = cursor.fetchall()
        elif queue_type == "duplicate":
            cursor.execute("""
                SELECT id, incoming_title, incoming_summary, incoming_date, existing_title, existing_summary, similarity_score, source_url, review_status
                FROM "DuplicateReviewQueue"
                WHERE review_status = 'PENDING_DUPLICATE'
                ORDER BY created_at DESC
            """)
            duplicate_records = cursor.fetchall()

        cursor.close()
    except Exception as e:
        print(f"[Database Warning] Could not query database: {e}")
        db_error = True
    finally:
        if conn:
            conn.close()

    return templates.TemplateResponse(
        request=request,
        name="queue.html",
        context={
            "queue_type": queue_type,
            "status": status,
            "records": records,
            "duplicate_records": duplicate_records,
            "db_error": db_error,
            "error": error,
            "section": "admin",
            "subtitle": SUBTITLES["admin"],
        },
    )


@app.post("/merge-preview", response_class=HTMLResponse)
async def merge_preview(request: Request):
    """Renders the summary selection screen when 2+ events are marked as duplicates."""
    form_data = await request.form()
    queue_type = form_data.get("queue_type", "daily")
    selected_ids = form_data.getlist("selected_ids")

    try:
        config = resolve_queue(queue_type)
    except ValueError as e:
        return redirect_back("daily", error=str(e))

    if len(selected_ids) < 2:
        return redirect_back(
            queue_type, error="Select at least two records to merge."
        )

    records = []
    conn = None
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        format_strings = ",".join(["%s"] * len(selected_ids))
        # Ordered so the template's "first record" defaults are deterministic.
        cursor.execute(
            f"SELECT * FROM {config['table']} "
            f"WHERE id IN ({format_strings}) ORDER BY id ASC",
            tuple(selected_ids),
        )
        records = cursor.fetchall()
        cursor.close()
    except Exception as e:
        print(f"[Database Error] Merge preview fetch failed: {e}")
        return redirect_back(queue_type, error=f"Merge preview failed: {e}")
    finally:
        if conn:
            conn.close()

    if not records:
        return redirect_back(
            queue_type, error="None of the selected records were found."
        )

    return templates.TemplateResponse(
        request=request,
        name="merge_resolve.html",
        context={"queue_type": queue_type, "records": records},
    )


@app.post("/confirm-merge")
async def confirm_merge(request: Request):
    """Executes summary choice, combines URLs, updates primary event, and archives merged records."""
    form_data = await request.form()
    queue_type = form_data.get("queue_type", "daily")
    primary_id = form_data.get("primary_id")
    selected_ids = form_data.getlist("selected_ids")

    try:
        config = resolve_queue(queue_type)
    except ValueError as e:
        return redirect_back("daily", error=str(e))

    # Validate before touching the database: an unvalidated primary_id meant
    # the UPDATE matched nothing while the other rows were still archived,
    # orphaning the whole group.
    if len(selected_ids) < 2:
        return redirect_back(queue_type, error="Nothing to merge.")
    if not primary_id or str(primary_id) not in [str(i) for i in selected_ids]:
        return redirect_back(
            queue_type, error="Choose which record to keep as primary."
        )

    # `summary_choice` carries either the sentinel "custom" or the full text of
    # a chosen record, so a record whose summary is literally "custom" would be
    # misread. The index-based value avoids that collision.
    summary_choice = form_data.get("summary_choice")
    if summary_choice == "custom":
        final_summary = (form_data.get("custom_summary") or "").strip()
    elif summary_choice is not None and summary_choice.startswith("record:"):
        final_summary = (form_data.get(f"summary_text_{summary_choice[7:]}") or "").strip()
    else:
        final_summary = (summary_choice or "").strip()

    final_date = (form_data.get("final_date") or "").strip()

    if not final_summary:
        return redirect_back(queue_type, error="Merged summary cannot be empty.")
    if not final_date:
        return redirect_back(queue_type, error="Merged date cannot be empty.")

    conn = None
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        table = config["table"]
        date_col = config["date_col"]
        summary_col = config["summary_col"]

        # Combine Source URLs from all merged events
        format_strings = ",".join(["%s"] * len(selected_ids))
        cursor.execute(
            f"SELECT source_url FROM {table} WHERE id IN ({format_strings})",
            tuple(selected_ids),
        )
        urls = []
        for row in cursor.fetchall():
            for url in (row["source_url"] or "").split(" | "):
                url = url.strip()
                if url and url not in urls:
                    urls.append(url)
        combined_urls = " | ".join(urls)[:1000]

        # 1. Update Primary Record
        cursor.execute(
            f"UPDATE {table} SET {summary_col} = %s, {date_col} = %s, source_url = %s, review_status = 'APPROVED' WHERE id = %s",
            (final_summary, final_date, combined_urls, primary_id),
        )
        if cursor.rowcount != 1:
            raise LookupError(f"Primary record {primary_id} not found")

        # 2. Mark Other Selected Events as MERGED
        other_ids = [eid for eid in selected_ids if str(eid) != str(primary_id)]
        if other_ids:
            other_format = ",".join(["%s"] * len(other_ids))
            cursor.execute(
                f"UPDATE {table} SET review_status = 'MERGED' WHERE id IN ({other_format})",
                tuple(other_ids),
            )

        conn.commit()
        cursor.close()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[Database Error] Confirm merge failed: {e}")
        return redirect_back(queue_type, error=f"Merge failed: {e}")
    finally:
        if conn:
            conn.close()

    return redirect_back(queue_type)


@app.post("/batch-save")
async def batch_save(request: Request):
    """Processes inline date/summary edits and batch radio approvals/declines across multiple rows."""
    form_data = await request.form()
    queue_type = form_data.get("queue_type", "daily")
    status = form_data.get("status", "pending")

    try:
        config = resolve_queue(queue_type)
    except ValueError as e:
        return redirect_back("daily", error=str(e))

    conn = None
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor()

        table = config["table"]
        date_col = config["date_col"]
        summary_col = config["summary_col"]

        for eid in form_data.getlist("event_id"):
            action = form_data.get(f"action_{eid}")
            date_val = form_data.get(f"date_{eid}")
            summary_val = form_data.get(f"summary_{eid}")

            new_status = None
            if action == "approve":
                new_status = "APPROVED"
            elif action == "decline":
                new_status = "DECLINED"

            # A missing input means that field was not rendered, not that the
            # reviewer cleared it -- writing None would blank the column.
            sets, params = [], []
            if date_val is not None:
                sets.append(f"{date_col} = %s")
                params.append(date_val.strip())
            if summary_val is not None:
                sets.append(f"{summary_col} = %s")
                params.append(summary_val)
            if new_status:
                sets.append("review_status = %s")
                params.append(new_status)

            if not sets:
                continue

            params.append(eid)
            cursor.execute(
                f"UPDATE {table} SET {', '.join(sets)} WHERE id = %s",
                tuple(params),
            )

        conn.commit()
        cursor.close()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[Database Error] Batch save failed: {e}")
        return redirect_back(queue_type, status, error=f"Save failed: {e}")
    finally:
        if conn:
            conn.close()

    return redirect_back(queue_type, status)


@app.post("/duplicate-resolve")
async def duplicate_resolve(request: Request):
    """Resolves rows in the Duplicate Review Queue.

    'merge' folds the incoming event's source URL into the matched record and
    discards the duplicate; 'keep' promotes the incoming event into the daily
    queue as its own PENDING record. Either way the queue row is closed out.
    """
    form_data = await request.form()
    conn = None
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        for dup_id in form_data.getlist("duplicate_id"):
            action = form_data.get(f"dup_action_{dup_id}")
            if action not in ("merge", "keep"):
                continue

            cursor.execute(
                'SELECT * FROM "DuplicateReviewQueue" WHERE id = %s', (dup_id,)
            )
            dup = cursor.fetchone()
            if not dup:
                continue

            if action == "keep":
                cursor.execute(
                    """
                    INSERT INTO "DailyPendingEvents"
                    (event_date, title, summary, source_name, source_url, notes, review_status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                    """,
                    (
                        dup["incoming_date"],
                        dup["incoming_title"],
                        dup["incoming_summary"],
                        "Duplicate Review",
                        dup["source_url"],
                        "Kept from duplicate review",
                    ),
                )
                resolution = "KEPT"
            else:
                # Attach the corroborating URL to the record it matched.
                cursor.execute(
                    """
                    UPDATE "DailyPendingEvents"
                    SET source_url = CASE
                        WHEN source_url IS NULL OR source_url = '' THEN %s
                        WHEN position(%s in source_url) > 0 THEN source_url
                        ELSE left(source_url || ' | ' || %s, 1000)
                    END
                    WHERE title = %s
                    """,
                    (
                        dup["source_url"],
                        dup["source_url"],
                        dup["source_url"],
                        dup["existing_title"],
                    ),
                )
                resolution = "MERGED"

            cursor.execute(
                'UPDATE "DuplicateReviewQueue" SET review_status = %s WHERE id = %s',
                (resolution, dup_id),
            )

        conn.commit()
        cursor.close()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[Database Error] Duplicate resolve failed: {e}")
        return redirect_back("duplicate", error=f"Resolve failed: {e}")
    finally:
        if conn:
            conn.close()

    return redirect_back("duplicate")


# Hybrid semantic search
#
# Stage 1 is a local vector pre-filter: embed the query with the same MiniLM
# model used at ingest, score every stored vector by cosine similarity, and keep
# the top 20. Costs no tokens.
#
# Stage 2 hands only those 20 to Gemini Flash Lite for semantic re-ranking, so
# the LLM sees a short candidate list instead of the whole corpus.

SEARCH_CANDIDATES = 20
SEARCH_RESULTS = 10

# A separate key from the pipeline's, so search traffic and scrape traffic do
# not share a rate limit.
SEARCH_API_KEY = os.getenv("GEMINI_API_KEY_2")
# The "-latest" alias tracks whichever Flash Lite is current, so this cannot go
# dead the way a pinned version does -- gemini-2.5-flash-lite already 404s for
# new API keys. Trade-off: the underlying model can change without notice, so
# if ranking behaviour ever shifts unexpectedly, pin a version here.
SEARCH_MODEL = os.getenv("SEARCH_MODEL", "gemini-flash-lite-latest")
SEARCH_TIMEOUT_MS = int(os.getenv("SEARCH_TIMEOUT_MS", "20000"))

_search_client = None


def get_search_client():
    """Lazily builds the Gemini client so a missing key degrades rather than
    breaking app startup."""
    global _search_client
    if _search_client is None and SEARCH_API_KEY:
        from google import genai

        _search_client = genai.Client(api_key=SEARCH_API_KEY)
    return _search_client


class RankedHit(BaseModel):
    event_id: int = Field(description="The id of the candidate event.")
    relevance: int = Field(
        description="Relevance to the query, 0-100."
    )
    explanation: str = Field(
        description="One sentence on why this matches the user's intent."
    )


class RankedResults(BaseModel):
    results: list[RankedHit] = Field(
        description="Best matches, most relevant first."
    )


# Embeddings generalize well but blur exact names -- a query for "Moskva" or
# "Bakhmut" matches anything naval or anything about a siege. A small lexical
# term-match score, blended in, restores that precision at no token cost.
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "were", "are",
    "has", "have", "had", "his", "her", "its", "into", "onto", "over", "under",
    "about", "after", "before", "during", "between", "against", "what", "when",
    "where", "which", "who", "whom", "how", "why", "all", "any", "some", "more",
    "most", "other", "such", "than", "then", "there", "these", "those", "both",
}
LEXICAL_WEIGHT = 0.25
TITLE_TERM_WEIGHT = 2
SUMMARY_TERM_WEIGHT = 1

WORD_RE = re.compile(r"[a-z0-9]+")


def query_terms(query):
    """Content words worth matching literally."""
    return {
        term
        for term in WORD_RE.findall((query or "").lower())
        if len(term) > 2 and term not in STOPWORDS
    }


def lexical_score(terms, row):
    """0-1 share of query terms appearing in the record, title weighted double."""
    if not terms:
        return 0.0
    title = (row.get("title") or "").lower()
    summary = (row.get("summary") or "").lower()

    hits = 0
    for term in terms:
        pattern = re.compile(rf"\b{re.escape(term)}")
        if pattern.search(title):
            hits += TITLE_TERM_WEIGHT
        elif pattern.search(summary):
            hits += SUMMARY_TERM_WEIGHT
    return min(1.0, hits / (len(terms) * TITLE_TERM_WEIGHT))


def normalize_scores(rows):
    """Min-max the blended scores onto 0-100 for display.

    Raw cosine tops out around 0.5-0.65 on this corpus, so showing it directly
    made the single best match read as "49%". These are relative ranks within
    one result set, not absolute confidence -- the UI says so explicitly.
    """
    if not rows:
        return
    values = [r["blended_score"] for r in rows]
    low, high = min(values), max(values)
    span = high - low
    for row in rows:
        # A lone result (or an exact tie) has no spread to normalize against.
        row["relevance"] = (
            100 if span <= 0 else int(round((row["blended_score"] - low) / span * 100))
        )


def vector_prefilter(query, limit=SEARCH_CANDIDATES):
    """Stage 1: rank the stored corpus locally. No API calls, no tokens."""
    query_vec = embedding.embed_record(query, "")
    terms = query_terms(query)

    scored = []
    for row in database.fetch_search_corpus():
        vec = embedding.deserialize(row.get("embedding"))
        if vec is None:
            # Row predates the embedding column, or its value is corrupt. It is
            # skipped rather than silently scoring 0 -- run the backfill.
            continue
        cosine = embedding.cosine_similarity(query_vec, vec)
        lexical = lexical_score(terms, row)
        blended = (1 - LEXICAL_WEIGHT) * cosine + LEXICAL_WEIGHT * lexical
        scored.append((blended, cosine, lexical, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    hits = []
    for blended, cosine, lexical, row in scored[:limit]:
        hit = dict(row)
        hit.pop("embedding", None)
        # Components kept alongside the blend so ranking stays debuggable.
        hit["blended_score"] = round(blended, 4)
        hit["vector_score"] = round(cosine, 4)
        hit["lexical_score"] = round(lexical, 4)
        hits.append(hit)
    return hits


def rerank_with_gemini(query, candidates):
    """Stage 2: semantic re-rank. Returns None when unavailable."""
    client = get_search_client()
    if not client or not candidates:
        return None

    listing = "\n".join(
        f"[{c['id']}] ({c['event_date'] or 'undated'}) {c['title']} :: "
        f"{(c['summary'] or '')[:400]}"
        for c in candidates
    )
    prompt = (
        "You are ranking Russo-Ukrainian War timeline events against an "
        "analyst's search.\n\n"
        f"Query: {query}\n\n"
        f"Candidate events:\n{listing}\n\n"
        f"Select at most {SEARCH_RESULTS} events that genuinely answer the "
        "query, most relevant first. Use only the ids listed above. Score "
        "relevance 0-100 on semantic overlap with the query's intent, not "
        "keyword overlap. Give one sentence per result on why it matches. "
        "Return fewer results, or none, rather than padding with weak matches."
    )

    try:
        response = client.models.generate_content(
            model=SEARCH_MODEL,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": RankedResults,
                # Without a cap, a throttled or stalled upstream leaves the
                # request hanging indefinitely. Stage 1 results are already in
                # hand, so it is better to return those than to wait.
                "http_options": {"timeout": SEARCH_TIMEOUT_MS},
            },
        )
        return RankedResults.model_validate_json(response.text).results
    except Exception as e:
        print(f"[Search] Gemini re-rank failed, using vector order: {e}")
        return None


def format_long_date(value):
    """Renders 'Month DD, YYYY'; passes odd values through untouched."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").strftime(
            "%B %d, %Y"
        )
    except ValueError:
        return value or "Undated"


def decorate(rows):
    """Display fields every result needs, whichever path produced it."""
    for row in rows:
        row["display_date"] = format_long_date(row.get("event_date"))
        notes = (row.get("notes") or "").lower()
        row["uncertain"] = any(m in notes for m in UNCERTAIN_MARKERS)
    return rows


def run_search(query, use_ai=False):
    """Ranks events for a query.

    Local ranking is the default and costs nothing: no API call, no tokens, so
    repeated searches and page refreshes are free. `use_ai` opts into a single
    Gemini re-rank of the same candidates, and is gated in the UI so it is
    never spent by accident.
    """
    candidates = vector_prefilter(query)
    if not candidates:
        return {
            "query": query,
            "results": [],
            "mode": "local",
            "candidate_count": 0,
        }

    if not use_ai:
        results = [dict(row) for row in candidates[:SEARCH_RESULTS]]
        normalize_scores(results)
        for row in results:
            row["explanation"] = ""
        return {
            "query": query,
            "results": decorate(results),
            "mode": "local",
            "candidate_count": len(candidates),
        }

    by_id = {c["id"]: c for c in candidates}
    ranked = rerank_with_gemini(query, candidates)

    if not ranked:
        # Gemini unavailable or throttled: fall back to the local ordering
        # rather than failing the search outright.
        results = [dict(row) for row in candidates[:SEARCH_RESULTS]]
        normalize_scores(results)
        for row in results:
            row["explanation"] = ""
        return {
            "query": query,
            "results": decorate(results),
            "mode": "local",
            "ai_failed": True,
            "candidate_count": len(candidates),
        }

    results = []
    for hit in ranked[:SEARCH_RESULTS]:
        row = by_id.get(hit.event_id)
        if not row:
            continue  # model referenced an id outside the candidate set
        # Gemini's score is a real judgment of relevance, so unlike the local
        # path it is shown as-is rather than normalized.
        results.append(
            {
                **row,
                "relevance": max(0, min(100, hit.relevance)),
                "explanation": hit.explanation,
            }
        )

    return {
        "query": query,
        "results": decorate(results),
        "mode": "ai",
        "candidate_count": len(candidates),
    }


# AI spend gate
#
# This gate exists to stop accidental quota burn, NOT to protect anything. It
# guards a button that spends an API call; there is no sensitive data behind it,
# the secret is a short shared word, and it travels over plain HTTP locally.
# Treat it as a spend control and nothing more.
#
# The password is required on EVERY re-rank -- deliberately not remembered in a
# session or cookie. With roughly 20 calls a day available, having to type it
# again is the point: it forces a moment's thought before each spend.

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


def password_ok(supplied):
    """Constant-time comparison; an unset ADMIN_PASSWORD denies rather than allows."""
    if not ADMIN_PASSWORD:
        return False
    return hmac.compare_digest((supplied or "").strip(), ADMIN_PASSWORD)


# Timeline
#
# event_date is stored as VARCHAR, so parsing and ordering happen here rather
# than in SQL. Events whose notes flag an uncertain month/year are marked so the
# view can draw them as approximate instead of plotting them as precise.

UNCERTAIN_MARKERS = ("uncertain month", "uncertain year", "unparseable date")


# Only strictly-formed calendar dates are plotted. Placeholder values that the
# extractor has emitted in the past ("2025-08-XX", "") would otherwise slice to
# a valid-looking "2025-08" month key and inflate that month's bar with events
# whose day is unknown.
RE_STRICT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_STRICT_MONTH = re.compile(r"^\d{4}-\d{2}$")


def is_plottable_date(value):
    """True only for a real YYYY-MM-DD calendar date."""
    value = (value or "").strip()
    if not RE_STRICT_DATE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _month_key(date_str):
    return (date_str or "").strip()[:7]


DAILY_WINDOW_DAYS = 30


def source_counts(events):
    """How many events each outlet contributed, for the caption under the chart.

    Sources are reported as text, not colour. style.css documents the reason:
    the brand palette carries two hues (brand red for data, accent navy for
    controls), so a per-source colour scale would have to invent hues that are
    not in the system. Every event row already names its source in the left
    column, so nothing is lost.
    """
    counts = Counter(
        (e.get("source_name") or "Unattributed").split(",")[0].strip()
        for e in events
    )
    return [
        {"label": name, "count": n} for name, n in counts.most_common() if name
    ]


def build_monthly_timeline(data):
    """Groups reviewed records into an ordered month-by-month structure.

    The month bars measure daily-event volume (campaign tempo) while the detail
    sections list macro milestones only -- there are far too few milestones to
    make a readable bar chart on their own.
    """
    months = {}

    skipped_dates = 0
    for row in data["daily"]:
        # Undated and placeholder-dated events are excluded from the bars --
        # they cannot be placed on a time axis honestly.
        if not is_plottable_date(row["event_date"]):
            skipped_dates += 1
            continue
        key = _month_key(row["event_date"])
        bucket = months.setdefault(
            key, {"month": key, "events": [], "milestones": []}
        )
        notes = (row["notes"] or "").lower()
        bucket["events"].append(
            {
                **row,
                "uncertain": any(m in notes for m in UNCERTAIN_MARKERS),
                "duplicate": "possible duplicate" in notes,
            }
        )

    for row in data["monthly"]:
        key = _month_key(row["event_month"])
        if not RE_STRICT_MONTH.match(key):
            skipped_dates += 1
            continue
        bucket = months.setdefault(
            key, {"month": key, "events": [], "milestones": []}
        )
        bucket["milestones"].append(row)

    ordered = []
    for key in sorted(months):
        bucket = months[key]
        try:
            bucket["label"] = datetime.strptime(key, "%Y-%m").strftime("%b %Y")
        except ValueError:
            bucket["label"] = key
        # The count drives the bar; the events themselves are not rendered on
        # this tab -- the daily tab owns them.
        bucket["event_count"] = len(bucket.pop("events"))
        bucket["milestone_count"] = len(bucket["milestones"])
        ordered.append(bucket)

    return {
        "sources": [],
        "months": ordered,
        "max_count": max((m["event_count"] for m in ordered), default=0),
        "total_events": sum(m["event_count"] for m in ordered),
        "total_milestones": sum(m["milestone_count"] for m in ordered),
        # Surfaced rather than silently dropped, so an operator can see that
        # records exist which the chart cannot place.
        "excluded_undated": skipped_dates,
    }


def build_daily_window(data, end_date):
    """Builds a fixed 30-day window of tactical events, oldest day first.

    Every day in the window is emitted, including empty ones, so the bar strip
    has a continuous axis and quiet stretches read as quiet rather than being
    collapsed away.
    """
    start_date = end_date - timedelta(days=DAILY_WINDOW_DAYS - 1)

    by_day = {}
    excluded = 0
    for row in data["daily"]:
        if not is_plottable_date(row["event_date"]):
            excluded += 1
            continue
        notes = (row["notes"] or "").lower()
        by_day.setdefault(row["event_date"].strip(), []).append(
            {
                **row,
                "uncertain": any(m in notes for m in UNCERTAIN_MARKERS),
                "duplicate": "possible duplicate" in notes,
            }
        )

    days = []
    for offset in range(DAILY_WINDOW_DAYS):
        day = start_date + timedelta(days=offset)
        key = day.strftime("%Y-%m-%d")
        events = by_day.get(key, [])
        days.append(
            {
                "date": key,
                "label": day.strftime("%b %-d"),
                "weekday": day.strftime("%a"),
                "long_label": day.strftime("%A, %B %-d, %Y"),
                "events": events,
                "count": len(events),
            }
        )

    sources = source_counts([e for d in days for e in d["events"]])

    return {
        "sources": sources,
        "days": days,
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "start_label": start_date.strftime("%b %-d, %Y"),
        "end_label": end_date.strftime("%b %-d, %Y"),
        "prev_end": (end_date - timedelta(days=DAILY_WINDOW_DAYS)).strftime("%Y-%m-%d"),
        "next_end": (end_date + timedelta(days=DAILY_WINDOW_DAYS)).strftime("%Y-%m-%d"),
        "max_count": max((d["count"] for d in days), default=0),
        "total_events": sum(d["count"] for d in days),
        "active_days": sum(1 for d in days if d["count"]),
        "excluded_undated": excluded,
    }


def parse_end_date(value):
    """Reads the window's end date, falling back to today on anything odd.

    A bad date in the query string should not 500 the page -- the user can type
    directly into the URL, and the date input itself can be cleared.
    """
    if value:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today()


def timeline_payload(view, end):
    """Builds whichever view was asked for."""
    if view == "monthly":
        return build_monthly_timeline(database.fetch_timeline_data())

    end_date = parse_end_date(end)
    start_date = end_date - timedelta(days=DAILY_WINDOW_DAYS - 1)
    data = database.fetch_timeline_data(
        start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
    )
    return build_daily_window(data, end_date)


EMPTY_DAILY = {"sources": [], "days": [], "max_count": 0, "total_events": 0}
EMPTY_MONTHLY = {
    "sources": [],
    "months": [],
    "max_count": 0,
    "total_events": 0,
    "total_milestones": 0,
}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Public landing page: the macro view, built from monthly milestones."""
    db_error = False
    try:
        timeline = timeline_payload("monthly", None)
    except Exception as e:
        print(f"[Database Warning] Home timeline query failed: {e}")
        db_error = True
        timeline = dict(EMPTY_MONTHLY)

    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "timeline": timeline,
            "db_error": db_error,
            "section": "home",
            "subtitle": SUBTITLES["home"],
        },
    )


def render_daily(request, end=None, query=None, use_ai=False, unlock_error=None):
    """Renders the daily page, optionally with search results.

    Shared by the GET view and the AI-refine POST so both produce an identical
    page and the AI path is purely a different ranking of the same search.
    """
    db_error = False
    try:
        timeline = timeline_payload("daily", end)
    except Exception as e:
        print(f"[Database Warning] Daily timeline query failed: {e}")
        db_error = True
        timeline = dict(EMPTY_DAILY)

    search = None
    if query and query.strip():
        try:
            search = run_search(query.strip(), use_ai=use_ai)
        except Exception as e:
            print(f"[Search] failed: {e}")
            search = {"query": query, "results": [], "error": str(e)}

    return templates.TemplateResponse(
        request=request,
        name="daily.html",
        context={
            "timeline": timeline,
            "search": search,
            "db_error": db_error,
            "today": date.today().strftime("%Y-%m-%d"),
            "section": "daily",
            "subtitle": SUBTITLES["daily"],
            "unlock_error": unlock_error,
            "end": end or "",
        },
    )


@app.get("/daily", response_class=HTMLResponse)
def daily_timeline(request: Request, end: str = None, q: str = None):
    """Public tactical timeline: rolling 30-day window plus search.

    Always uses the free local ranking -- a GET must never spend an API call,
    or refreshes and shared links would quietly burn quota.
    """
    return render_daily(request, end=end, query=q, use_ai=False)


@app.post("/search/refine", response_class=HTMLResponse)
async def search_refine(request: Request):
    """Re-ranks an existing search with Gemini. Costs one API call.

    Deliberately a POST that renders directly rather than redirecting to an
    `?ai=1` URL: a GET parameter would make refresh and bookmarks re-spend the
    call silently, whereas resubmitting a POST prompts the browser first.
    """
    form = await request.form()
    query = (form.get("q") or "").strip()
    end = (form.get("end") or "").strip() or None

    if not query:
        return RedirectResponse(url="/daily", status_code=303)

    if not password_ok(form.get("password")):
        # Show the free results anyway -- a failed password should not destroy
        # the search the user already ran.
        return render_daily(
            request,
            end=end,
            query=query,
            use_ai=False,
            unlock_error="Incorrect password — no API call was made.",
        )

    # No cookie, no session: the next re-rank asks for the password again.
    return render_daily(request, end=end, query=query, use_ai=True)


@app.get("/timeline")
async def timeline_redirect(view: str = "daily", end: str = None):
    """The timeline used to live here; keep old links working."""
    if view == "monthly":
        return RedirectResponse(url="/", status_code=307)
    target = "/daily" + (f"?end={quote(end)}" if end else "")
    return RedirectResponse(url=target, status_code=307)


# Declared sync (def, not async def) on purpose: search does blocking work --
# ONNX inference, psycopg2 queries, an HTTPS call to Gemini. FastAPI runs sync
# handlers in a threadpool, whereas blocking inside an async handler would stall
# the event loop and freeze every other request for the duration.
@app.get("/search")
def search_endpoint(q: str = ""):
    """JSON search API. Always local ranking -- this endpoint never spends a call.

    There is no `ai=1` here on purpose. AI re-ranking requires a password, and
    a password does not belong in a GET query string (it would land in browser
    history, logs, and referrers). The only way to spend a call is the
    POST /search/refine form.
    """
    if not q.strip():
        return JSONResponse({"query": q, "results": [], "mode": "local"})
    try:
        return run_search(q.strip(), use_ai=False)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.get("/api/timeline")
def timeline_api(view: str = "daily", end: str = None):
    if view not in ("daily", "monthly"):
        view = "daily"
    try:
        return timeline_payload(view, end)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)


if __name__ == "__main__":
    import uvicorn

    try:
        database.init_db()
        print("[Database] Initialized PostgreSQL tables successfully.")
        print(f"[Database] Connected via: {database.ACTIVE_DSN_LABEL}")
    except Exception as e:
        print(
            f"[Database Notice] Database offline or unreachable. Starting UI with disconnect banner. Error: {e}"
        )

    uvicorn.run(app, host="127.0.0.1", port=8080)
