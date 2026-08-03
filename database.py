# Connect this to the database (MSSQL) to store the data scraped from the website.
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()


import os
from dotenv import find_dotenv, load_dotenv
import psycopg2

load_dotenv(find_dotenv(usecwd=True))


def get_db_connection():
    remote_url = os.getenv("DATABASE_URL")
    local_url = os.getenv(
        "DATABASE_URL_LOCAL",
        "postgresql://sa:scspisawesome@localhost:5432/scsp_scraper",
    )

    # Wrap the REMOTE call in try/except with a quick 3s timeout
    if remote_url:
        try:
            print("[Database] Trying remote database (172.25.11.105)...")
            return psycopg2.connect(remote_url, connect_timeout=3)
        except psycopg2.OperationalError:
            print(
                "[Database] Remote unreachable/timed out. Falling back to LOCAL..."
            )

    # Fallback to LOCAL Docker
    if local_url:
        try:
            conn = psycopg2.connect(local_url, connect_timeout=3)
            print("[Database] Connected to LOCAL database!")
            return conn
        except psycopg2.OperationalError as e:
            raise ConnectionError(
                "Both remote and local PostgreSQL connections failed!"
            ) from e

    raise ValueError("No database URL configured.")


def init_db():
    """Initializes PostgreSQL staging, review, and timeline tables if they do not exist."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Daily Events Queue (High-density tactical updates)
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
        review_status VARCHAR(50) DEFAULT 'PENDING',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 2. Monthly Macro Milestones Queue (High-level trends / M&S suitable)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS "MonthlyMacroMilestones" (
        id SERIAL PRIMARY KEY,
        event_month VARCHAR(10), -- Format: YYYY-MM
        title VARCHAR(500),
        strategic_summary TEXT,
        category VARCHAR(100), -- Military, Diplomatic, Economic, Infrastructure
        source_name VARCHAR(250),
        source_url VARCHAR(1000),
        review_status VARCHAR(50) DEFAULT 'PENDING',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 3. Duplicate Review Queue (Side-by-side comparison for analysts)
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

    conn.commit()
    cursor.close()
    conn.close()


def save_daily_event(event_date, title, summary, score, is_event, source_name, source_url):
    """Saves an extracted daily event to the DailyPendingEvents queue."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO "DailyPendingEvents" 
        (event_date, title, summary, confidence_score, is_event_flag, source_name, source_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """,
        (
            event_date,
            title,
            summary,
            float(score),
            bool(is_event),
            source_name,
            source_url,
        ),
    )
    conn.commit()
    cursor.close()
    conn.close()


def save_monthly_milestone(
    event_month, title, strategic_summary, category, source_name, source_url
):
    """Saves an extracted high-level milestone to the MonthlyMacroMilestones queue."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO "MonthlyMacroMilestones" 
        (event_month, title, strategic_summary, category, source_name, source_url)
        VALUES (%s, %s, %s, %s, %s, %s)
    """,
        (event_month, title, strategic_summary, category, source_name, source_url),
    )
    conn.commit()
    cursor.close()
    conn.close()


def save_duplicate_review(incoming_event, matched_event, score, source_url):
    """Flags a near-duplicate event pair for side-by-side human review."""
    conn = get_db_connection()
    cursor = conn.cursor()
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
            matched_event["title"],
            matched_event["summary"],
            float(score),
            source_url,
        ),
    )
    conn.commit()
    cursor.close()
    conn.close()


def fetch_recent_approved_summaries():
    """Fetches titles and summaries from the last approved records for ONNX vector deduplication."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        'SELECT title, summary FROM "DailyPendingEvents" WHERE review_status = \'APPROVED\''
    )
    records = cursor.fetchall()
    cursor.close()
    conn.close()
    return [{"title": r[0], "summary": r[1]} for r in records]