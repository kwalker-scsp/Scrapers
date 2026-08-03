"""Shared-secret auth for the ingest and review endpoints.

Two separate keys, because the two audiences are different: automated scrapers
get a key that can only add things to the queue, and you get a key that can
change the published dataset. A leaked scraper key cannot publish anything.

Keys are accepted as `X-API-Key: <key>` or `Authorization: Bearer <key>`, and
compared with `hmac.compare_digest` so the comparison doesn't leak length or
prefix information through timing.

This is deliberately the simplest thing that works for a single researcher plus
cron jobs. If this ever grows real multi-user review, replace `require_reviewer`
with a session/OIDC dependency — the rest of the app only depends on it
returning an actor string.
"""

from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings

_UNAUTH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Missing or invalid API key. Send it as 'X-API-Key: <key>'.",
    headers={"WWW-Authenticate": "Bearer"},
)


def _presented_key(x_api_key: Optional[str], authorization: Optional[str]) -> Optional[str]:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _matches(presented: Optional[str], expected: str) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def _sanitize_actor(raw: Optional[str], fallback: str) -> str:
    """Actor strings land in the audit log, so keep them short and printable."""
    if not raw:
        return fallback
    cleaned = "".join(ch for ch in raw.strip() if ch.isprintable())[:120]
    return cleaned or fallback


def require_ingest(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
    x_scraper_agent: Optional[str] = Header(
        None, alias="X-Scraper-Agent", description="Optional label recorded in the audit log"
    ),
    settings: Settings = Depends(get_settings),
) -> str:
    """Guards POST /api/v1/ingest. Returns an actor string for the audit log."""
    presented = _presented_key(x_api_key, authorization)
    # The review key is also accepted, so you can hand-submit test payloads
    # without juggling two keys; the reverse is not true.
    if _matches(presented, settings.ingest_api_key):
        return _sanitize_actor(x_scraper_agent, "ingest")
    if _matches(presented, settings.review_api_key):
        return _sanitize_actor(x_scraper_agent, "reviewer")
    raise _UNAUTH


def require_reviewer(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
    x_actor: Optional[str] = Header(
        None, alias="X-Actor", description="Who is deciding; recorded in the audit log"
    ),
    settings: Settings = Depends(get_settings),
) -> str:
    """Guards /api/v1/review/*. Returns 'reviewer:<name>' for the audit log."""
    presented = _presented_key(x_api_key, authorization)
    if not _matches(presented, settings.review_api_key):
        raise _UNAUTH
    return "reviewer:{0}".format(_sanitize_actor(x_actor, "unnamed"))


def optional_read_auth(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Public reads are open unless REQUIRE_KEY_FOR_READS=true (embargoed data)."""
    if not settings.require_key_for_reads:
        return
    presented = _presented_key(x_api_key, authorization)
    if _matches(presented, settings.review_api_key) or _matches(presented, settings.ingest_api_key):
        return
    raise _UNAUTH
