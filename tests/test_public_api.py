"""Public read API: filtering semantics, and the audit trail's reconstructability."""

from __future__ import annotations

from tests.conftest import new_event_payload


def test_health(client, seeded):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["published_events"] == 3


def test_events_are_ordered_chronologically_with_undated_last(client, seeded):
    events = client.get("/api/v1/events").json()["events"]
    assert [e["date_start"] for e in events] == ["2014-02-01", "2022-02-24", None]


def test_descending_order_still_puts_undated_last(client, seeded):
    events = client.get("/api/v1/events", params={"order": "desc"}).json()["events"]
    assert [e["date_start"] for e in events] == ["2022-02-24", "2014-02-01", None]


def test_narrative_order_keeps_each_section_contiguous(client, ingest_headers, review_headers):
    """Sections are the document's chapters and are not date-contiguous.

    A "2021–2025" summary event belongs to the 2025 chapter but starts in 2021.
    Under strict date order it lands between the 2021 events and splits the
    earlier chapter in two; narrative order keeps each chapter whole.
    """
    from app import db as db_module
    from app.models import Section
    from app.services import vocab

    session = db_module.SessionLocal()
    try:
        early = vocab.ensure_section(session, "Chapter A: 2021", sort_order=0)
        late = vocab.ensure_section(session, "Chapter B: 2025", sort_order=10)
        session.commit()
        assert isinstance(early, Section) and isinstance(late, Section)
    finally:
        session.close()

    specs = [
        ("Chapter A: 2021", "2021-03-01", "First event of the earlier chapter, in March 2021."),
        ("Chapter B: 2025", "2021-01-01", "A five-year cumulative total that starts back in 2021."),
        ("Chapter A: 2021", "2021-09-01", "Second event of the earlier chapter, in September 2021."),
    ]
    for section, start, body in specs:
        sid = client.post(
            "/api/v1/ingest",
            json=new_event_payload(
                scraper="ordering-test",
                event={"section": section, "date_start": start, "date_end": start,
                       "date_precision": "day", "body": body, "sources": ["ISW"]},
            ),
            headers=ingest_headers,
        ).json()["submission_id"]
        assert client.post(
            "/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers
        ).status_code == 200

    narrative = [e["section"] for e in client.get("/api/v1/events").json()["events"]]
    assert narrative == ["Chapter A: 2021", "Chapter A: 2021", "Chapter B: 2025"]

    # Strict date order genuinely interleaves them — this is what the default avoids.
    chronological = [e["section"] for e in
                     client.get("/api/v1/events", params={"order": "asc"}).json()["events"]]
    assert chronological == ["Chapter B: 2025", "Chapter A: 2021", "Chapter A: 2021"]


def test_is_imprecise_flag(client, seeded):
    by_precision = {e["date_precision"]: e["is_imprecise"] for e in client.get("/api/v1/events").json()["events"]}
    assert by_precision["day"] is False
    assert by_precision["month"] is True
    assert by_precision["undated"] is True


def test_tag_filter(client, seeded):
    assert client.get("/api/v1/events", params={"tag": "Diplomacy"}).json()["total"] == 1
    assert client.get("/api/v1/events", params={"tag": "diplomacy"}).json()["total"] == 1, "slug form"
    assert client.get("/api/v1/events", params={"tag": "Nonexistent"}).json()["total"] == 0


def test_tag_mode_any_vs_all(client, seeded):
    params = {"tag": ["Warfare Shift", "Diplomacy"]}
    assert client.get("/api/v1/events", params=dict(params, tag_mode="any")).json()["total"] == 2
    assert client.get("/api/v1/events", params=dict(params, tag_mode="all")).json()["total"] == 0
    assert client.get(
        "/api/v1/events", params={"tag": ["Warfare Shift", "Territory"], "tag_mode": "all"}
    ).json()["total"] == 1


def test_research_category_filter_is_separate_from_tags(client, seeded):
    assert client.get("/api/v1/events", params={"research_category": "Fire/Maneuver"}).json()["total"] == 1
    assert client.get("/api/v1/events", params={"tag": "Fire/Maneuver"}).json()["total"] == 0


def test_section_and_subsection_filters(client, seeded):
    assert client.get("/api/v1/events", params={"section": "Origins: 2013-2022"}).json()["total"] == 1
    assert client.get("/api/v1/events", params={"subsection": "Invasion and early fighting"}).json()["total"] == 1


def test_source_filter(client, seeded):
    assert client.get("/api/v1/events", params={"source": "CFR"}).json()["total"] == 1
    assert client.get("/api/v1/events", params={"source": "ISW"}).json()["total"] == 1


def test_date_window_uses_overlap_so_imprecise_events_surface(client, seeded):
    """A February-2014 month event must match a query for 10 Feb 2014."""
    r = client.get("/api/v1/events", params={"date_from": "2014-02-10", "date_to": "2014-02-10",
                                            "include_undated": "false"})
    assert r.json()["total"] == 0, "month events collapse to the 1st in this dataset"

    r = client.get("/api/v1/events", params={"date_from": "2014-01-01", "date_to": "2014-12-31",
                                            "include_undated": "false"})
    assert r.json()["total"] == 1


def test_range_event_matches_any_overlapping_window(client, ingest_headers, review_headers):
    sid = client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={
            "date_precision": "month-range", "date_start": "2022-03-01", "date_end": "2022-05-31",
            "body": "Fighting for Mariupol continues through the spring of 2022 before the city falls.",
        }),
        headers=ingest_headers,
    ).json()["submission_id"]
    client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)

    for window in [("2022-04-01", "2022-04-02"), ("2022-01-01", "2022-03-05"), ("2022-05-30", "2022-08-01")]:
        r = client.get("/api/v1/events", params={"date_from": window[0], "date_to": window[1]})
        assert r.json()["total"] == 1, window

    r = client.get("/api/v1/events", params={"date_from": "2023-01-01", "date_to": "2023-12-31",
                                            "include_undated": "false"})
    assert r.json()["total"] == 0


def test_include_undated_toggle(client, seeded):
    assert client.get("/api/v1/events", params={"include_undated": "true"}).json()["total"] == 3
    assert client.get("/api/v1/events", params={"include_undated": "false"}).json()["total"] == 2
    # Undated events survive a date window by default rather than vanishing.
    r = client.get("/api/v1/events", params={"date_from": "2014-01-01", "date_to": "2014-12-31"})
    assert r.json()["total"] == 2


def test_date_precision_filter(client, seeded):
    assert client.get("/api/v1/events", params={"date_precision": "day"}).json()["total"] == 1
    assert client.get("/api/v1/events", params={"date_precision": ["day", "month"]}).json()["total"] == 2
    assert client.get("/api/v1/events", params={"date_precision": "decade"}).status_code == 422


def test_text_search(client, seeded):
    assert client.get("/api/v1/events", params={"q": "Yanukovych"}).json()["total"] == 1
    assert client.get("/api/v1/events", params={"q": "yanukovych"}).json()["total"] == 1
    assert client.get("/api/v1/events", params={"q": "zzzz"}).json()["total"] == 0


def test_filters_combine_with_and(client, seeded):
    r = client.get("/api/v1/events", params={"tag": "Diplomacy", "section": "2022: Full-Scale Invasion"})
    assert r.json()["total"] == 0


def test_pagination(client, seeded):
    page = client.get("/api/v1/events", params={"limit": 2, "offset": 0}).json()
    assert page["total"] == 3 and len(page["events"]) == 2
    page2 = client.get("/api/v1/events", params={"limit": 2, "offset": 2}).json()
    assert page2["total"] == 3 and len(page2["events"]) == 1


def test_get_single_event_and_404(client, seeded):
    assert client.get("/api/v1/events/{0}".format(seeded["e1"])).status_code == 200
    assert client.get("/api/v1/events/9999").status_code == 404


def test_meta_powers_the_filter_controls(client, seeded):
    meta = client.get("/api/v1/meta").json()
    assert meta["event_count"] == 3
    assert meta["date_min"] == "2014-02-01"
    assert {s["name"] for s in meta["sections"]} >= {"Origins: 2013-2022", "2022: Full-Scale Invasion"}
    invasion = [s for s in meta["sections"] if s["name"] == "2022: Full-Scale Invasion"][0]
    assert [sub["name"] for sub in invasion["subsections"]] == ["Invasion and early fighting"]
    assert {t["name"] for t in meta["tags"]} == {"Diplomacy", "Warfare Shift", "Territory", "EW/Counter-Drone"}
    assert {c["name"] for c in meta["research_categories"]} == {"Fire/Maneuver"}
    assert dict((t["name"], t["event_count"]) for t in meta["tags"])["Diplomacy"] == 1


def test_meta_counts_exclude_pending(client, seeded, ingest_headers):
    client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers)
    meta = client.get("/api/v1/meta").json()
    assert meta["event_count"] == 3
    assert meta["pending_submission_count"] == 1


# --- audit trail -------------------------------------------------------------


def test_history_records_creation_and_every_edit(client, seeded, ingest_headers, review_headers):
    sid = client.post(
        "/api/v1/ingest",
        json={"submission_type": "edit", "scraper": "date-fixer", "target_event_id": seeded["e1"],
              "patch": {"date_precision": "day", "date_start": "2014-02-22", "date_end": "2014-02-22"}},
        headers=ingest_headers,
    ).json()["submission_id"]
    client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers,
                json={"reason": "Confirmed by CFR."})

    hist = client.get("/api/v1/events/{0}/history".format(seeded["e1"])).json()
    actions = [e["action"] for e in hist["entries"]]
    assert actions[0] == "event.created"
    assert "event.updated" in actions
    assert "submission.approved" in actions

    updated = [e for e in hist["entries"] if e["action"] == "event.updated"][0]
    assert updated["changes"]["date_precision"] == {"before": "month", "after": "day"}
    assert updated["submission_id"] == sid
    assert updated["event_version"] == 2

    approved = [e for e in hist["entries"] if e["action"] == "submission.approved"][0]
    assert approved["actor"] == "reviewer:pytest-reviewer"
    assert approved["note"] == "Confirmed by CFR."


def test_audit_log_reconstructs_current_state(client, seeded, ingest_headers, review_headers):
    """The defensibility guarantee: the log alone reproduces the published row."""
    for patch, scraper in [
        ({"date_precision": "day", "date_start": "2014-02-22", "date_end": "2014-02-22"}, "a"),
        ({"tags": ["Diplomacy", "Territory"]}, "b"),
        ({"body": "Yanukovych flees the country as parliament votes to strip him of office."}, "c"),
    ]:
        sid = client.post(
            "/api/v1/ingest",
            json={"submission_type": "edit", "scraper": scraper,
                  "target_event_id": seeded["e1"], "patch": patch},
            headers=ingest_headers,
        ).json()["submission_id"]
        assert client.post(
            "/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers
        ).status_code == 200

    hist = client.get("/api/v1/events/{0}/history".format(seeded["e1"]), params={"verify": "true"}).json()
    assert hist["current_version"] == 4
    assert hist["verification"]["reconstructs_current_state"] is True
    assert hist["verification"]["mismatched_fields"] == []


def test_rejections_are_in_the_audit_log(client, ingest_headers, review_headers):
    sid = client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers).json()["submission_id"]
    client.post("/api/v1/review/submissions/{0}/reject".format(sid), headers=review_headers,
                json={"reason": "Unreliable source."})

    entries = client.get("/api/v1/changelog", params={"action": "submission.rejected"}).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["note"] == "Unreliable source."
    assert entries[0]["submission_id"] == sid
    # The rejected proposal is preserved in the log, not just in the queue row.
    assert entries[0]["detail"]["proposed"]["body"].startswith("Ukrainian forces withdraw")


def test_corroboration_is_logged(client, ingest_headers):
    client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers)
    client.post("/api/v1/ingest", json=new_event_payload(scraper="second"), headers=ingest_headers)
    entries = client.get("/api/v1/changelog", params={"action": "submission.corroborated"}).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["actor"] == "scraper:second"
    assert entries[0]["detail"]["corroboration_count"] == 2


def test_changelog_is_newest_first(client, seeded, ingest_headers):
    client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers)
    entries = client.get("/api/v1/changelog").json()["entries"]
    assert entries[0]["action"] == "submission.received"


def test_history_for_a_missing_event_is_404(client):
    assert client.get("/api/v1/events/9999/history").status_code == 404


# --- frontend routes ---------------------------------------------------------


def test_frontend_pages_are_served(client):
    for path in ("/", "/review"):
        r = client.get(path)
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/timeline.js").status_code == 200
    assert client.get("/static/review.js").status_code == 200


def test_openapi_schema_documents_the_ingest_contract(client):
    schema = client.get("/openapi.json").json()
    assert "/api/v1/ingest" in schema["paths"]
    components = schema["components"]["schemas"]
    assert "NewEventSubmission" in components
    assert "EditSubmission" in components
