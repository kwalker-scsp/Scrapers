"""One-off maintenance for rows that predate later schema/logic changes.

    python backfill.py embeddings   # compute vectors for rows with none
    python backfill.py titles       # restore titles truncated by the seeder
    python backfill.py all

Both steps are idempotent -- re-running only touches rows still needing work.
"""

import sys

import database
import embedding
import seed_from_json


def backfill_embeddings():
    """Indexes rows written before the embedding column existed."""
    pending = database.fetch_unembedded_daily()
    if not pending:
        print("Embeddings: nothing to do.")
        return 0

    print(f"Embeddings: computing {len(pending)} vectors…")
    for i, row in enumerate(pending, 1):
        vec = embedding.embed_record(row["title"], row["summary"])
        database.set_embedding(row["id"], embedding.serialize(vec))
        if i % 25 == 0:
            print(f"  {i}/{len(pending)}")
    print(f"Embeddings: done ({len(pending)} rows).")
    return len(pending)


def backfill_titles():
    """Restores titles the seeder cut at 90 characters.

    The original seeding pass truncated first sentences with an ellipsis, so
    147 rows read like "Protests erupt in Kyiv after President Viktor Yan…".
    The full sentence is re-derived from the summary, which was never cut.
    """
    conn = database.get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'SELECT id, title, summary FROM "DailyPendingEvents" '
            "WHERE title LIKE '%…' OR title LIKE '%...'"
        )
        daily = cursor.fetchall()
        cursor.execute(
            'SELECT id, title, strategic_summary FROM "MonthlyMacroMilestones" '
            "WHERE title LIKE '%…' OR title LIKE '%...'"
        )
        monthly = cursor.fetchall()

        if not daily and not monthly:
            print("Titles: nothing to do.")
            return 0

        # No character cap this time -- the column holds 500 and a title that
        # gets cut mid-clause defeats the purpose of having one.
        for record_id, _old, body in daily:
            # Clearing the vector marks the row for re-embedding: the vector
            # indexes title+summary, so a changed title invalidates it.
            cursor.execute(
                'UPDATE "DailyPendingEvents" SET title = %s, embedding = %s '
                "WHERE id = %s",
                (seed_from_json.make_title(body or "", limit=480), "", record_id),
            )
        for record_id, _old, body in monthly:
            cursor.execute(
                'UPDATE "MonthlyMacroMilestones" SET title = %s WHERE id = %s',
                (seed_from_json.make_title(body or "", limit=480), record_id),
            )

        conn.commit()
        total = len(daily) + len(monthly)
        print(f"Titles: restored {total} truncated titles.")
        return total
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    database.init_db()
    print(f"Connected via: {database.ACTIVE_DSN_LABEL}\n")

    # Titles first: retitling clears those rows' vectors, and the embedding
    # pass immediately below then recomputes them.
    if what in ("titles", "all"):
        backfill_titles()

    if what in ("embeddings", "all"):
        backfill_embeddings()
