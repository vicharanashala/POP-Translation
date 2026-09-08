"""MongoDB client/database setup for the dashboard package.

Sync driver (pymongo), matching the rest of this package -- the queue worker
and translation runner are plain threads, so an async driver (Motor/Beanie)
would fight the existing architecture for no gain.

There is no session or transaction object to pass around: pymongo writes apply
immediately, so `get_db()`/`get_session()` both simply hand out the Database.

Which database this is depends on POP_ENV -- see dashboard/config.py. On
staging it is SHARED with a different application, so every collection of ours
is prefixed `pop_` and ours and theirs cannot collide; they have their own
`states` and `crops`.

No Atlas Vector Search index is created here. `unique_documents.chunk_embeddings`
exists but is always empty -- fingerprints are computed and stored on local disk
and joined back by sha256, because the cluster is a 512 MB free tier.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collation import Collation
from pymongo.database import Database

from dashboard.config import _PREFIX, DB_NAME, DB_URL, POP_ENV
from dashboard.models import (
    COLL_CONFIG,
    COLL_CROPS,
    COLL_DOCUMENTS,
    COLL_LANGUAGES,
    COLL_STATES,
    COLL_TRANSLATION_JOBS,
    COLL_UNIQUE_DOCUMENTS,
    COLL_UPLOAD_QUEUE_ITEMS,
)

# Client/database are constructed lazily on first real use, not at import
# time -- DB_URL may not be set yet, and pop_server.py imports this module
# chain unconditionally at startup, so eagerly connecting here would crash the
# already-working pipeline server before a single request is served.
_client: MongoClient | None = None

# Case-insensitive comparison, passed per query for the state/crop name
# filters. Those are now free-text fields rather than rows in a lookup table,
# so a filter has to match regardless of how the caller cased it.
CI_COLLATION = Collation(locale="en", strength=2)


def _get_client() -> MongoClient:
    global _client
    if _client is None:
        if not DB_URL:
            raise RuntimeError(
                "DB_URL is not set -- the dashboard database is unavailable. "
                "Set it in .env (see docs/deployment.md) and restart."
            )
        if not DB_URL.startswith(("mongodb://", "mongodb+srv://")):
            # Say WHICH value is wrong and show only its scheme, never the
            # credentials. pymongo's own InvalidURI names neither, which made
            # this hard to place when it first appeared in production.
            raise RuntimeError(
                "The configured MongoDB URL is not a MongoDB URI (it starts "
                f"{DB_URL[:12]!r}...). POP_ENV={POP_ENV!r}, so the dashboard "
                f"reads {'DB_URL' if os.environ.get('DB_URL') else _PREFIX + '_DB_URL'}"
                " from .env. Note that `docker run --env-file` keeps the "
                "surrounding quotes that `docker compose` strips."
            )
        _client = MongoClient(DB_URL, appname="pop-render")
    return _client


def get_database() -> Database:
    return _get_client()[DB_NAME]


# Index specs, applied by init_db(). Kept here rather than scattered through
# the code so there is one place to see every constraint.
#
# Sort order note: every paginated listing sorts by (created_at DESC, _id ASC).
# `created_at` alone is not a unique ordering -- a bulk migration inserts
# thousands of documents with an identical timestamp -- and skip/limit over a
# non-unique sort can return the same document on two pages or skip one
# entirely. The compound indexes below serve that exact sort.
_INDEXES: dict[str, list[dict]] = {
    # -- the main table: pure association --------------------------------------
    COLL_DOCUMENTS: [
        # The placement's identity (POP_#####).
        {"keys": [("row_id", ASCENDING)], "unique": True, "name": "uq_row_id"},
        # Every join and every merge walks this. NOT unique: many placements
        # point at one document -- that is the whole point.
        {"keys": [("unique_document_id", ASCENDING)], "name": "unique_document_id"},
        # The main table's two filter columns. Compound so "everything in
        # Karnataka" and "Karnataka + Amaranthus" are both served by one index;
        # `crop` alone gets its own.
        {"keys": [("state", ASCENDING), ("crop", ASCENDING)], "name": "state_crop"},
        {"keys": [("crop", ASCENDING)], "name": "crop"},
        {"keys": [("created_at", DESCENDING), ("_id", ASCENDING)], "name": "created_at_id"},
    ],
    # -- one row per distinct document -----------------------------------------
    COLL_UNIQUE_DOCUMENTS: [
        {"keys": [("display_id", ASCENDING)], "unique": True, "name": "uq_display_id"},
        # UNIQUE here, unlike the old flat schema: sha256 is the grouping key
        # now, so two rows with the same hash would BE the same document. The
        # index is what enforces that the corpus load and the upload path agree.
        # partialFilterExpression rather than sparse: a document whose bytes were
        # never hashed still needs a row, and `sparse` only skips a MISSING
        # field -- an explicit sha256:null is still indexed, so several of them
        # would collide. Restricting the index to actual strings is what makes
        # unhashed documents possible.
        {"keys": [("sha256", ASCENDING)], "unique": True, "name": "uq_sha256",
         "partialFilterExpression": {"sha256": {"$type": "string"}}},
        {"keys": [("translation_status", ASCENDING)], "name": "translation_status"},
        {"keys": [("review_status", ASCENDING)], "name": "review_status"},
        # Finding the document behind a given physical copy -- used by the
        # upload path and by anything starting from a WorkDrive file id.
        {"keys": [("duplicate_links.zoho_file_id", ASCENDING)], "name": "copy_file_id"},
        {"keys": [("created_at", DESCENDING), ("_id", ASCENDING)], "name": "created_at_id"},
    ],
    # -- controlled vocabularies for the dashboard's dropdowns -----------------
    # Names are the key. These are not foreign keys (rows store the name as a
    # plain string); they exist so the agri team picks from a list instead of
    # typing a new spelling of an existing state.
    COLL_STATES: [{"keys": [("name", ASCENDING)], "unique": True, "name": "uq_name"}],
    COLL_CROPS: [{"keys": [("name", ASCENDING)], "unique": True, "name": "uq_name"}],
    COLL_LANGUAGES: [{"keys": [("code", ASCENDING)], "unique": True, "name": "uq_code"}],
    COLL_UPLOAD_QUEUE_ITEMS: [
        {"keys": [("created_at", DESCENDING), ("_id", ASCENDING)], "name": "created_at_id"},
        {"keys": [("status", ASCENDING)], "name": "status"},
    ],
    COLL_TRANSLATION_JOBS: [
        # Scoped to a unique document now, not a placement -- translating a
        # document once covers all of its placements.
        {"keys": [("document_id", ASCENDING)], "name": "document_id"},
        {"keys": [("status", ASCENDING), ("created_at", DESCENDING)], "name": "status_created_at"},
        {"keys": [("created_at", DESCENDING), ("_id", ASCENDING)], "name": "created_at_id"},
    ],
    COLL_CONFIG: [],
}


def init_db() -> None:
    """Create every collection index.

    Idempotent -- safe to call on every startup, matching the existing
    idempotent Zoho-singleton init in pop_server.py. Raises RuntimeError
    (caught and logged, not fatal) if DB_URL isn't configured yet.
    """
    db = get_database()
    for collection_name, specs in _INDEXES.items():
        collection = db[collection_name]
        for spec in specs:
            kwargs = {k: v for k, v in spec.items() if k != "keys"}
            collection.create_index(spec["keys"], **kwargs)


@contextmanager
def get_session():
    """Context-manager form for use from background threads (the upload
    worker, the translation job runner) that aren't inside a FastAPI request.

    There is no transaction to commit or roll back -- pymongo writes apply
    immediately -- so this simply yields the Database.
    """
    yield get_database()


def get_db():
    """FastAPI dependency form of get_session()."""
    yield get_database()


def close() -> None:
    """Close the client. Only needed by tests and one-off scripts."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
