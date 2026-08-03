"""Scraper-facing ingest endpoint.

The only write path available to a scraper, and it can only reach
`pending_submissions`. Validation happens in three layers before anything is
stored:

1. Pydantic (`app.schemas`) — required fields, types, enum membership, the
   date_precision/date_start cross-rules. Failures return 422 with a field path
   and never touch the database.
2. Semantic (`app.services.submissions`) — for edits, the patch is merged with
   the live event and re-validated, so a patch that is individually well-formed
   but contradictory in context (e.g. precision 'day' on an undated event) is
   also rejected at 422.
3. Dedup — the content/patch hash is checked against published events and open
   submissions, so a re-run corroborates rather than duplicating.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app import dedup
from app.db import get_db
from app.enums import DATE_PRECISIONS, EDITABLE_EVENT_FIELDS
from app.schemas import EditSubmission, IngestResult, NewEventSubmission, SubmissionIn
from app.security import require_ingest
from app.services import submissions as submissions_svc

router = APIRouter(prefix="/api/v1", tags=["ingest"])


async def raw_json_body(request: Request) -> Dict[str, Any]:
    """The request body exactly as sent, for the audit record.

    An async dependency on a sync endpoint: Starlette caches the body after
    FastAPI reads it for validation, so this re-read is free and cannot consume
    the stream out from under the parser.
    """
    try:
        body = await request.json()
    except Exception:  # pragma: no cover - unparseable bodies 422 before this runs
        return {}
    return body if isinstance(body, dict) else {"_raw": body}


@router.post(
    "/ingest",
    response_model=IngestResult,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a candidate event or edit (scrapers)",
    responses={
        200: {"description": "Accepted but not queued: corroborated, or already published"},
        201: {"description": "Queued as a new pending submission"},
        401: {"description": "Missing or invalid API key"},
        404: {"description": "Edit targets an event that doesn't exist"},
        422: {"description": "Payload failed validation; nothing was stored"},
    },
)
def ingest(
    response: Response,
    payload: SubmissionIn = Body(
        ...,
        # Without this, FastAPI validates against both union members and reports
        # errors blaming the wrong one — a bad `edit` payload would come back
        # complaining that NewEventSubmission is missing `event`.
        discriminator="submission_type",
        openapi_examples={
            "new_event": {
                "summary": "Propose a new event",
                "value": {
                    "submission_type": "new",
                    "scraper": "isw-daily",
                    "scraper_run_id": "2026-07-30T06:00Z",
                    "confidence": 0.85,
                    "event": {
                        "date_text": "17 February 2024",
                        "date_start": "2024-02-17",
                        "date_precision": "day",
                        "section": "2024: Avdiivka Falls, Ukraine Strikes Kursk",
                        "body": "Ukrainian forces withdraw from Avdiivka after a months-long "
                        "Russian assault, ceding the town to Russian control.",
                        "tags": ["Territory", "Warfare Shift"],
                        "research_categories": ["Fire/Maneuver"],
                        "sources": [
                            {
                                "name": "ISW",
                                "url": "https://example.org/isw/2024-02-17",
                                "title": "Russian Offensive Campaign Assessment",
                                "accessed_at": "2026-07-30",
                            }
                        ],
                    },
                },
            },
            "edit_event": {
                "summary": "Propose an edit (send only changed fields)",
                "value": {
                    "submission_type": "edit",
                    "scraper": "date-refiner",
                    "target_event_id": 1,
                    "notes": "CFR gives an exact date for the crackdown.",
                    "patch": {
                        "date_start": "2013-11-21",
                        "date_end": "2013-11-21",
                        "date_precision": "day",
                        "date_text": "21 November 2013",
                    },
                },
            },
        },
    ),
    raw_payload: Dict[str, Any] = Depends(raw_json_body),
    db: Session = Depends(get_db),
    actor: str = Depends(require_ingest),
) -> IngestResult:
    """Queue a candidate contribution for human review.

    Outcomes (see `IngestResult.outcome`):
      * `queued` (201) — a new pending item was created.
      * `corroborated` (200) — an equivalent item was already open; this report
        was attached to it as supporting evidence and the count bumped.
      * `duplicate_of_published` (200) — already in the published dataset, or a
        no-op edit. Recorded for audit, but there is nothing to review.

    Ingest is effectively idempotent: re-posting the same payload will never
    create a second queue item or a duplicate event.
    """
    # `raw_payload` is the body exactly as received (audit ground truth); the
    # normalized proposal is derived separately inside the service.
    try:
        if isinstance(payload, NewEventSubmission):
            row, outcome, warnings = submissions_svc.ingest_new_event(db, payload, raw_payload)
        else:
            assert isinstance(payload, EditSubmission)
            row, outcome, warnings = submissions_svc.ingest_edit(db, payload, raw_payload)
    except submissions_svc.SubmissionError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    db.commit()

    if outcome != "queued":
        response.status_code = status.HTTP_200_OK

    return IngestResult(
        outcome=outcome,
        submission_id=row.id,
        status=row.status,
        dedup_key=row.dedup_key,
        corroboration_count=row.corroboration_count,
        event_id=row.resulting_event_id,
        warnings=warnings,
    )


@router.get(
    "/ingest/contract",
    summary="Machine-readable ingest contract (scrapers)",
    tags=["ingest"],
)
def ingest_contract() -> dict:
    """Self-describing contract, so a scraper can assert compatibility at startup.

    Full JSON Schema for the request bodies lives at `/openapi.json`; this is the
    short version plus the dedup algorithm, which a scraper needs in order to
    check "have I already submitted this?" without a network round trip.
    """
    return {
        "endpoint": "POST /api/v1/ingest",
        "auth": {
            "header": "X-API-Key",
            "alternative": "Authorization: Bearer <key>",
            "env_var": "INGEST_API_KEY",
            "optional_headers": {
                "X-Scraper-Agent": "free-text label recorded in the audit log"
            },
        },
        "submission_types": {
            "new": {
                "required": ["submission_type", "scraper", "event"],
                "optional": ["scraper_run_id", "confidence", "notes", "external_id"],
                "event_required": ["date_precision", "body"],
                "event_optional": [
                    "date_text",
                    "date_start",
                    "date_end",
                    "section",
                    "subsection",
                    "tags",
                    "research_categories",
                    "sources",
                ],
            },
            "edit": {
                "required": ["submission_type", "scraper", "patch", "target_event_id"],
                "note": "target_external_id may be used instead of target_event_id",
                "optional": ["scraper_run_id", "confidence", "notes"],
                "patch_allowed_fields": list(EDITABLE_EVENT_FIELDS),
                "patch_semantics": (
                    "Send only the fields you want to change. The server reads the "
                    "current published values and stores a field-level before/after "
                    "diff. An explicit null clears a nullable field."
                ),
            },
        },
        "date_rules": {
            "date_precision_enum": list(DATE_PRECISIONS),
            "date_start_required_unless": "date_precision == 'undated'",
            "date_end": "defaults to date_start; must not precede it",
            "format": "ISO 8601 date, YYYY-MM-DD",
        },
        "sources": {
            "accepts": ["bare string, e.g. \"ISW\"", "object with name/url/title/quote/accessed_at"],
            "note": "url must be http(s). Bare strings and {'name': ...} are equivalent.",
        },
        "dedup": {
            "algorithm_version": dedup.DEDUP_ALGORITHM_VERSION,
            "new_event_key": (
                "sha256('{v}|' + (date_start ISO or 'undated') + '|' + normalized_body_excerpt)"
            ).format(v=dedup.DEDUP_ALGORITHM_VERSION),
            "normalization": (
                "NFKD decompose, drop combining marks, lowercase, replace runs of "
                "non-alphanumerics with a single space, strip, then truncate to "
                "{n} characters".format(n=dedup.BODY_EXCERPT_CHARS)
            ),
            "external_id_key": "sha256('{v}|external|' + external_id.strip())".format(
                v=dedup.DEDUP_ALGORITHM_VERSION
            ),
            "edit_key": (
                "sha256('{v}|edit|' + target_event_id + '|' + canonical_json(effective_patch)) "
                "— note this uses the *effective* patch (fields that actually change), "
                "so it is computed server-side"
            ).format(v=dedup.DEDUP_ALGORITHM_VERSION),
            "duplicate_behaviour": (
                "A key matching an open submission attaches your report as "
                "corroborating evidence and bumps corroboration_count. A key "
                "matching a published event returns duplicate_of_published. Neither "
                "creates a duplicate."
            ),
        },
        "outcomes": {
            "201": "queued — new pending submission",
            "200": "corroborated or duplicate_of_published — nothing new to review",
            "401": "bad or missing API key",
            "404": "edit target not found",
            "422": "validation failed; nothing stored",
        },
        "guarantees": [
            "Ingest never writes to the published events table.",
            "Submissions are invisible to the public read API until approved.",
            "Re-posting an identical payload is safe and creates no duplicates.",
        ],
    }
