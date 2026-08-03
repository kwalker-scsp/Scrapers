"""Application entrypoint. Mounts the API and serves the two frontend views."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import REPO_ROOT, get_settings
from app.routers import ingest, public, review

log = logging.getLogger("ukr_timeline")

STATIC_DIR = REPO_ROOT / "static"

DESCRIPTION = """
Curated timeline of the Russo-Ukrainian War.

**Two write paths, one direction.** Scrapers `POST /api/v1/ingest`, which can only
create rows in the submission queue. Nothing reaches the published dataset except
through `POST /api/v1/review/submissions/{id}/approve`. The public read endpoints
serve published events only — pending submissions are invisible until approved.

* `/` — public interactive timeline
* `/review` — approval queue (needs the review key)
* `/docs` — interactive API docs
* `/api/v1/ingest/contract` — the scraper contract, including the dedup algorithm
"""

@asynccontextmanager
async def lifespan(_app: FastAPI):
    if get_settings().using_default_keys:
        log.warning(
            "INGEST_API_KEY/REVIEW_API_KEY are still the example values. "
            "Set real keys in .env before exposing this service."
        )
    yield


app = FastAPI(
    title="Russo-Ukrainian War Timeline",
    lifespan=lifespan,
    description=DESCRIPTION,
    version=__version__,
    openapi_tags=[
        {"name": "public", "description": "Read-only. Published events, metadata, changelog."},
        {"name": "ingest", "description": "Scraper submissions. Requires INGEST_API_KEY."},
        {"name": "review", "description": "Approval queue. Requires REVIEW_API_KEY."},
    ],
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(public.router)
app.include_router(ingest.router)
app.include_router(review.router)


@app.exception_handler(500)
def _server_error(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --- Frontend ---------------------------------------------------------------
# Two plain HTML/JS pages, no build step: the API is the interesting part and a
# toolchain here would only be something else to keep working.

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _page(name: str) -> Response:
    path = STATIC_DIR / name
    if not path.is_file():  # pragma: no cover
        return JSONResponse(status_code=404, content={"detail": "{0} not found".format(name)})
    return FileResponse(str(path))


@app.get("/", include_in_schema=False)
def timeline_page() -> Response:
    """Public interactive timeline."""
    return _page("index.html")


@app.get("/review", include_in_schema=False)
def review_page() -> Response:
    """Review dashboard for the approval queue."""
    return _page("review.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return _page("favicon.svg")
