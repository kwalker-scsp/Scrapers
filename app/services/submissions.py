"""The submission queue: the boundary between scrapers and the published dataset.

Invariants
----------
1. Ingest never writes to `events`. Not once, not for "obvious" cases.
2. `proposed` and `diff` are written at ingest and never rewritten. What a
   reviewer approves is byte-identical to what was submitted. Reviewer changes
   are recorded separately in `applied` + `edited_by_reviewer`.
3. Nothing is deleted. Rejections move to status='rejected' with a reason.
4. Every state transition writes an audit entry.

Duplicate handling — the design choice and its tradeoff
-------------------------------------------------------
When a submission's dedup key matches one that is already open, this module
attaches the new report to the existing queue item as *corroborating evidence*
(a `submission_evidence` row) and bumps `corroboration_count`, rather than
ignoring it.

Why corroborate rather than ignore:
  * Independent agreement is exactly the signal a reviewer wants. "ISW and UK
    Parliament both report this" is a stronger basis for publishing than one
    scraper's word, and it is only visible if the second report is retained.
  * The second report usually carries a *different citation*. Ignoring it throws
    away a source URL you would otherwise have to re-find by hand.
  * It keeps the queue at one row per real-world claim, so review effort scales
    with the number of distinct claims rather than the number of scraper runs.

The cost, and how it's contained:
  * A queue item is no longer a single immutable request, which could muddy the
    audit story. Contained by never letting corroboration touch the proposal
    itself: `proposed`/`diff` are frozen at creation, each report's verbatim
    payload is preserved in its own evidence row, and the reviewer can see all of
    them. Corroboration is strictly additive metadata.
  * A scraper stuck in a loop inflates `corroboration_count`. Contained because
    evidence rows record `scraper` and `scraper_run_id`, so the review UI shows
    *distinct* scraper count alongside the raw total — ten reports from one
    scraper read very differently from two reports from two scrapers.

The alternative (ignore, return 200, drop the payload) is simpler and stateless,
but it makes corroboration invisible and silently discards source URLs. For a
research artifact that has to be defensible, keeping the evidence wins.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app import dedup
from app.enums import OPEN_SUBMISSION_STATUSES
from app.models import Event, PendingSubmission, SubmissionEvidence, utcnow
from app.schemas import EditSubmission, NewEventSubmission, coerce_citation
from app.services import audit, events as events_svc, vocab


class SubmissionError(Exception):
    """A submission that cannot be queued. Maps to a 4xx."""

    def __init__(self, message: str, status_code: int = 422, **extra: Any):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_summary(citations: List[Dict[str, Any]]) -> str:
    parts = []
    for c in citations or []:
        name = c.get("name") or "?"
        parts.append("{0} <{1}>".format(name, c["url"]) if c.get("url") else name)
    return "; ".join(parts) or "(no sources cited)"


def _vocabulary_warnings(db: Session, fields: Dict[str, Any]) -> List[str]:
    """Flag terms that don't exist yet. Non-blocking: reviewers decide.

    Approving is what creates a vocabulary term, so a scraper cannot pollute the
    public filter lists on its own.
    """
    warnings: List[str] = []
    for key, kind, label in (
        ("tags", "tag", "tag"),
        ("research_categories", "research_category", "research category"),
    ):
        if key not in fields:
            continue
        unknown = vocab.unknown_labels(vocab.known_tag_slugs(db, kind), fields.get(key) or [])
        for term in unknown:
            warnings.append("introduces a new {0}: {1!r}".format(label, term))

    if "sources" in fields:
        known = vocab.known_source_slugs(db)
        names = [(c.get("name") or "") for c in (fields.get("sources") or [])]
        for term in vocab.unknown_labels(known, names):
            warnings.append("introduces a new source: {0!r}".format(term))

    known_sections = vocab.known_section_slugs(db)
    for key, label in (("section", "section"), ("subsection", "subsection")):
        value = fields.get(key)
        if key in fields and value and vocab.slugify(value) not in known_sections:
            warnings.append("introduces a new {0}: {1!r}".format(label, value))
    return warnings


def _find_open_submission(db: Session, dedup_key: str) -> Optional[PendingSubmission]:
    return db.scalar(
        select(PendingSubmission)
        .where(
            PendingSubmission.dedup_key == dedup_key,
            PendingSubmission.status.in_(OPEN_SUBMISSION_STATUSES),
        )
        .order_by(PendingSubmission.id.asc())
    )


def _attach_evidence(
    db: Session,
    submission: PendingSubmission,
    *,
    scraper: str,
    scraper_run_id: Optional[str],
    confidence: Optional[float],
    payload: Dict[str, Any],
    citations: List[Dict[str, Any]],
    actor: str,
) -> PendingSubmission:
    """Record a corroborating report against an existing queue item."""
    db.add(
        SubmissionEvidence(
            submission_id=submission.id,
            scraper=scraper,
            scraper_run_id=scraper_run_id,
            confidence=confidence,
            payload=payload,
            source_summary=_source_summary(citations),
        )
    )
    submission.corroboration_count += 1
    submission.last_seen_at = utcnow()
    db.flush()

    audit.log(
        db,
        action="submission.corroborated",
        actor=actor,
        submission_id=submission.id,
        event_id=submission.target_event_id,
        detail={
            "scraper": scraper,
            "scraper_run_id": scraper_run_id,
            "corroboration_count": submission.corroboration_count,
            "sources": _source_summary(citations),
        },
        note="Equivalent report received; attached as corroborating evidence.",
    )
    return submission


def _seed_evidence(
    db: Session, submission: PendingSubmission, payload: Dict[str, Any], citations: List[Dict[str, Any]]
) -> None:
    """The originating report gets an evidence row too, so count == len(evidence)."""
    db.add(
        SubmissionEvidence(
            submission_id=submission.id,
            scraper=submission.scraper,
            scraper_run_id=submission.scraper_run_id,
            confidence=submission.confidence,
            payload=payload,
            source_summary=_source_summary(citations),
        )
    )
    db.flush()


# ---------------------------------------------------------------------------
# Ingest: new events
# ---------------------------------------------------------------------------


def ingest_new_event(
    db: Session, submission: NewEventSubmission, raw_payload: Dict[str, Any]
) -> Tuple[PendingSubmission, str, List[str]]:
    """Queue a proposed new event. Returns (submission, outcome, warnings)."""
    actor = "scraper:{0}".format(submission.scraper)
    fields = events_svc.validate_event_fields(
        {
            "date_text": submission.event.date_text,
            "date_start": submission.event.date_start,
            "date_end": submission.event.date_end,
            "date_precision": submission.event.date_precision,
            "section": submission.event.section,
            "subsection": submission.event.subsection,
            "body": submission.event.body,
            "tags": submission.event.tags,
            "research_categories": submission.event.research_categories,
            "sources": [coerce_citation(s).model_dump(mode="json") for s in submission.event.sources],
        }
    )
    key = (
        dedup.external_dedup_key(submission.external_id)
        if submission.external_id
        else dedup.event_dedup_key(fields["date_start"], fields["body"])
    )
    warnings = _vocabulary_warnings(db, fields)

    # --- already published? ---------------------------------------------------
    published = events_svc.find_matching_event(
        db, external_id=submission.external_id, dedup_key=key
    )
    if published is not None:
        drift = events_svc.diff_fields(events_svc.event_fields(published), fields)
        if drift:
            warnings.append(
                "this matches published event #{0}, but {1} field(s) differ ({2}). "
                "Submit submission_type='edit' targeting that event to propose the "
                "change.".format(published.id, len(drift), ", ".join(sorted(drift)))
            )
        else:
            warnings.append("already published as event #{0}".format(published.id))

        existing = _find_open_submission(db, key)
        if existing is not None:
            _attach_evidence(
                db,
                existing,
                scraper=submission.scraper,
                scraper_run_id=submission.scraper_run_id,
                confidence=submission.confidence,
                payload=raw_payload,
                citations=fields["sources"],
                actor=actor,
            )
            return existing, "duplicate_of_published", warnings

        row = _create_row(
            db,
            submission_type="new",
            status="auto_closed",
            dedup_key=key,
            submission=submission,
            payload=raw_payload,
            proposed=fields,
            diff=None,
            warnings=warnings,
            external_id=submission.external_id,
        )
        row.resulting_event_id = published.id
        row.decided_at = utcnow()
        row.decided_by = "system:ingest"
        row.decision_reason = "Duplicate of published event #{0}".format(published.id)
        db.flush()
        _seed_evidence(db, row, raw_payload, fields["sources"])
        audit.log(
            db,
            action="submission.auto_closed",
            actor=actor,
            submission_id=row.id,
            event_id=published.id,
            detail={"reason": "duplicate_of_published", "dedup_key": key},
            note=row.decision_reason,
        )
        return row, "duplicate_of_published", warnings

    # --- already queued? ------------------------------------------------------
    existing = _find_open_submission(db, key)
    if existing is not None:
        _attach_evidence(
            db,
            existing,
            scraper=submission.scraper,
            scraper_run_id=submission.scraper_run_id,
            confidence=submission.confidence,
            payload=raw_payload,
            citations=fields["sources"],
            actor=actor,
        )
        return existing, "corroborated", warnings

    # --- queue it -------------------------------------------------------------
    row = _create_row(
        db,
        submission_type="new",
        status="pending",
        dedup_key=key,
        submission=submission,
        payload=raw_payload,
        proposed=fields,
        diff=None,
        warnings=warnings,
        external_id=submission.external_id,
    )
    _seed_evidence(db, row, raw_payload, fields["sources"])
    audit.log(
        db,
        action="submission.received",
        actor=actor,
        submission_id=row.id,
        detail={
            "submission_type": "new",
            "dedup_key": key,
            "sources": _source_summary(fields["sources"]),
            "warnings": warnings,
        },
    )
    return row, "queued", warnings


# ---------------------------------------------------------------------------
# Ingest: edits
# ---------------------------------------------------------------------------


def ingest_edit(
    db: Session, submission: EditSubmission, raw_payload: Dict[str, Any]
) -> Tuple[PendingSubmission, str, List[str]]:
    """Queue a proposed edit. Returns (submission, outcome, warnings)."""
    actor = "scraper:{0}".format(submission.scraper)

    target = _resolve_target(db, submission)
    current = events_svc.event_fields(target)

    merged, warnings = events_svc.merge_patch(current, submission.patch)
    try:
        effective_full = events_svc.validate_event_fields(merged)
    except ValidationError as exc:
        raise SubmissionError(
            "the patch is not valid when applied to event #{0}: {1}".format(
                target.id, _first_error(exc)
            ),
            status_code=422,
            target_event_id=target.id,
        )

    # Reduce back to only the fields that actually change something. A patch that
    # restates existing values is a no-op, not a review task.
    changes = events_svc.diff_fields(current, effective_full)
    if not changes:
        return _noop_edit(db, submission, target, raw_payload, actor, warnings)

    effective_patch = {field: change["after"] for field, change in changes.items()}
    key = dedup.edit_dedup_key(target.id, effective_patch)
    warnings = warnings + _vocabulary_warnings(db, effective_patch)

    existing = _find_open_submission(db, key)
    if existing is not None:
        _attach_evidence(
            db,
            existing,
            scraper=submission.scraper,
            scraper_run_id=submission.scraper_run_id,
            confidence=submission.confidence,
            payload=raw_payload,
            citations=effective_patch.get("sources") or [],
            actor=actor,
        )
        return existing, "corroborated", warnings

    row = _create_row(
        db,
        submission_type="edit",
        status="pending",
        dedup_key=key,
        submission=submission,
        payload=raw_payload,
        proposed=effective_patch,
        diff=changes,
        warnings=warnings,
        external_id=None,
        target_event_id=target.id,
        base_version=target.version,
    )
    _seed_evidence(db, row, raw_payload, effective_patch.get("sources") or [])
    audit.log(
        db,
        action="submission.received",
        actor=actor,
        submission_id=row.id,
        event_id=target.id,
        event_version=target.version,
        detail={
            "submission_type": "edit",
            "dedup_key": key,
            "fields": sorted(changes),
            "base_version": target.version,
            "warnings": warnings,
        },
    )
    return row, "queued", warnings


def _resolve_target(db: Session, submission: EditSubmission) -> Event:
    if submission.target_event_id is not None:
        target = events_svc.get_event(db, submission.target_event_id)
        if target is None:
            raise SubmissionError(
                "no published event with id {0}".format(submission.target_event_id),
                status_code=404,
            )
        return target
    target = events_svc.get_event_by_external_id(db, submission.target_external_id or "")
    if target is None:
        raise SubmissionError(
            "no published event with external_id {0!r}".format(submission.target_external_id),
            status_code=404,
        )
    return target


def _noop_edit(
    db: Session,
    submission: EditSubmission,
    target: Event,
    raw_payload: Dict[str, Any],
    actor: str,
    warnings: List[str],
) -> Tuple[PendingSubmission, str, List[str]]:
    """The patch matches what's already published. Record it, don't queue it."""
    warnings = warnings + [
        "event #{0} already has these values; nothing to review".format(target.id)
    ]
    key = dedup.edit_dedup_key(target.id, {"__noop__": target.version})
    existing = _find_open_submission(db, key)
    if existing is not None:
        _attach_evidence(
            db,
            existing,
            scraper=submission.scraper,
            scraper_run_id=submission.scraper_run_id,
            confidence=submission.confidence,
            payload=raw_payload,
            citations=[],
            actor=actor,
        )
        return existing, "duplicate_of_published", warnings

    row = _create_row(
        db,
        submission_type="edit",
        status="auto_closed",
        dedup_key=key,
        submission=submission,
        payload=raw_payload,
        proposed={},
        diff={},
        warnings=warnings,
        external_id=None,
        target_event_id=target.id,
        base_version=target.version,
    )
    row.resulting_event_id = target.id
    row.decided_at = utcnow()
    row.decided_by = "system:ingest"
    row.decision_reason = "No-op: event #{0} already matches the patch".format(target.id)
    db.flush()
    _seed_evidence(db, row, raw_payload, [])
    audit.log(
        db,
        action="submission.auto_closed",
        actor=actor,
        submission_id=row.id,
        event_id=target.id,
        detail={"reason": "noop_edit"},
        note=row.decision_reason,
    )
    return row, "duplicate_of_published", warnings


def _create_row(
    db: Session,
    *,
    submission_type: str,
    status: str,
    dedup_key: str,
    submission: Any,
    payload: Dict[str, Any],
    proposed: Dict[str, Any],
    diff: Optional[Dict[str, Any]],
    warnings: List[str],
    external_id: Optional[str],
    target_event_id: Optional[int] = None,
    base_version: Optional[int] = None,
) -> PendingSubmission:
    # target_event_id has to be set before the INSERT: the
    # ck_pending_submissions_edit_requires_target CHECK fires at flush time, not
    # at commit, so assigning it afterwards is too late.
    row = PendingSubmission(
        submission_type=submission_type,
        status=status,
        dedup_key=dedup_key,
        external_id=external_id,
        target_event_id=target_event_id,
        base_version=base_version,
        scraper=submission.scraper,
        scraper_run_id=submission.scraper_run_id,
        confidence=submission.confidence,
        notes=submission.notes,
        payload=payload,
        proposed=proposed,
        diff=diff,
        warnings=warnings or None,
        corroboration_count=1,
    )
    db.add(row)
    db.flush()
    return row


def _first_error(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ())) or "(root)"
    return "{0}: {1}".format(loc, first.get("msg"))


# ---------------------------------------------------------------------------
# Review reads
# ---------------------------------------------------------------------------


def build_query(
    *,
    status: Optional[str] = "pending",
    submission_type: Optional[str] = None,
    scraper: Optional[str] = None,
    target_event_id: Optional[int] = None,
) -> Select:
    stmt = select(PendingSubmission)
    if status and status != "all":
        stmt = stmt.where(PendingSubmission.status == status)
    if submission_type:
        stmt = stmt.where(PendingSubmission.submission_type == submission_type)
    if scraper:
        stmt = stmt.where(PendingSubmission.scraper == scraper)
    if target_event_id is not None:
        stmt = stmt.where(PendingSubmission.target_event_id == target_event_id)
    return stmt


def counts_by_status(db: Session) -> Dict[str, int]:
    rows = db.execute(
        select(PendingSubmission.status, func.count()).group_by(PendingSubmission.status)
    ).all()
    return {status: count for status, count in rows}


def pending_count(db: Session) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(PendingSubmission).where(
                PendingSubmission.status == "pending"
            )
        )
        or 0
    )


def resolve_diff_for_review(
    db: Session, submission: PendingSubmission
) -> Tuple[List[Dict[str, Any]], bool]:
    """Re-resolve a stored diff against the live event to expose conflicts.

    A pending edit's `before` values were captured at ingest. If the published
    event changed since, approving would overwrite that newer data — so each
    field is compared against the live value and flagged.
    """
    stored = submission.diff or {}
    if not stored:
        return [], False

    live = (
        events_svc.event_fields(submission.target_event)
        if submission.target_event is not None
        else {}
    )
    rows: List[Dict[str, Any]] = []
    has_conflict = False
    for field in sorted(stored):
        change = stored[field]
        before, after = change.get("before"), change.get("after")
        current = live.get(field)
        conflict = bool(live) and not events_svc.values_equal(field, before, current)
        if conflict:
            has_conflict = True
        rows.append(
            {
                "field": field,
                "before": before,
                "after": after,
                "conflict": conflict,
                "current": current if conflict else None,
            }
        )
    if submission.target_event is not None and submission.base_version is not None:
        if submission.target_event.version != submission.base_version:
            # Version moved even if no reviewed field changed — surface it.
            has_conflict = has_conflict or any(r["conflict"] for r in rows)
    return rows, has_conflict


def to_out_dict(
    db: Session, submission: PendingSubmission, *, include_payload: bool = False
) -> Dict[str, Any]:
    """Assemble the review-facing representation of a submission."""
    diff_rows, has_conflict = resolve_diff_for_review(db, submission)
    data: Dict[str, Any] = {
        "id": submission.id,
        "submission_type": submission.submission_type,
        "status": submission.status,
        "dedup_key": submission.dedup_key,
        "external_id": submission.external_id,
        "scraper": submission.scraper,
        "scraper_run_id": submission.scraper_run_id,
        "confidence": submission.confidence,
        "notes": submission.notes,
        "corroboration_count": submission.corroboration_count,
        "submitted_at": submission.submitted_at,
        "last_seen_at": submission.last_seen_at,
        "decided_at": submission.decided_at,
        "decided_by": submission.decided_by,
        "decision_reason": submission.decision_reason,
        "edited_by_reviewer": submission.edited_by_reviewer,
        "warnings": submission.warnings or [],
        "preview": submission.proposed if submission.submission_type == "new" else None,
        "diff": diff_rows,
        "target_event_id": submission.target_event_id,
        "target_event": (
            events_svc.event_out_dict(submission.target_event)
            if submission.target_event is not None
            else None
        ),
        "base_version": submission.base_version,
        "has_conflict": has_conflict,
        "resulting_event_id": submission.resulting_event_id,
        "evidence": [
            {
                "id": e.id,
                "scraper": e.scraper,
                "scraper_run_id": e.scraper_run_id,
                "confidence": e.confidence,
                "source_summary": e.source_summary,
                "received_at": e.received_at,
            }
            for e in submission.evidence
        ],
        "raw_payload": submission.payload if include_payload else None,
    }
    return data


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def _require_pending(submission: PendingSubmission) -> None:
    if submission.status != "pending":
        raise SubmissionError(
            "submission #{0} is already {1} and cannot be decided again".format(
                submission.id, submission.status
            ),
            status_code=409,
        )


def approve(
    db: Session,
    submission: PendingSubmission,
    *,
    actor: str,
    overrides: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Apply a submission to the published dataset.

    `overrides` implements edit-then-approve: the reviewer's values are merged
    over the proposal, and both the proposal and the applied result are recorded.
    """
    _require_pending(submission)

    if submission.submission_type == "new":
        result = _approve_new(db, submission, actor=actor, overrides=overrides)
    else:
        result = _approve_edit(db, submission, actor=actor, overrides=overrides, force=force)

    submission.status = "approved"
    submission.decided_at = utcnow()
    submission.decided_by = actor
    submission.decision_reason = reason
    submission.edited_by_reviewer = bool(overrides)
    submission.applied = result["applied_changes"] or None
    submission.resulting_event_id = result["event_id"]
    db.flush()

    entry = audit.log(
        db,
        action="submission.approved",
        actor=actor,
        submission_id=submission.id,
        event_id=result["event_id"],
        event_version=result["event_version"],
        changes=result["applied_changes"] or None,
        detail={
            "submission_type": submission.submission_type,
            "scraper": submission.scraper,
            "corroboration_count": submission.corroboration_count,
            "reviewer_overrides": overrides or None,
            "forced_over_conflict": bool(force) and submission.submission_type == "edit",
        },
        note=reason,
    )
    result["audit_entry_id"] = entry.id
    return result


def _approve_new(
    db: Session,
    submission: PendingSubmission,
    *,
    actor: str,
    overrides: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    proposed = dict(submission.proposed or {})
    if overrides:
        proposed, _ = events_svc.merge_patch(proposed, overrides)
    try:
        fields = events_svc.validate_event_fields(proposed)
    except ValidationError as exc:
        raise SubmissionError(
            "cannot approve: the resulting event is invalid ({0})".format(_first_error(exc)),
            status_code=422,
        )

    try:
        event = events_svc.create_event(
            db, fields, external_id=submission.external_id, submission_id=submission.id
        )
    except events_svc.ConflictError as exc:
        raise SubmissionError(exc.message, status_code=409, event_id=exc.event_id)

    audit.log(
        db,
        action="event.created",
        actor=actor,
        event_id=event.id,
        submission_id=submission.id,
        event_version=event.version,
        changes=events_svc.full_snapshot_diff(events_svc.event_fields(event)),
        detail={"dedup_key": event.dedup_key, "scraper": submission.scraper},
    )
    return {
        "submission_id": submission.id,
        "status": "approved",
        "event_id": event.id,
        "event_version": event.version,
        "applied_changes": events_svc.full_snapshot_diff(events_svc.event_fields(event)),
    }


def _approve_edit(
    db: Session,
    submission: PendingSubmission,
    *,
    actor: str,
    overrides: Optional[Dict[str, Any]],
    force: bool,
) -> Dict[str, Any]:
    target = submission.target_event
    if target is None or target.status != "published":
        raise SubmissionError(
            "the target event no longer exists or is not published; reject this submission",
            status_code=409,
        )

    diff_rows, has_conflict = resolve_diff_for_review(db, submission)
    if has_conflict and not force:
        conflicted = [r["field"] for r in diff_rows if r["conflict"]]
        raise SubmissionError(
            "event #{0} changed after this edit was submitted (conflicting field(s): {1}). "
            "Re-check the diff, then approve with force=true to apply anyway.".format(
                target.id, ", ".join(conflicted)
            ),
            status_code=409,
            conflicting_fields=conflicted,
        )

    patch = dict(submission.proposed or {})
    if overrides:
        patch.update(events_svc.normalize_fields(overrides))

    merged, _ = events_svc.merge_patch(events_svc.event_fields(target), patch)
    try:
        # Apply the *validated merged* result rather than the raw patch, so the
        # date reconciliation in merge_patch (and date_end defaulting) actually
        # lands. apply_changes diffs it against the live row, so passing a full
        # field dict still only records the fields that really moved.
        effective = events_svc.validate_event_fields(merged)
    except ValidationError as exc:
        raise SubmissionError(
            "cannot approve: the resulting event would be invalid ({0})".format(_first_error(exc)),
            status_code=422,
        )

    before_version = target.version
    try:
        changes = events_svc.apply_changes(db, target, effective)
    except events_svc.ConflictError as exc:
        raise SubmissionError(exc.message, status_code=409, event_id=exc.event_id)

    if changes:
        audit.log(
            db,
            action="event.updated",
            actor=actor,
            event_id=target.id,
            submission_id=submission.id,
            event_version=target.version,
            changes=changes,
            detail={
                "from_version": before_version,
                "scraper": submission.scraper,
                "forced_over_conflict": bool(force and has_conflict),
            },
        )
    return {
        "submission_id": submission.id,
        "status": "approved",
        "event_id": target.id,
        "event_version": target.version,
        "applied_changes": changes,
    }


def reject(
    db: Session, submission: PendingSubmission, *, actor: str, reason: Optional[str] = None
) -> Dict[str, Any]:
    """Archive a submission as rejected. The row is retained for audit."""
    _require_pending(submission)

    submission.status = "rejected"
    submission.decided_at = utcnow()
    submission.decided_by = actor
    submission.decision_reason = reason
    db.flush()

    entry = audit.log(
        db,
        action="submission.rejected",
        actor=actor,
        submission_id=submission.id,
        event_id=submission.target_event_id,
        detail={
            "submission_type": submission.submission_type,
            "scraper": submission.scraper,
            "dedup_key": submission.dedup_key,
            "proposed": submission.proposed,
        },
        note=reason,
    )
    return {
        "submission_id": submission.id,
        "status": "rejected",
        "event_id": None,
        "event_version": None,
        "applied_changes": {},
        "audit_entry_id": entry.id,
    }
