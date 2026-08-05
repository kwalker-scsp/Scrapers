import logging
import os
import re
from dotenv import find_dotenv, load_dotenv
import psycopg2

load_dotenv(find_dotenv(usecwd=True))

logger = logging.getLogger(__name__)

# Records which DSN the most recent connection actually used, so a silent
# remote -> local failover cannot leave the writer and the review UI pointed
# at different databases without anyone noticing.
ACTIVE_DSN_LABEL = None

# Once the remote has timed out, stop probing it for the rest of the process.
# Every save_* opens its own connection, so re-probing a down remote costs the
# 3s timeout on each of them -- seeding 216 events took ~11 minutes of pure
# waiting. One probe per process is enough to make the failover decision.
_REMOTE_UNAVAILABLE = False


def _redact(dsn: str) -> str:
    """Strips credentials out of a DSN so it is safe to log."""
    return re.sub(r"://[^@/]*@", "://***@", dsn) if dsn else ""


def reset_remote_probe():
    """Re-enables remote probing after a failover.

    A long-running process (the review UI) would otherwise stay pinned to local
    for its whole lifetime once the remote blipped.
    """
    global _REMOTE_UNAVAILABLE
    _REMOTE_UNAVAILABLE = False


def get_db_connection():
    """Attempts connection to remote PostgreSQL database first (3s timeout).

    Falls back to local Docker container if remote times out. The fallback is
    logged loudly: reading from a different database than the pipeline wrote to
    looks like data loss, so it must never be silent.
    """
    global ACTIVE_DSN_LABEL, _REMOTE_UNAVAILABLE

    remote_url = os.getenv("DATABASE_URL")
    local_url = os.getenv("DATABASE_URL_LOCAL")

    if remote_url and not _REMOTE_UNAVAILABLE:
        try:
            conn = psycopg2.connect(remote_url, connect_timeout=3)
            ACTIVE_DSN_LABEL = f"remote {_redact(remote_url)}"
            return conn
        except psycopg2.OperationalError as e:
            _REMOTE_UNAVAILABLE = True
            logger.warning(
                "Remote database %s unreachable (%s). Falling back to "
                "DATABASE_URL_LOCAL -- writes and reads may target a "
                "DIFFERENT database than usual.",
                _redact(remote_url),
                e,
            )

    if local_url:
        try:
            conn = psycopg2.connect(local_url, connect_timeout=3)
            ACTIVE_DSN_LABEL = f"local {_redact(local_url)}"
            return conn
        except psycopg2.OperationalError as e:
            raise ConnectionError(
                "Both remote and local PostgreSQL connections failed!"
            ) from e

    raise ValueError(
        "No database URL configured. Set DATABASE_URL and/or "
        "DATABASE_URL_LOCAL in your .env."
    )


def init_db():
    """Initializes PostgreSQL staging, review, and timeline tables if they do not exist."""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 1. Daily Pending Events Table (with notes column)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS "DailyPendingEvents" (
            id SERIAL PRIMARY KEY,
            event_date VARCHAR(20),
            title VARCHAR(500),
            summary TEXT,
            confidence_score DOUBLE PRECISION DEFAULT 1.0,
            is_event_flag BOOLEAN DEFAULT TRUE,
            source_name VARCHAR(250),
            source_url VARCHAR(1000),
            notes VARCHAR(250) DEFAULT '',
            review_status VARCHAR(50) DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 2. Monthly Macro Milestones Table (with notes column)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS "MonthlyMacroMilestones" (
            id SERIAL PRIMARY KEY,
            event_month VARCHAR(10),
            title VARCHAR(500),
            strategic_summary TEXT,
            category VARCHAR(100),
            source_name VARCHAR(250),
            source_url VARCHAR(1000),
            notes VARCHAR(250) DEFAULT '',
            review_status VARCHAR(50) DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 3. Duplicate Review Queue
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS "DuplicateReviewQueue" (
            id SERIAL PRIMARY KEY,
            incoming_title VARCHAR(500),
            incoming_summary TEXT,
            incoming_date VARCHAR(20),
            existing_title VARCHAR(500),
            existing_summary TEXT,
            similarity_score DOUBLE PRECISION,
            source_url VARCHAR(1000),
            review_status VARCHAR(50) DEFAULT 'PENDING_DUPLICATE',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 4. Scrape Watermarks: the high-water mark per source. Without this
        #    every run reprocesses the full page and re-inserts everything.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS "ScrapeWatermarks" (
            source_name VARCHAR(250) PRIMARY KEY,
            last_event_date VARCHAR(20),
            last_run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_status VARCHAR(50)
        );
        """)

        # 5. Reconcile columns on tables that already exist.
        #    CREATE TABLE IF NOT EXISTS is a no-op against an older table, so
        #    without these a database created before a column was introduced
        #    silently stays behind and every write to it fails.
        for table, column, ddl in (
            ("DailyPendingEvents", "notes", "VARCHAR(250) DEFAULT ''"),
            # Pre-computed 384-dim MiniLM vector, JSON-serialized. Stored so
            # search does not have to re-embed the corpus on every query.
            ("DailyPendingEvents", "embedding", "TEXT DEFAULT ''"),
            ("MonthlyMacroMilestones", "notes", "VARCHAR(250) DEFAULT ''"),
            (
                "DailyPendingEvents",
                "review_status",
                "VARCHAR(50) DEFAULT 'PENDING'",
            ),
            (
                "MonthlyMacroMilestones",
                "review_status",
                "VARCHAR(50) DEFAULT 'PENDING'",
            ),
        ):
            cursor.execute(
                f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS {column} {ddl};'
            )

        # 6. Indexes supporting the review queue and timeline reads.
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_daily_status '
            'ON "DailyPendingEvents" (review_status);'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_daily_date '
            'ON "DailyPendingEvents" (event_date);'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_monthly_status '
            'ON "MonthlyMacroMilestones" (review_status);'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_monthly_month '
            'ON "MonthlyMacroMilestones" (event_month);'
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()


def save_daily_event(
    event_date,
    title,
    summary,
    score,
    is_event,
    source_name,
    source_url,
    notes="",
    review_status="PENDING",
    embedding="",
):
    """Inserts a daily event and returns its new id.

    Raises on failure -- a swallowed write here made the pipeline report
    success for events that never landed.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO "DailyPendingEvents"
            (event_date, title, summary, confidence_score, is_event_flag, source_name, source_url, notes, review_status, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """,
            (
                event_date,
                title,
                summary,
                float(score),
                bool(is_event),
                source_name,
                source_url,
                notes,
                review_status,
                embedding,
            ),
        )
        new_id = cursor.fetchone()[0]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def save_monthly_milestone(
    event_month,
    title,
    strategic_summary,
    category,
    source_name,
    source_url,
    notes="",
    review_status="PENDING",
):
    """Inserts a macro milestone and returns its new id. Raises on failure."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO "MonthlyMacroMilestones"
            (event_month, title, strategic_summary, category, source_name, source_url, notes, review_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """,
            (
                event_month,
                title,
                strategic_summary,
                category,
                source_name,
                source_url,
                notes,
                review_status,
            ),
        )
        new_id = cursor.fetchone()[0]
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def save_duplicate_review(incoming_event, matched_event, score, source_url):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO "DuplicateReviewQueue" 
            (incoming_title, incoming_summary, incoming_date, existing_title, existing_summary, similarity_score, source_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
            (
                incoming_event["title"],
                incoming_event["summary"],
                incoming_event["event_date"],
                matched_event["title"] if matched_event else "",
                matched_event["summary"] if matched_event else "",
                float(score),
                source_url,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# Source URLs are joined with this separator when an auto-merge folds a new
# sighting into an existing record. The review UI's merge flow uses the same
# convention, so merged rows render identically regardless of origin.
SOURCE_URL_SEPARATOR = " | "


def append_source_url(table, record_id, url):
    """Adds `url` to a record's source_url list, skipping exact repeats.

    This is the write half of the >= 0.95 auto-merge rule: the duplicate event
    is dropped, but the URL that reported it is preserved as corroboration.
    """
    if table not in ("DailyPendingEvents", "MonthlyMacroMilestones"):
        raise ValueError(f"Unsupported table for append_source_url: {table}")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f'SELECT source_url FROM "{table}" WHERE id = %s', (record_id,)
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError(f"{table} id {record_id} not found")

        existing = row[0] or ""
        urls = [u.strip() for u in existing.split(SOURCE_URL_SEPARATOR) if u.strip()]
        if url in urls:
            return False

        urls.append(url)
        cursor.execute(
            f'UPDATE "{table}" SET source_url = %s WHERE id = %s',
            (SOURCE_URL_SEPARATOR.join(urls)[:1000], record_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def fetch_dedup_corpus_daily():
    """Returns the daily rows the deduplicator compares against.

    Includes PENDING as well as APPROVED: an unreviewed row is still a row, and
    excluding it let the same event re-enter the queue on every run.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT id, title, summary FROM "DailyPendingEvents" '
            "WHERE review_status IN ('APPROVED', 'PENDING')"
        )
        return [
            {"id": r[0], "title": r[1], "summary": r[2] or ""}
            for r in cursor.fetchall()
        ]
    finally:
        cursor.close()
        conn.close()


def fetch_dedup_corpus_monthly():
    """Monthly equivalent of fetch_dedup_corpus_daily."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT id, title, strategic_summary FROM "MonthlyMacroMilestones" '
            "WHERE review_status IN ('APPROVED', 'PENDING')"
        )
        return [
            {"id": r[0], "title": r[1], "summary": r[2] or ""}
            for r in cursor.fetchall()
        ]
    finally:
        cursor.close()
        conn.close()


# Retained for backwards compatibility with any caller still importing it.
def fetch_recent_approved_summaries():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT id, title, summary FROM "DailyPendingEvents" '
            "WHERE review_status = 'APPROVED'"
        )
        return [
            {"id": r[0], "title": r[1], "summary": r[2] or ""}
            for r in cursor.fetchall()
        ]
    finally:
        cursor.close()
        conn.close()


def fetch_search_corpus():
    """Every daily event with its stored vector, for search Stage 1.

    Returns all review statuses except MERGED -- a merged row's content already
    lives on the record that survived, so surfacing it would duplicate results.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT id, event_date, title, summary, source_name, source_url, '
            'notes, review_status, embedding '
            'FROM "DailyPendingEvents" '
            "WHERE review_status <> 'MERGED'"
        )
        return [
            {
                "id": r[0],
                "event_date": r[1],
                "title": r[2],
                "summary": r[3] or "",
                "source_name": r[4],
                "source_url": r[5],
                "notes": r[6] or "",
                "review_status": r[7],
                "embedding": r[8],
            }
            for r in cursor.fetchall()
        ]
    finally:
        cursor.close()
        conn.close()


def set_embedding(record_id, blob):
    """Writes a computed vector back to a row (used by the backfill)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'UPDATE "DailyPendingEvents" SET embedding = %s WHERE id = %s',
            (blob, record_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def fetch_unembedded_daily():
    """Rows written before the embedding column existed."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT id, title, summary FROM "DailyPendingEvents" '
            "WHERE embedding IS NULL OR embedding = ''"
        )
        return [
            {"id": r[0], "title": r[1], "summary": r[2] or ""}
            for r in cursor.fetchall()
        ]
    finally:
        cursor.close()
        conn.close()


def get_watermark(source_name):
    """Returns the newest event_date already ingested for a source, or None."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT last_event_date FROM "ScrapeWatermarks" WHERE source_name = %s',
            (source_name,),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        cursor.close()
        conn.close()


def set_watermark(source_name, last_event_date, status="OK"):
    """Advances a source's high-water mark.

    Only ever moves forward: a source that emits an old event must not rewind
    the mark and reopen the door to everything after it.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO "ScrapeWatermarks" (source_name, last_event_date, last_run_at, last_status)
            VALUES (%s, %s, CURRENT_TIMESTAMP, %s)
            ON CONFLICT (source_name) DO UPDATE SET
                last_event_date = GREATEST(
                    "ScrapeWatermarks".last_event_date,
                    EXCLUDED.last_event_date
                ),
                last_run_at = CURRENT_TIMESTAMP,
                last_status = EXCLUDED.last_status
            """,
            (source_name, last_event_date, status),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def fetch_timeline_data(start=None, end=None):
    """Returns reviewed daily events and macro milestones for the timeline view.

    MERGED rows are excluded: they are the redundant half of a merge and their
    content already lives on the surviving APPROVED record.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        daily_sql = (
            'SELECT id, event_date, title, summary, source_name, source_url, notes '
            'FROM "DailyPendingEvents" WHERE review_status = \'APPROVED\''
        )
        daily_params = []
        if start:
            daily_sql += " AND event_date >= %s"
            daily_params.append(start)
        if end:
            daily_sql += " AND event_date <= %s"
            daily_params.append(end)
        daily_sql += " ORDER BY event_date ASC"
        cursor.execute(daily_sql, tuple(daily_params))
        daily = [
            {
                "id": r[0],
                "event_date": r[1],
                "title": r[2],
                "summary": r[3],
                "source_name": r[4],
                "source_url": r[5],
                "notes": r[6] or "",
            }
            for r in cursor.fetchall()
        ]

        monthly_sql = (
            'SELECT id, event_month, title, strategic_summary, category, '
            'source_name, source_url, notes '
            'FROM "MonthlyMacroMilestones" WHERE review_status = \'APPROVED\''
        )
        monthly_params = []
        if start:
            monthly_sql += " AND event_month >= %s"
            monthly_params.append(start[:7])
        if end:
            monthly_sql += " AND event_month <= %s"
            monthly_params.append(end[:7])
        monthly_sql += " ORDER BY event_month ASC"
        cursor.execute(monthly_sql, tuple(monthly_params))
        monthly = [
            {
                "id": r[0],
                "event_month": r[1],
                "title": r[2],
                "summary": r[3],
                "category": r[4],
                "source_name": r[5],
                "source_url": r[6],
                "notes": r[7] or "",
            }
            for r in cursor.fetchall()
        ]

        return {"daily": daily, "monthly": monthly}
    finally:
        cursor.close()
        conn.close()