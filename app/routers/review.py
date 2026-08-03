"""Reviewer-facing endpoints. Gated behind REVIEW_API_KEY.

This is the only route by which anything reaches the published `events` table.
Each decision is a single transaction covering the data change and its audit
entry, so the log can never disagree with the dataset.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import SUBMISSION_STATUSES, SUBMISSION_TYPES
from app.models import Event, PendingSubmission
from app.schemas import (
    ApproveRequest,
    AuditListOut,
    DecisionResult,
    RejectRequest,
    SubmissionListOut,
    SubmissionOut,
)
from app.security import require_reviewer
from app.services import audit as audit_svc
from app.services import submissions as submissions_svc

router = APIRouter(prefix="/api/v1/review", tags=["review"])


def _load(db: Session, submission_id: int) -> PendingSubmission:
    row = db.get(PendingSubmission, submission_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No submission with id {0}".format(submission_id))
    return row


@router.get("/submissions", response_model=SubmissionListOut, summary="List submissions")
def list_submissions(
    db: Session = Depends(get_db),
    actor: str = Depends(require_reviewer),
    status_filter: str = Query(
        "pending",
        alias="status",
        description="One of: " + ", ".join(SUBMISSION_STATUSES) + ", or 'all'",
    ),
    submission_type: Optional[str] = Query(None, description="'new' or 'edit'"),
    scraper: Optional[str] = Query(None, description="Filter to one scraper"),
    target_event_id: Optional[int] = Query(None, description="Edits targeting this event"),
    order: str = Query(
        "oldest",
        pattern="^(oldest|newest|corroboration)$",
        description="oldest first (review order), newest first, or most-corroborated first",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> SubmissionListOut:
    """The review queue. Defaults to pending, oldest first."""
    if status_filter != "all" and status_filter not in SUBMISSION_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="unknown status {0!r}. Valid: {1}, all".format(
                status_filter, ", ".join(SUBMISSION_STATUSES)
            ),
        )
    if submission_type and submission_type not in SUBMISSION_TYPES:
        raise HTTPException(
            status_code=422,
            detail="unknown submission_type {0!r}. Valid: {1}".format(
                submission_type, ", ".join(SUBMISSION_TYPES)
            ),
        )

    stmt = submissions_svc.build_query(
        status=status_filter,
        submission_type=submission_type,
        scraper=scraper,
        target_event_id=target_event_id,
    )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    if order == "newest":
        stmt = stmt.order_by(PendingSubmission.submitted_at.desc(), PendingSubmission.id.desc())
    elif order == "corroboration":
        stmt = stmt.order_by(
            PendingSubmission.corroboration_count.desc(), PendingSubmission.id.asc()
        )
    else:
        stmt = stmt.order_by(PendingSubmission.submitted_at.asc(), PendingSubmission.id.asc())

    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return SubmissionListOut(
        total=total,
        limit=limit,
        offset=offset,
        counts_by_status=submissions_svc.counts_by_status(db),
        submissions=[
            SubmissionOut.model_validate(submissions_svc.to_out_dict(db, r)) for r in rows
        ],
    )


@router.get(
    "/submissions/{submission_id}",
    response_model=SubmissionOut,
    summary="One submission, with the raw payload",
)
def get_submission(
    submission_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(require_reviewer),
) -> SubmissionOut:
    row = _load(db, submission_id)
    return SubmissionOut.model_validate(
        submissions_svc.to_out_dict(db, row, include_payload=True)
    )


@router.post(
    "/submissions/{submission_id}/approve",
    response_model=DecisionResult,
    summary="Approve, or edit-then-approve",
    responses={
        409: {
            "description": "Already decided, or the target event changed since "
            "submission (retry with force=true), or approving would duplicate an "
            "existing event"
        },
        422: {"description": "The resulting event would be invalid"},
    },
)
def approve_submission(
    submission_id: int,
    body: Optional[ApproveRequest] = None,
    db: Session = Depends(get_db),
    actor: str = Depends(require_reviewer),
) -> DecisionResult:
    """Apply a submission to the published dataset.

    * `submission_type='new'` inserts a new published event.
    * `submission_type='edit'` applies the diff to the target event and bumps its
      version.

    Supply `overrides` to correct values before publishing (edit-then-approve);
    the original proposal is preserved and the audit entry records both.

    A stale edit — one whose target changed after submission — returns 409 rather
    than silently overwriting newer data. Re-send with `force=true` once you've
    read the conflict.
    """
    body = body or ApproveRequest()
    row = _load(db, submission_id)
    try:
        result = submissions_svc.approve(
            db,
            row,
            actor=actor,
            overrides=body.overrides,
            reason=body.reason,
            force=body.force,
        )
        db.commit()
    except submissions_svc.SubmissionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return DecisionResult(**result)


@router.post(
    "/submissions/{submission_id}/reject",
    response_model=DecisionResult,
    summary="Reject and archive (never deletes)",
)
def reject_submission(
    submission_id: int,
    body: Optional[RejectRequest] = None,
    db: Session = Depends(get_db),
    actor: str = Depends(require_reviewer),
) -> DecisionResult:
    """Archive a submission as rejected, with an optional reason.

    The row and its evidence are retained; only the status changes. Rejected
    items stay queryable at `?status=rejected`.
    """
    body = body or RejectRequest()
    row = _load(db, submission_id)
    try:
        result = submissions_svc.reject(db, row, actor=actor, reason=body.reason)
        db.commit()
    except submissions_svc.SubmissionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return DecisionResult(**result)


@router.get("/stats", summary="Queue health")
def stats(db: Session = Depends(get_db), actor: str = Depends(require_reviewer)) -> dict:
    """Counts for the dashboard header, plus per-scraper submission volume."""
    by_scraper = db.execute(
        select(
            PendingSubmission.scraper,
            PendingSubmission.status,
            func.count(),
        ).group_by(PendingSubmission.scraper, PendingSubmission.status)
    ).all()

    scrapers: dict = {}
    for scraper, sub_status, count in by_scraper:
        scrapers.setdefault(scraper, {})[sub_status] = count

    return {
        "counts_by_status": submissions_svc.counts_by_status(db),
        "by_scraper": scrapers,
        "published_events": db.scalar(
            select(func.count()).select_from(Event).where(Event.status == "published")
        )
        or 0,
        "audit_entries": db.scalar(select(func.count()).select_from(audit_svc.AuditLogEntry)) or 0,
        "pending_with_conflicts": sum(
            1
            for row in db.scalars(
                select(PendingSubmission).where(PendingSubmission.status == "pending")
            ).all()
            if submissions_svc.resolve_diff_for_review(db, row)[1]
        ),
    }


@router.get("/audit", response_model=AuditListOut, summary="Full audit log")
def review_audit(
    db: Session = Depends(get_db),
    actor: str = Depends(require_reviewer),
    submission_id: Optional[int] = Query(None),
    event_id: Optional[int] = Query(None),
    action: Optional[str] = Query(None),
    actor_filter: Optional[str] = Query(None, alias="actor"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> AuditListOut:
    """Same underlying log as the public changelog, filterable by submission."""
    stmt = audit_svc.build_query(
        event_id=event_id, submission_id=submission_id, action=action, actor=actor_filter
    )
    total = audit_svc.count(db, stmt)
    return AuditListOut(
        total=total,
        limit=limit,
        offset=offset,
        entries=audit_svc.page(db, stmt, limit=limit, offset=offset),
    )
