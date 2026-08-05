"""Seeds the database from Russo-Ukrainian_War_Timeline_Dates.json.

This populates the review queue and the visual timeline without running any
live crawls, so the front end can be built and demoed against realistic data.

Routing mirrors what the live pipeline does, so what you see here is what the
scraper will produce:

  date_precision 'day'                     -> DailyPendingEvents, APPROVED
  'month' / 'month-range' / 'season'       -> MonthlyMacroMilestones, APPROVED
  'year' / 'range' / 'approx' / 'undated'  -> DailyPendingEvents, PENDING
                                              with uncertainty notes

Approved rows are what the timeline draws; the imprecise ones land in the
queue so there is something real to review.

Usage:
    python seed_from_json.py            # insert, skipping anything already there
    python seed_from_json.py --reset    # DELETE all rows first, then insert
"""

import argparse
import json
import re
import sys
from pathlib import Path

import database

JSON_PATH = Path(__file__).resolve().parent / "Russo-Ukrainian_War_Timeline_Dates.json"

# The JSON cites organizations, not URLs. Mapping the frequent ones to their
# homepages keeps the source links in the UI real rather than dead anchors.
SOURCE_HOMEPAGES = {
    "CFR": "https://www.cfr.org/global-conflict-tracker/conflict/conflict-ukraine",
    "ISW": "https://understandingwar.org/research/russia-ukraine/",
    "UK Parliament": "https://commonslibrary.parliament.uk/",
    "Britannica": "https://www.britannica.com/event/2022-Russian-invasion-of-Ukraine",
    "CNAS": "https://www.cnas.org/",
    "DOD": "https://www.defense.gov/",
    "Carnegie": "https://carnegieendowment.org/",
    "Irregular Warfare Center": "https://irregularwarfarecenter.org/",
    "GAO-26-107860": "https://www.gao.gov/",
    "CRS IF12040": "https://crsreports.congress.gov/",
}

# Precision values that mean "this date is a best guess", and the note each
# one earns. The queue and timeline both key off these notes.
UNCERTAIN_PRECISION = {
    "year": "uncertain month",
    "year-range": "uncertain month",
    "range": "uncertain month",
    "approx": "uncertain month",
    "undated": "unparseable date",
}
MONTHLY_PRECISION = {"month", "month-range", "season"}


# A sentence ends at a period only when the preceding character is a lowercase
# letter, digit, or closing bracket. Requiring that keeps "U.S." and "Sept."
# intact -- splitting on them produced titles like "U.S" that collided across
# unrelated events.
SENTENCE_END = re.compile(r"(?<=[a-z0-9)\]\"])[.;]\s+(?=[A-Z(])")

MIN_TITLE = 30

# Some entries are one long run-on whose real headline is the clause before a
# colon or dash ("X intensifies: on Oct 7 ..."). Without this the whole
# paragraph becomes the title.
CLAUSE_BREAK = re.compile(r"\s*[:—–]\s+")
LONG_SENTENCE = 140


def make_title(body: str, limit: int = 90) -> str:
    """Derives a short title from an event body.

    The source JSON has no title field, so take the leading clause and trim it
    to something that reads as a headline.
    """
    body = body.strip()
    first = SENTENCE_END.split(body)[0].strip().rstrip(".;")

    # A very short opener ("In 2022") is not a usable title; fall back to the
    # raw body so the trim below has something to work with.
    if len(first) < MIN_TITLE:
        first = body.rstrip(".;")

    # Only break on a clause boundary when the sentence is unwieldy, and only
    # if the leading clause stands on its own.
    if len(first) > LONG_SENTENCE:
        clause = CLAUSE_BREAK.split(first)[0].strip()
        if MIN_TITLE <= len(clause) <= limit:
            return clause

    if len(first) <= limit:
        return first
    return first[:limit].rsplit(" ", 1)[0] + "…"


def source_fields(sources):
    names = ", ".join(sources) if sources else "Unattributed"
    urls = [SOURCE_HOMEPAGES[s] for s in sources if s in SOURCE_HOMEPAGES]
    # Same separator the merge and auto-merge paths use.
    return names[:250], database.SOURCE_URL_SEPARATOR.join(urls)[:1000]


def load_events():
    with open(JSON_PATH, encoding="utf-8") as fh:
        return json.load(fh)["events"]


def reset_tables(conn):
    cursor = conn.cursor()
    try:
        for table in (
            "DailyPendingEvents",
            "MonthlyMacroMilestones",
            "DuplicateReviewQueue",
            "ScrapeWatermarks",
        ):
            cursor.execute(f'DELETE FROM "{table}"')
        conn.commit()
        print("Cleared existing rows from all four tables.")
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def existing_keys(conn):
    """Titles already present, so a re-run does not duplicate the dataset."""
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT title FROM "DailyPendingEvents"')
        daily = {r[0] for r in cursor.fetchall()}
        cursor.execute('SELECT title FROM "MonthlyMacroMilestones"')
        monthly = {r[0] for r in cursor.fetchall()}
        return daily, monthly
    finally:
        cursor.close()


def seed(reset=False):
    database.init_db()
    print(f"Connected via: {database.ACTIVE_DSN_LABEL}")

    conn = database.get_db_connection()
    try:
        if reset:
            reset_tables(conn)
        seen_daily, seen_monthly = existing_keys(conn)
    finally:
        conn.close()

    events = load_events()
    counts = {"approved_daily": 0, "approved_monthly": 0, "pending": 0, "skipped": 0}

    for evt in events:
        body = (evt.get("body") or "").strip()
        date_start = evt.get("date_start")
        precision = evt.get("date_precision") or "undated"
        if not body:
            counts["skipped"] += 1
            continue

        title = make_title(body)
        source_name, source_url = source_fields(evt.get("sources") or [])
        tags = evt.get("tags") or []
        category = tags[0] if tags else "Military"

        if precision in MONTHLY_PRECISION and date_start:
            if title in seen_monthly:
                counts["skipped"] += 1
                continue
            notes = f"Imported: {evt.get('date_text', '')}"[:250]
            database.save_monthly_milestone(
                date_start[:7],
                title,
                body,
                category,
                source_name,
                source_url,
                notes=notes,
                review_status="APPROVED",
            )
            seen_monthly.add(title)
            counts["approved_monthly"] += 1
            continue

        if title in seen_daily:
            counts["skipped"] += 1
            continue

        note_list = []
        uncertainty = UNCERTAIN_PRECISION.get(precision)
        if uncertainty:
            note_list.append(uncertainty)

        if not date_start:
            # Nothing to place it on the axis with; park it in the queue.
            note_list.append("unparseable date")
            event_date = ""
        else:
            event_date = date_start

        note_list.append(f"Imported: {evt.get('date_text', '')}")
        notes = ", ".join(note_list)[:250]

        # Uncertain dates go to the queue for a human; clean ones are approved
        # and become timeline content immediately.
        review_status = "PENDING" if uncertainty else "APPROVED"

        database.save_daily_event(
            event_date,
            title,
            body,
            0.6 if uncertainty else 0.95,
            precision == "day",
            source_name,
            source_url,
            notes,
            review_status=review_status,
        )
        seen_daily.add(title)
        if review_status == "APPROVED":
            counts["approved_daily"] += 1
        else:
            counts["pending"] += 1

    print("\n--- Seed Summary ---")
    print(f"  Approved daily events   : {counts['approved_daily']}")
    print(f"  Approved macro milestones: {counts['approved_monthly']}")
    print(f"  Pending (needs review)  : {counts['pending']}")
    print(f"  Skipped (dupe/empty)    : {counts['skipped']}")
    print("\nOpen http://127.0.0.1:8080/timeline for the visual timeline,")
    print("or http://127.0.0.1:8080/ for the review queue.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DELETE all rows from the event tables before seeding.",
    )
    args = parser.parse_args()

    if args.reset:
        answer = input(
            "This deletes every row in DailyPendingEvents, "
            "MonthlyMacroMilestones, DuplicateReviewQueue and "
            "ScrapeWatermarks. Type 'yes' to continue: "
        )
        if answer.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(1)

    sys.exit(seed(reset=args.reset))
