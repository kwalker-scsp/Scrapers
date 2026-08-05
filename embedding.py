"""Local ONNX sentence embeddings (all-MiniLM-L6-v2).

Extracted from pipeline.py so the review/search app can embed a query without
importing the whole scraping stack (Crawl4AI, Gemini). There is still exactly
one implementation -- pipeline.py imports this module.
"""

import json
import os

import numpy as np
import onnxruntime as ort
from dotenv import find_dotenv, load_dotenv
from tokenizers import Tokenizer

load_dotenv(find_dotenv(usecwd=True))
os.environ["HF_HUB_OFFLINE"] = "1"

# Model paths default to the repo root, resolved against THIS file rather than
# the working directory -- the review app runs from "Application Interface/",
# where a bare "./model.onnx" would not exist.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _resolve(path):
    return path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)


LOCAL_ONNX_MODEL_PATH = _resolve(
    os.getenv("LOCAL_ONNX_MODEL_PATH", "model.onnx")
)
LOCAL_TOKENIZER_PATH = _resolve(
    os.getenv("LOCAL_TOKENIZER_PATH", "tokenizer.json")
)

EMBEDDING_DIM = 384

# Titles and summaries are truncated to this before embedding; MiniLM's useful
# context is short and this keeps ingest cheap.
EMBED_CHAR_LIMIT = 400


class LocalONNXEmbedder:

    def __init__(self, model_path: str, tokenizer_path: str):
        self.model_path = model_path
        self.tokenizer_path = tokenizer_path
        self.session = (
            ort.InferenceSession(model_path)
            if os.path.exists(model_path)
            else None
        )
        self.tokenizer = (
            Tokenizer.from_file(tokenizer_path)
            if os.path.exists(tokenizer_path)
            else None
        )
        if self.tokenizer:
            self.tokenizer.enable_truncation(max_length=512)
            self.tokenizer.enable_padding(length=512)

    @property
    def available(self) -> bool:
        return bool(self.session and self.tokenizer)

    def embed_single(self, text: str) -> np.ndarray:
        # Returning a zero vector here would make every similarity 0.0, which
        # silently disables deduplication instead of stopping the run.
        if not self.session:
            raise RuntimeError(
                f"ONNX embedding model not found at {self.model_path}. "
                "Deduplication and search cannot run without it."
            )
        if not self.tokenizer:
            raise RuntimeError(
                f"Tokenizer not found at {self.tokenizer_path}. "
                "Deduplication and search cannot run without it."
            )
        encoded = self.tokenizer.encode(text)
        inputs = {
            "input_ids": np.array([encoded.ids], dtype=np.int64),
            "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([encoded.type_ids], dtype=np.int64),
        }
        outputs = self.session.run(None, inputs)
        embeddings = outputs[0]
        mask_expanded = np.expand_dims(inputs["attention_mask"], -1)
        sum_embeddings = np.sum(embeddings * mask_expanded, 1)
        sum_mask = np.clip(mask_expanded.sum(1), a_min=1e-9, a_max=None)
        pooled = sum_embeddings / sum_mask
        vec = pooled[0]
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec


local_embedder = LocalONNXEmbedder(
    LOCAL_ONNX_MODEL_PATH, LOCAL_TOKENIZER_PATH
)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm_a, norm_b = np.linalg.norm(a), np.linalg.norm(b)
    return (
        float(np.dot(a, b) / (norm_a * norm_b))
        if norm_a > 0 and norm_b > 0
        else 0.0
    )


def embed_record(title: str, summary: str) -> np.ndarray:
    """The canonical text an event is indexed under.

    Ingest and search must embed the same way or the vectors are not
    comparable, so both go through this one function.
    """
    return local_embedder.embed_single(
        f"{title or ''} {summary or ''}"[:EMBED_CHAR_LIMIT]
    )


def serialize(vec) -> str:
    """Vectors are stored as a JSON array of floats in a TEXT column."""
    return json.dumps([round(float(x), 6) for x in vec])


def deserialize(blob):
    """Parses a stored vector, returning None for missing or corrupt values."""
    if not blob:
        return None
    try:
        vec = np.array(json.loads(blob), dtype=np.float32)
    except (ValueError, TypeError):
        return None
    return vec if vec.shape == (EMBEDDING_DIM,) else None
