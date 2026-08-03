"""Published-event reads, writes, and the field-level diff machinery.

The central abstraction is `event_fields()`: a plain, JSON-serializable dict of
the ten editable fields. Diffs, audit-log entries, submission previews and patch
application all operate on that shape, so there is exactly one definition of
"what an event's value is" and the review UI never has to know about the ORM.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app import dedup
from app.enums import EDITABLE_EVENT_FIELDS, IMPRECISE_PRECISIONS
from app.models import Event, EventSource, EventTag, Section, Source, Tag
from app.schemas import EventCore, coerce_citation
from app.services import vocab


class ConflictError(Exception):
    """Raised when a write would collide with existing published data."""

    def __init__(self, message: str, event_id: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.event_id = event_id


# ---------------------------------------------------------------------------
# Canonical field view
# ---------------------------------------------------------------------------


def _iso(value: Optional[dt.date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def citation_dict(cite: EventSource) -> Dict[str, Any]:
    return {
        "name": cite.source.name,
        # '' is the in-DB stand-in for "no URL" (see EventSource docstring).
        "url": cite.url or None,
        "title": cite.title,
        "quote": cite.quote,
        "accessed_at": _iso(cite.accessed_at),
    }


def event_fields(event: Event) -> Dict[str, Any]:
    """The ten editable fields, JSON-safe. The unit of diffing and auditing."""
    return {
        "date_text": event.date_text,
        "date_start": _iso(event.date_start),
        "date_end": _iso(event.date_end),
        "date_precision": event.date_precision,
        "section": event.section.name if event.section else None,
        "subsection": event.subsection.name if event.subsection else None,
        "body": event.body,
        "tags": [link.tag.name for link in event.tag_links if link.tag.kind == "tag"],
        "research_categories": [
            link.tag.name for link in event.tag_links if link.tag.kind == "research_category"
        ],
        "sources": [citation_dict(c) for c in event.citations],
    }


def event_out_dict(event: Event) -> Dict[str, Any]:
    """Public read-model payload."""
    fields = event_fields(event)
    fields.update(
        {
            "id": event.id,
            "is_imprecise": event.date_precision in IMPRECISE_PRECISIONS,
            "external_id": event.external_id,
            "seed_id": event.seed_id,
            "dedup_key": event.dedup_key,
            "version": event.version,
            "created_at": event.created_at,
            "updated_at": event.updated_at,
        }
    )
    return fields


# ---------------------------------------------------------------------------
# Normalization + comparison
# ---------------------------------------------------------------------------


def normalize_value(field: str, value: Any) -> Any:
    """Coerce a field value into the canonical JSON-safe form used everywhere."""
    if isinstance(value, dt.date):
        return value.isoformat()
    if field in ("tags", "research_categories"):
        if value is None:
            return []
        seen, out = set(), []
        for item in value:
            label = str(item).strip()
            if label and label.lower() not in seen:
                seen.add(label.lower())
                out.append(label)
        return out
    if field == "sources":
        if value is None:
            return []
        out = []
        for item in value:
            cite = coerce_citation(item) if not isinstance(item, dict) else None
            if cite is not None:
                item = cite.model_dump()
            item = dict(item)
            out.append(
                {
                    "name": (item.get("name") or "").strip(),
                    "url": (item.get("url") or None),
                    "title": item.get("title") or None,
                    "quote": item.get("quote") or None,
                    "accessed_at": normalize_value("accessed_at", item.get("accessed_at")),
                }
            )
        return out
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return value


def normalize_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: normalize_value(k, v) for k, v in data.items()}


def _comparable(field: str, value: Any) -> Any:
    """Order-insensitive form for equality checks.

    Tag and source order carries no meaning, so a scraper listing the same tags
    in a different order must not read as a change.
    """
    if field in ("tags", "research_categories"):
        return sorted((v or "").lower() for v in (value or []))
    if field == "sources":
        return sorted(
            ((c.get("name") or "").lower(), (c.get("url") or "")) for c in (value or [])
        )
    return value


def values_equal(field: str, left: Any, right: Any) -> bool:
    return _comparable(field, left) == _comparable(field, right)


def diff_fields(current: Dict[str, Any], proposed: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """{field: {"before": ..., "after": ...}} for fields that actually change."""
    out: Dict[str, Dict[str, Any]] = {}
    for field, after in proposed.items():
        if field not in EDITABLE_EVENT_FIELDS:
            continue
        before = current.get(field)
        if not values_equal(field, before, after):
            out[field] = {"before": before, "after": after}
    return out


def full_snapshot_diff(fields: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """A creation logged in the same shape as an update, so replays are uniform."""
    return {k: {"before": None, "after": v} for k, v in fields.items()}


# ---------------------------------------------------------------------------
# Merging a patch onto an event
# ---------------------------------------------------------------------------


def merge_patch(
    current: Dict[str, Any], patch: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """Apply a partial patch to a canonical field dict.

    Returns the merged fields plus any warnings. Handles one reconciliation rule:
    if a patch moves `date_start` past an untouched `date_end`, `date_end` is
    dragged along, because the alternative is a constraint violation at approval
    time. Explicit spans (patches that set `date_end` themselves) are untouched.
    """
    warnings: List[str] = []
    merged = dict(current)
    merged.update(normalize_fields(patch))

    if merged.get("date_precision") == "undated":
        if merged.get("date_start") or merged.get("date_end"):
            merged["date_start"] = None
            merged["date_end"] = None
            warnings.append(
                "date_precision was set to 'undated', so date_start/date_end were cleared"
            )
        return merged, warnings

    start, end = merged.get("date_start"), merged.get("date_end")
    if start and "date_end" not in patch and (end is None or end < start):
        merged["date_end"] = start
        warnings.append(
            "date_end ({0}) preceded the new date_start ({1}) and was set to match it; "
            "send date_end explicitly to describe a span".format(end, start)
        )
    return merged, warnings


def validate_event_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Run a merged/complete field dict through the same rules as an ingest.

    Raises pydantic.ValidationError. Returns the validated, normalized fields —
    this is what makes a patch that contradicts the live event (e.g. setting
    precision to 'day' on an event with no date_start) fail at ingest rather than
    at approval.
    """
    core = EventCore.model_validate({k: fields.get(k) for k in EDITABLE_EVENT_FIELDS})
    return {
        "date_text": core.date_text,
        "date_start": _iso(core.date_start),
        "date_end": _iso(core.date_end),
        "date_precision": core.date_precision,
        "section": core.section,
        "subsection": core.subsection,
        "body": core.body,
        "tags": core.tags,
        "research_categories": core.research_categories,
        "sources": [coerce_citation(s).model_dump(mode="json") for s in core.sources],
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> Optional[dt.date]:
    if value in (None, ""):
        return None
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _apply_tags(db: Session, event: Event, names: Sequence[str], kind: str) -> None:
    desired = {}
    for name in names or []:
        desired[vocab.slugify(name)] = name
    # Drop links of this kind that are no longer wanted.
    event.tag_links[:] = [
        link
        for link in event.tag_links
        if link.tag.kind != kind or link.tag.slug in desired
    ]
    have = {link.tag.slug for link in event.tag_links if link.tag.kind == kind}
    for slug, name in desired.items():
        if slug not in have:
            event.tag_links.append(EventTag(tag=vocab.ensure_tag(db, name, kind)))


def _apply_citations(db: Session, event: Event, citations: Sequence[Dict[str, Any]]) -> None:
    # Rebuild wholesale: citation lists are short and this keeps ordering exactly
    # as the reviewer approved it.
    event.citations[:] = []
    db.flush()
    seen = set()
    for order, raw in enumerate(citations or []):
        name = (raw.get("name") or "").strip()
        if not name:
            continue
        url = (raw.get("url") or "").strip()
        key = (vocab.slugify(name), url)
        if key in seen:  # satisfies uq_event_sources_event_id_source_id_url
            continue
        seen.add(key)
        event.citations.append(
            EventSource(
                source=vocab.ensure_source(db, name),
                url=url,
                title=raw.get("title"),
                quote=raw.get("quote"),
                accessed_at=_parse_date(raw.get("accessed_at")),
                sort_order=order,
            )
        )


def _assign_fields(db: Session, event: Event, fields: Dict[str, Any]) -> None:
    """Write a canonical field dict onto an Event (scalar, relational and join)."""
    if "date_text" in fields:
        event.date_text = fields["date_text"]
    if "date_precision" in fields:
        event.date_precision = fields["date_precision"]
    if "date_start" in fields:
        event.date_start = _parse_date(fields["date_start"])
    if "date_end" in fields:
        event.date_end = _parse_date(fields["date_end"])
    if "body" in fields:
        event.body = fields["body"]

    if "section" in fields or "subsection" in fields:
        section_name = fields.get("section") if "section" in fields else (
            event.section.name if event.section else None
        )
        subsection_name = fields.get("subsection") if "subsection" in fields else (
            event.subsection.name if event.subsection else None
        )
        section, subsection = vocab.resolve_section_pair(db, section_name, subsection_name)
        event.section = section
        event.subsection = subsection

    if "tags" in fields:
        _apply_tags(db, event, fields.get("tags") or [], "tag")
    if "research_categories" in fields:
        _apply_tags(db, event, fields.get("research_categories") or [], "research_category")
    if "sources" in fields:
        _apply_citations(db, event, fields.get("sources") or [])


def _check_dedup_collision(db: Session, key: str, exclude_event_id: Optional[int] = None) -> None:
    stmt = select(Event).where(Event.dedup_key == key)
    if exclude_event_id is not None:
        stmt = stmt.where(Event.id != exclude_event_id)
    clash = db.scalar(stmt)
    if clash is not None:
        raise ConflictError(
            "This content is identical to published event #{0} (same date and opening "
            "text). Edit that event instead of creating a duplicate.".format(clash.id),
            event_id=clash.id,
        )


def create_event(
    db: Session,
    fields: Dict[str, Any],
    *,
    external_id: Optional[str] = None,
    seed_id: Optional[int] = None,
    submission_id: Optional[int] = None,
) -> Event:
    """Insert a published event. Only ever called from an approved decision."""
    key = (
        dedup.external_dedup_key(external_id)
        if external_id
        else dedup.event_dedup_key(fields.get("date_start"), fields.get("body"))
    )
    _check_dedup_collision(db, key)
    if external_id and db.scalar(select(Event).where(Event.external_id == external_id)):
        raise ConflictError("external_id {0!r} is already in use".format(external_id))

    event = Event(
        dedup_key=key,
        external_id=external_id,
        seed_id=seed_id,
        date_precision=fields["date_precision"],
        body=fields["body"],
        status="published",
        version=1,
        created_from_submission_id=submission_id,
    )
    db.add(event)
    db.flush()
    _assign_fields(db, event, fields)
    db.flush()
    return event


def apply_changes(
    db: Session, event: Event, fields: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Apply a (partial or full) field dict to a published event.

    Returns the effective before/after changes. Recomputes the dedup key when
    the identity-bearing fields move, refusing the write if that would make the
    event a duplicate of a different published event.
    """
    before = event_fields(event)
    changes = diff_fields(before, normalize_fields(fields))
    if not changes:
        return {}

    _assign_fields(db, event, {k: fields[k] for k in fields if k in EDITABLE_EVENT_FIELDS})

    if not event.external_id and ("body" in changes or "date_start" in changes):
        new_key = dedup.event_dedup_key(event.date_start, event.body)
        if new_key != event.dedup_key:
            _check_dedup_collision(db, new_key, exclude_event_id=event.id)
            changes["dedup_key"] = {"before": event.dedup_key, "after": new_key}
            event.dedup_key = new_key

    event.version += 1
    db.flush()
    return changes


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def published_only(stmt: Select) -> Select:
    return stmt.where(Event.status == "published")


def build_event_query(
    *,
    tags: Optional[Iterable[str]] = None,
    tag_mode: str = "any",
    research_categories: Optional[Iterable[str]] = None,
    section: Optional[str] = None,
    subsection: Optional[str] = None,
    source: Optional[str] = None,
    date_from: Optional[dt.date] = None,
    date_to: Optional[dt.date] = None,
    date_precision: Optional[Iterable[str]] = None,
    include_undated: bool = True,
    q: Optional[str] = None,
) -> Select:
    """Filtered SELECT over published events. Filters combine with AND."""
    stmt = published_only(select(Event))

    def _tag_filter(labels: Iterable[str], kind: str, mode: str) -> Select:
        slugs = [vocab.slugify(t) for t in labels]
        sub = (
            select(EventTag.event_id)
            .join(Tag, Tag.id == EventTag.tag_id)
            .where(Tag.kind == kind, Tag.slug.in_(slugs))
        )
        if mode == "all":
            # Every requested label must be present on the event.
            sub = sub.group_by(EventTag.event_id).having(
                func.count(func.distinct(Tag.slug)) == len(set(slugs))
            )
        return Event.id.in_(sub)

    if tags:
        stmt = stmt.where(_tag_filter(tags, "tag", tag_mode))
    if research_categories:
        stmt = stmt.where(_tag_filter(research_categories, "research_category", tag_mode))

    if section:
        slug = vocab.slugify(section)
        stmt = stmt.where(
            Event.section_id.in_(select(Section.id).where(Section.slug == slug))
        )
    if subsection:
        slug = vocab.slugify(subsection)
        stmt = stmt.where(
            Event.subsection_id.in_(select(Section.id).where(Section.slug == slug))
        )
    if source:
        slug = vocab.slugify(source)
        stmt = stmt.where(
            Event.id.in_(
                select(EventSource.event_id)
                .join(Source, Source.id == EventSource.source_id)
                .where(Source.slug == slug)
            )
        )
    if date_precision:
        stmt = stmt.where(Event.date_precision.in_(list(date_precision)))

    # Range overlap: the event's span intersects the requested window. This is
    # what makes month/year/range events show up for any query touching them.
    if date_from is not None or date_to is not None:
        dated = []
        if date_to is not None:
            dated.append(Event.date_start <= date_to)
        if date_from is not None:
            dated.append(func.coalesce(Event.date_end, Event.date_start) >= date_from)
        overlap = dated[0] if len(dated) == 1 else and_(*dated)
        if include_undated:
            # Undated events have no span to compare, so keep them rather than
            # silently dropping them from a date-filtered view.
            stmt = stmt.where(or_(Event.date_start.is_(None), overlap))
        else:
            stmt = stmt.where(Event.date_start.is_not(None), overlap)
    elif not include_undated:
        stmt = stmt.where(Event.date_start.is_not(None))

    if q:
        needle = "%{0}%".format(q.strip().lower())
        stmt = stmt.where(
            or_(
                func.lower(Event.body).like(needle),
                func.lower(func.coalesce(Event.date_text, "")).like(needle),
            )
        )
    return stmt


def count_query(db: Session, stmt: Select) -> int:
    return db.scalar(select(func.count()).select_from(stmt.subquery())) or 0


def apply_order(stmt: Select, mode: str = "narrative") -> Select:
    """Order results. Undated events sort last in every mode.

    `narrative` (the default the timeline UI uses) sorts by the section's own
    position first, then by date within the section. This matters because the
    dataset's sections are the source document's chapters and they are NOT
    date-contiguous — a "2021–2025 five-year total" event belongs to the 2025
    chapter but starts in 2021. Sorting purely by date interleaves chapters and
    makes the same section heading appear repeatedly.

    `asc`/`desc` are strict chronological order, for callers that want a single
    continuous sequence regardless of chapter.
    """
    col = Event.date_start
    if mode == "desc":
        return stmt.order_by(col.is_(None).asc(), col.desc(), Event.id.desc())
    if mode == "asc":
        return stmt.order_by(col.is_(None).asc(), col.asc(), Event.id.asc())

    section = aliased(Section)
    return (
        stmt.outerjoin(section, Event.section_id == section.id)
        # COALESCE rather than NULLS LAST: sectionless events go to the end on
        # both backends without relying on dialect-specific null ordering.
        .order_by(
            func.coalesce(section.sort_order, 10 ** 6).asc(),
            col.is_(None).asc(),
            col.asc(),
            Event.id.asc(),
        )
    )


def get_event(db: Session, event_id: int, *, published_only_flag: bool = True) -> Optional[Event]:
    stmt = select(Event).where(Event.id == event_id)
    if published_only_flag:
        stmt = published_only(stmt)
    return db.scalar(stmt)


def get_event_by_external_id(db: Session, external_id: str) -> Optional[Event]:
    return db.scalar(published_only(select(Event).where(Event.external_id == external_id)))


def find_matching_event(
    db: Session, *, external_id: Optional[str], dedup_key: Optional[str]
) -> Optional[Event]:
    """Locate an already-published event: external_id first, then content hash."""
    if external_id:
        found = db.scalar(select(Event).where(Event.external_id == external_id))
        if found is not None:
            return found
    if dedup_key:
        return db.scalar(select(Event).where(Event.dedup_key == dedup_key))
    return None
