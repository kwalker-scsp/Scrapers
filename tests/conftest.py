"""Test fixtures: a fresh file-backed SQLite DB per test, seeded with 3 events.

A temp *file* rather than :memory: so the schema matches what migrations produce
and the FK/partial-index behaviour is real.
"""

from __future__ import annotations

import os
import tempfile
from typing import Dict, Iterator

import pytest

# Configure the environment before app modules read settings.
_TMP = tempfile.mkdtemp(prefix="ukrtl-test-")
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///{0}/test.db".format(_TMP)
os.environ["INGEST_API_KEY"] = "test-ingest-key"
os.environ["REVIEW_API_KEY"] = "test-review-key"
os.environ["CORS_ORIGINS"] = "*"
os.environ["REQUIRE_KEY_FOR_READS"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import db as db_module  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Event, EventSource, EventTag  # noqa: E402
from app.services import vocab  # noqa: E402
from app import dedup  # noqa: E402

SEED_EVENTS = [
    {
        "date_text": "February 2014",
        "date_start": "2014-02-01",
        "date_end": "2014-02-01",
        "date_precision": "month",
        "section": "Origins: 2013-2022",
        "body": "Yanukovych flees Ukraine amid escalating protests in Kyiv.",
        "tags": ["Diplomacy"],
        "research_categories": [],
        "sources": ["CFR", "Britannica"],
    },
    {
        "date_text": "24 February 2022",
        "date_start": "2022-02-24",
        "date_end": "2022-02-24",
        "date_precision": "day",
        "section": "2022: Full-Scale Invasion",
        "subsection": "Invasion and early fighting",
        "body": "Russia launches a full-scale invasion of Ukraine from the north, east and south.",
        "tags": ["Warfare Shift", "Territory"],
        "research_categories": ["Fire/Maneuver"],
        "sources": ["ISW"],
    },
    {
        "date_text": None,
        "date_start": None,
        "date_end": None,
        "date_precision": "undated",
        "section": "Undated / Cross-Cutting",
        "body": "Both sides increasingly rely on electronic warfare to blunt drone operations.",
        "tags": ["EW/Counter-Drone"],
        "research_categories": [],
        "sources": ["CNAS"],
    },
]


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.create_all(db_module.engine)
    yield


@pytest.fixture(autouse=True)
def fresh_db(_schema) -> Iterator[None]:
    """Empty every table before each test.

    Deleting rather than dropping: `sections` is self-referential, and SQLite
    with `PRAGMA foreign_keys=ON` (which the app sets, matching Postgres) refuses
    to DROP a table whose rows still reference each other. Reversed
    `sorted_tables` gives child-before-parent order across tables; the
    self-reference inside `sections` needs the extra subsection pass.
    """
    with db_module.engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "sections":
                conn.execute(text("DELETE FROM sections WHERE parent_id IS NOT NULL"))
            conn.execute(table.delete())
    yield


def _insert(session: Session, spec: Dict) -> Event:
    import datetime as dt

    def d(v):
        return dt.date.fromisoformat(v) if v else None

    section = vocab.ensure_section(session, spec.get("section"))
    subsection = (
        vocab.ensure_section(session, spec.get("subsection"), parent=section)
        if spec.get("subsection")
        else None
    )
    event = Event(
        dedup_key=dedup.event_dedup_key(spec["date_start"], spec["body"]),
        date_text=spec["date_text"],
        date_start=d(spec["date_start"]),
        date_end=d(spec["date_end"]),
        date_precision=spec["date_precision"],
        body=spec["body"],
        section=section,
        subsection=subsection,
        status="published",
        version=1,
    )
    session.add(event)
    session.flush()
    for label in spec["tags"]:
        event.tag_links.append(EventTag(tag=vocab.ensure_tag(session, label, "tag")))
    for label in spec["research_categories"]:
        event.tag_links.append(EventTag(tag=vocab.ensure_tag(session, label, "research_category")))
    for order, name in enumerate(spec["sources"]):
        event.citations.append(
            EventSource(source=vocab.ensure_source(session, name), url="", sort_order=order)
        )
    session.flush()
    return event


@pytest.fixture()
def seeded(fresh_db) -> Dict[str, int]:
    """Three published events: month-precision, day-precision, undated."""
    from app.services import audit as audit_svc
    from app.services import events as events_svc

    session = db_module.SessionLocal()
    ids = {}
    try:
        for i, spec in enumerate(SEED_EVENTS, start=1):
            event = _insert(session, spec)
            audit_svc.log(
                session,
                action="event.created",
                actor="system:seed",
                event_id=event.id,
                event_version=1,
                changes=events_svc.full_snapshot_diff(events_svc.event_fields(event)),
                detail={"origin": "test_fixture"},
            )
            ids["e{0}".format(i)] = event.id
        session.commit()
    finally:
        session.close()
    return ids


@pytest.fixture()
def client(fresh_db) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def ingest_headers() -> Dict[str, str]:
    return {"X-API-Key": "test-ingest-key", "X-Scraper-Agent": "pytest"}


@pytest.fixture()
def review_headers() -> Dict[str, str]:
    return {"X-API-Key": "test-review-key", "X-Actor": "pytest-reviewer"}


def new_event_payload(**overrides) -> Dict:
    payload = {
        "submission_type": "new",
        "scraper": "test-scraper",
        "scraper_run_id": "run-1",
        "confidence": 0.9,
        "event": {
            "date_text": "17 February 2024",
            "date_start": "2024-02-17",
            "date_precision": "day",
            "section": "2024: Avdiivka Falls",
            "body": "Ukrainian forces withdraw from Avdiivka after a months-long Russian assault.",
            "tags": ["Territory"],
            "research_categories": ["Fire/Maneuver"],
            "sources": [{"name": "ISW", "url": "https://example.org/isw/avdiivka"}],
        },
    }
    event_overrides = overrides.pop("event", None)
    payload.update(overrides)
    if event_overrides:
        payload["event"].update(event_overrides)
    return payload
