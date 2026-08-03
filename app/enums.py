"""Controlled vocabularies shared by the ORM models and the API schemas.

These are stored as VARCHAR + CHECK constraint rather than a native DB enum: it
works identically on SQLite and Postgres, and adding a value later is a data
migration instead of an `ALTER TYPE`.
"""

from __future__ import annotations

from typing import Tuple

# --- Date precision -----------------------------------------------------------
# Exactly the nine values present in the seed dataset.
DATE_PRECISIONS: Tuple[str, ...] = (
    "day",  # a specific calendar day
    "month",  # a named month
    "month-range",  # e.g. "March–May 2022"
    "season",  # e.g. "summer 2023"
    "year",  # a calendar year
    "year-range",  # e.g. "2014–2022"
    "range",  # an explicit start/end span that isn't month/year aligned
    "approx",  # a best-effort anchor date; treat as soft
    "undated",  # no date could be assigned
)

#: Precisions that describe a span rather than a point. The UI draws these
#: differently and the read API can exclude them from strict day queries.
IMPRECISE_PRECISIONS: Tuple[str, ...] = (
    "month",
    "month-range",
    "season",
    "year",
    "year-range",
    "range",
    "approx",
    "undated",
)

# --- Event lifecycle ----------------------------------------------------------
EVENT_STATUSES: Tuple[str, ...] = (
    "published",  # visible in the public read API and timeline
    "retracted",  # withdrawn but retained for audit; never served publicly
)

# --- Submission queue ---------------------------------------------------------
SUBMISSION_TYPES: Tuple[str, ...] = ("new", "edit")

SUBMISSION_STATUSES: Tuple[str, ...] = (
    "pending",  # awaiting review
    "approved",  # applied to the published dataset
    "rejected",  # declined; archived with a reason, never deleted
    "auto_closed",  # ingest determined it duplicates an already-published event
)

#: Statuses that occupy the dedup namespace. A new submission whose dedup key
#: matches a submission in one of these states is corroboration, not a new item.
OPEN_SUBMISSION_STATUSES: Tuple[str, ...] = ("pending", "auto_closed")

# --- Tag vocabularies ---------------------------------------------------------
# `tags` and `research_categories` in the source JSON are two separate
# vocabularies; one table with a `kind` discriminator keeps the join simple.
TAG_KINDS: Tuple[str, ...] = ("tag", "research_category")

# --- Audit actions ------------------------------------------------------------
AUDIT_ACTIONS: Tuple[str, ...] = (
    "seed.import",
    "submission.received",
    "submission.corroborated",
    "submission.auto_closed",
    "submission.approved",
    "submission.rejected",
    "event.created",
    "event.updated",
    "event.retracted",
)

#: Event fields a scraper may propose changes to.
EDITABLE_EVENT_FIELDS: Tuple[str, ...] = (
    "date_text",
    "date_start",
    "date_end",
    "date_precision",
    "section",
    "subsection",
    "body",
    "tags",
    "research_categories",
    "sources",
)
