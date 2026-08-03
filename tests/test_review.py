"""Review decisions: approve, edit-then-approve, reject, and conflict handling."""

from __future__ import annotations

from tests.conftest import new_event_payload


def _queue(client, headers, payload=None):
    return client.post(
        "/api/v1/ingest", json=payload or new_event_payload(), headers=headers
    ).json()["submission_id"]


def _edit(client, headers, event_id, patch, scraper="s"):
    return client.post(
        "/api/v1/ingest",
        json={"submission_type": "edit", "scraper": scraper, "target_event_id": event_id, "patch": patch},
        headers=headers,
    ).json()["submission_id"]


# --- auth --------------------------------------------------------------------


def test_review_endpoints_require_the_review_key(client):
    assert client.get("/api/v1/review/submissions").status_code == 401
    assert client.get("/api/v1/review/stats").status_code == 401
    assert client.post("/api/v1/review/submissions/1/approve").status_code == 401
    assert client.post("/api/v1/review/submissions/1/reject").status_code == 401


# --- approving a new event ---------------------------------------------------


def test_approving_a_new_event_publishes_it(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers)
    assert client.get("/api/v1/events").json()["total"] == 0

    r = client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)
    assert r.status_code == 200
    event_id = r.json()["event_id"]

    listing = client.get("/api/v1/events").json()
    assert listing["total"] == 1
    event = client.get("/api/v1/events/{0}".format(event_id)).json()
    assert event["date_text"] == "17 February 2024"
    assert event["tags"] == ["Territory"]
    assert event["research_categories"] == ["Fire/Maneuver"]
    assert event["sources"][0]["url"] == "https://example.org/isw/avdiivka"
    assert event["version"] == 1
    assert event["is_imprecise"] is False


def test_approval_creates_the_vocabulary_terms(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers, new_event_payload(event={"tags": ["Sabotage"]}))
    client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)
    assert "Sabotage" in [t["name"] for t in client.get("/api/v1/meta").json()["tags"]]


def test_approving_twice_is_rejected(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers)
    client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)
    r = client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)
    assert r.status_code == 409
    assert client.get("/api/v1/events").json()["total"] == 1


def test_resubmitting_an_approved_event_reports_it_as_published(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers)
    event_id = client.post(
        "/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers
    ).json()["event_id"]

    r = client.post("/api/v1/ingest", json=new_event_payload(scraper="again"), headers=ingest_headers)
    assert r.status_code == 200
    assert r.json()["outcome"] == "duplicate_of_published"
    assert r.json()["event_id"] == event_id
    assert client.get("/api/v1/events").json()["total"] == 1


# --- edit-then-approve -------------------------------------------------------


def test_edit_then_approve_applies_the_reviewers_values(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers, new_event_payload(event={"tags": ["Territory", "Bogus Tag"]}))
    r = client.post(
        "/api/v1/review/submissions/{0}/approve".format(sid),
        headers=review_headers,
        json={"overrides": {"tags": ["Territory"], "body": "A corrected description of the withdrawal from Avdiivka."},
              "reason": "Dropped a junk tag."},
    )
    assert r.status_code == 200
    event = client.get("/api/v1/events/{0}".format(r.json()["event_id"])).json()
    assert event["tags"] == ["Territory"]
    assert event["body"] == "A corrected description of the withdrawal from Avdiivka."
    assert "Bogus Tag" not in [t["name"] for t in client.get("/api/v1/meta").json()["tags"]]


def test_reviewer_edits_are_recorded_without_rewriting_the_proposal(client, ingest_headers, review_headers):
    """Audit integrity: the scraper's original proposal must survive intact."""
    sid = _queue(client, ingest_headers, new_event_payload(event={"tags": ["Territory", "Bogus Tag"]}))
    client.post(
        "/api/v1/review/submissions/{0}/approve".format(sid),
        headers=review_headers,
        json={"overrides": {"tags": ["Territory"]}},
    )
    sub = client.get("/api/v1/review/submissions/{0}".format(sid), headers=review_headers).json()
    assert sub["edited_by_reviewer"] is True
    assert sub["preview"]["tags"] == ["Territory", "Bogus Tag"], "the proposal was overwritten"

    audit = client.get("/api/v1/review/audit", params={"submission_id": sid}, headers=review_headers).json()
    approved = [e for e in audit["entries"] if e["action"] == "submission.approved"][0]
    assert approved["detail"]["reviewer_overrides"] == {"tags": ["Territory"]}


def test_overrides_are_validated(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers)
    r = client.post(
        "/api/v1/review/submissions/{0}/approve".format(sid),
        headers=review_headers,
        json={"overrides": {"date_precision": "decade"}},
    )
    assert r.status_code == 422
    assert client.get("/api/v1/events").json()["total"] == 0


def test_overrides_cannot_touch_non_editable_fields(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers)
    r = client.post(
        "/api/v1/review/submissions/{0}/approve".format(sid),
        headers=review_headers,
        json={"overrides": {"status": "retracted"}},
    )
    assert r.status_code == 422


# --- approving an edit -------------------------------------------------------


def test_approving_an_edit_applies_it_and_bumps_the_version(client, seeded, ingest_headers, review_headers):
    sid = _edit(client, ingest_headers, seeded["e1"],
                {"date_precision": "day", "date_start": "2014-02-22", "date_end": "2014-02-22"})
    r = client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)
    assert r.status_code == 200
    assert r.json()["event_version"] == 2

    event = client.get("/api/v1/events/{0}".format(seeded["e1"])).json()
    assert event["date_precision"] == "day"
    assert event["date_start"] == "2014-02-22"
    assert event["version"] == 2
    assert event["is_imprecise"] is False


def test_approving_an_edit_leaves_untouched_fields_alone(client, seeded, ingest_headers, review_headers):
    before = client.get("/api/v1/events/{0}".format(seeded["e2"])).json()
    sid = _edit(client, ingest_headers, seeded["e2"], {"tags": ["Warfare Shift", "Territory", "Naval"]})
    client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)
    after = client.get("/api/v1/events/{0}".format(seeded["e2"])).json()
    assert after["body"] == before["body"]
    assert after["date_start"] == before["date_start"]
    assert sorted(after["tags"]) == ["Naval", "Territory", "Warfare Shift"]
    assert after["research_categories"] == before["research_categories"]


def test_editing_the_body_recomputes_the_dedup_key(client, seeded, ingest_headers, review_headers):
    before = client.get("/api/v1/events/{0}".format(seeded["e1"])).json()
    sid = _edit(client, ingest_headers, seeded["e1"],
                {"body": "Viktor Yanukovych flees the country as parliament votes to remove him."})
    client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)
    after = client.get("/api/v1/events/{0}".format(seeded["e1"])).json()
    assert after["dedup_key"] != before["dedup_key"]


def test_an_edit_that_would_duplicate_another_event_is_blocked(client, seeded, ingest_headers, review_headers):
    """Two events must never converge onto the same identity."""
    other = client.get("/api/v1/events/{0}".format(seeded["e2"])).json()
    sid = _edit(client, ingest_headers, seeded["e1"],
                {"body": other["body"], "date_start": other["date_start"], "date_end": other["date_end"],
                 "date_precision": "day"})
    r = client.post("/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers)
    assert r.status_code == 409
    assert "identical to published event" in r.json()["detail"]


# --- stale edits / conflicts -------------------------------------------------


def test_a_stale_edit_is_flagged_and_blocked(client, seeded, ingest_headers, review_headers):
    a = _edit(client, ingest_headers, seeded["e1"], {"date_text": "22 February 2014"}, scraper="a")
    b = _edit(client, ingest_headers, seeded["e1"], {"date_text": "21 February 2014"}, scraper="b")

    assert client.post("/api/v1/review/submissions/{0}/approve".format(a), headers=review_headers).status_code == 200

    sub_b = client.get("/api/v1/review/submissions/{0}".format(b), headers=review_headers).json()
    assert sub_b["has_conflict"] is True
    row = [d for d in sub_b["diff"] if d["field"] == "date_text"][0]
    assert row["conflict"] is True
    assert row["current"] == "22 February 2014"

    r = client.post("/api/v1/review/submissions/{0}/approve".format(b), headers=review_headers)
    assert r.status_code == 409
    assert "changed after this edit was submitted" in r.json()["detail"]
    assert client.get("/api/v1/events/{0}".format(seeded["e1"])).json()["date_text"] == "22 February 2014"


def test_a_stale_edit_can_be_force_approved(client, seeded, ingest_headers, review_headers):
    a = _edit(client, ingest_headers, seeded["e1"], {"date_text": "22 February 2014"}, scraper="a")
    b = _edit(client, ingest_headers, seeded["e1"], {"date_text": "21 February 2014"}, scraper="b")
    client.post("/api/v1/review/submissions/{0}/approve".format(a), headers=review_headers)

    r = client.post(
        "/api/v1/review/submissions/{0}/approve".format(b),
        headers=review_headers,
        json={"force": True, "reason": "b's date is better sourced."},
    )
    assert r.status_code == 200
    assert client.get("/api/v1/events/{0}".format(seeded["e1"])).json()["date_text"] == "21 February 2014"

    audit = client.get("/api/v1/review/audit", params={"submission_id": b}, headers=review_headers).json()
    approved = [e for e in audit["entries"] if e["action"] == "submission.approved"][0]
    assert approved["detail"]["forced_over_conflict"] is True


# --- rejecting ---------------------------------------------------------------


def test_rejecting_archives_with_a_reason_and_does_not_delete(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers)
    r = client.post(
        "/api/v1/review/submissions/{0}/reject".format(sid),
        headers=review_headers,
        json={"reason": "Single low-quality source."},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert client.get("/api/v1/events").json()["total"] == 0

    sub = client.get("/api/v1/review/submissions/{0}".format(sid), headers=review_headers).json()
    assert sub["status"] == "rejected"
    assert sub["decision_reason"] == "Single low-quality source."
    assert sub["decided_by"] == "reviewer:pytest-reviewer"
    assert sub["preview"] is not None, "the proposal must be retained for audit"
    assert len(sub["evidence"]) == 1

    assert client.get(
        "/api/v1/review/submissions", params={"status": "rejected"}, headers=review_headers
    ).json()["total"] == 1


def test_rejecting_without_a_reason_is_allowed(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers)
    r = client.post("/api/v1/review/submissions/{0}/reject".format(sid), headers=review_headers)
    assert r.status_code == 200


def test_a_rejected_submission_cannot_be_approved(client, ingest_headers, review_headers):
    sid = _queue(client, ingest_headers)
    client.post("/api/v1/review/submissions/{0}/reject".format(sid), headers=review_headers)
    assert client.post(
        "/api/v1/review/submissions/{0}/approve".format(sid), headers=review_headers
    ).status_code == 409


def test_rejection_frees_the_dedup_key_for_resubmission(client, ingest_headers, review_headers):
    """A rejected item may legitimately come back with better sourcing."""
    sid = _queue(client, ingest_headers)
    client.post("/api/v1/review/submissions/{0}/reject".format(sid), headers=review_headers)
    r = client.post("/api/v1/ingest", json=new_event_payload(scraper="better"), headers=ingest_headers)
    assert r.status_code == 201
    assert r.json()["outcome"] == "queued"
    assert r.json()["submission_id"] != sid


# --- queue listing -----------------------------------------------------------


def test_queue_filters_and_counts(client, seeded, ingest_headers, review_headers):
    _queue(client, ingest_headers)
    _edit(client, ingest_headers, seeded["e1"], {"date_text": "22 February 2014"}, scraper="date-fixer")

    assert client.get("/api/v1/review/submissions", headers=review_headers).json()["total"] == 2
    assert client.get(
        "/api/v1/review/submissions", params={"submission_type": "edit"}, headers=review_headers
    ).json()["total"] == 1
    assert client.get(
        "/api/v1/review/submissions", params={"scraper": "date-fixer"}, headers=review_headers
    ).json()["total"] == 1
    assert client.get(
        "/api/v1/review/submissions", params={"target_event_id": seeded["e1"]}, headers=review_headers
    ).json()["total"] == 1

    stats = client.get("/api/v1/review/stats", headers=review_headers).json()
    assert stats["counts_by_status"]["pending"] == 2
    assert "date-fixer" in stats["by_scraper"]


def test_missing_submission_is_404(client, review_headers):
    assert client.get("/api/v1/review/submissions/9999", headers=review_headers).status_code == 404
