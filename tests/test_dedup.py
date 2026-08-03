"""The dedup key is the system's identity function — scrapers depend on it being
stable and on its normalization tolerating cosmetic rewording."""

from __future__ import annotations

from app import dedup


def test_identical_input_gives_identical_key():
    a = dedup.event_dedup_key("2022-02-24", "Russia launches a full-scale invasion of Ukraine.")
    b = dedup.event_dedup_key("2022-02-24", "Russia launches a full-scale invasion of Ukraine.")
    assert a == b


def test_normalization_ignores_case_punctuation_and_whitespace():
    base = dedup.event_dedup_key("2022-02-24", "Russia launches a full-scale invasion of Ukraine.")
    variants = [
        "russia launches a full scale invasion of ukraine",
        "RUSSIA  launches   a full-scale invasion of Ukraine!!",
        "Russia launches a full–scale invasion of Ukraine",  # en dash
        "\n Russia launches a full-scale invasion of Ukraine. \t",
    ]
    for text in variants:
        assert dedup.event_dedup_key("2022-02-24", text) == base, text


def test_accents_are_folded():
    assert dedup.event_dedup_key("2022-03-01", "Fighting near Zaporizhzhia continues today.") == \
        dedup.event_dedup_key("2022-03-01", "Fighting near Zaporízhzhía continues today.")


def test_trailing_edits_beyond_the_excerpt_do_not_fork_the_event():
    """Appending a clause must not create a second copy of the same event."""
    long_body = (
        "Ukrainian forces withdraw from Avdiivka after a months-long Russian assault, "
        "ceding the town to Russian control after heavy losses on both sides"
    )
    assert dedup.event_dedup_key("2024-02-17", long_body) == dedup.event_dedup_key(
        "2024-02-17", long_body + ", according to later reporting from multiple outlets."
    )


def test_different_date_gives_different_key():
    body = "Russia launches a full-scale invasion of Ukraine."
    assert dedup.event_dedup_key("2022-02-24", body) != dedup.event_dedup_key("2022-02-25", body)


def test_different_opening_text_gives_different_key():
    assert dedup.event_dedup_key("2022-02-24", "Russia launches an invasion.") != \
        dedup.event_dedup_key("2022-02-24", "Ukraine declares martial law.")


def test_undated_events_share_the_undated_bucket_but_not_the_key():
    a = dedup.event_dedup_key(None, "Both sides rely on electronic warfare.")
    b = dedup.event_dedup_key(None, "Drone production scales up dramatically.")
    assert a != b
    assert a == dedup.event_dedup_key("", "Both sides rely on electronic warfare.")


def test_external_id_key_is_namespaced():
    assert dedup.external_dedup_key("isw-2024-02-17") == dedup.external_dedup_key(" isw-2024-02-17 ")
    assert dedup.external_dedup_key("a") != dedup.event_dedup_key(None, "a")


def test_edit_key_ignores_list_order():
    """Tag order carries no meaning, so it must not fork an edit's identity."""
    a = dedup.edit_dedup_key(7, {"tags": ["Naval", "Territory"]})
    b = dedup.edit_dedup_key(7, {"tags": ["Territory", "Naval"]})
    assert a == b


def test_edit_key_is_scoped_to_its_target():
    patch = {"date_precision": "day"}
    assert dedup.edit_dedup_key(7, patch) != dedup.edit_dedup_key(8, patch)


def test_edit_key_changes_with_the_patch():
    assert dedup.edit_dedup_key(7, {"date_precision": "day"}) != \
        dedup.edit_dedup_key(7, {"date_precision": "month"})


def test_key_is_a_sha256_hex_digest():
    key = dedup.event_dedup_key("2022-02-24", "Something happened here today.")
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
