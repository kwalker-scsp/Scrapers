"""One-time import of the seed JSON as already-published events.

    python -m scripts.seed Russo-Ukrainian_War_Timeline_Dates.json

Idempotent: events are matched on dedup key, so re-running skips what's already
there instead of duplicating it. `--force` wipes the published dataset first (it
will refuse if there are any submissions, so you can't destroy review history by
accident).

Every imported event gets an `event.created` audit entry with a full field
snapshot, actor `system:seed`. That means the seed rows are on exactly the same
provenance footing as scraper-contributed ones: `GET /events/{id}/history` works
for event 1 the same way it works for event 900.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import dedup
from app.config import REPO_ROOT
from app.db import SessionLocal
from app.enums import DATE_PRECISIONS
from app.models import (
    AuditLogEntry,
    Event,
    EventSource,
    EventTag,
    PendingSubmission,
    Section,
    Source,
    Tag,
)
from app.services import audit as audit_svc
from app.services import events as events_svc
from app.services import vocab

SEED_ACTOR = "system:seed"


def load_json(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data, {}
    events = data.get("events")
    if not isinstance(events, list):
        raise SystemExit("{0}: expected a top-level 'events' array".format(path))
    return events, data.get("meta") or {}


def validate_seed(events: List[Dict[str, Any]]) -> List[str]:
    """Pre-flight check. Returns a list of problems; empty means good to go."""
    problems: List[str] = []
    seen_ids = set()
    seen_keys: Dict[str, int] = {}

    for idx, raw in enumerate(events):
        label = "event[{0}] (id={1})".format(idx, raw.get("id"))
        precision = raw.get("date_precision")
        if precision not in DATE_PRECISIONS:
            problems.append(
                "{0}: date_precision {1!r} is not one of {2}".format(
                    label, precision, ", ".join(DATE_PRECISIONS)
                )
            )
        if not (raw.get("body") or "").strip():
            problems.append("{0}: empty body".format(label))
        if raw.get("id") in seen_ids:
            problems.append("{0}: duplicate id".format(label))
        seen_ids.add(raw.get("id"))

        start, end = raw.get("date_start"), raw.get("date_end")
        if precision != "undated" and not start:
            problems.append("{0}: date_precision={1!r} but no date_start".format(label, precision))
        if start and end and end < start:
            problems.append("{0}: date_end {1} precedes date_start {2}".format(label, end, start))

        key = dedup.event_dedup_key(start, raw.get("body"))
        if key in seen_keys:
            problems.append(
                "{0}: dedup key collides with id={1} — same date and same opening "
                "{2} characters of body. Reword one of them or give them distinct "
                "dates before importing.".format(label, seen_keys[key], dedup.BODY_EXCERPT_CHARS)
            )
        seen_keys[key] = raw.get("id")
    return problems


def wipe_published(db: Session) -> None:
    """Delete published data. Refuses if any review history would be orphaned."""
    submissions = db.scalar(select(func.count()).select_from(PendingSubmission)) or 0
    if submissions:
        raise SystemExit(
            "Refusing --force: there are {0} submission(s) in the queue, and wiping "
            "events would orphan their review history. Delete data/timeline.db and "
            "re-migrate if you really want a clean slate.".format(submissions)
        )
    for model in (AuditLogEntry, EventSource, EventTag, Event, Tag, Source, Section):
        db.query(model).delete()
    db.flush()


def seed(path: Path, *, force: bool = False, dry_run: bool = False) -> int:
    events, meta = load_json(path)

    problems = validate_seed(events)
    if problems:
        print("Seed file has {0} problem(s):".format(len(problems)), file=sys.stderr)
        for p in problems[:25]:
            print("  - {0}".format(p), file=sys.stderr)
        if len(problems) > 25:
            print("  ... and {0} more".format(len(problems) - 25), file=sys.stderr)
        return 1

    print("Seed file: {0}".format(path))
    print("  events declared: {0}".format(meta.get("event_count", "?")))
    print("  events found:    {0}".format(len(events)))
    if dry_run:
        print("\n--dry-run: validation passed, nothing written.")
        return 0

    db = SessionLocal()
    try:
        if force:
            wipe_published(db)
            print("  --force: cleared existing published data")

        # Sections keep the order they appear in the source document, which is
        # chronological; alphabetical would scramble "Origins" against "2022".
        section_order: Dict[str, int] = {}
        for raw in events:
            name = (raw.get("section") or "").strip()
            if name and name not in section_order:
                section_order[name] = len(section_order) * 10

        created = skipped = 0
        for raw in events:
            key = dedup.event_dedup_key(raw.get("date_start"), raw.get("body"))
            if db.scalar(select(Event).where(Event.dedup_key == key)) is not None:
                skipped += 1
                continue

            section = vocab.ensure_section(
                db,
                (raw.get("section") or "").strip() or None,
                sort_order=section_order.get((raw.get("section") or "").strip(), 0),
            )
            subsection = (
                vocab.ensure_section(db, (raw.get("subsection") or "").strip() or None, parent=section)
                if (raw.get("subsection") or "").strip()
                else None
            )

            event = Event(
                dedup_key=key,
                seed_id=raw.get("id"),
                date_text=(raw.get("date_text") or "").strip() or None,
                date_start=_date(raw.get("date_start")),
                date_end=_date(raw.get("date_end")) or _date(raw.get("date_start")),
                date_precision=raw["date_precision"],
                body=raw["body"].strip(),
                section=section,
                subsection=subsection,
                status="published",
                version=1,
            )
            db.add(event)
            db.flush()

            for label in raw.get("tags") or []:
                event.tag_links.append(EventTag(tag=vocab.ensure_tag(db, label, "tag")))
            for label in raw.get("research_categories") or []:
                event.tag_links.append(
                    EventTag(tag=vocab.ensure_tag(db, label, "research_category"))
                )
            for order, name in enumerate(raw.get("sources") or []):
                # Seed sources are bare outlet names; scrapers will supply URLs.
                event.citations.append(
                    EventSource(
                        source=vocab.ensure_source(db, name), url="", sort_order=order
                    )
                )
            db.flush()

            audit_svc.log(
                db,
                action="event.created",
                actor=SEED_ACTOR,
                event_id=event.id,
                event_version=event.version,
                changes=events_svc.full_snapshot_diff(events_svc.event_fields(event)),
                detail={
                    "origin": "seed_import",
                    "seed_id": raw.get("id"),
                    "source_file": path.name,
                    "dedup_key": key,
                },
                note="Imported from the initial seed dataset as an already-published event.",
            )
            created += 1

        audit_svc.log(
            db,
            action="seed.import",
            actor=SEED_ACTOR,
            changes=None,
            detail={
                "source_file": path.name,
                "generated_from": meta.get("generated_from"),
                "events_in_file": len(events),
                "events_created": created,
                "events_skipped_existing": skipped,
                "dedup_algorithm_version": dedup.DEDUP_ALGORITHM_VERSION,
            },
            note="One-time seed import.",
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    _report(created, skipped)
    return 0


def _date(value: Optional[str]):
    if not value:
        return None
    import datetime as dt

    return dt.date.fromisoformat(str(value)[:10])


def _report(created: int, skipped: int) -> None:
    db = SessionLocal()
    try:
        counts = {
            "published events": db.scalar(
                select(func.count()).select_from(Event).where(Event.status == "published")
            ),
            "sections": db.scalar(
                select(func.count()).select_from(Section).where(Section.parent_id.is_(None))
            ),
            "subsections": db.scalar(
                select(func.count()).select_from(Section).where(Section.parent_id.is_not(None))
            ),
            "tags": db.scalar(select(func.count()).select_from(Tag).where(Tag.kind == "tag")),
            "research categories": db.scalar(
                select(func.count()).select_from(Tag).where(Tag.kind == "research_category")
            ),
            "sources": db.scalar(select(func.count()).select_from(Source)),
            "audit entries": db.scalar(select(func.count()).select_from(AuditLogEntry)),
        }
    finally:
        db.close()

    print("\nImported {0} event(s); skipped {1} already present.".format(created, skipped))
    print("\nDatabase now holds:")
    for label, value in counts.items():
        print("  {0:<20} {1}".format(label, value))
    print("\nNext: make dev   ->   http://127.0.0.1:8000")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "path",
        nargs="?",
        default=str(REPO_ROOT / "Russo-Ukrainian_War_Timeline_Dates.json"),
        help="Path to the seed JSON",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clear existing published data first (refuses if submissions exist)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate the file and exit without writing"
    )
    args = parser.parse_args()

    path = Path(args.path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        raise SystemExit("No such file: {0}".format(path))

    return seed(path, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
