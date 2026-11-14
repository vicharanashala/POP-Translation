"""Light English CPU embedder: BAAI/bge-small-en-v1.5 (384 dims, cosine).

    embed_passages(["text", ...]) -> list[list[float]]   # for chunks
    embed_query("question")       -> list[float]         # adds bge's query instruction

Vectors are L2-normalised, so cosine similarity is a dot product.
"""
from __future__ import annotations

import threading

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMS = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_model = None
_lock = threading.Lock()


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                import torch
                from sentence_transformers import SentenceTransformer
                torch.set_num_threads(max(1, torch.get_num_threads()))
                _model = SentenceTransformer(MODEL_NAME, device="cpu")
                _model.max_seq_length = 384
    return _model


def _encode(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    vecs = get_model().encode(texts, batch_size=batch_size, normalize_embeddings=True,
                              convert_to_numpy=True, show_progress_bar=False)
    return [[round(float(x), 6) for x in v] for v in vecs]


def embed_passages(texts: list[str]) -> list[list[float]]:
    return _encode(texts)


def embed_query(query: str) -> list[float]:
    return _encode([QUERY_PREFIX + query])[0]
