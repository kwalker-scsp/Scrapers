"""Natural / dedup keys.

The dedup key is the answer to "have I seen this real-world event before?" It is
a content hash, so it is stable across scraper runs and across scrapers, without
requiring any coordination between them.

    dedup_key = sha256("v1|<date_bucket>|<normalized body excerpt>")

`date_bucket` is date_start (ISO), or the literal "undated" when there is no
start date. The body excerpt is aggressively normalized (case-folded, accents
stripped, punctuation removed, whitespace collapsed) and then truncated, so
cosmetic rewording of the tail of a description does not create a new event.

This function is intentionally simple and fully documented, because scrapers
need to be able to reproduce it locally to check "did I already submit this?"
before making a network call. The exact algorithm is versioned by the "v1"
prefix; if it ever changes, old keys stay resolvable under their old prefix.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Any, Dict, Mapping, Optional, Union

DEDUP_ALGORITHM_VERSION = "v1"

#: Number of normalized characters of body text folded into the key. Long enough
#: to be specific, short enough that appending a clause doesn't fork the event.
BODY_EXCERPT_CHARS = 120

_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: Optional[str]) -> str:
    """Fold text to a comparable form: ascii, lowercase, alphanumeric + spaces."""
    if not text:
        return ""
    # NFKD splits accented chars into base + combining mark; dropping the marks
    # makes "Zaporizhzhia"/"Zaporizhzhía" and en/em dashes compare equal.
    folded = unicodedata.normalize("NFKD", text)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.replace("’", "'").replace("‘", "'")
    folded = folded.lower()
    folded = _NON_ALNUM.sub(" ", folded)
    return _WHITESPACE.sub(" ", folded).strip()


def body_excerpt(body: Optional[str], chars: int = BODY_EXCERPT_CHARS) -> str:
    """The normalized, truncated body fragment that goes into the key."""
    return normalize_text(body)[:chars]


def _iso(value: Union[str, date, None]) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def event_dedup_key(date_start: Union[str, date, None], body: Optional[str]) -> str:
    """Content-addressed identity for a timeline event."""
    bucket = _iso(date_start) or "undated"
    payload = "{v}|{bucket}|{excerpt}".format(
        v=DEDUP_ALGORITHM_VERSION, bucket=bucket, excerpt=body_excerpt(body)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def external_dedup_key(external_id: str) -> str:
    """Identity for events a scraper tracks under its own stable id."""
    payload = "{v}|external|{eid}".format(
        v=DEDUP_ALGORITHM_VERSION, eid=external_id.strip()
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> Any:
    """Order-insensitive canonical form, so tag order doesn't fork an edit key."""
    if isinstance(value, (list, tuple)):
        items = [_canonical(v) for v in value]
        # Sort only if the members are all scalars we can compare deterministically.
        if all(isinstance(i, str) for i in items):
            return sorted(i.strip() for i in items)
        return items
    if isinstance(value, Mapping):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, date):
        return value.isoformat()
    return value


def edit_dedup_key(target_event_id: int, patch: Dict[str, Any]) -> str:
    """Identity for a *proposed change*: same target + same effective patch."""
    canonical = json.dumps(
        _canonical(patch), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    payload = "{v}|edit|{target}|{patch}".format(
        v=DEDUP_ALGORITHM_VERSION, target=target_event_id, patch=canonical
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
