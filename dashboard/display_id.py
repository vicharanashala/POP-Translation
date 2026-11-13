"""Human-readable sequential ids shown in place of a raw ObjectId.

TWO SERIES, one per collection:

    ANNAM_00000..ANNAM_50000   unique_documents.display_id   names the DOCUMENT
    POP_00000..POP_50000       documents.row_id              names the PLACEMENT

The ANNAM id is the one the agri team knows and the one the previous schema
used, so it stays on the document. The POP id names one row of the main table:
this document, filed under this state and crop. A team-approved merge repoints a placement at a different document and
never renumbers it, so a POP id is stable for the life of the row.

Allocation always picks the SMALLEST number not currently used -- when a row is
deleted its number is immediately free again, because "in use" is derived live
from the stored values rather than tracked in a counter that could drift out of
sync with reality.

Concurrency: the unique index is the arbiter. Two racing allocators can compute
the same number, and the loser's insert fails with DuplicateKeyError.
allocate_and_insert() retries on exactly that.

50000 is a hard ceiling per the user. The corpus is ~9.8k placements over ~7.9k
documents, so both pools have ample room.
"""
from __future__ import annotations

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from dashboard.models import COLL_DOCUMENTS, COLL_UNIQUE_DOCUMENTS

DISPLAY_ID_PREFIX = "ANNAM"
ROW_ID_PREFIX = "POP"
DISPLAY_ID_MAX = 50000

# The two series, so callers name a series rather than repeating a
# (collection, field) pair that could be mismatched.
DOC_SERIES = (COLL_UNIQUE_DOCUMENTS, "display_id", DISPLAY_ID_PREFIX)
ROW_SERIES = (COLL_DOCUMENTS, "row_id", ROW_ID_PREFIX)


def _allocate(db: Database, series) -> int:
    """Smallest value not currently in use in this series.

    Scans the unique index in order and returns the first gap. A covered query
    (only the indexed field is read), so it stays cheap at this size.
    """
    collection, field, _prefix = series
    cursor = db[collection].find({field: {"$exists": True}}, {field: 1, "_id": 0}).sort(field, 1)
    candidate = 0
    for row in cursor:
        current = row.get(field)
        if current is None:
            continue
        if current > candidate:
            break  # found the gap
        if current == candidate:
            candidate += 1
    if candidate > DISPLAY_ID_MAX:
        raise RuntimeError(f"{collection}.{field} pool exhausted (0..{DISPLAY_ID_MAX})")
    return candidate


def _free(db: Database, series, count: int) -> list[int]:
    """The `count` smallest unused values, ascending.

    Bulk equivalent of calling _allocate() `count` times, which rescans the
    collection every call -- fine for one upload, quadratic across a migration's
    thousands of inserts. Produces the identical result.
    """
    collection, field, _prefix = series
    used = {
        row[field]
        for row in db[collection].find({field: {"$exists": True}}, {field: 1, "_id": 0})
        if row.get(field) is not None
    }
    free, candidate = [], 0
    while len(free) < count:
        if candidate > DISPLAY_ID_MAX:
            raise RuntimeError(f"{collection}.{field} pool exhausted (0..{DISPLAY_ID_MAX})")
        if candidate not in used:
            free.append(candidate)
        candidate += 1
    return free


def allocate_display_id(db: Database) -> int:
    """Next free ANNAM number (unique_documents)."""
    return _allocate(db, DOC_SERIES)


def free_display_ids(db: Database, count: int) -> list[int]:
    """The `count` smallest free ANNAM numbers."""
    return _free(db, DOC_SERIES, count)


def allocate_row_id(db: Database) -> int:
    """Next free POP number (documents)."""
    return _allocate(db, ROW_SERIES)


def free_row_ids(db: Database, count: int) -> list[int]:
    """The `count` smallest free POP numbers."""
    return _free(db, ROW_SERIES, count)


def allocate_and_insert(db: Database, build_document, *, series=DOC_SERIES, retries: int = 5):
    """Insert a row, allocating its id and retrying if another writer claimed
    the same number first.

    `build_document(value)` returns the document to insert. Returns the
    inserted document (including its _id).
    """
    collection, _field, _prefix = series
    for attempt in range(retries + 1):
        value = _allocate(db, series)
        document = build_document(value)
        try:
            result = db[collection].insert_one(document)
        except DuplicateKeyError:
            # Both collections have more than one unique index now (sha256 is
            # unique on unique_documents), so a duplicate key is NOT necessarily
            # a lost id race -- a re-inserted sha raises here too. Retrying is
            # still correct: the sha case raises again on the last attempt
            # rather than silently inserting a second copy of a document.
            if attempt >= retries:
                raise
            continue
        document["_id"] = result.inserted_id
        return document
    raise RuntimeError(f"could not allocate a free id for {collection}")


def format_display_id(display_id: int | None) -> str | None:
    """42 -> 'ANNAM_00042'."""
    if display_id is None:
        return None
    return f"{DISPLAY_ID_PREFIX}_{display_id:05d}"


def format_row_id(row_id: int | None) -> str | None:
    """42 -> 'POP_00042'."""
    if row_id is None:
        return None
    return f"{ROW_ID_PREFIX}_{row_id:05d}"


def _parse(value: str | None, prefix: str) -> int | None:
    if value is None:
        return None
    digits = str(value).strip().upper()
    if digits.startswith(f"{prefix}_"):
        digits = digits[len(prefix) + 1 :]
    try:
        return int(digits)
    except ValueError:
        return None


def parse_display_id(value: str | None) -> int | None:
    """Inverse of format_display_id: 'ANNAM_00042' or '42' -> 42, else None."""
    return _parse(value, DISPLAY_ID_PREFIX)


def parse_row_id(value: str | None) -> int | None:
    """Inverse of format_row_id: 'POP_00042' or '42' -> 42, else None."""
    return _parse(value, ROW_ID_PREFIX)
