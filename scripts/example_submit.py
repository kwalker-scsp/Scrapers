"""Reference scraper client. Copy this into your scraper repo as a starting point.

    python -m scripts.example_submit --demo
    python -m scripts.example_submit --demo-edit 1

It uses only the standard library, so it drops into any scraper without adding a
dependency. Two things here are worth lifting verbatim:

  * `local_dedup_key()` reproduces the server's algorithm, so a scraper can skip
    the network call for events it has already submitted.
  * `submit()` treats 200 and 201 as success (200 means corroborated or already
    published), and only raises on a real failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

DEFAULT_BASE_URL = os.environ.get("TIMELINE_URL", "http://127.0.0.1:8000")
DEFAULT_API_KEY = os.environ.get("INGEST_API_KEY", "dev-ingest-key-change-me")

DEDUP_ALGORITHM_VERSION = "v1"
BODY_EXCERPT_CHARS = 120

_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")


def local_dedup_key(date_start: Optional[str], body: str) -> str:
    """Mirror of the server's dedup key. Keep in sync with GET /api/v1/ingest/contract.

    Lets a scraper answer "have I already submitted this?" from its own records
    without a round trip.
    """
    folded = unicodedata.normalize("NFKD", body or "")
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.replace("’", "'").replace("‘", "'")
    folded = _NON_ALNUM.sub(" ", folded.lower())
    excerpt = _WHITESPACE.sub(" ", folded).strip()[:BODY_EXCERPT_CHARS]
    payload = "{v}|{bucket}|{excerpt}".format(
        v=DEDUP_ALGORITHM_VERSION, bucket=(date_start or "undated"), excerpt=excerpt
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def submit(payload: Dict[str, Any], base_url: str = DEFAULT_BASE_URL,
           api_key: str = DEFAULT_API_KEY, scraper_agent: str = "example-client") -> Dict[str, Any]:
    """POST one submission. Returns the parsed IngestResult.

    Raises RuntimeError on 4xx/5xx with the server's message, which for a 422
    names the offending field.
    """
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/v1/ingest",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "X-Scraper-Agent": scraper_agent,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            # 201 = queued for review, 200 = corroborated or already published.
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.dumps(json.loads(detail)["detail"], indent=2)
        except Exception:
            pass
        raise RuntimeError("ingest failed with HTTP {0}:\n{1}".format(exc.code, detail))
    except urllib.error.URLError as exc:
        raise RuntimeError("could not reach {0}: {1}".format(base_url, exc.reason))


def new_event(scraper: str, event: Dict[str, Any], **provenance: Any) -> Dict[str, Any]:
    """Build a 'new event' submission body."""
    payload = {"submission_type": "new", "scraper": scraper, "event": event}
    payload.update({k: v for k, v in provenance.items() if v is not None})
    return payload


def edit_event(scraper: str, target_event_id: int, patch: Dict[str, Any],
               **provenance: Any) -> Dict[str, Any]:
    """Build an 'edit' submission body. Include only the fields you want changed."""
    payload = {
        "submission_type": "edit",
        "scraper": scraper,
        "target_event_id": target_event_id,
        "patch": patch,
    }
    payload.update({k: v for k, v in provenance.items() if v is not None})
    return payload


# --- demo --------------------------------------------------------------------

DEMO_EVENT = {
    "date_text": "12 August 2026",
    "date_start": "2026-08-12",
    "date_end": "2026-08-12",
    "date_precision": "day",
    "section": "2026: Ceasefire Attempts, Continued Strikes, and Ukraine Regains the Initiative",
    "body": (
        "Ukrainian long-range drones strike an oil refinery in Volgograd, halting "
        "processing for several days and cutting regional fuel supply."
    ),
    "tags": ["Warfare Shift"],
    "research_categories": ["Fire/Maneuver"],
    "sources": [
        {
            "name": "ISW",
            "url": "https://example.org/isw/2026-08-12",
            "title": "Russian Offensive Campaign Assessment, August 12 2026",
            "accessed_at": "2026-08-13",
        }
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_BASE_URL)
    parser.add_argument("--key", default=DEFAULT_API_KEY)
    parser.add_argument("--scraper", default="example-client")
    parser.add_argument("--demo", action="store_true", help="submit a sample new event")
    parser.add_argument("--demo-edit", type=int, metavar="EVENT_ID",
                        help="submit a sample edit against this published event id")
    parser.add_argument("--file", help="submit a JSON file containing one payload or a list")
    parser.add_argument("--dedup-key", nargs=2, metavar=("DATE_START", "BODY"),
                        help="print the dedup key for a date and body, then exit")
    args = parser.parse_args()

    if args.dedup_key:
        print(local_dedup_key(args.dedup_key[0] or None, args.dedup_key[1]))
        return 0

    payloads = []
    if args.demo:
        payloads.append(new_event(
            args.scraper, DEMO_EVENT,
            scraper_run_id="demo-run-1",
            confidence=0.85,
            notes="Submitted by scripts/example_submit.py --demo",
        ))
    if args.demo_edit:
        payloads.append(edit_event(
            args.scraper, args.demo_edit,
            {"date_precision": "day", "date_text": "21 November 2013",
             "date_start": "2013-11-21", "date_end": "2013-11-21"},
            notes="CFR gives an exact date for the start of the crackdown.",
        ))
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        payloads.extend(loaded if isinstance(loaded, list) else [loaded])

    if not payloads:
        parser.error("nothing to do — pass --demo, --demo-edit ID, or --file PATH")

    failures = 0
    for payload in payloads:
        if payload.get("submission_type") == "new":
            event = payload["event"]
            print("local dedup key: {0}".format(
                local_dedup_key(event.get("date_start"), event.get("body", ""))
            ))
        try:
            result = submit(payload, args.url, args.key, args.scraper)
        except RuntimeError as exc:
            print("ERROR: {0}".format(exc), file=sys.stderr)
            failures += 1
            continue
        print("{outcome}: submission #{sid} (status={status}, corroboration={n})".format(
            outcome=result["outcome"].upper(),
            sid=result["submission_id"],
            status=result["status"],
            n=result["corroboration_count"],
        ))
        if result.get("event_id"):
            print("  published event: #{0}".format(result["event_id"]))
        for warning in result.get("warnings") or []:
            print("  warning: {0}".format(warning))
        # Sanity check that the client and server agree on the algorithm.
        if payload.get("submission_type") == "new" and not payload.get("external_id"):
            expected = local_dedup_key(payload["event"].get("date_start"), payload["event"]["body"])
            if expected != result["dedup_key"]:
                print("  NOTE: local dedup key differs from the server's — check "
                      "GET /api/v1/ingest/contract for the current algorithm.", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
