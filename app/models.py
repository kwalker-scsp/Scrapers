"""ORM models.

Design notes
------------
* `events` is the *published* dataset. Nothing but an approved review decision
  writes to it. The public read API only ever selects status='published'.
* `pending_submissions` is the scraper-facing queue. It holds the verbatim
  payload plus a normalized proposal, and never mutates the published data.
* Tags / sources / sections are real relations so they can be filtered and
  counted, but the join tables stay thin (no per-join metadata beyond ordering
  and, for citations, the URL of the specific article).
* Only `payload`/`proposed`/`diff`/`changes`/`detail` are JSON, and each is
  either an audit artifact or an intentionally open-ended proposal blob.
* Column types are chosen for SQLite/Postgres parity: dates are DATE, enums are
  VARCHAR+CHECK, JSON upgrades to JSONB on Postgres automatically.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import (
    AUDIT_ACTIONS,
    DATE_PRECISIONS,
    EVENT_STATUSES,
    OPEN_SUBMISSION_STATUSES,
    SUBMISSION_STATUSES,
    SUBMISSION_TYPES,
    TAG_KINDS,
)

# Postgres gets JSONB (indexable, deduplicated keys); SQLite gets its JSON/TEXT.
JSONType = JSON().with_variant(postgresql.JSONB, "postgresql")

# Predictable constraint names, so Alembic can autogenerate reversible
# migrations and DROP CONSTRAINT works on Postgres.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


#: WHERE clause for the partial unique index that enforces submission dedup.
#: Derived from the enum so the two can never drift apart.
OPEN_STATUS_SQL = "status IN ({0})".format(
    ", ".join("'{0}'".format(s) for s in OPEN_SUBMISSION_STATUSES)
)


def utcnow() -> datetime:
    """Timezone-aware UTC now. Set in Python, not the DB, for backend parity."""
    return datetime.now(timezone.utc)


def _enum(values, name: str) -> Enum:
    """VARCHAR + CHECK constraint instead of a native DB enum type.

    `create_constraint=True` is required — SQLAlchemy defaults it to False, which
    would give a plain VARCHAR with no database-level enforcement at all.
    """
    return Enum(
        *values, name=name, native_enum=False, create_constraint=True, validate_strings=True
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Vocabulary tables
# ---------------------------------------------------------------------------


class Section(Base):
    """Chronological grouping. Self-referential: a subsection has a parent.

    One table handles both levels because the two are structurally identical and
    an event carries a direct FK to each level it belongs to.
    """

    __tablename__ = "sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(400), nullable=False)
    slug: Mapped[str] = mapped_column(String(400), nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sections.id", ondelete="RESTRICT"), nullable=True
    )
    # Chronological display order; sections are not alphabetical.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    parent: Mapped[Optional["Section"]] = relationship(
        "Section", remote_side=[id], back_populates="children"
    )
    children: Mapped[List["Section"]] = relationship(
        "Section", back_populates="parent", cascade="save-update"
    )

    __table_args__ = (
        # A subsection name only needs to be unique within its parent, so
        # parent_id is part of the key. Top-level sections have parent_id NULL,
        # which both backends treat as distinct — `ensure_section` therefore
        # looks rows up explicitly rather than relying on this to dedupe them.
        UniqueConstraint("parent_id", "slug", name="uq_sections_parent_id_slug"),
        Index("ix_sections_parent_id", "parent_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return "<Section {0!r}>".format(self.name)


class Tag(Base):
    """A label from either the `tags` or the `research_categories` vocabulary."""

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(_enum(TAG_KINDS, "tag_kind"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("kind", "slug", name="uq_tags_kind_slug"),
        Index("ix_tags_kind", "kind"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return "<Tag {0}:{1!r}>".format(self.kind, self.name)


class Source(Base):
    """A publisher / outlet, e.g. "ISW", "UK Parliament"."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    slug: Mapped[str] = mapped_column(String(300), nullable=False, unique=True)
    publisher: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    homepage_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover
        return "<Source {0!r}>".format(self.name)


# ---------------------------------------------------------------------------
# Published dataset
# ---------------------------------------------------------------------------


class Event(Base):
    """A published timeline event. Written only by an approved review decision."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- identity -------------------------------------------------------------
    #: Content hash of date_start + normalized body excerpt. See app/dedup.py.
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: Optional stable id assigned by whichever scraper owns this event.
    external_id: Mapped[Optional[str]] = mapped_column(String(300), nullable=True, unique=True)
    #: `id` from the original seed JSON, for traceability back to the docx export.
    seed_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # --- dates ----------------------------------------------------------------
    date_text: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    date_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    date_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    date_precision: Mapped[str] = mapped_column(
        _enum(DATE_PRECISIONS, "date_precision"), nullable=False
    )

    # --- content --------------------------------------------------------------
    body: Mapped[str] = mapped_column(Text, nullable=False)
    section_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sections.id", ondelete="RESTRICT"), nullable=True
    )
    subsection_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("sections.id", ondelete="RESTRICT"), nullable=True
    )

    # --- lifecycle ------------------------------------------------------------
    status: Mapped[str] = mapped_column(
        _enum(EVENT_STATUSES, "event_status"), nullable=False, default="published"
    )
    #: Incremented on every applied edit. Used for stale-edit detection.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    #: Which submission created this event (NULL for seed rows). Deliberately a
    #: plain integer rather than a FK: pending_submissions already points at
    #: events, and a FK back would make the pair mutually dependent, which
    #: SQLite cannot resolve (it has no ALTER TABLE ADD CONSTRAINT). The audit
    #: log is the authoritative link between an event and its submission.
    created_from_submission_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    section: Mapped[Optional[Section]] = relationship("Section", foreign_keys=[section_id])
    subsection: Mapped[Optional[Section]] = relationship("Section", foreign_keys=[subsection_id])
    tag_links: Mapped[List["EventTag"]] = relationship(
        "EventTag", back_populates="event", cascade="all, delete-orphan", lazy="selectin"
    )
    citations: Mapped[List["EventSource"]] = relationship(
        "EventSource",
        back_populates="event",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="EventSource.sort_order",
    )

    __table_args__ = (
        Index("ix_events_date_start", "date_start"),
        Index("ix_events_status_date_start", "status", "date_start"),
        Index("ix_events_section_id", "section_id"),
        Index("ix_events_subsection_id", "subsection_id"),
        CheckConstraint(
            "date_end IS NULL OR date_start IS NULL OR date_end >= date_start",
            name="date_end_after_start",
        ),
    )

    # -- convenience views over the relations ---------------------------------
    def tag_names(self, kind: str = "tag") -> List[str]:
        return [link.tag.name for link in self.tag_links if link.tag.kind == kind]

    def __repr__(self) -> str:  # pragma: no cover
        return "<Event {0} {1}>".format(self.id, self.date_text)


class EventTag(Base):
    """Event <-> Tag join. Covers both tag kinds."""

    __tablename__ = "event_tags"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )

    event: Mapped[Event] = relationship("Event", back_populates="tag_links")
    tag: Mapped[Tag] = relationship("Tag", lazy="joined")

    __table_args__ = (
        # Reverse index: "all events with tag X" without scanning the join.
        Index("ix_event_tags_tag_id_event_id", "tag_id", "event_id"),
    )


class EventSource(Base):
    """A citation: this event was reported by this source, optionally at this URL.

    `url` is NOT NULL DEFAULT '' rather than nullable so the uniqueness
    constraint below behaves identically on SQLite and Postgres (both treat NULLs
    as distinct, which would allow unlimited duplicate un-URL'd citations). The
    API serializes '' back to null.
    """

    __tablename__ = "event_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quote: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    accessed_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    event: Mapped[Event] = relationship("Event", back_populates="citations")
    source: Mapped[Source] = relationship("Source", lazy="joined")

    __table_args__ = (
        UniqueConstraint("event_id", "source_id", "url", name="uq_event_sources_event_id_source_id_url"),
        Index("ix_event_sources_source_id", "source_id"),
    )


# ---------------------------------------------------------------------------
# Submission queue
# ---------------------------------------------------------------------------


class PendingSubmission(Base):
    """A scraper's candidate contribution, awaiting human review.

    Rows are append-mostly: `proposed` and `diff` are written once at ingest and
    never rewritten, so what a reviewer approves is exactly what was submitted.
    Corroboration from later identical submissions accumulates in
    `submission_evidence` and bumps `corroboration_count` — additive metadata
    only.
    """

    __tablename__ = "pending_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    submission_type: Mapped[str] = mapped_column(
        _enum(SUBMISSION_TYPES, "submission_type"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        _enum(SUBMISSION_STATUSES, "submission_status"), nullable=False, default="pending"
    )

    #: For 'new': content hash of the proposed event. For 'edit': hash of
    #: (target event, canonical patch). See app/dedup.py.
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    #: Required for submission_type='edit'.
    target_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    #: events.version at ingest time. If the event has moved on, the reviewer
    #: sees a conflict warning instead of silently clobbering newer data.
    base_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # --- provenance -----------------------------------------------------------
    scraper: Mapped[str] = mapped_column(String(200), nullable=False)
    scraper_run_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- the proposal ---------------------------------------------------------
    #: Verbatim request body as received. Never normalized. Audit ground truth.
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: Normalized proposal: a full event dict for 'new', the patch for 'edit'.
    proposed: Mapped[Dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: For 'edit': {field: {"before": <published value at ingest>, "after": ...}}
    diff: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONType, nullable=True)
    #: Non-blocking validation notes, e.g. "introduces new tag 'Sabotage'".
    warnings: Mapped[Optional[List[str]]] = mapped_column(JSONType, nullable=True)

    corroboration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- decision -------------------------------------------------------------
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: True when the reviewer changed values before approving.
    edited_by_reviewer: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: What the reviewer actually applied, if it differed from `proposed`.
    applied: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONType, nullable=True)
    resulting_event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )

    evidence: Mapped[List["SubmissionEvidence"]] = relationship(
        "SubmissionEvidence",
        back_populates="submission",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SubmissionEvidence.received_at",
    )
    target_event: Mapped[Optional[Event]] = relationship("Event", foreign_keys=[target_event_id])

    __table_args__ = (
        # Dedup is enforced only across *open* submissions: once an item is
        # approved or rejected, the same key may legitimately be proposed again
        # (e.g. a rejected event later gains a better source). Partial unique
        # indexes work on both SQLite (3.8+) and Postgres (9.0+).
        Index(
            "uq_pending_submissions_open_dedup_key",
            "dedup_key",
            unique=True,
            sqlite_where=text(OPEN_STATUS_SQL),
            postgresql_where=text(OPEN_STATUS_SQL),
        ),
        Index("ix_pending_submissions_status_submitted_at", "status", "submitted_at"),
        Index("ix_pending_submissions_dedup_key", "dedup_key"),
        Index("ix_pending_submissions_scraper", "scraper"),
        Index("ix_pending_submissions_target_event_id", "target_event_id"),
        CheckConstraint(
            "(submission_type = 'edit' AND target_event_id IS NOT NULL)"
            " OR submission_type = 'new'",
            name="edit_requires_target",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return "<PendingSubmission {0} {1}/{2}>".format(self.id, self.submission_type, self.status)


class SubmissionEvidence(Base):
    """One scraper's report backing a submission.

    The submission that created the queue item gets an evidence row too, so
    `corroboration_count == len(evidence)` always holds and the review UI can
    show "3 scrapers agree" with the raw payloads behind it.
    """

    __tablename__ = "submission_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("pending_submissions.id", ondelete="CASCADE"), nullable=False
    )
    scraper: Mapped[str] = mapped_column(String(200), nullable=False)
    scraper_run_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    #: Verbatim payload of this particular report.
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONType, nullable=False)
    #: Human-readable summary of the sources this report cited.
    source_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    submission: Mapped[PendingSubmission] = relationship(
        "PendingSubmission", back_populates="evidence"
    )

    __table_args__ = (Index("ix_submission_evidence_submission_id", "submission_id"),)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class AuditLogEntry(Base):
    """Append-only record of everything that touched the dataset.

    Replaying `event.created` (a full field snapshot) followed by every
    `event.updated` (per-field before/after) in `occurred_at` order reconstructs
    any event's complete history. Nothing in the app updates or deletes rows
    here.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    action: Mapped[str] = mapped_column(_enum(AUDIT_ACTIONS, "audit_action"), nullable=False)
    #: Who or what decided. "reviewer:<name>", "scraper:<name>", "system:seed".
    actor: Mapped[str] = mapped_column(String(200), nullable=False)

    event_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True
    )
    submission_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("pending_submissions.id", ondelete="SET NULL"), nullable=True
    )
    #: Event version *after* this entry was applied, for ordering replays.
    event_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: {field: {"before": ..., "after": ...}} for whatever actually landed.
    changes: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONType, nullable=True)
    #: Free-form context: rejection reason, reviewer overrides, conflict notes.
    detail: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONType, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_audit_log_event_id_occurred_at", "event_id", "occurred_at"),
        Index("ix_audit_log_submission_id", "submission_id"),
        Index("ix_audit_log_occurred_at", "occurred_at"),
        Index("ix_audit_log_action", "action"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return "<Audit {0} {1}>".format(self.action, self.occurred_at)
