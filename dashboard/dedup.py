"""SHA-256 + middle-page-embedding duplicate detection for uploaded documents.

Reuses the exact single-middle-page OCR+embedding pipeline already proven
out in scripts/hash_and_embed_report_true.py: same render_middle_page /
ocr_transcript functions (including the 0-page-PDF guard), same
intfloat/multilingual-e5-base model, same "passage: {transcript}" encode
convention, same 6-decimal rounding. No parallel OCR/embedding
implementation here, only orchestration against the dashboard's MongoDB
collections -- this is what keeps a new upload's embedding directly comparable
via cosine similarity to the ~8000 embeddings that script has already
computed (migrated in via migrate_from_pops_csv.py).

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


def to_cosine_similarity(atlas_score: float) -> float:
    """Convert an Atlas `vectorSearchScore` back to a true cosine similarity.

    Atlas NORMALIZES cosine scores into [0, 1] as (1 + cosine) / 2, so an
    unrelated (orthogonal) document scores 0.5, not 0. pgvector returned the
    raw cosine (1 - cosine_distance), and DUPLICATE_EMBEDDING_THRESHOLD (0.95)
    is calibrated against that raw value -- so the score MUST be converted
    before it is compared or stored, or a raw cosine of 0.90 would arrive as
    0.95 and trip the threshold falsely.

    Verified against the live cluster: cosine 1 / 0.7071 / 0 / -1 come back as
    1.0 / 0.853553 / 0.5 / 0.0 respectively, and this inverts all four exactly.
    """
    return min(1.0, max(-1.0, 2.0 * float(atlas_score) - 1.0))


def find_closest_match(db, embedding: list[float], *, exclude_id=None):
    """Returns (document | None, similarity: float) -- the closest existing
    document by cosine similarity, and that similarity.

    Replaces pgvector's indexed `<=>` operator with Atlas Vector Search. Uses
    `exact: true` (exhaustive ENN) rather than the default approximate HNSW:
    at this corpus size (~7.5k documents, 50k hard ceiling) exact search is
    cheap, and it makes the result identical to what pgvector returned instead
    of merely close -- no approximation error, and no numCandidates to tune.

    (None, 0.0) if there is nothing to compare against -- an empty collection,
    or one where no document has an embedding yet.
    """
    from dashboard.models import COLL_UNIQUE_DOCUMENTS, VECTOR_INDEX_NAME, from_stored_vector

    # $vectorSearch must be the first stage in the pipeline, so exclusions are
    # applied afterwards. Over-fetch and filter in Python rather than
    # declaring a filter field on the vector index: absorbed documents (a merge a human has not confirmed,
    # or has confirmed) still carry an embedding, but they are NOT unique
    # documents and must never be offered as the match for a new upload.
    # Rebuilding the index to add a filter field would make duplicate detection
    # unavailable while it rebuilt, and at ~7.9k documents with exact: true the
    # extra candidates cost nothing.
    limit = 20
    pipeline = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": from_stored_vector(embedding),
                "exact": True,
                "limit": limit,
            }
        },
        {"$addFields": {"_atlas_score": {"$meta": "vectorSearchScore"}}},
        # Never drag the 768-number embedding back over the wire.
        {"$project": {"embedding": 0}},
    ]
    for doc in db[COLL_UNIQUE_DOCUMENTS].aggregate(pipeline):
        if exclude_id is not None and doc["_id"] == exclude_id:
            continue
        if doc.get("absorbed_into") is not None:
            continue
        return doc, to_cosine_similarity(doc.pop("_atlas_score"))
    return None, 0.0
