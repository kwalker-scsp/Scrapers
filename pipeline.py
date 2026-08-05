### 1. Crawl Part first html -> .md ###
# We used Crawl4AI: Blazing fast async engine that strips the HTML/doc noise,
# bypasses JS hydration, and delivers a pristine, token-optimized Markdown/JSON stream
# directly to the model's context window. Pure high-density signal, zero bloat.



### 2. .md -> Gemini Flash 2.5 this is the agentic layer ###
# Embed a model with google to flag dates and events to upkeep the timeline
# This will attach a date and event to add to the timeline
# There's two parts to this it'll flag a) daily events and b) monthly events
# the two parts will be different 'timelines' if you will, as one is comprehensive and the other one flags major things across the month so better for high-level things
# monthly should also allow for any modeling & simulation aspects to cut out some of the daily noise and focus on the bigger picture



### ONNX hugging face model -> this is also an agentic layer but super mini model ###
# https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
# using a mini downloadable model we'll run a quick semantic cosine similarity vector
# This scoring cuts out easy duplicate events so you don't have to check for them
# Will still flag lower confidence items, pretty quick check though

import os
import re
from datetime import datetime
from google import genai
from crawl4ai import AsyncWebCrawler
from dotenv import load_dotenv
from pydantic import BaseModel, Field

import database
import embedding

load_dotenv()
os.environ["HF_HUB_OFFLINE"] = "1"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LOCAL_ONNX_MODEL_PATH = os.getenv("LOCAL_ONNX_MODEL_PATH", "./model.onnx")
LOCAL_TOKENIZER_PATH = os.getenv("LOCAL_TOKENIZER_PATH", "./tokenizer.json")

THRESHOLD_AUTO_MERGE = float(os.getenv("THRESHOLD_AUTO_MERGE", "0.95"))
THRESHOLD_DUP_REVIEW = float(os.getenv("THRESHOLD_DUP_REVIEW", "0.90"))


# Pydantic Schemas for Structured Gemini Outputs
class DailyEvent(BaseModel):
    event_date: str = Field(
        description="Exact date of event in YYYY-MM-DD format, YYYY-MM format, or empty if unknown."
    )
    title: str = Field(description="Short title (5-10 words).")
    summary: str = Field(description="1-2 sentence detailed description.")
    is_concrete_event: bool = Field(
        description="True if distinct physical event. False if speculation."
    )
    confidence_score: float = Field(description="Score between 0.0 and 1.0.")


class MonthlyMacroMilestone(BaseModel):
    event_month: str = Field(
        description="Month of milestone in YYYY-MM format."
    )
    title: str = Field(
        description="High-level title of strategic development."
    )
    strategic_summary: str = Field(
        description="Macro summary for modeling/simulation."
    )
    category: str = Field(
        description="Category: Military, Diplomatic, Economic, Infrastructure."
    )


class ExtractedDualTimeline(BaseModel):
    daily_events: list[DailyEvent] = Field(
        description="Granular daily tactical events."
    )
    monthly_milestones: list[MonthlyMacroMilestone] = Field(
        description="High-level strategic developments."
    )


gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# The embedder lives in its own module so the review/search app can use it
# without importing Crawl4AI and Gemini. Re-exported here for existing callers.
local_embedder = embedding.local_embedder
cosine_similarity = embedding.cosine_similarity


# Date Normalization
#
# The model is asked for YYYY-MM-DD / YYYY-MM / "" but is not guaranteed to
# comply. Branching on len(date_str) let "2024-1-5", "November 2024" and
# "unknown" fall through into the database unchecked, so every shape is now
# matched explicitly and anything unrecognized is routed to human review
# rather than written as-is.

RE_FULL_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
RE_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
RE_YEAR_ONLY = re.compile(r"^(\d{4})$")

GRANULARITY_DAY = "day"
GRANULARITY_MONTH = "month"
GRANULARITY_UNPARSEABLE = "unparseable"


def normalize_event_date(
    raw: str, anchor_year: str, anchor_month: str
) -> tuple[str, str, list[str]]:
    """Coerces a model-supplied date into a canonical form.

    Returns (date_str, granularity, notes). `granularity` tells the caller how
    the value should be routed: day-level events go to the daily table,
    month-level ones to the macro table, and unparseable ones fall back to the
    anchor date carrying an explicit note so a reviewer can correct them.
    """
    notes: list[str] = []
    raw = (raw or "").strip()

    if not raw:
        return (
            f"{anchor_year}-{anchor_month}-01",
            GRANULARITY_DAY,
            ["uncertain year", "uncertain month"],
        )

    match = RE_FULL_DATE.match(raw)
    if match:
        year, month, day = match.groups()
        try:
            parsed = datetime(int(year), int(month), int(day))
        except ValueError:
            # Well-formed but impossible, e.g. 2024-02-31.
            return (
                f"{anchor_year}-{anchor_month}-01",
                GRANULARITY_DAY,
                ["unparseable date"],
            )
        return parsed.strftime("%Y-%m-%d"), GRANULARITY_DAY, notes

    match = RE_YEAR_MONTH.match(raw)
    if match:
        year, month = match.groups()
        if not 1 <= int(month) <= 12:
            return (
                f"{anchor_year}-{anchor_month}-01",
                GRANULARITY_DAY,
                ["unparseable date"],
            )
        return f"{year}-{int(month):02d}", GRANULARITY_MONTH, notes

    match = RE_YEAR_ONLY.match(raw)
    if match:
        return (
            f"{match.group(1)}-{anchor_month}-01",
            GRANULARITY_DAY,
            ["uncertain month"],
        )

    # Anything else ("November 2024", "unknown", free text): anchor it and
    # flag it rather than letting it reach the database untouched.
    return (
        f"{anchor_year}-{anchor_month}-01",
        GRANULARITY_UNPARSEABLE,
        ["unparseable date"],
    )


# Source-Specific Text Pre-Processor & Anchor Date Extractor
def preprocess_markdown(
    markdown: str, source_name: str, url: str
) -> tuple[str, str]:
    fallback_date = datetime.now().strftime("%Y-%m-%d")

    # 1. ISW Targeting: Dynamically find "Key Takeaways" & extract URL date
    if "understandingwar.org" in url or "ISW" in source_name:
        match = re.search(r"assessment-([a-z]+)-(\d{1,2})-(\d{4})", url)
        if match:
            try:
                parsed_dt = datetime.strptime(
                    f"{match.group(1)} {match.group(2)} {match.group(3)}",
                    "%B %d %Y",
                )
                fallback_date = parsed_dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

        kt_match = re.search(
            r"(Key Takeaways.*)(?=Main Issues|\n#|\Z)",
            markdown,
            re.DOTALL | re.IGNORECASE,
        )
        if kt_match:
            return kt_match.group(1)[:10000], fallback_date
        return markdown[:15000], fallback_date

    # 2. CFR Tracker: Extract Top "Recent Developments"
    elif "cfr.org" in url:
        recent_match = re.search(
            r"(Recent Developments.*)(?=Background|\n##|\Z)",
            markdown,
            re.DOTALL | re.IGNORECASE,
        )
        if recent_match:
            return recent_match.group(1)[:12000], fallback_date
        return markdown[:12000], fallback_date

    # 3. Covert Shores (H.I. Sutton): Full context
    return markdown[:30000], fallback_date


async def crawl_url_to_markdown(url: str) -> str:
    async with AsyncWebCrawler(verbose=False) as crawler:
        result = await crawler.arun(url=url)
        markdown = result.markdown or ""
        if (
            not result.success
            or result.status_code == 404
            or "Page not found" in markdown
        ):
            print(f"[Notice] Page not found or 404 at {url}. Skipping.")
            return ""
        return markdown


def extract_dual_timelines(
    markdown_text: str, source_url: str, fallback_date: str
) -> ExtractedDualTimeline:
    if not gemini_client:
        print("[Error] GEMINI_API_KEY missing in .env")
        return ExtractedDualTimeline(daily_events=[], monthly_milestones=[])

    prompt = f"""
    You are an intelligence analysis agent processing text from URL: {source_url}
    Default Anchor Date: {fallback_date}

    INSTRUCTIONS:
    1. Extract granular daily tactical events into DailyEvent.
       - Extract date as YYYY-MM-DD. If missing day, format as YYYY-MM. If date is unknown, return empty "".
    2. Extract broad campaign trends into MonthlyMacroMilestone (YYYY-MM format).
    """

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"{prompt}\n\nSource Content:\n{markdown_text}",
            config={
                "response_mime_type": "application/json",
                "response_schema": ExtractedDualTimeline,
            },
        )
        return ExtractedDualTimeline.model_validate_json(response.text)
    except Exception as e:
        print(f"[Error] Gemini Flash extraction failed: {e}")
        return ExtractedDualTimeline(daily_events=[], monthly_milestones=[])


LOW_CONFIDENCE_WORDS = ["continues", "likely", "probably", "considering"]


def _split_anchor(fallback_date: str) -> tuple[str, str]:
    """Splits an anchor date into (year, month), tolerating odd shapes."""
    parts = (fallback_date or "").split("-")
    if len(parts) >= 2 and len(parts[0]) == 4 and parts[1].isdigit():
        return parts[0], f"{int(parts[1]):02d}"
    now = datetime.now()
    return now.strftime("%Y"), now.strftime("%m")


def _is_isw(source_name: str, source_url: str) -> bool:
    return "understandingwar.org" in source_url or "ISW" in source_name


def _find_best_match(vec, corpus_vectors, corpus_records):
    max_score, matched = 0.0, None
    for idx, existing in enumerate(corpus_vectors):
        score = cosine_similarity(vec, existing)
        if score > max_score:
            max_score, matched = score, corpus_records[idx]
    return max_score, matched


def process_and_save_daily_events(
    daily_events: list[DailyEvent],
    source_name: str,
    source_url: str,
    fallback_date: str,
    watermark: str | None = None,
) -> str | None:
    """Routes extracted daily events into the database.

    Returns the newest event_date processed, for the caller to record as the
    source's high-water mark.
    """
    anchor_year, anchor_month = _split_anchor(fallback_date)

    # Slice to last 20 for Covert Shores ONNX deduplication
    target_events = (
        daily_events[-20:] if "hisutton.com" in source_url else daily_events
    )

    # The corpus includes PENDING rows, and newly written events are appended
    # to it inside the loop so duplicates within a single batch are caught too.
    corpus_records = database.fetch_dedup_corpus_daily()
    corpus_vectors = [
        embedding.embed_record(r["title"], r["summary"])
        for r in corpus_records
    ]

    newest_date = None

    for evt in target_events:
        notes_list = []

        # 1. Speculative Phrasal Note Heuristic
        if any(w in evt.summary.lower() for w in LOW_CONFIDENCE_WORDS):
            notes_list.append("Low confidence event")

        # 2. Date normalization and routing
        date_str, granularity, date_notes = normalize_event_date(
            evt.event_date, anchor_year, anchor_month
        )
        notes_list.extend(date_notes)

        if granularity == GRANULARITY_MONTH:
            # Month-precision event: belongs on the macro timeline, but keep
            # its category and confidence rather than flattening to "Military".
            process_and_save_monthly_milestones(
                [
                    MonthlyMacroMilestone(
                        event_month=date_str,
                        title=evt.title,
                        strategic_summary=evt.summary,
                        category="Military",
                    )
                ],
                source_name,
                source_url,
                extra_notes=notes_list,
            )
            continue

        # 3. High-water mark: skip anything at or before what we already have.
        if watermark and date_str <= watermark:
            continue

        final_notes = ", ".join(notes_list)

        # 4. Local Vector Deduplication
        incoming_vec = embedding.embed_record(evt.title, evt.summary)
        max_score, matched_record = _find_best_match(
            incoming_vec, corpus_vectors, corpus_records
        )

        if max_score >= THRESHOLD_AUTO_MERGE and matched_record:
            # Drop the duplicate but keep the corroborating URL.
            database.append_source_url(
                "DailyPendingEvents", matched_record["id"], source_url
            )
            print(
                f"[Auto-Merge] {max_score:.2f} -> id {matched_record['id']} "
                f"| {evt.title}"
            )
            continue

        if max_score >= THRESHOLD_DUP_REVIEW:
            database.save_duplicate_review(
                {
                    "title": evt.title,
                    "summary": evt.summary,
                    "event_date": date_str,
                },
                matched_record,
                max_score,
                source_url,
            )
            continue

        # 5. Auto-approval: non-ISW sources with a clean, complete date and no
        #    uncertainty flags bypass the review queue. ISW always gets a human.
        review_status = (
            "PENDING"
            if _is_isw(source_name, source_url) or notes_list
            else "APPROVED"
        )

        # The dedup vector is exactly what search indexes on, so persist the
        # one already computed above rather than embedding a second time.
        new_id = database.save_daily_event(
            date_str,
            evt.title,
            evt.summary,
            evt.confidence_score,
            evt.is_concrete_event,
            source_name,
            source_url,
            final_notes,
            review_status=review_status,
            embedding=embedding.serialize(incoming_vec),
        )

        # Make this event visible to the rest of the batch.
        corpus_records.append(
            {"id": new_id, "title": evt.title, "summary": evt.summary}
        )
        corpus_vectors.append(incoming_vec)

        if newest_date is None or date_str > newest_date:
            newest_date = date_str

    return newest_date


def process_and_save_monthly_milestones(
    milestones: list[MonthlyMacroMilestone],
    source_name: str,
    source_url: str,
    extra_notes: list[str] | None = None,
):
    """Routes macro milestones through the same dedup rules as daily events.

    Previously these were written straight in with no similarity check, so the
    macro timeline accumulated a duplicate of every milestone on every run.
    """
    if not milestones:
        return

    corpus_records = database.fetch_dedup_corpus_monthly()
    corpus_vectors = [
        embedding.embed_record(r["title"], r["summary"])
        for r in corpus_records
    ]

    for macro in milestones:
        notes_list = list(extra_notes or [])
        if any(
            w in macro.strategic_summary.lower() for w in LOW_CONFIDENCE_WORDS
        ):
            notes_list.append("Low confidence event")

        incoming_vec = embedding.embed_record(
            macro.title, macro.strategic_summary
        )
        max_score, matched_record = _find_best_match(
            incoming_vec, corpus_vectors, corpus_records
        )

        if max_score >= THRESHOLD_AUTO_MERGE and matched_record:
            database.append_source_url(
                "MonthlyMacroMilestones", matched_record["id"], source_url
            )
            print(
                f"[Auto-Merge/Macro] {max_score:.2f} -> id "
                f"{matched_record['id']} | {macro.title}"
            )
            continue

        if max_score >= THRESHOLD_DUP_REVIEW:
            notes_list.append("Possible Duplicate")

        review_status = (
            "PENDING"
            if _is_isw(source_name, source_url) or notes_list
            else "APPROVED"
        )

        new_id = database.save_monthly_milestone(
            macro.event_month,
            macro.title,
            macro.strategic_summary,
            macro.category,
            source_name,
            source_url,
            notes=", ".join(notes_list)[:250],
            review_status=review_status,
        )

        corpus_records.append(
            {
                "id": new_id,
                "title": macro.title,
                "summary": macro.strategic_summary,
            }
        )
        corpus_vectors.append(incoming_vec)


async def run_pipeline_for_url(source_name: str, url: str):
    print(f"\n====================================")
    print(f"Crawling: {source_name} ({url})")
    raw_markdown = await crawl_url_to_markdown(url)
    if not raw_markdown:
        # Not an error: the page may simply not be published yet. Leave the
        # watermark untouched so the next run retries this date.
        return False

    trimmed_markdown, fallback_date = preprocess_markdown(
        raw_markdown, source_name, url
    )
    extracted = extract_dual_timelines(
        trimmed_markdown, url, fallback_date
    )

    process_and_save_monthly_milestones(
        extracted.monthly_milestones, source_name, url
    )

    watermark = database.get_watermark(source_name)
    newest_date = None
    if extracted.daily_events:
        newest_date = process_and_save_daily_events(
            extracted.daily_events, source_name, url, fallback_date, watermark
        )

    if newest_date:
        database.set_watermark(source_name, newest_date, "OK")

    return True