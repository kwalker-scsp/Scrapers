"""The audit trail.

Append-only. Nothing in this module updates or deletes an existing row, and no
other module writes to `audit_log` directly.

Reconstructability guarantee
----------------------------
For any published event, replaying its entries in `occurred_at, id` order gives
you the full history:

    event.created   changes = {field: {before: null, after: <initial value>}}
    event.updated   changes = {field: {before: <old>,  after: <new>}}
    event.retracted changes = null

Applying those in order reproduces the event's current state, and stopping early
reproduces any earlier state. Every entry carries the `submission_id` it came
from and the `actor` who decided it, so "why does this event say what it says"
is always answerable.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models import AuditLogEntry


def log(
    db: Session,
    *,
    action: str,
    actor: str,
    event_id: Optional[int] = None,
    submission_id: Optional[int] = None,
    event_version: Optional[int] = None,
    changes: Optional[Dict[str, Any]] = None,
    detail: Optional[Dict[str, Any]] = None,
    note: Optional[str] = None,
) -> AuditLogEntry:
    """Record one thing that happened. Caller owns the transaction."""
    entry = AuditLogEntry(
        action=action,
        actor=actor,
        event_id=event_id,
        submission_id=submission_id,
        event_version=event_version,
        changes=changes or None,
        detail=detail or None,
        note=note,
    )
    db.add(entry)
    db.flush()
    return entry


def build_query(
    *,
    event_id: Optional[int] = None,
    submission_id: Optional[int] = None,
    action: Optional[str] = None,
    actor: Optional[str] = None,
) -> Select:
    stmt = select(AuditLogEntry)
    if event_id is not None:
        stmt = stmt.where(AuditLogEntry.event_id == event_id)
    if submission_id is not None:
        stmt = stmt.where(AuditLogEntry.submission_id == submission_id)
    if action:
        stmt = stmt.where(AuditLogEntry.action == action)
    if actor:
        stmt = stmt.where(AuditLogEntry.actor == actor)
    return stmt


def count(db: Session, stmt: Select) -> int:
    return db.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def page(db: Session, stmt: Select, *, limit: int, offset: int, newest_first: bool = True) -> List[AuditLogEntry]:
    order = (
        (AuditLogEntry.occurred_at.desc(), AuditLogEntry.id.desc())
        if newest_first
        else (AuditLogEntry.occurred_at.asc(), AuditLogEntry.id.asc())
    )
    return list(db.scalars(stmt.order_by(*order).limit(limit).offset(offset)).all())


def replay_event(db: Session, event_id: int) -> Dict[str, Any]:
    """Reconstruct an event's field values from the audit log alone.

    Used by `GET /api/v1/events/{id}/history?verify=true` to demonstrate that the
    log is complete — if the replayed state doesn't match the live row, something
    wrote to `events` outside the review path and the dataset's provenance is
    suspect.
    """
    entries = page(
        db,
        build_query(event_id=event_id),
        limit=10_000,
        offset=0,
        newest_first=False,
    )
    state: Dict[str, Any] = {}
    applied = 0
    for entry in entries:
        if entry.action not in ("event.created", "event.updated"):
            continue
        for field, change in (entry.changes or {}).items():
            if field == "dedup_key":
                continue
            state[field] = change.get("after")
        applied += 1
    return {"replayed_fields": state, "entries_applied": applied, "entry_count": len(entries)}
