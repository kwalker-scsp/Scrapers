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

import asyncio
import os
os.environ["HF_HUB_OFFLINE"] = "1"
from google import genai
import numpy as np
import onnxruntime as ort
from crawl4ai import AsyncWebCrawler
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tokenizers import Tokenizer

import database

load_dotenv()

# Environment Configurations
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LOCAL_ONNX_MODEL_PATH = os.getenv("LOCAL_ONNX_MODEL_PATH", "./model.onnx")
LOCAL_TOKENIZER_PATH = os.getenv("LOCAL_TOKENIZER_PATH", "./tokenizer.json")

# Fallback values match .env defaults
THRESHOLD_AUTO_MERGE = float(os.getenv("THRESHOLD_AUTO_MERGE", "0.95"))
THRESHOLD_DUP_REVIEW = float(os.getenv("THRESHOLD_DUP_REVIEW", "0.90"))

### 1. Pydantic Schemas for Gemini 2.5 Flash ###


class DailyEvent(BaseModel):
    event_date: str = Field(
        description="Exact date of the event in YYYY-MM-DD format."
    )
    title: str = Field(
        description="Short, 5-10 word title summarizing the event."
    )
    summary: str = Field(
        description="1-2 sentence detailed description of the physical or political event."
    )
    is_concrete_event: bool = Field(
        description="True if this is a distinct physical attack, strike, or policy shift. False if pure speculation."
    )
    confidence_score: float = Field(
        description="Score between 0.0 and 1.0. Deduct for passive or speculative words like 'likely' or 'possibly'."
    )


class MonthlyMacroMilestone(BaseModel):
    event_month: str = Field(
        description="Month of the milestone in YYYY-MM format."
    )
    title: str = Field(
        description="High-level title of the strategic development."
    )
    strategic_summary: str = Field(
        description="Macro-level summary focusing on campaign trends, systemic impacts, or M&S inputs."
    )
    category: str = Field(
        description="Category: Military, Diplomatic, Economic, or Infrastructure."
    )


class ExtractedDualTimeline(BaseModel):
    daily_events: list[DailyEvent] = Field(
        description="Granular day-to-day tactical events."
    )
    monthly_milestones: list[MonthlyMacroMilestone] = Field(
        description="High-level strategic developments for macro modeling."
    )


### 2. Initialize Gemini Client & Purely Local ONNX Engine ###

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


class LocalONNXEmbedder:
    """Loads locally available model.onnx and tokenizer.json for MiniLM vector embeddings."""

    def __init__(self, model_path: str, tokenizer_path: str):
        self.session = None
        self.tokenizer = None

        if os.path.exists(model_path):
            self.session = ort.InferenceSession(model_path)
        else:
            print(
                f"[Warning] Local ONNX model file not found at path: {model_path}"
            )

        if os.path.exists(tokenizer_path):
            # Purely local loading - no network request to Hugging Face
            self.tokenizer = Tokenizer.from_file(tokenizer_path)
            self.tokenizer.enable_truncation(max_length=512)
            self.tokenizer.enable_padding(length=512)
        else:
            print(
                f"[Warning] Local tokenizer file not found at path: {tokenizer_path}"
            )

    def embed_single(self, text: str) -> np.ndarray:
        if not self.session or not self.tokenizer:
            return np.zeros(384)

        encoded = self.tokenizer.encode(text)
        input_ids = np.array([encoded.ids], dtype=np.int64)
        attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
        token_type_ids = np.array([encoded.type_ids], dtype=np.int64)

        inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }

        outputs = self.session.run(None, inputs)
        # Mean pooling output across tokens
        embeddings = outputs[0]
        mask_expanded = np.expand_dims(attention_mask, -1)
        sum_embeddings = np.sum(embeddings * mask_expanded, 1)
        sum_mask = np.clip(mask_expanded.sum(1), a_min=1e-9, a_max=None)
        pooled = sum_embeddings / sum_mask

        # Normalize vector
        vec = pooled[0]
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec


local_embedder = LocalONNXEmbedder(
    LOCAL_ONNX_MODEL_PATH, LOCAL_TOKENIZER_PATH
)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


### 3. Step 1: Crawl4AI HTML to Token-Optimized Markdown ###


async def crawl_url_to_markdown(url: str) -> str:
    async with AsyncWebCrawler(verbose=False) as crawler:
        result = await crawler.arun(url=url)

        if (
            not result.success
            or result.status_code == 404
            or "Page not found" in result.markdown
        ):
            print(
                f"[Notice] Page not found or 404 error at {url}. Skipping."
            )
            return ""

        return result.markdown


### 4. Step 2: Agentic Gemini 2.5 Flash Dual-Extraction ###


def extract_dual_timelines(
    markdown_text: str, source_url: str
) -> ExtractedDualTimeline:
    if not gemini_client:
        print("[Error] GEMINI_API_KEY is not configured in .env")
        return ExtractedDualTimeline(daily_events=[], monthly_milestones=[])

    if len(markdown_text.strip()) < 300:
        print(
            "[Warning] Markdown payload too short or low signal. Skipping."
        )
        return ExtractedDualTimeline(daily_events=[], monthly_milestones=[])

    prompt = f"""
    You are an intelligence analysis agent processing text from URL: {source_url}

    TASKS:
    1. **Daily Events Timeline**: Extract every specific, granular physical event (strikes, losses, diplomatic decisions).
       - Assign dates in YYYY-MM-DD format (infer missing years from surrounding section headers).
       - Assign a confidence_score (0.0 to 1.0), penalizing speculative phrasing like 'likely' or 'continues to'.
    2. **Monthly Macro Timeline**: Extract high-level strategic shifts, major structural developments, and systemic trends suitable for macro Modeling & Simulation (M&S).
       - Assign months in YYYY-MM format.
    """

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"{prompt}\n\nSource Content:\n{markdown_text[:35000]}",
            config={
                "response_mime_type": "application/json",
                "response_schema": ExtractedDualTimeline,
            },
        )
        return ExtractedDualTimeline.model_validate_json(response.text)
    except Exception as e:
        print(f"[Error] Gemini Flash extraction failed: {e}")
        return ExtractedDualTimeline(daily_events=[], monthly_milestones=[])


### 5. Step 3: Local ONNX Semantic Cosine Deduplication ###


def deduplicate_and_route_daily_events(
    daily_events: list[DailyEvent], source_name: str, source_url: str
):
    recent_records = database.fetch_recent_approved_summaries()
    existing_embeddings = (
        [
            local_embedder.embed_single(
                f"{r['title']} {r['summary']}"[:400]
            )
            for r in recent_records
        ]
        if recent_records
        else []
    )

    for evt in daily_events:
        incoming_text = f"{evt.title} {evt.summary}"[:400]
        incoming_vec = local_embedder.embed_single(incoming_text)

        matched_record = None
        max_score = 0.0

        for idx, exist_vec in enumerate(existing_embeddings):
            score = cosine_similarity(incoming_vec, exist_vec)
            if score > max_score:
                max_score = score
                matched_record = recent_records[idx]

        if max_score >= THRESHOLD_AUTO_MERGE:
            print(
                f"[Auto-Merge] Score: {max_score:.2f} | Title: {evt.title}"
            )
        elif max_score >= THRESHOLD_DUP_REVIEW:
            print(
                f"[Duplicate Review Queue] Score: {max_score:.2f} | Title: {evt.title}"
            )
            database.save_duplicate_review(
                {
                    "title": evt.title,
                    "summary": evt.summary,
                    "event_date": evt.event_date,
                },
                matched_record,
                max_score,
                source_url,
            )
        else:
            print(f"[Daily Pending Queue] Title: {evt.title}")
            database.save_daily_event(
                evt.event_date,
                evt.title,
                evt.summary,
                evt.confidence_score,
                evt.is_concrete_event,
                source_name,
                source_url,
            )


### 6. Master Pipeline Execution ###


async def run_pipeline_for_url(source_name: str, url: str):
    print(f"\n####################################")
    print(f"Crawling Source: {source_name}")
    print(f"URL: {url}")

    markdown = await crawl_url_to_markdown(url)
    if not markdown:
        return

    print("Extracting Daily and Monthly Timelines via Gemini 2.5 Flash...")
    extracted = extract_dual_timelines(markdown, url)
    print(
        f"Extracted {len(extracted.daily_events)} Daily Events and {len(extracted.monthly_milestones)} Monthly Milestones."
    )

    for macro in extracted.monthly_milestones:
        print(f"[Monthly Queue] [{macro.event_month}] {macro.title}")
        database.save_monthly_milestone(
            macro.event_month,
            macro.title,
            macro.strategic_summary,
            macro.category,
            source_name,
            url,
        )

    if extracted.daily_events:
        print("Deduplicating Daily Events via Local model.onnx Runtime...")
        deduplicate_and_route_daily_events(
            extracted.daily_events, source_name, url
        )