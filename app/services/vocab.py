"""Get-or-create helpers for the vocabulary tables (sections, tags, sources).

Vocabulary rows are only ever created as a side effect of *approving* something.
Ingest deliberately does not create them: otherwise one scraper typo would
permanently pollute the tag list that the public filter UI is built from.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Section, Source, Tag

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Stable, ascii, url-safe key for a label. Used for lookups and filtering."""
    folded = unicodedata.normalize("NFKD", value or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _SLUG_STRIP.sub("-", folded.lower()).strip("-") or "unnamed"


# ---------------------------------------------------------------------------
# Lookups (no writes) — used by ingest to raise "new vocabulary" warnings
# ---------------------------------------------------------------------------


def known_tag_slugs(db: Session, kind: str) -> set:
    return set(db.scalars(select(Tag.slug).where(Tag.kind == kind)).all())


def known_source_slugs(db: Session) -> set:
    return set(db.scalars(select(Source.slug)).all())


def known_section_slugs(db: Session) -> set:
    return set(db.scalars(select(Section.slug)).all())


# ---------------------------------------------------------------------------
# Get-or-create (writes) — used when applying an approved decision
# ---------------------------------------------------------------------------


def ensure_tag(db: Session, name: str, kind: str) -> Tag:
    slug = slugify(name)
    existing = db.scalar(select(Tag).where(Tag.kind == kind, Tag.slug == slug))
    if existing is not None:
        return existing
    tag = Tag(name=name.strip(), slug=slug, kind=kind)
    db.add(tag)
    db.flush()
    return tag


def ensure_source(db: Session, name: str, homepage_url: Optional[str] = None) -> Source:
    slug = slugify(name)
    existing = db.scalar(select(Source).where(Source.slug == slug))
    if existing is not None:
        return existing
    source = Source(name=name.strip(), slug=slug, homepage_url=homepage_url)
    db.add(source)
    db.flush()
    return source


def ensure_section(
    db: Session, name: Optional[str], parent: Optional[Section] = None, sort_order: int = 0
) -> Optional[Section]:
    """Get-or-create a section (parent=None) or subsection (parent set)."""
    if not name:
        return None
    slug = slugify(name)
    parent_id = parent.id if parent is not None else None
    stmt = select(Section).where(Section.slug == slug)
    stmt = stmt.where(Section.parent_id.is_(None) if parent_id is None else Section.parent_id == parent_id)
    existing = db.scalar(stmt)
    if existing is not None:
        return existing
    section = Section(name=name.strip(), slug=slug, parent_id=parent_id, sort_order=sort_order)
    db.add(section)
    db.flush()
    return section


def resolve_section_pair(
    db: Session, section_name: Optional[str], subsection_name: Optional[str]
) -> Tuple[Optional[Section], Optional[Section]]:
    """Create/find the section and, nested beneath it, the subsection."""
    section = ensure_section(db, section_name)
    subsection = ensure_section(db, subsection_name, parent=section) if subsection_name else None
    return section, subsection


def next_section_sort_order(db: Session) -> int:
    existing = db.scalars(select(Section.sort_order)).all()
    return (max(existing) + 10) if existing else 0


def unknown_labels(known: set, labels: Iterable[str]) -> List[str]:
    """Labels that would introduce a brand-new vocabulary term."""
    out = []
    for label in labels or []:
        if slugify(label) not in known:
            out.append(label)
    return out


def vocab_counts(rows: Sequence[Tuple[str, str, int]]) -> List[Dict[str, object]]:
    return [{"name": name, "slug": slug, "event_count": count} for name, slug, count in rows]
