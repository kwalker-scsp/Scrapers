"""Public read API. Serves published events only — never the pending queue.

Every query in here goes through `events.published_only()`, so there is no code
path by which an unapproved submission can reach a public response.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import DATE_PRECISIONS
from app.models import Event, EventSource, EventTag, Section, Source, Tag
from app.schemas import (
    AuditListOut,
    EventListOut,
    EventOut,
    MetaOut,
    SectionItem,
    VocabItem,
)
from app.security import optional_read_auth
from app.services import audit as audit_svc
from app.services import events as events_svc
from app.services import submissions as submissions_svc

router = APIRouter(prefix="/api/v1", tags=["public"], dependencies=[Depends(optional_read_auth)])


@router.get("/health", summary="Liveness probe")
def health(db: Session = Depends(get_db)) -> dict:
    published = db.scalar(
        select(func.count()).select_from(Event).where(Event.status == "published")
    )
    return {
        "status": "ok",
        "published_events": published or 0,
        "pending_submissions": submissions_svc.pending_count(db),
    }


@router.get("/events", response_model=EventListOut, summary="List published events")
def list_events(
    db: Session = Depends(get_db),
    tag: Optional[List[str]] = Query(
        None, description="Tag name or slug. Repeat for multiple; see tag_mode."
    ),
    research_category: Optional[List[str]] = Query(
        None, description="Research category name or slug. Repeatable."
    ),
    tag_mode: str = Query(
        "any",
        pattern="^(any|all)$",
        description="'any' matches events with at least one of the given labels; "
        "'all' requires every one of them.",
    ),
    section: Optional[str] = Query(None, description="Section name or slug"),
    subsection: Optional[str] = Query(None, description="Subsection name or slug"),
    source: Optional[str] = Query(None, description="Source name or slug, e.g. 'ISW'"),
    date_from: Optional[dt.date] = Query(
        None, description="Inclusive start of the window (YYYY-MM-DD)"
    ),
    date_to: Optional[dt.date] = Query(None, description="Inclusive end of the window"),
    date_precision: Optional[List[str]] = Query(
        None, description="Restrict to these precisions. Repeatable."
    ),
    include_undated: bool = Query(
        True, description="Keep events with no date. They cannot satisfy a date window."
    ),
    q: Optional[str] = Query(None, min_length=2, max_length=200, description="Substring search"),
    order: str = Query(
        "narrative",
        pattern="^(narrative|asc|desc)$",
        description="'narrative' groups by the section's own order then date within it "
        "(sections are the source document's chapters and are not date-contiguous). "
        "'asc'/'desc' are strict chronological order.",
    ),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> EventListOut:
    """Filtered, paginated list of published events.

    Date filtering is *overlap*-based: an event whose span touches the requested
    window matches, so a month- or year-precision event surfaces for any query
    that intersects it.
    """
    if date_precision:
        invalid = sorted(set(date_precision) - set(DATE_PRECISIONS))
        if invalid:
            raise HTTPException(
                status_code=422,
                detail="unknown date_precision value(s): {0}. Valid: {1}".format(
                    ", ".join(invalid), ", ".join(DATE_PRECISIONS)
                ),
            )

    stmt = events_svc.build_event_query(
        tags=tag,
        tag_mode=tag_mode,
        research_categories=research_category,
        section=section,
        subsection=subsection,
        source=source,
        date_from=date_from,
        date_to=date_to,
        date_precision=date_precision,
        include_undated=include_undated,
        q=q,
    )
    total = events_svc.count_query(db, stmt)
    stmt = events_svc.apply_order(stmt, order)
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()

    return EventListOut(
        total=total,
        limit=limit,
        offset=offset,
        events=[EventOut.model_validate(events_svc.event_out_dict(e)) for e in rows],
    )


@router.get("/events/{event_id}", response_model=EventOut, summary="Get one published event")
def get_event(event_id: int, db: Session = Depends(get_db)) -> EventOut:
    event = events_svc.get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="No published event with id {0}".format(event_id))
    return EventOut.model_validate(events_svc.event_out_dict(event))


@router.get(
    "/events/{event_id}/history",
    response_model=None,
    summary="Full audit history for one event",
)
def event_history(
    event_id: int,
    verify: bool = Query(
        False,
        description="Also replay the log and report whether it reproduces the live row",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Every audit entry that touched this event, oldest first.

    This is the provenance record: which submission proposed each change, which
    scraper produced it, who approved it, and what the values were before and
    after.
    """
    event = events_svc.get_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="No published event with id {0}".format(event_id))

    entries = audit_svc.page(
        db, audit_svc.build_query(event_id=event_id), limit=1000, offset=0, newest_first=False
    )
    payload = {
        "event_id": event_id,
        "current_version": event.version,
        "entries": [
            {
                "id": e.id,
                "occurred_at": e.occurred_at,
                "action": e.action,
                "actor": e.actor,
                "submission_id": e.submission_id,
                "event_version": e.event_version,
                "changes": e.changes,
                "detail": e.detail,
                "note": e.note,
            }
            for e in entries
        ],
    }
    if verify:
        replay = audit_svc.replay_event(db, event_id)
        live = events_svc.event_fields(event)
        mismatches = sorted(
            field
            for field in live
            if not events_svc.values_equal(
                field, live[field], replay["replayed_fields"].get(field)
            )
        )
        payload["verification"] = {
            "entries_applied": replay["entries_applied"],
            "reconstructs_current_state": not mismatches,
            "mismatched_fields": mismatches,
        }
    return payload


@router.get("/changelog", response_model=AuditListOut, summary="Dataset-wide changelog")
def changelog(
    db: Session = Depends(get_db),
    action: Optional[str] = Query(None, description="Filter to one audit action"),
    event_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> AuditListOut:
    """Newest-first audit feed. Public: the change history is part of the artifact."""
    stmt = audit_svc.build_query(action=action, event_id=event_id)
    total = audit_svc.count(db, stmt)
    entries = audit_svc.page(db, stmt, limit=limit, offset=offset)
    return AuditListOut(total=total, limit=limit, offset=offset, entries=entries)


@router.get("/meta", response_model=MetaOut, summary="Vocabularies and bounds for filters")
def meta(db: Session = Depends(get_db)) -> MetaOut:
    """Everything the frontend needs to build its filter controls in one call."""
    published = Event.status == "published"

    tag_rows = db.execute(
        select(Tag.name, Tag.slug, Tag.kind, func.count(Event.id))
        .join(EventTag, EventTag.tag_id == Tag.id)
        .join(Event, Event.id == EventTag.event_id)
        .where(published)
        .group_by(Tag.id)
        .order_by(func.count(Event.id).desc(), Tag.name.asc())
    ).all()

    source_rows = db.execute(
        select(Source.name, Source.slug, func.count(func.distinct(Event.id)))
        .join(EventSource, EventSource.source_id == Source.id)
        .join(Event, Event.id == EventSource.event_id)
        .where(published)
        .group_by(Source.id)
        .order_by(func.count(func.distinct(Event.id)).desc(), Source.name.asc())
    ).all()

    precision_rows = db.execute(
        select(Event.date_precision, func.count())
        .where(published)
        .group_by(Event.date_precision)
        .order_by(func.count().desc())
    ).all()

    # Sections keep their chronological sort_order; subsections nest under them.
    section_rows = db.execute(
        select(Section.id, Section.name, Section.slug, Section.parent_id, Section.sort_order)
        .order_by(Section.sort_order.asc(), Section.name.asc())
    ).all()
    section_counts = dict(
        db.execute(
            select(Event.section_id, func.count()).where(published).group_by(Event.section_id)
        ).all()
    )
    subsection_counts = dict(
        db.execute(
            select(Event.subsection_id, func.count()).where(published).group_by(Event.subsection_id)
        ).all()
    )

    by_parent = {}
    for sid, name, slug, parent_id, _ in section_rows:
        if parent_id is not None:
            by_parent.setdefault(parent_id, []).append(
                VocabItem(name=name, slug=slug, event_count=subsection_counts.get(sid, 0))
            )

    sections = [
        SectionItem(
            name=name,
            slug=slug,
            event_count=section_counts.get(sid, 0),
            subsections=by_parent.get(sid, []),
        )
        for sid, name, slug, parent_id, _ in section_rows
        if parent_id is None
    ]

    bounds = db.execute(
        select(func.min(Event.date_start), func.max(func.coalesce(Event.date_end, Event.date_start)))
        .where(published)
    ).one()

    return MetaOut(
        event_count=db.scalar(select(func.count()).select_from(Event).where(published)) or 0,
        date_min=bounds[0],
        date_max=bounds[1],
        date_precisions=[
            VocabItem(name=p, slug=p, event_count=c) for p, c in precision_rows
        ],
        sections=sections,
        tags=[
            VocabItem(name=n, slug=s, event_count=c)
            for n, s, kind, c in tag_rows
            if kind == "tag"
        ],
        research_categories=[
            VocabItem(name=n, slug=s, event_count=c)
            for n, s, kind, c in tag_rows
            if kind == "research_category"
        ],
        sources=[VocabItem(name=n, slug=s, event_count=c) for n, s, c in source_rows],
        pending_submission_count=submissions_svc.pending_count(db),
    )
