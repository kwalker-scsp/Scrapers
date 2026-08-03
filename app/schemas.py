"""Request/response schemas.

The ingest schemas here ARE the scraper contract. They are published as JSON
Schema at /openapi.json, and `make openapi` dumps them to a file you can vendor
into the scraper repo. Anything rejected here never reaches the queue.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Annotated

from app.enums import DATE_PRECISIONS, EDITABLE_EVENT_FIELDS

DatePrecision = Literal[
    "day", "month", "month-range", "season", "year", "year-range", "range", "approx", "undated"
]

# Guard against the Literal above drifting from the DB's CHECK constraint.
assert set(DatePrecision.__args__) == set(DATE_PRECISIONS), "DatePrecision out of sync with enums"


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------


class CitationIn(BaseModel):
    """A source citation. Scrapers may send a bare string instead of this object.

    `"CFR"` and `{"name": "CFR"}` are equivalent; the object form adds the URL,
    headline and access date that a scraper actually has in hand.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=300, description="Outlet, e.g. 'ISW'")
    url: Optional[str] = Field(None, max_length=2000, description="Link to the specific item")
    title: Optional[str] = Field(None, max_length=1000, description="Headline / document title")
    quote: Optional[str] = Field(None, max_length=4000, description="Supporting excerpt")
    accessed_at: Optional[date] = Field(None, description="When the scraper fetched it")

    @field_validator("name", "url", "title", "quote", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v


CitationInput = Union[str, CitationIn]


def coerce_citation(value: CitationInput) -> CitationIn:
    """Normalize the bare-string shorthand into a CitationIn."""
    if isinstance(value, str):
        return CitationIn(name=value.strip())
    return value


class CitationOut(BaseModel):
    name: str
    url: Optional[str] = None
    title: Optional[str] = None
    quote: Optional[str] = None
    accessed_at: Optional[date] = None


# ---------------------------------------------------------------------------
# Event payload (used for 'new' submissions and as the read-model)
# ---------------------------------------------------------------------------


class EventCore(BaseModel):
    """The fields that describe a timeline event, as a scraper would submit them.

    Cross-field rules enforced here:
      * `date_precision='undated'`  -> date_start/date_end must both be absent.
      * any other precision         -> date_start is required.
      * date_end defaults to date_start for point-in-time precisions.
      * date_end must not precede date_start.
    """

    model_config = ConfigDict(extra="forbid")

    date_text: Optional[str] = Field(
        None,
        max_length=300,
        description="The human-readable date exactly as it appeared in the source, "
        "e.g. 'Late 2013' or 'March–May 2022'.",
    )
    date_start: Optional[date] = Field(None, description="ISO 8601 start (YYYY-MM-DD)")
    date_end: Optional[date] = Field(None, description="ISO 8601 end (inclusive)")
    date_precision: DatePrecision = Field(
        ..., description="How firm the date is. Drives how the timeline renders it."
    )
    section: Optional[str] = Field(
        None, max_length=400, description="Top-level chronological grouping"
    )
    subsection: Optional[str] = Field(
        None, max_length=400, description="Optional grouping within the section"
    )
    body: str = Field(..., min_length=10, max_length=20000, description="Event description")
    tags: List[str] = Field(default_factory=list, max_length=40)
    research_categories: List[str] = Field(default_factory=list, max_length=40)
    sources: List[CitationInput] = Field(default_factory=list, max_length=40)

    @field_validator("date_text", "section", "subsection", "body", mode="before")
    @classmethod
    def _strip_str(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("tags", "research_categories", mode="before")
    @classmethod
    def _clean_labels(cls, v: Any) -> Any:
        if not isinstance(v, list):
            return v
        seen, out = set(), []
        for item in v:
            if not isinstance(item, str):
                raise ValueError("tag labels must be strings")
            label = item.strip()
            if label and label.lower() not in seen:
                seen.add(label.lower())
                out.append(label)
        return out

    @field_validator("body")
    @classmethod
    def _body_required(cls, v: Optional[str]) -> str:
        if not v:
            raise ValueError("body is required and must be at least 10 characters")
        return v

    @model_validator(mode="after")
    def _check_dates(self) -> "EventCore":
        if self.date_precision == "undated":
            if self.date_start or self.date_end:
                raise ValueError(
                    "date_precision='undated' cannot carry date_start/date_end; "
                    "use 'approx' or 'year' if you have an anchor date"
                )
            return self

        if self.date_start is None:
            raise ValueError(
                "date_start is required unless date_precision='undated' "
                "(got date_precision={0!r})".format(self.date_precision)
            )
        if self.date_end is None:
            # Point precisions collapse to a single day; spans must be explicit.
            self.date_end = self.date_start
        if self.date_end < self.date_start:
            raise ValueError("date_end must not be earlier than date_start")
        return self


class EventOut(BaseModel):
    """Public read-model for a published event."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    date_text: Optional[str] = None
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    date_precision: str
    #: True for every precision other than 'day' — the timeline uses this to
    #: draw the event as a fuzzy span instead of a point.
    is_imprecise: bool = False
    section: Optional[str] = None
    subsection: Optional[str] = None
    body: str
    tags: List[str] = Field(default_factory=list)
    research_categories: List[str] = Field(default_factory=list)
    sources: List[CitationOut] = Field(default_factory=list)
    external_id: Optional[str] = None
    seed_id: Optional[int] = None
    dedup_key: str
    version: int
    created_at: datetime
    updated_at: datetime


class EventListOut(BaseModel):
    total: int = Field(..., description="Matching events, ignoring limit/offset")
    limit: int
    offset: int
    events: List[EventOut]


# ---------------------------------------------------------------------------
# Ingest: what scrapers POST
# ---------------------------------------------------------------------------


class SubmissionProvenance(BaseModel):
    """Who produced this candidate. Required on every submission."""

    scraper: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Stable identifier for the scraper, e.g. 'isw-daily'",
    )
    scraper_run_id: Optional[str] = Field(
        None,
        max_length=200,
        description="Identifier for this particular run, so a bad run can be traced",
    )
    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Scraper's own 0..1 confidence, shown to the reviewer"
    )
    notes: Optional[str] = Field(
        None, max_length=4000, description="Free text for the reviewer, e.g. extraction caveats"
    )


class NewEventSubmission(SubmissionProvenance):
    """Propose an event that does not exist in the published dataset yet."""

    model_config = ConfigDict(extra="forbid")

    submission_type: Literal["new"] = "new"
    external_id: Optional[str] = Field(
        None,
        max_length=300,
        description="Your own stable id for this event. If you send one, it takes "
        "precedence over the content hash when matching against existing data, "
        "which lets you re-submit a corrected version of your own event.",
    )
    event: EventCore


class EditSubmission(SubmissionProvenance):
    """Propose changes to specific fields of an existing published event.

    Send only the fields you want to change. The server reads the current
    published values and stores a field-level before/after diff for review.
    """

    model_config = ConfigDict(extra="forbid")

    submission_type: Literal["edit"] = "edit"
    target_event_id: Optional[int] = Field(
        None, ge=1, description="Published event id to modify (or use target_external_id)"
    )
    target_external_id: Optional[str] = Field(
        None, max_length=300, description="Resolve the target by external_id instead of id"
    )
    patch: Dict[str, Any] = Field(
        ...,
        description="Partial event object: only the fields to change. Allowed keys: "
        + ", ".join(EDITABLE_EVENT_FIELDS),
    )

    @model_validator(mode="after")
    def _check_target_and_patch(self) -> "EditSubmission":
        if not self.target_event_id and not self.target_external_id:
            raise ValueError("an edit needs target_event_id or target_external_id")
        if self.target_event_id and self.target_external_id:
            raise ValueError("send target_event_id or target_external_id, not both")
        if not self.patch:
            raise ValueError("patch must contain at least one field")
        unknown = sorted(set(self.patch) - set(EDITABLE_EVENT_FIELDS))
        if unknown:
            raise ValueError(
                "patch contains fields that cannot be edited: {0}. Allowed: {1}".format(
                    ", ".join(unknown), ", ".join(EDITABLE_EVENT_FIELDS)
                )
            )
        # Validate the patch values against the same rules as a full event by
        # type-checking each key in isolation. Cross-field date rules are checked
        # after the patch is merged with the live event, in services/submissions.
        PatchFields.model_validate(self.patch)
        return self


class PatchFields(BaseModel):
    """Per-field type validation for an edit patch. Every field is optional.

    Note `date_start`/`date_end` are `Optional`, so `{"date_end": null}` is a
    meaningful patch meaning "clear this field".
    """

    model_config = ConfigDict(extra="forbid")

    date_text: Optional[str] = Field(None, max_length=300)
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    date_precision: Optional[DatePrecision] = None
    section: Optional[str] = Field(None, max_length=400)
    subsection: Optional[str] = Field(None, max_length=400)
    body: Optional[str] = Field(None, min_length=10, max_length=20000)
    tags: Optional[List[str]] = Field(None, max_length=40)
    research_categories: Optional[List[str]] = Field(None, max_length=40)
    sources: Optional[List[CitationInput]] = Field(None, max_length=40)


#: Discriminated union: FastAPI picks the right model from `submission_type`, so
#: a scraper gets a precise 422 naming the offending field rather than a generic
#: "no union member matched".
SubmissionIn = Annotated[
    Union[NewEventSubmission, EditSubmission], Field(discriminator="submission_type")
]


class IngestResult(BaseModel):
    outcome: Literal["queued", "corroborated", "duplicate_of_published"] = Field(
        ...,
        description=(
            "queued: a new pending item was created. "
            "corroborated: an equivalent item was already open, so this report was "
            "attached to it as supporting evidence. "
            "duplicate_of_published: this already exists in the published dataset; "
            "nothing needs review."
        ),
    )
    submission_id: int
    status: str
    dedup_key: str
    corroboration_count: int
    #: Set when outcome is duplicate_of_published, or after approval.
    event_id: Optional[int] = None
    #: Non-blocking notes, e.g. new vocabulary terms or a stale-edit conflict.
    warnings: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class FieldDiff(BaseModel):
    field: str
    before: Any = None
    after: Any = None
    #: True when the published value has changed since this edit was submitted,
    #: i.e. `before` no longer reflects reality. Approving would overwrite
    #: whatever landed in between.
    conflict: bool = False
    current: Any = Field(
        None, description="The live published value now, when it differs from `before`"
    )


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    scraper: str
    scraper_run_id: Optional[str] = None
    confidence: Optional[float] = None
    source_summary: Optional[str] = None
    received_at: datetime


class SubmissionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    submission_type: str
    status: str
    dedup_key: str
    external_id: Optional[str] = None
    scraper: str
    scraper_run_id: Optional[str] = None
    confidence: Optional[float] = None
    notes: Optional[str] = None
    corroboration_count: int
    submitted_at: datetime
    last_seen_at: datetime
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    decision_reason: Optional[str] = None
    edited_by_reviewer: bool = False
    warnings: List[str] = Field(default_factory=list)

    #: Full proposed event, for submission_type='new'.
    preview: Optional[Dict[str, Any]] = None
    #: Field-level diff, for submission_type='edit'.
    diff: List[FieldDiff] = Field(default_factory=list)
    target_event_id: Optional[int] = None
    target_event: Optional[EventOut] = None
    base_version: Optional[int] = None
    has_conflict: bool = False
    resulting_event_id: Optional[int] = None
    evidence: List[EvidenceOut] = Field(default_factory=list)
    raw_payload: Optional[Dict[str, Any]] = None


class SubmissionListOut(BaseModel):
    total: int
    limit: int
    offset: int
    counts_by_status: Dict[str, int] = Field(default_factory=dict)
    submissions: List[SubmissionOut]


class ApproveRequest(BaseModel):
    """Approve as-is, or edit-then-approve by supplying `overrides`."""

    model_config = ConfigDict(extra="forbid")

    overrides: Optional[Dict[str, Any]] = Field(
        None,
        description="Reviewer's corrected field values, merged over the proposal. "
        "Same allowed keys as an edit patch. Presence of this field marks the "
        "submission as edited_by_reviewer in the audit log.",
    )
    reason: Optional[str] = Field(None, max_length=4000, description="Optional decision note")
    #: Approving a stale edit requires opting in, so a conflict can't be
    #: rubber-stamped by accident.
    force: bool = Field(
        False, description="Approve even though the target event changed since submission"
    )

    @model_validator(mode="after")
    def _check_overrides(self) -> "ApproveRequest":
        if self.overrides is not None:
            if not self.overrides:
                raise ValueError("overrides cannot be an empty object; omit it instead")
            unknown = sorted(set(self.overrides) - set(EDITABLE_EVENT_FIELDS))
            if unknown:
                raise ValueError("overrides contains unknown fields: " + ", ".join(unknown))
            PatchFields.model_validate(self.overrides)
        return self


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(
        None, max_length=4000, description="Why it was rejected. Optional but recommended."
    )


class DecisionResult(BaseModel):
    submission_id: int
    status: str
    event_id: Optional[int] = None
    event_version: Optional[int] = None
    applied_changes: Dict[str, Any] = Field(default_factory=dict)
    audit_entry_id: int


# ---------------------------------------------------------------------------
# Audit / changelog
# ---------------------------------------------------------------------------


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    occurred_at: datetime
    action: str
    actor: str
    event_id: Optional[int] = None
    submission_id: Optional[int] = None
    event_version: Optional[int] = None
    changes: Optional[Dict[str, Any]] = None
    detail: Optional[Dict[str, Any]] = None
    note: Optional[str] = None


class AuditListOut(BaseModel):
    total: int
    limit: int
    offset: int
    entries: List[AuditEntryOut]


# ---------------------------------------------------------------------------
# Metadata (drives the frontend filter controls)
# ---------------------------------------------------------------------------


class VocabItem(BaseModel):
    name: str
    slug: str
    event_count: int


class SectionItem(BaseModel):
    name: str
    slug: str
    event_count: int
    subsections: List[VocabItem] = Field(default_factory=list)


class MetaOut(BaseModel):
    event_count: int
    date_min: Optional[date] = None
    date_max: Optional[date] = None
    date_precisions: List[VocabItem] = Field(default_factory=list)
    sections: List[SectionItem] = Field(default_factory=list)
    tags: List[VocabItem] = Field(default_factory=list)
    research_categories: List[VocabItem] = Field(default_factory=list)
    sources: List[VocabItem] = Field(default_factory=list)
    pending_submission_count: int = 0
