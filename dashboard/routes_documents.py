"""CRUD routes for the main table (`documents`) and the documents behind it
(`unique_documents`).

A main-table row is a pure association -- a state id, a crop id, and a pointer.
Every listing therefore $lookups its unique document, resolves the two ids to
names, and returns a complete row, so the frontend renders the table without a
second request. Stored split, served joined.

WHICH COLLECTION A FILTER HITS is decided by _DOCUMENT_FILTERS / _JOINED_FILTERS
below. Placement filters run BEFORE the join so an index does the work;
`filter[state]` / `filter[crop]` first turn the typed names into ids, then
match on those. Document filters run after the join, against the joined field.
Both are whitelisted, so arbitrary field names can't be injected.

A PATCH is routed the same way: state/crop change the row, everything else
changes the DOCUMENT and therefore every placement of it. That is the point of
the split -- editing "the document" is now one write instead of up to 66.

`GET /states`, `/crops`, `/organizations` and `/languages` read the lookups.
States and organisations can also be renamed, merged and deleted here
(dashboard/vocabulary.py); a delete is refused while anything still uses the
entry. Crops come from the crop master, maintained elsewhere, and are read-only.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request
from pymongo import ASCENDING, DESCENDING

from dashboard import vocabulary
from dashboard.config import PAGE_SIZE
from dashboard.db import get_db
from dashboard.display_id import format_display_id, format_row_id, parse_display_id, parse_row_id
from dashboard.languages import LANGUAGES, TESSDATA_BEST
from dashboard.models import (
    COLL_DOCUMENTS,
    COLL_LANGUAGES,
    COLL_ORGANIZATIONS,
    COLL_STATES,
    COLL_TRANSLATION_JOBS,
    COLL_UNIQUE_DOCUMENTS,
    normalize_format,
    unlink_placement,
    utcnow,
)
from dashboard.schemas import (
    CropOut,
    DocumentOut,
    DocumentUpdate,
    FolderOut,
    LanguageOut,
    OrganizationOut,
    Paginated,
    StateOut,
    UniqueDocumentOut,
    UniqueDocumentUpdate,
    VocabularyMerge,
    VocabularyMergeResult,
    VocabularyRename,
)

router = APIRouter()

# Sentinel returned by a filter builder when the supplied value can't possibly
# match anything (an unparseable number or date). The whole query then
# short-circuits to an empty page rather than being sent to the server.
_NO_MATCH = object()

# Sort applied to every paginated listing. `created_at` alone is not a unique
# ordering -- a bulk migration inserts thousands of rows in one pass, all with
# an identical timestamp -- and skip/limit over a non-unique sort can return the
# same row on two pages or skip one entirely. `_id` breaks the tie.
_PAGE_SORT = [("created_at", DESCENDING), ("_id", ASCENDING)]
_PAGE_SORT_AGG = {"created_at": -1, "_id": 1}

# `sort=<field>` / `sort=-<field>` on the two listings. Only the audit
# timestamps for now; no param keeps the default order above.
_SORTABLE = ("translated_at", "reviewed_at")


def _sort_stages(request: Request, prefix: str = "") -> list[dict]:
    """The aggregation stages for ?sort=, or [] for the default order.

    Documents with no value sort LAST in both directions -- most documents have
    never been translated, and "oldest first" should start at the oldest
    translation, not at thousands of blanks. `_id` breaks ties so skip/limit
    stays stable.
    """
    raw = (request.query_params.get("sort") or "").strip()
    if not raw:
        return []
    field = raw.lstrip("-")
    if field not in _SORTABLE:
        raise HTTPException(400, f"sort must be one of {', '.join(_SORTABLE)} (prefix '-' for descending)")
    path = f"{prefix}{field}"
    return [
        {"$addFields": {"_sort_missing": {"$cond": [{"$ifNull": [f"${path}", False]}, 0, 1]}}},
        {"$sort": {"_sort_missing": 1, path: -1 if raw.startswith("-") else 1, "_id": 1}},
        {"$project": {"_sort_missing": 0}},
    ]

# The join, as an aggregation stage pair. `doc` is the unique document; a row
# whose document has somehow gone missing still comes back (preserveNull...),
# because dropping it would make a listing silently under-report.
_JOIN_STAGES = [
    {"$lookup": {
        "from": COLL_UNIQUE_DOCUMENTS,
        "localField": "unique_document_id",
        "foreignField": "_id",
        "as": "doc",
    }},
    {"$unwind": {"path": "$doc", "preserveNullAndEmptyArrays": True}},
]


def _pagination(request: Request) -> tuple[int, int]:
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    return page, PAGE_SIZE


# -- filter kinds --------------------------------------------------------------
# Each whitelisted filter names a stored field and how its value is matched.
# Keeping the kind next to the field (rather than a builder function per entry)
# is what lets one place add multi-value and range handling to every filter at
# once instead of per column.
TEXT = "text"          # case-insensitive substring; comma-separated = OR
EXACT = "exact"        # equality; comma-separated = IN
INT = "int"            # equality, plus <key>_min / <key>_max
DATE = "date"          # ISO "YYYY-MM-DD" string, plus <key>_from / <key>_to
DATETIME = "datetime"  # real BSON date; a whole day, plus _from / _to
ANNAM = "annam"        # ANNAM_00042 or 42
POP = "pop"            # POP_00042 or 42


def _icontains(value: str) -> dict:
    """Case-insensitive substring match -- the MongoDB equivalent of `ilike
    '%value%'`. The value is regex-escaped so a user-supplied '.' or '*' is
    matched literally rather than interpreted as a pattern."""
    return {"$regex": re.escape(value), "$options": "i"}


def _split(value: str) -> list[str]:
    """Comma-separated values, for a multi-select. Empty parts are dropped so a
    trailing comma is harmless.

    Comma rather than a repeated `filter[state]=A&filter[state]=B` param: with
    repeats, `query_params.get()` silently returns only the LAST one, so a
    two-state selection quietly filtered on one state. A single comma-joined
    value has one obvious reading.

    A name with a comma in it ("Ministry of Jal Shakti, Government of India")
    therefore cannot be filtered as one term by name -- it splits. Filter on
    filter[crop_id] for those.
    """
    return [part.strip() for part in value.split(",") if part.strip()]


def _int_or_none(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _day_bounds(value: str):
    """A calendar date as a half-open range, so the (created_at, _id) index can
    serve it -- a $expr on an extracted date part could not."""
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


def _display_id_eq(value: str):
    parsed = parse_display_id(value)
    return _NO_MATCH if parsed is None else parsed


def _row_id_eq(value: str):
    parsed = parse_row_id(value)
    return _NO_MATCH if parsed is None else parsed


def _to_object_id(value: str, what: str = "document") -> ObjectId:
    """Path-parameter conversion. A malformed id returns 422, not 404 --
    ObjectId has no FastAPI validator, so the status FastAPI would have produced
    for a typed path param is raised by hand."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(422, f"malformed {what} id")


def _match_for(kind: str, raw: str):
    """The Mongo clause for one filter value, or _NO_MATCH if it cannot match.

    _NO_MATCH short-circuits the whole query to an empty page rather than being
    sent to the server: an unparseable filter value is an empty result set, not
    a 500 and not a silently ignored filter.
    """
    values = _split(raw)
    if not values:
        return _NO_MATCH

    if kind == TEXT:
        if len(values) == 1:
            return _icontains(values[0])
        return {"$in": [re.compile(re.escape(v), re.IGNORECASE) for v in values]}
    if kind == EXACT:
        return values[0] if len(values) == 1 else {"$in": values}
    if kind == INT:
        parsed = [_int_or_none(v) for v in values]
        if any(v is None for v in parsed):
            return _NO_MATCH
        return parsed[0] if len(parsed) == 1 else {"$in": parsed}
    if kind == DATE:
        return values[0] if len(values) == 1 else {"$in": values}
    if kind == DATETIME:
        bounds = _day_bounds(values[0])
        return _NO_MATCH if bounds is None else {"$gte": bounds[0], "$lt": bounds[1]}
    if kind in (ANNAM, POP):
        build = _display_id_eq if kind == ANNAM else _row_id_eq
        parsed = [build(v) for v in values]
        if any(v is _NO_MATCH for v in parsed):
            return _NO_MATCH
        return parsed[0] if len(parsed) == 1 else {"$in": parsed}
    raise AssertionError(f"unknown filter kind {kind!r}")


def _range_for(kind: str, low: str | None, high: str | None):
    """The clause for a <key>_min/_max or <key>_from/_to pair. One end may be
    omitted -- "at least 10 pages" is as reasonable a filter as a bounded range.
    """
    clause: dict = {}
    if kind == INT:
        for bound, op in ((low, "$gte"), (high, "$lte")):
            if bound is None:
                continue
            parsed = _int_or_none(bound)
            if parsed is None:
                return _NO_MATCH
            clause[op] = parsed
    elif kind == DATE:
        # Stored as ISO "YYYY-MM-DD" strings, which sort lexicographically, so a
        # string range IS a date range. Anything not in that shape (the field
        # accepts free-form values) simply falls outside every range rather than
        # erroring.
        for bound, op in ((low, "$gte"), (high, "$lte")):
            if bound is None:
                continue
            if _day_bounds(bound) is None:
                return _NO_MATCH
            clause[op] = bound
    elif kind == DATETIME:
        for bound, op, edge in ((low, "$gte", 0), (high, "$lt", 1)):
            if bound is None:
                continue
            bounds = _day_bounds(bound)
            if bounds is None:
                return _NO_MATCH
            clause[op] = bounds[edge]
    else:
        return _NO_MATCH
    return clause or _NO_MATCH


# filter[<key>] on fields stored on the PLACEMENT. Applied before the join.
# `state` and `crop` are not here: they are names, and the placement stores
# ids -- see _vocabulary_filter.
_DOCUMENT_FILTERS = {
    "row_id": ("row_id", POP),
    "created_at": ("created_at", DATETIME),
}


def _vocabulary_filter(db, request: Request) -> dict | None:
    """The folder filters, as a clause on the placement's id fields. None when
    nothing can match.

      filter[state]=<names>              case-insensitive substring, comma = any of
      filter[state_id]=<ids>             exact, comma = any of
      filter[crop]=<names>               matches crops AND organisations by name --
                                         it is the one "Crop" column
      filter[crop_id]=<ids>              crop master ids
      filter[organization_id]=<ids>      organisation ids
      filter[crop_kind]=crop|organization
    """
    for key in ("state", "crop"):
        # A range on a name column is a caller error; answered with an empty
        # page, as for every other text column, rather than silently ignored.
        if any(request.query_params.get(f"filter[{key}{suffix}]")
               for suffix in ("_min", "_max", "_from", "_to")):
            return None

    def ids_param(name: str) -> set | None:
        raw = request.query_params.get(f"filter[{name}]")
        if not raw:
            return None
        ids = {vocabulary.to_object_id(v) for v in _split(raw)}
        ids.discard(None)
        return ids

    clauses: list[dict] = []
    # -- state
    wanted = None
    if request.query_params.get("filter[state]"):
        wanted = set(vocabulary.ids_matching(db, "state", _split(request.query_params["filter[state]"])))
    by_id = ids_param("state_id")
    if by_id is not None:
        wanted = by_id if wanted is None else wanted & by_id
    if wanted is not None:
        if not wanted:
            return None
        clauses.append({"state_id": {"$in": sorted(wanted)}})

    # -- the folder: crop or organisation
    kind = request.query_params.get("filter[crop_kind]")
    if kind:
        if kind not in ("crop", "organization"):
            return None
        clauses.append({vocabulary.field(kind): {"$exists": True}})
    for name in ("crop_id", "organization_id"):
        ids = ids_param(name)
        if ids is not None:
            if not ids:
                return None
            clauses.append({name: {"$in": sorted(ids)}})
    raw_names = request.query_params.get("filter[crop]")
    if raw_names:
        values = _split(raw_names)
        either = [{vocabulary.field(k): {"$in": ids}}
                  for k in ("crop", "organization")
                  if (ids := vocabulary.ids_matching(db, k, values))]
        if not either:
            return None
        clauses.append({"$or": either})

    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


# filter[<key>] on fields stored on the DOCUMENT. Applied after the join, so the
# field name is prefixed with the joined alias.
#
# Every INT/DATE key also accepts a range: `<key>_min`/`<key>_max` for numbers,
# `<key>_from`/`<key>_to` for dates. Every key accepts a comma-separated list.
_JOINED_FILTERS = {
    "document_id": ("display_id", ANNAM),
    "sha256": ("sha256", TEXT),
    "shareable_name": ("shareable_name", TEXT),
    "shareable_link": ("shareable_link", TEXT),
    "language": ("language", EXACT),
    "language_source": ("language_source", EXACT),
    "num_pages": ("num_pages", INT),
    "format_original": ("format_original", EXACT),
    "translation_status": ("translation_status", EXACT),
    "review_status": ("review_status", EXACT),
    "advisory_type": ("advisory_type", TEXT),
    "advisory_scope": ("advisory_scope", TEXT),
    "advisory_name": ("advisory_name", TEXT),
    "advisory_released_org": ("advisory_released_org", TEXT),
    "advisory_org_address": ("advisory_org_address", TEXT),
    "edition_revision_volume": ("edition_revision_volume", TEXT),
    "live_source_link": ("live_source_link", TEXT),
    "season": ("season", TEXT),
    "domain": ("domain", TEXT),
    "verification_status": ("verification_status", TEXT),
    "verified_by": ("verified_by", TEXT),
    "document_status": ("document_status", TEXT),
    # The release/collection dates. NOTE: every `*_of_release` field is empty on
    # all 8,748 documents -- no corpus pass ever wrote them and they are filled
    # in by hand through the dashboard. Filtering on one is valid and returns
    # nothing, which is correct, not a bug.
    "date_of_release": ("date_of_release", DATE),
    "month_of_release": ("month_of_release", INT),
    "year_of_release": ("year_of_release", INT),
    "date_of_collection": ("date_of_collection", DATE),
    "month_of_collection": ("month_of_collection", INT),
    "year_of_collection": ("year_of_collection", INT),
    # The audit trail. *_at are real datetimes: a day match, or _from/_to.
    "translated_by": ("translated_by", TEXT),
    "translated_at": ("translated_at", DATETIME),
    "reviewed_by": ("reviewed_by", TEXT),
    "reviewed_at": ("reviewed_at", DATETIME),
}

# Fields a PATCH on a main-table row applies to the ROW; everything else goes to
# the document.
_ROW_FIELDS = {"state", "crop", "organization", "state_id", "crop_id", "organization_id"}


def _build_filter(request: Request, allowed: dict, prefix: str = "") -> dict | None:
    """Apply the whitelist to the request's filter[...] params.

    Returns the Mongo query, or None if any filter cannot match anything -- the
    caller then short-circuits to an empty page instead of querying.

    Three forms per key, all optional and combinable:
        filter[state]=Karnataka              match
        filter[state]=Karnataka,Kerala       any of (multi-select)
        filter[num_pages_min]=10             range end -- INT uses _min/_max,
        filter[date_of_collection_from]=...  DATE/DATETIME use _from/_to
    """
    query: dict = {}
    for key, (field, kind) in allowed.items():
        raw = request.query_params.get(f"filter[{key}]")
        if raw:
            value = _match_for(kind, raw)
            if value is _NO_MATCH:
                return None
            query[f"{prefix}{field}"] = value

        low_key, high_key = ("_min", "_max") if kind == INT else ("_from", "_to")
        low = request.query_params.get(f"filter[{key}{low_key}]")
        high = request.query_params.get(f"filter[{key}{high_key}]")
        if low or high:
            if kind not in (INT, DATE, DATETIME):
                return None  # a range on a text column is a caller error
            clause = _range_for(kind, low, high)
            if clause is _NO_MATCH:
                return None
            # A range alongside an exact match on the same field would overwrite
            # it; merge instead, so an impossible combination returns nothing
            # rather than silently dropping one of the two.
            existing = query.get(f"{prefix}{field}")
            if isinstance(existing, dict):
                existing.update(clause)
            elif existing is not None:
                query[f"{prefix}{field}"] = {"$eq": existing, **clause}
            else:
                query[f"{prefix}{field}"] = clause
    return query


def _empty_page(page: int, page_size: int) -> Paginated:
    return Paginated(items=[], total=0, page=page, page_size=page_size)


def _names(db) -> dict:
    """id -> name for every vocabulary, loaded once per request."""
    return {kind: vocabulary.name_map(db, kind) for kind in vocabulary.KINDS}


def _folder_of(row: dict, names: dict) -> tuple[str, str | None]:
    """(name, kind) of the folder a placement is filed under."""
    if row.get("crop_id") is not None:
        return names["crop"].get(row["crop_id"], ""), "crop"
    if row.get("organization_id") is not None:
        return names["organization"].get(row["organization_id"], ""), "organization"
    return "", None


def _document_out(row: dict, names: dict) -> DocumentOut:
    """A joined row -> the API shape. `row["doc"]` is the unique document, put
    there by _JOIN_STAGES (or by a manual find for the single-row endpoints);
    `names` is _names()."""
    doc = row.get("doc") or {}
    return DocumentOut(
        id=str(row["_id"]),
        row_id=format_row_id(row.get("row_id")) or "",
        state=names["state"].get(row.get("state_id"), ""),
        crop=_folder_of(row, names)[0],
        crop_kind=_folder_of(row, names)[1],
        state_id=str(row["state_id"]) if row.get("state_id") else None,
        crop_id=str(row["crop_id"]) if row.get("crop_id") else None,
        organization_id=str(row["organization_id"]) if row.get("organization_id") else None,
        subpath=row.get("subpath"),
        unique_document_id=str(row.get("unique_document_id", "")),
        document_id=format_display_id(doc.get("display_id")) or "",
        shareable_name=doc.get("shareable_name"),
        shareable_link=doc.get("shareable_link"),
        sha256=doc.get("sha256"),
        num_pages=doc.get("num_pages"),
        format_original=doc.get("format_original"),
        language=doc.get("language"),
        language_source=doc.get("language_source"),
        representative_file_id=doc.get("representative_file_id"),
        translation_status=doc.get("translation_status"),
        translation_file_id=doc.get("translation_zoho_file_id"),
        translation_shareable_link=doc.get("translation_shareable_link"),
        review_status=doc.get("review_status"),
        review_file_id=doc.get("review_zoho_file_id"),
        review_shareable_link=doc.get("review_shareable_link"),
        translated_by=doc.get("translated_by"),
        translated_at=doc.get("translated_at"),
        reviewed_by=doc.get("reviewed_by"),
        reviewed_at=doc.get("reviewed_at"),
        placement_count=len(doc.get("main_row_ids") or []) or 1,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _copy_places(db, docs: list[dict]) -> dict[int, tuple[str, str]]:
    """row_id -> (state name, crop name) for every copy on these documents.

    A copy entry names its placement by row_id and carries no folder of its
    own, so the folder is read from the placement. One query for a whole page.
    """
    row_ids = {c.get("row_id") for d in docs for c in (d.get("duplicate_links") or [])}
    row_ids.discard(None)
    if not row_ids:
        return {}
    names = _names(db)
    return {
        r["row_id"]: (names["state"].get(r.get("state_id"), ""), _folder_of(r, names)[0])
        for r in db[COLL_DOCUMENTS].find(
            {"row_id": {"$in": list(row_ids)}},
            {"row_id": 1, "state_id": 1, "crop_id": 1, "organization_id": 1})
    }


def _unique_out(doc: dict, places: dict[int, tuple[str, str]]) -> UniqueDocumentOut:
    """`places` is _copy_places() for a set of documents including this one."""
    data = dict(doc)
    links = []
    for link in data.get("duplicate_links") or []:
        state, crop = places.get(link.get("row_id"), (None, None))
        links.append({**link, "state": state, "crop": crop})
    data["duplicate_links"] = links
    data["id"] = str(data.pop("_id"))
    data["document_id"] = format_display_id(data.pop("display_id", None)) or ""
    data["placement_count"] = len(data.get("main_row_ids") or [])
    # The translated/review copies are downloaded through the same
    # /dashboard/files/{id}/download proxy as the original, so their Zoho ids
    # have to be visible -- the WorkDrive share link alone opens Zoho's viewer
    # and needs a Zoho login, which is why those two icons did nothing.
    data["translation_file_id"] = data.pop("translation_zoho_file_id", None)
    data["review_file_id"] = data.pop("review_zoho_file_id", None)
    # Internal-only: the Mongo ids of the placements (the API exposes them via
    # /placements) and the vectors, which are always empty and would dwarf the
    # payload if not.
    for internal in ("main_row_ids", "chunk_embeddings"):
        data.pop(internal, None)
    return UniqueDocumentOut(**data)


def _joined_one(db, row: dict) -> DocumentOut:
    row = dict(row)
    row["doc"] = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": row.get("unique_document_id")}) or {}
    return _document_out(row, _names(db))


def _unique_one(db, doc: dict) -> UniqueDocumentOut:
    return _unique_out(doc, _copy_places(db, [doc]))


# -- the main table ------------------------------------------------------------


@router.get("/documents", response_model=Paginated[DocumentOut])
def list_documents(request: Request, db=Depends(get_db)):
    page, page_size = _pagination(request)
    sort = _sort_stages(request, prefix="doc.") or [{"$sort": _PAGE_SORT_AGG}]
    row_query = _build_filter(request, _DOCUMENT_FILTERS)
    names_query = _vocabulary_filter(db, request)
    doc_query = _build_filter(request, _JOINED_FILTERS, prefix="doc.")
    if row_query is None or names_query is None or doc_query is None:
        return _empty_page(page, page_size)
    row_query.update(names_query)

    # Placement filters first so the (state_id, crop_id) index narrows the set
    # before the join runs; document filters after, because they need the
    # joined field.
    pipeline: list[dict] = []
    if row_query:
        pipeline.append({"$match": row_query})
    pipeline.extend(_JOIN_STAGES)
    if doc_query:
        pipeline.append({"$match": doc_query})

    # One round trip for the page and the count: $facet runs both branches over
    # the same filtered stream, so the filters cannot be applied differently to
    # the two the way two separate queries could drift.
    pipeline.append({"$facet": {
        "items": [*sort, {"$skip": (page - 1) * page_size}, {"$limit": page_size}],
        "total": [{"$count": "n"}],
    }})
    result = next(iter(db[COLL_DOCUMENTS].aggregate(pipeline)), {"items": [], "total": []})
    total = result["total"][0]["n"] if result["total"] else 0
    names = _names(db)
    return Paginated(
        items=[_document_out(row, names) for row in result["items"]],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/documents/{row_id}", response_model=DocumentOut)
def get_document(row_id: str, db=Depends(get_db)):
    row = db[COLL_DOCUMENTS].find_one({"_id": _to_object_id(row_id)})
    if row is None:
        raise HTTPException(404, "document not found")
    return _joined_one(db, row)


@router.get("/documents/{row_id}/siblings", response_model=list[DocumentOut])
def list_siblings(row_id: str, db=Depends(get_db)):
    """The other placements of this row's document -- the same document filed
    under a different state or crop.

    Now an exact lookup rather than a hash comparison: two rows are siblings if
    they point at the same unique document, which is what "the same document"
    means in this schema. After a team-approved merge, near-duplicates become
    siblings too.
    """
    row = db[COLL_DOCUMENTS].find_one({"_id": _to_object_id(row_id)})
    if row is None:
        raise HTTPException(404, "document not found")
    others = db[COLL_DOCUMENTS].aggregate([
        {"$match": {"unique_document_id": row["unique_document_id"], "_id": {"$ne": row["_id"]}}},
        *_JOIN_STAGES,
        {"$sort": _PAGE_SORT_AGG},
    ])
    names = _names(db)
    return [_document_out(other, names) for other in others]


@router.patch("/documents/{row_id}", response_model=DocumentOut)
def update_document(row_id: str, body: DocumentUpdate, db=Depends(get_db)):
    """Edit a row. state/crop change this placement; every other field changes
    the DOCUMENT, and therefore all of its placements."""
    oid = _to_object_id(row_id)
    row = db[COLL_DOCUMENTS].find_one({"_id": oid})
    if row is None:
        raise HTTPException(404, "document not found")

    submitted = body.model_dump(exclude_unset=True)
    row_updates = {k: v for k, v in submitted.items() if k in _ROW_FIELDS}
    doc_updates = {k: v for k, v in submitted.items() if k not in _ROW_FIELDS}

    # Moving a row: an id must name an existing entry; a state or organisation
    # name is resolved, and created if nobody has used it; a crop name must be
    # in the crop master (or be an existing organisation's name). The raw name
    # moves with it, so the OCR-language lookup and the WorkDrive folder name
    # don't silently point at the row's previous placement. Nothing on the
    # document needs touching -- its copy entries read their folder from here.
    move: dict = {}
    unset: dict = {}
    if row_updates.get("state_id") is not None or row_updates.get("state") is not None:
        if row_updates.get("state_id") is not None:
            state = vocabulary.get(db, "state", row_updates["state_id"])
            if state is None:
                raise HTTPException(400, "state_id does not name an existing state")
        else:
            state = vocabulary.resolve(db, "state", row_updates["state"])
            if state is None:
                raise HTTPException(400, "state name cannot be empty")
        move["state_id"] = state["_id"]
        move["state_raw"] = row_updates.get("state") if row_updates.get("state_id") is None else state["name"]

    folder = None  # (kind, entry, raw)
    asked = [k for k in ("crop_id", "organization_id", "crop", "organization") if row_updates.get(k) is not None]
    if len(asked) > 1:
        raise HTTPException(400, "send only one of crop, crop_id, organization, organization_id")
    if asked:
        key, value = asked[0], row_updates[asked[0]]
        if key in ("crop_id", "organization_id"):
            kind = "crop" if key == "crop_id" else "organization"
            entry = vocabulary.get(db, kind, value)
            if entry is None:
                raise HTTPException(400, f"{key} does not name an existing {kind}")
            folder = (kind, entry, entry["name"])
        elif key == "organization":
            entry = vocabulary.resolve(db, "organization", value)
            if entry is None:
                raise HTTPException(400, "organization name cannot be empty")
            folder = ("organization", entry, value)
        else:
            found = vocabulary.folder(db, value, create=False)
            if found is None:
                raise HTTPException(
                    400, f"{value!r} is not a crop in the crop master -- send it as "
                         f"\"organization\" if it is an organisation or grouping")
            folder = (found[0], found[1], value)
    if folder is not None:
        kind, entry, raw = folder
        other = "organization" if kind == "crop" else "crop"
        move[vocabulary.field(kind)] = entry["_id"]
        move["crop_raw"] = raw
        unset[vocabulary.field(other)] = ""

    if doc_updates:
        _apply_document_updates(db, row["unique_document_id"], doc_updates)
    if move:
        move["updated_at"] = utcnow()
        db[COLL_DOCUMENTS].update_one({"_id": oid}, {"$set": move, **({"$unset": unset} if unset else {})})
    return _joined_one(db, db[COLL_DOCUMENTS].find_one({"_id": oid}))


@router.delete("/documents/{row_id}", status_code=204)
def delete_document(row_id: str, db=Depends(get_db)):
    """Remove one placement.

    Only the row. The file in WorkDrive is left alone, and so is the document --
    even its last placement going away does not delete it, because its metadata
    and any translation are still worth keeping. The document's main_row_ids and
    duplicate_links are updated together so the two cannot drift.
    """
    oid = _to_object_id(row_id)
    row = db[COLL_DOCUMENTS].find_one({"_id": oid})
    if row is None:
        raise HTTPException(404, "document not found")
    db[COLL_DOCUMENTS].delete_one({"_id": oid})
    unlink_placement(db, row["unique_document_id"], row_obj_id=oid, row_id=row["row_id"])


# -- the documents behind them -------------------------------------------------


@router.delete("/unique-documents/{document_id}", status_code=204)
def delete_unique_document(document_id: str, db=Depends(get_db)):
    """Delete a document, its placements, and its files. Nothing survives.

    This is the destructive one, and deliberately unlike DELETE /documents/{id},
    which removes a single placement and leaves everything else standing. Here
    every file the document owns is deleted from WorkDrive first -- each copy in
    duplicate_links, plus the translation and the review -- and only then are
    the placement rows and the document removed from the database.

    For a document that came from the corpus crawl, those copies ARE the files
    in the shared WorkDrive repository. Deleting the document deletes them from
    there. Zoho's delete is a move to trash rather than a purge, so it can be
    undone from WorkDrive's own trash, but nothing in this dashboard can undo
    it.

    The files go first on purpose. If WorkDrive refuses one, this returns 502
    with the ids it could not remove and the database is left completely
    untouched, so the call can simply be retried -- the alternative (rows gone,
    files still there) would leave files nothing can ever reach again.
    """
    oid = _to_object_id(document_id, "document")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "document not found")

    live = db[COLL_TRANSLATION_JOBS].find_one(
        {"document_id": oid, "status": {"$in": ["queued", "running"]}}
    )
    if live is not None:
        raise HTTPException(409, "a translation job is running for this document -- cancel it first")

    # Every distinct file: the copies, the translation, the review. A file id
    # can repeat across copies when placements share one upload, so dedupe --
    # deleting the same file twice would look like a failure the second time.
    file_ids: list[str] = []
    for candidate in (
        *[c.get("zoho_file_id") for c in doc.get("duplicate_links") or []],
        doc.get("translation_zoho_file_id"),
        doc.get("review_zoho_file_id"),
    ):
        if candidate and candidate not in file_ids:
            file_ids.append(candidate)

    failed: list[str] = []
    if file_ids:
        from pop_server import _get_zoho

        wd = _get_zoho()
        for file_id in file_ids:
            try:
                if not wd.delete(file_id):
                    failed.append(file_id)
            except Exception:  # noqa: BLE001
                failed.append(file_id)
    if failed:
        raise HTTPException(
            502,
            "could not delete from WorkDrive: " + ", ".join(failed)
            + " -- nothing was removed, try again",
        )

    rows = db[COLL_DOCUMENTS].delete_many({"unique_document_id": oid}).deleted_count
    db[COLL_TRANSLATION_JOBS].delete_many({"document_id": oid})
    db[COLL_UNIQUE_DOCUMENTS].delete_one({"_id": oid})
    print(f"[delete] {format_display_id(doc.get('display_id'))}: {rows} placement(s), "
          f"{len(file_ids)} file(s) trashed in WorkDrive", flush=True)


def _apply_document_updates(db, unique_document_id, updates: dict) -> None:
    """Validate and write document-level fields. Shared by PATCH /documents and
    PATCH /unique-documents so the two cannot validate differently."""
    if "format_original" in updates:
        updates["format_original"] = normalize_format(updates["format_original"])
    if updates.get("language") is not None:
        # Must be a code from the languages collection -- that is the whole
        # reason the collection exists, so free text is refused here.
        if db[COLL_LANGUAGES].count_documents({"code": updates["language"]}, limit=1) == 0:
            raise HTTPException(400, "language must be a code from GET /dashboard/languages")
        # A person's choice is a known language, not a guess -- the same
        # category as the OCR pass having read it. Marking it "detected" is also
        # what makes it survive a re-run of
        # scripts/fill_language_from_state.py, which only recomputes "state".
        updates.setdefault("language_source", "detected")
    if updates.get("representative_file_id") is not None:
        # A document can only be anchored to one of its OWN copies. Anchoring it
        # at some other document's file would make translation act on bytes this
        # document's metadata does not describe.
        doc = db[COLL_UNIQUE_DOCUMENTS].find_one(
            {"_id": unique_document_id}, {"duplicate_links": 1}
        ) or {}
        match = next((l for l in (doc.get("duplicate_links") or [])
                      if l.get("zoho_file_id") == updates["representative_file_id"]), None)
        if match is None:
            raise HTTPException(400, "representative_file_id must be one of this document's own copies")
        updates["representative_row_id"] = match.get("row_id")
        # Re-anchoring moves the document's own link with it; leaving the old
        # one would point the document at a copy it no longer claims.
        updates["shareable_link"] = match.get("shareable_link")
        updates.setdefault("shareable_name", match.get("shareable_name"))
    updates["updated_at"] = utcnow()
    db[COLL_UNIQUE_DOCUMENTS].update_one({"_id": unique_document_id}, {"$set": updates})


# filter[<key>] on unique_documents when it is queried directly. Same fields as
# _JOINED_FILTERS, which is deliberate: a filter must mean the same thing whether
# the caller starts from the main table or from the documents list.
_UNIQUE_FILTERS = dict(_JOINED_FILTERS)


def _multi_placement(value: str):
    """filter[multi_placement]=true -- documents filed in more than one place.
    The duplication the grouping already resolved, and the natural starting
    point for a review pass."""
    if str(value).strip().lower() in ("1", "true", "yes"):
        return {"$exists": True}
    if str(value).strip().lower() in ("0", "false", "no"):
        return {"$exists": False}
    return _NO_MATCH


@router.get("/unique-documents", response_model=Paginated[UniqueDocumentOut])
def list_unique_documents(request: Request, db=Depends(get_db)):
    """The documents themselves, paginated 100/page.

    The counterpart of GET /documents: that lists PLACEMENTS (9,811 rows, the
    same document appearing once per folder it is filed in), this lists
    DOCUMENTS (8,748). A caller cannot derive one from the other by
    de-duplicating a page -- pagination is server-side, so a page of placements
    collapses to an unpredictable number of documents and the total is wrong.

    Same filter names as the main table's document-level filters, plus
    `filter[multi_placement]=true` for the documents filed in more than one
    place.
    """
    page, page_size = _pagination(request)
    sort = _sort_stages(request)
    query = _build_filter(request, _UNIQUE_FILTERS)
    if query is None:
        return _empty_page(page, page_size)

    raw = request.query_params.get("filter[multi_placement]")
    if raw:
        value = _multi_placement(raw)
        if value is _NO_MATCH:
            return _empty_page(page, page_size)
        query["main_row_ids.1"] = value

    total = db[COLL_UNIQUE_DOCUMENTS].count_documents(query)
    if sort:
        rows = list(db[COLL_UNIQUE_DOCUMENTS].aggregate([
            {"$match": query}, *sort, {"$skip": (page - 1) * page_size}, {"$limit": page_size},
        ]))
    else:
        rows = list(
            db[COLL_UNIQUE_DOCUMENTS]
            .find(query)
            .sort(_PAGE_SORT)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
    places = _copy_places(db, rows)
    return Paginated(items=[_unique_out(row, places) for row in rows], total=total,
                     page=page, page_size=page_size)


@router.get("/unique-documents/{document_id}", response_model=UniqueDocumentOut)
def get_unique_document(document_id: str, db=Depends(get_db)):
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": _to_object_id(document_id)})
    if doc is None:
        raise HTTPException(404, "unique document not found")
    return _unique_one(db, doc)


@router.patch("/unique-documents/{document_id}", response_model=UniqueDocumentOut)
def update_unique_document(document_id: str, body: UniqueDocumentUpdate, db=Depends(get_db)):
    oid = _to_object_id(document_id)
    if db[COLL_UNIQUE_DOCUMENTS].count_documents({"_id": oid}, limit=1) == 0:
        raise HTTPException(404, "unique document not found")
    updates = body.model_dump(exclude_unset=True)
    if updates:
        _apply_document_updates(db, oid, updates)
    return _unique_one(db, db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid}))


@router.get("/unique-documents/{document_id}/placements", response_model=list[DocumentOut])
def list_placements(document_id: str, db=Depends(get_db)):
    """Every main-table row that uses this document."""
    oid = _to_object_id(document_id)
    if db[COLL_UNIQUE_DOCUMENTS].count_documents({"_id": oid}, limit=1) == 0:
        raise HTTPException(404, "unique document not found")
    rows = db[COLL_DOCUMENTS].aggregate([
        {"$match": {"unique_document_id": oid}}, *_JOIN_STAGES, {"$sort": _PAGE_SORT_AGG},
    ])
    names = _names(db)
    return [_document_out(row, names) for row in rows]


# -- lookups -------------------------------------------------------------------


def _entry_out(model, row: dict, counts: dict, spellings: dict | None = None):
    raw = (spellings or {}).get(row["_id"]) if spellings is not None else row.get("raw_names")
    return model(id=str(row["_id"]), name=row["name"], raw_names=sorted(raw or []),
                 document_count=counts.get(row["_id"], 0))


def _vocabulary_call(fn, *args):
    try:
        return fn(*args)
    except vocabulary.VocabularyError as e:
        raise HTTPException(e.status, str(e))


def _narrowed_by_state(db, request: Request, kind: str) -> dict | None:
    """`?state=<name>` / `?state_id=<id>`: only entries actually used under
    that state -- what the upload form needs. {} when not narrowed, None when
    the state does not exist."""
    state_id, state_name = request.query_params.get("state_id"), request.query_params.get("state")
    if not (state_id or state_name):
        return {}
    state = vocabulary.get(db, "state", state_id) if state_id else vocabulary.find(db, "state", state_name)
    if state is None:
        return None
    f = vocabulary.field(kind)
    return {"_id": {"$in": db[COLL_DOCUMENTS].distinct(f, {"state_id": state["_id"], f: {"$exists": True}})}}


@router.get("/states", response_model=list[StateOut])
def list_states(db=Depends(get_db)):
    """The states vocabulary, for the dropdown. Read from the lookup collection
    rather than derived with distinct(), so a state the team added survives even
    before any row uses it."""
    counts = vocabulary.usage_counts(db, "state")
    return [_entry_out(StateOut, row, counts)
            for row in db[COLL_STATES].find().sort("name", ASCENDING)]


@router.get("/crops", response_model=list[CropOut])
def list_crops(request: Request, db=Depends(get_db)):
    """The crop master's crops (never its pesticides), for the dropdown.
    `?state=` / `?state_id=` narrows to the crops filed under that state.
    `raw_names` are our older spellings that resolve to each crop."""
    query = _narrowed_by_state(db, request, "crop")
    if query is None:
        return []
    counts, spellings = vocabulary.usage_counts(db, "crop"), vocabulary.crop_spellings(db)
    return sorted((_entry_out(CropOut, row, counts, spellings)
                   for row in vocabulary.entries(db, "crop", query)),
                  key=lambda c: c.name.lower())


@router.get("/organizations", response_model=list[OrganizationOut])
def list_organizations(request: Request, db=Depends(get_db)):
    """Folders that are not crops: organisations, departments, groupings.
    `?state=` / `?state_id=` narrows to those filed under that state."""
    query = _narrowed_by_state(db, request, "organization")
    if query is None:
        return []
    counts = vocabulary.usage_counts(db, "organization")
    return sorted((_entry_out(OrganizationOut, row, counts) for row in db[COLL_ORGANIZATIONS].find(query)),
                  key=lambda o: o.name.lower())


@router.get("/folders", response_model=list[FolderOut])
def list_folders(request: Request, db=Depends(get_db)):
    """The Folder dropdown -- for the Add Document form and the table's Folder
    column filter -- driven by the advisory type:

        Comprehensive, Crop Advisory   crops (crop master)
        Non-Crop Advisory              organisations
        General, blank or other        both

    `?advisory_type=` picks the rule; `?state=` / `?state_id=` narrows to
    folders actually used under that state, as for /crops.
    """
    out: list[FolderOut] = []
    for kind in vocabulary.folder_kinds_for_advisory(request.query_params.get("advisory_type")):
        query = _narrowed_by_state(db, request, kind)
        if query is None:
            return []
        counts = vocabulary.usage_counts(db, kind)
        spellings = vocabulary.crop_spellings(db) if kind == "crop" else None
        for row in vocabulary.entries(db, kind, query):
            e = _entry_out(CropOut if kind == "crop" else OrganizationOut, row, counts, spellings)
            out.append(FolderOut(kind=kind, **e.model_dump()))
    return sorted(out, key=lambda f: (f.name.lower(), f.kind))


def _create_entry(db, kind: str, body: dict, model):
    raw = (body or {}).get("name")
    if not raw or not str(raw).strip():
        raise HTTPException(400, "name is required")
    entry = vocabulary.resolve(db, kind, raw)
    return _entry_out(model, entry, vocabulary.usage_counts(db, kind))


def _rename_entry(db, kind: str, entry_id: str, body: VocabularyRename, model):
    entry = _vocabulary_call(vocabulary.rename, db, kind, entry_id, body.name)
    return _entry_out(model, entry, vocabulary.usage_counts(db, kind))


def _merge_entries(db, kind: str, entry_id: str, body: VocabularyMerge) -> VocabularyMergeResult:
    result = _vocabulary_call(vocabulary.merge, db, kind, entry_id, body.absorb)
    return VocabularyMergeResult(
        id=str(result["survivor"]["_id"]), name=result["survivor"]["name"],
        absorbed=result["absorbed"], placements_repointed=result["placements_repointed"])


@router.post("/states", response_model=StateOut, status_code=201)
def create_state(body: dict, db=Depends(get_db)):
    """Add a state to the dropdown before anything uses it. Idempotent -- posting
    an existing name (in any letter case, or a known alternative spelling)
    returns that entry rather than erroring, because the caller's intent ("make
    sure this exists") is already satisfied."""
    return _create_entry(db, "state", body, StateOut)


@router.patch("/states/{state_id}", response_model=StateOut)
def rename_state(state_id: str, body: VocabularyRename, db=Depends(get_db)):
    """Rename a state. Every placement shows the new name at once. The old name
    is kept as an alternative spelling. 409 if another state already has the
    name -- merge into that one instead."""
    return _rename_entry(db, "state", state_id, body, StateOut)


@router.post("/states/{state_id}/merge", response_model=VocabularyMergeResult)
def merge_states(state_id: str, body: VocabularyMerge, db=Depends(get_db)):
    """Fold the `absorb` states into this one: their placements move here, their
    names become alternative spellings of this one, and they are deleted."""
    return _merge_entries(db, "state", state_id, body)


@router.delete("/states/{state_id}", status_code=204)
def delete_state(state_id: str, db=Depends(get_db)):
    """Delete a state nothing uses. 409 while any placement or pending upload
    still references it."""
    _vocabulary_call(vocabulary.delete, db, "state", state_id)


@router.post("/organizations", response_model=OrganizationOut, status_code=201)
def create_organization(body: dict, db=Depends(get_db)):
    """Add an organisation or grouping. Idempotent, like POST /states."""
    return _create_entry(db, "organization", body, OrganizationOut)


@router.patch("/organizations/{organization_id}", response_model=OrganizationOut)
def rename_organization(organization_id: str, body: VocabularyRename, db=Depends(get_db)):
    """Rename, stored exactly as sent. 409 if another organisation already has
    the name -- merge into that one instead."""
    return _rename_entry(db, "organization", organization_id, body, OrganizationOut)


@router.post("/organizations/{organization_id}/merge", response_model=VocabularyMergeResult)
def merge_organizations(organization_id: str, body: VocabularyMerge, db=Depends(get_db)):
    """Fold the `absorb` organisations into this one."""
    return _merge_entries(db, "organization", organization_id, body)


@router.delete("/organizations/{organization_id}", status_code=204)
def delete_organization(organization_id: str, db=Depends(get_db)):
    """Delete an organisation nothing uses. 409 while anything references it."""
    _vocabulary_call(vocabulary.delete, db, "organization", organization_id)


# Crops are read-only here: the crop master is edited from another application.
# The write routes stay so a caller gets a clear 403 instead of a bare 405.
@router.post("/crops", status_code=201)
def create_crop(body: dict | None = None):
    raise HTTPException(403, vocabulary.READ_ONLY_CROPS)


@router.patch("/crops/{crop_id}")
def rename_crop(crop_id: str, body: dict | None = None):
    raise HTTPException(403, vocabulary.READ_ONLY_CROPS)


@router.post("/crops/{crop_id}/merge")
def merge_crops(crop_id: str, body: dict | None = None):
    raise HTTPException(403, vocabulary.READ_ONLY_CROPS)


@router.delete("/crops/{crop_id}")
def delete_crop(crop_id: str):
    raise HTTPException(403, vocabulary.READ_ONLY_CROPS)


@router.get("/languages", response_model=list[LanguageOut])
def list_languages(db=Depends(get_db)):
    """The languages vocabulary: English, the 22 Eighth Schedule languages, and
    Non-English, which is not a language but IS what the OCR pass concluded for
    761 documents, so the team needs it in the list to refine them from.
    `tessdata_best` says whether each one can be OCR'd."""
    rows = list(db[COLL_LANGUAGES].find())
    if not rows:
        # The collection is seeded by the corpus load; fall back to the static
        # list so a fresh database still serves a usable dropdown.
        return [LanguageOut(code=c, label=l, tessdata_best=c in TESSDATA_BEST)
                for c, l in sorted(LANGUAGES.items(), key=lambda kv: kv[1])]
    return sorted((LanguageOut(code=r["code"], label=r["label"],
                               tessdata_best=r.get("tessdata_best")) for r in rows),
                  key=lambda l: l.label)


@router.get("/stats")
def stats(db=Depends(get_db)):
    """Counts for the dashboard header.

    `documents` and `files` differ on purpose: a row is a placement, and one
    document filed under several crops is several rows. The gap is the
    duplication that is already resolved; `files` counts distinct documents.
    """
    rows = db[COLL_DOCUMENTS]
    docs = db[COLL_UNIQUE_DOCUMENTS]
    return {
        "documents": rows.count_documents({}),
        "files": docs.count_documents({}),
        "states": db[COLL_STATES].count_documents({}),
        "crops": vocabulary.coll(db, "crop").count_documents({"type": {"$ne": "chemical"}}),
        "organizations": db[COLL_ORGANIZATIONS].count_documents({}),
        "translated": docs.count_documents({"translation_status": "done"}),
        "reviewed": docs.count_documents({"review_status": "done"}),
    }
