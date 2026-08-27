"""SHA-256 + middle-page-embedding duplicate detection for uploaded documents.

Reuses the exact single-middle-page OCR+embedding pipeline already proven
out in scripts/hash_and_embed_report_true.py: same render_middle_page /
ocr_transcript functions (including the 0-page-PDF guard), same
intfloat/multilingual-e5-base model, same "passage: {transcript}" encode
convention, same 6-decimal rounding. No parallel OCR/embedding
implementation here, only orchestration against the dashboard's Postgres
tables -- this is what keeps a new upload's embedding directly comparable
via cosine similarity to the ~8000 embeddings that script has already
computed (migrated in via migrate_from_report_true.py).

Per the user's explicit confirmation, this is intentionally ONE page (the
middle page) -- not multi-page OCR. That scope is not being expanded here.
"""
from __future__ import annotations

import hashlib
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.hash_and_embed_report_true import (  # noqa: E402
    EMBED_MODEL_NAME,
    ocr_transcript,
    render_middle_page,
)

# The set of OCR languages we can actually run -- one entry per
# tessdata_best/*.traineddata file on disk (verified 2026-08-26: exactly
# these 14 codes, same set STATE_LANG used to map states to). Language is
# now a required, explicit choice on upload (drives which tessdata model
# does the OCR that feeds the embedding) rather than being inferred from the
# selected state -- a state doesn't reliably imply one language. Keep this
# in sync with tessdata_best/ if a model is ever added or removed.
LANGUAGES = {
    "asm": "Assamese",
    "ben": "Bengali",
    "eng": "English",
    "guj": "Gujarati",
    "hin": "Hindi",
    "kan": "Kannada",
    "mal": "Malayalam",
    "mar": "Marathi",
    "nep": "Nepali",
    "ori": "Odia",
    "pan": "Punjabi",
    "tam": "Tamil",
    "tel": "Telugu",
    "urd": "Urdu",
}

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                import torch

                torch.set_num_threads(1)
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(EMBED_MODEL_NAME, device="cpu")
    return _model


def sha256_of(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def compute_signature(pdf_bytes: bytes, ocr_lang: str) -> tuple[str, list[float], str]:
    """Returns (sha256, embedding, transcript). ocr_lang is the caller's
    already-validated choice (see LANGUAGES above) -- no longer inferred
    from state here."""
    sha = sha256_of(pdf_bytes)
    img = render_middle_page(pdf_bytes)
    transcript = ocr_transcript(img, ocr_lang)
    model = _get_model()
    vector = model.encode(f"passage: {transcript}" if transcript else "passage: ", convert_to_numpy=True)
    embedding = [round(float(x), 6) for x in vector]
    return sha, embedding, transcript


def find_closest_match(session, embedding: list[float]):
    """Returns (UniqueDocument | None, similarity: float) -- similarity is
    1 - cosine_distance via pgvector's indexed `<=>` operator. None match if
    the table is empty."""
    from dashboard.models import UniqueDocument

    distance_col = UniqueDocument.embedding.cosine_distance(embedding).label("distance")
    row = session.query(UniqueDocument, distance_col).order_by(distance_col).first()
    if row is None:
        return None, 0.0
    doc, distance = row
    return doc, 1.0 - float(distance)
