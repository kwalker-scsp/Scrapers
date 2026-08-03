"""Ingest: validation, auth, dedup/corroboration, and the queue's isolation from
the published dataset."""

from __future__ import annotations

from tests.conftest import new_event_payload


# --- auth --------------------------------------------------------------------


def test_ingest_requires_a_key(client):
    assert client.post("/api/v1/ingest", json=new_event_payload()).status_code == 401


def test_ingest_rejects_a_wrong_key(client):
    r = client.post("/api/v1/ingest", json=new_event_payload(), headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_ingest_accepts_a_bearer_token(client):
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(),
        headers={"Authorization": "Bearer test-ingest-key"},
    )
    assert r.status_code == 201


def test_ingest_key_cannot_reach_the_review_api(client, ingest_headers):
    """A leaked scraper key must not be able to publish anything."""
    assert client.get("/api/v1/review/submissions", headers=ingest_headers).status_code == 401
    assert client.post("/api/v1/review/submissions/1/approve", headers=ingest_headers).status_code == 401


# --- validation --------------------------------------------------------------


def test_valid_new_event_is_queued(client, ingest_headers):
    r = client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers)
    assert r.status_code == 201
    body = r.json()
    assert body["outcome"] == "queued"
    assert body["status"] == "pending"
    assert body["corroboration_count"] == 1
    assert len(body["dedup_key"]) == 64


def test_missing_body_is_rejected(client, ingest_headers):
    payload = new_event_payload()
    del payload["event"]["body"]
    r = client.post("/api/v1/ingest", json=payload, headers=ingest_headers)
    assert r.status_code == 422


def test_invalid_date_precision_is_rejected(client, ingest_headers):
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"date_precision": "decade"}),
        headers=ingest_headers,
    )
    assert r.status_code == 422
    assert "date_precision" in r.text


def test_missing_date_start_is_rejected_unless_undated(client, ingest_headers):
    payload = new_event_payload()
    del payload["event"]["date_start"]
    r = client.post("/api/v1/ingest", json=payload, headers=ingest_headers)
    assert r.status_code == 422
    assert "date_start is required" in r.text


def test_undated_event_is_accepted_without_dates(client, ingest_headers):
    payload = new_event_payload(event={"date_precision": "undated"})
    del payload["event"]["date_start"]
    r = client.post("/api/v1/ingest", json=payload, headers=ingest_headers)
    assert r.status_code == 201


def test_undated_event_with_dates_is_rejected(client, ingest_headers):
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"date_precision": "undated"}),
        headers=ingest_headers,
    )
    assert r.status_code == 422


def test_date_end_before_date_start_is_rejected(client, ingest_headers):
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(
            event={"date_precision": "range", "date_start": "2024-05-01", "date_end": "2024-01-01"}
        ),
        headers=ingest_headers,
    )
    assert r.status_code == 422


def test_date_end_defaults_to_date_start(client, ingest_headers, review_headers):
    payload = new_event_payload()
    payload["event"].pop("date_end", None)
    sid = client.post("/api/v1/ingest", json=payload, headers=ingest_headers).json()["submission_id"]
    sub = client.get("/api/v1/review/submissions/{0}".format(sid), headers=review_headers).json()
    assert sub["preview"]["date_end"] == "2024-02-17"


def test_unknown_field_is_rejected(client, ingest_headers):
    """extra='forbid' keeps the contract tight — typos fail loudly."""
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"dat_precision": "day"}),
        headers=ingest_headers,
    )
    assert r.status_code == 422


def test_missing_scraper_is_rejected(client, ingest_headers):
    payload = new_event_payload()
    del payload["scraper"]
    assert client.post("/api/v1/ingest", json=payload, headers=ingest_headers).status_code == 422


def test_bare_string_and_object_sources_are_equivalent(client, ingest_headers, review_headers):
    a = client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"sources": ["ISW"]}),
        headers=ingest_headers,
    ).json()
    sub = client.get("/api/v1/review/submissions/{0}".format(a["submission_id"]), headers=review_headers).json()
    assert sub["preview"]["sources"] == [
        {"name": "ISW", "url": None, "title": None, "quote": None, "accessed_at": None}
    ]


def test_non_http_source_url_is_rejected(client, ingest_headers):
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"sources": [{"name": "ISW", "url": "javascript:alert(1)"}]}),
        headers=ingest_headers,
    )
    assert r.status_code == 422


def test_validation_failure_stores_nothing(client, ingest_headers, review_headers):
    client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"date_precision": "decade"}),
        headers=ingest_headers,
    )
    r = client.get("/api/v1/review/submissions", params={"status": "all"}, headers=review_headers)
    assert r.json()["total"] == 0


# --- dedup / corroboration ---------------------------------------------------


def test_identical_resubmission_corroborates_instead_of_duplicating(client, ingest_headers, review_headers):
    first = client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers).json()
    second = client.post(
        "/api/v1/ingest",
        json=new_event_payload(scraper="other-scraper", scraper_run_id="run-2"),
        headers=ingest_headers,
    )
    assert second.status_code == 200
    body = second.json()
    assert body["outcome"] == "corroborated"
    assert body["submission_id"] == first["submission_id"]
    assert body["corroboration_count"] == 2

    queue = client.get("/api/v1/review/submissions", headers=review_headers).json()
    assert queue["total"] == 1, "corroboration must not create a second queue item"


def test_corroboration_preserves_the_original_proposal(client, ingest_headers, review_headers):
    """The reviewed proposal must be exactly what was first submitted."""
    first = client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers).json()
    sid = first["submission_id"]
    before = client.get("/api/v1/review/submissions/{0}".format(sid), headers=review_headers).json()

    client.post(
        "/api/v1/ingest",
        json=new_event_payload(scraper="other", event={"tags": ["Naval", "Diplomacy"]}),
        headers=ingest_headers,
    )
    after = client.get("/api/v1/review/submissions/{0}".format(sid), headers=review_headers).json()

    assert after["preview"] == before["preview"], "the proposal was rewritten by corroboration"
    assert after["corroboration_count"] == 2
    assert len(after["evidence"]) == 2
    # ...and the second report's own payload is retained for audit.
    scrapers = sorted(e["scraper"] for e in after["evidence"])
    assert scrapers == ["other", "test-scraper"]


def test_corroboration_records_each_reports_sources(client, ingest_headers, review_headers):
    client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers)
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(scraper="cfr-watch", event={"sources": [{"name": "CFR", "url": "https://example.org/cfr"}]}),
        headers=ingest_headers,
    ).json()
    sub = client.get("/api/v1/review/submissions/{0}".format(r["submission_id"]), headers=review_headers).json()
    summaries = " ".join(e["source_summary"] or "" for e in sub["evidence"])
    assert "https://example.org/cfr" in summaries


def test_external_id_takes_precedence_for_matching(client, ingest_headers, review_headers):
    client.post(
        "/api/v1/ingest",
        json=new_event_payload(external_id="isw-42"),
        headers=ingest_headers,
    )
    # Same external_id, different body -> still recognised as the same event.
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(
            external_id="isw-42",
            scraper="other",
            event={"body": "A completely different description of the very same event."},
        ),
        headers=ingest_headers,
    )
    assert r.json()["outcome"] == "corroborated"
    assert client.get("/api/v1/review/submissions", headers=review_headers).json()["total"] == 1


# --- the queue never touches published data ----------------------------------


def test_pending_submissions_are_invisible_to_the_public_api(client, ingest_headers):
    client.post("/api/v1/ingest", json=new_event_payload(), headers=ingest_headers)
    assert client.get("/api/v1/events", params={"q": "Avdiivka"}).json()["total"] == 0
    assert client.get("/api/v1/events").json()["total"] == 0
    assert client.get("/api/v1/health").json()["published_events"] == 0
    assert client.get("/api/v1/health").json()["pending_submissions"] == 1


def test_ingest_does_not_create_vocabulary_terms(client, ingest_headers):
    """A scraper typo must not pollute the public filter lists."""
    client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"tags": ["Definitely Not A Real Tag"]}),
        headers=ingest_headers,
    )
    tags = [t["name"] for t in client.get("/api/v1/meta").json()["tags"]]
    assert "Definitely Not A Real Tag" not in tags


def test_new_vocabulary_terms_are_flagged_as_warnings(client, ingest_headers):
    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"tags": ["Sabotage"]}),
        headers=ingest_headers,
    ).json()
    assert any("Sabotage" in w for w in r["warnings"])


# --- edits -------------------------------------------------------------------


def test_edit_builds_a_before_after_diff(client, seeded, ingest_headers, review_headers):
    r = client.post(
        "/api/v1/ingest",
        json={
            "submission_type": "edit",
            "scraper": "date-refiner",
            "target_event_id": seeded["e1"],
            "patch": {"date_precision": "day", "date_start": "2014-02-22", "date_end": "2014-02-22"},
        },
        headers=ingest_headers,
    )
    assert r.status_code == 201
    sub = client.get(
        "/api/v1/review/submissions/{0}".format(r.json()["submission_id"]), headers=review_headers
    ).json()
    diff = {row["field"]: row for row in sub["diff"]}
    assert diff["date_precision"]["before"] == "month"
    assert diff["date_precision"]["after"] == "day"
    assert diff["date_start"]["before"] == "2014-02-01"
    assert diff["date_start"]["after"] == "2014-02-22"
    assert all(row["conflict"] is False for row in sub["diff"])


def test_edit_diff_excludes_unchanged_fields(client, seeded, ingest_headers, review_headers):
    """A patch that restates an existing value is not a change."""
    r = client.post(
        "/api/v1/ingest",
        json={
            "submission_type": "edit",
            "scraper": "s",
            "target_event_id": seeded["e1"],
            "patch": {"date_precision": "day", "date_text": "February 2014"},
        },
        headers=ingest_headers,
    ).json()
    sub = client.get("/api/v1/review/submissions/{0}".format(r["submission_id"]), headers=review_headers).json()
    assert [row["field"] for row in sub["diff"]] == ["date_precision"]


def test_edit_matching_current_values_is_a_noop(client, seeded, ingest_headers, review_headers):
    r = client.post(
        "/api/v1/ingest",
        json={
            "submission_type": "edit",
            "scraper": "s",
            "target_event_id": seeded["e1"],
            "patch": {"date_precision": "month"},
        },
        headers=ingest_headers,
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "duplicate_of_published"
    assert client.get("/api/v1/review/submissions", headers=review_headers).json()["total"] == 0


def test_edit_to_a_missing_event_is_404(client, ingest_headers):
    r = client.post(
        "/api/v1/ingest",
        json={"submission_type": "edit", "scraper": "s", "target_event_id": 9999, "patch": {"date_precision": "day"}},
        headers=ingest_headers,
    )
    assert r.status_code == 404


def test_edit_without_a_target_is_rejected(client, ingest_headers):
    r = client.post(
        "/api/v1/ingest",
        json={"submission_type": "edit", "scraper": "s", "patch": {"date_precision": "day"}},
        headers=ingest_headers,
    )
    assert r.status_code == 422


def test_empty_patch_is_rejected(client, seeded, ingest_headers):
    r = client.post(
        "/api/v1/ingest",
        json={"submission_type": "edit", "scraper": "s", "target_event_id": seeded["e1"], "patch": {}},
        headers=ingest_headers,
    )
    assert r.status_code == 422


def test_patch_cannot_touch_non_editable_fields(client, seeded, ingest_headers):
    for field in ("id", "dedup_key", "version", "status"):
        r = client.post(
            "/api/v1/ingest",
            json={
                "submission_type": "edit",
                "scraper": "s",
                "target_event_id": seeded["e1"],
                "patch": {field: "x"},
            },
            headers=ingest_headers,
        )
        assert r.status_code == 422, field


def test_patch_contradicting_the_live_event_is_rejected(client, seeded, ingest_headers):
    """`day` precision on an undated event is individually valid but impossible."""
    r = client.post(
        "/api/v1/ingest",
        json={
            "submission_type": "edit",
            "scraper": "s",
            "target_event_id": seeded["e3"],  # the undated one
            "patch": {"date_precision": "day"},
        },
        headers=ingest_headers,
    )
    assert r.status_code == 422
    assert "date_start is required" in r.text


def test_identical_edits_from_two_scrapers_corroborate(client, seeded, ingest_headers, review_headers):
    patch = {"submission_type": "edit", "scraper": "a", "target_event_id": seeded["e1"],
             "patch": {"date_precision": "day", "date_start": "2014-02-22", "date_end": "2014-02-22"}}
    first = client.post("/api/v1/ingest", json=patch, headers=ingest_headers).json()
    second = client.post("/api/v1/ingest", json=dict(patch, scraper="b"), headers=ingest_headers).json()
    assert second["submission_id"] == first["submission_id"]
    assert second["corroboration_count"] == 2
    assert client.get("/api/v1/review/submissions", headers=review_headers).json()["total"] == 1


def test_validation_errors_name_the_right_submission_type(client, seeded, ingest_headers):
    """The union is discriminated on submission_type.

    Without that, a malformed edit comes back complaining that
    NewEventSubmission is missing `event`, which sends whoever is debugging a
    scraper in entirely the wrong direction.
    """
    r = client.post(
        "/api/v1/ingest",
        json={"submission_type": "edit", "scraper": "s", "target_event_id": seeded["e1"],
              "patch": {"date_precision": "decade"}},
        headers=ingest_headers,
    )
    assert r.status_code == 422
    locs = [e["loc"] for e in r.json()["detail"]]
    assert all("NewEventSubmission" not in loc for loc in locs), locs
    assert ["body", "edit", "date_precision"] in locs

    r = client.post(
        "/api/v1/ingest",
        json=new_event_payload(event={"date_precision": "decade"}),
        headers=ingest_headers,
    )
    assert ["body", "new", "event", "date_precision"] in [e["loc"] for e in r.json()["detail"]]


def test_unknown_submission_type_is_reported_clearly(client, ingest_headers):
    r = client.post(
        "/api/v1/ingest", json={"submission_type": "delete", "scraper": "s"}, headers=ingest_headers
    )
    assert r.status_code == 422
    assert "submission_type" in r.text


def test_openapi_publishes_the_discriminator(client):
    """Scraper-side codegen depends on this mapping being present."""
    schema = client.get("/openapi.json").json()
    body = schema["paths"]["/api/v1/ingest"]["post"]["requestBody"]
    ref = body["content"]["application/json"]["schema"]
    assert ref["discriminator"]["propertyName"] == "submission_type"
    assert set(ref["discriminator"]["mapping"]) == {"new", "edit"}


def test_contract_endpoint_describes_the_dedup_algorithm(client):
    contract = client.get("/api/v1/ingest/contract").json()
    assert contract["dedup"]["algorithm_version"] == "v1"
    assert "date_precision_enum" in contract["date_rules"]
    assert "patch_allowed_fields" in contract["submission_types"]["edit"]
