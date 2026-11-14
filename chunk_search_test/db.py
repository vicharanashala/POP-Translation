"""MongoDB Atlas access for the test (your own cluster, MONGO_URI in chunk_search_test/.env).

    db = get_db()                      # database MONGO_DB (default "chunk_search_test")
    db.unique_documents                # one row per document: _id, name, link, ...
    db.doc_embeddings                  # one row per chunk: document_id, page, text, embedding
    ensure_collections(db)             # collections + unique index (idempotent)
"""
from __future__ import annotations

import os

from pymongo import ASCENDING, MongoClient

import common  # noqa: F401  (loads .env)

DB_NAME = os.environ.get("MONGO_DB", "chunk_search_test")
UNIQUE_DOCS = "unique_documents"
EMBEDDINGS = "doc_embeddings"

_client = None


def get_db():
    global _client
    if _client is None:
        _client = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=20000)
    return _client[DB_NAME]


def ensure_collections(db) -> None:
    existing = set(db.list_collection_names())
    for name in (UNIQUE_DOCS, EMBEDDINGS):
        if name not in existing:
            db.create_collection(name)
    db[EMBEDDINGS].create_index([("document_id", ASCENDING), ("page", ASCENDING),
                                 ("chunk_index", ASCENDING)], unique=True)
