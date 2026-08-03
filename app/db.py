"""Engine / session wiring.

Everything here is deliberately dialect-agnostic apart from two SQLite-only
pragmas, so moving to Postgres is a DATABASE_URL change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import REPO_ROOT, get_settings


def _sqlite_path(url: str) -> Path | None:
    """Extract the on-disk path from a sqlite URL, or None for in-memory."""
    tail = url.split("///", 1)[-1] if "///" in url else ""
    if not tail or tail == ":memory:":
        return None
    p = Path(tail)
    return p if p.is_absolute() else (REPO_ROOT / p)


def build_engine(database_url: str | None = None) -> Engine:
    settings = get_settings()
    url = database_url or settings.database_url

    connect_args: Dict[str, Any] = {}
    if url.startswith("sqlite"):
        # The DB file's parent dir may not exist on a fresh clone.
        path = _sqlite_path(url)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI serves requests from a threadpool; sessions are per-request so
        # the connection is never actually shared concurrently.
        connect_args["check_same_thread"] = False

    engine = create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            # Foreign keys are OFF by default in SQLite; Postgres always enforces
            # them, so turn them on to keep behaviour identical across backends.
            cur.execute("PRAGMA foreign_keys=ON")
            # WAL lets the read API keep serving while a review approval writes.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rolled back on error."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
