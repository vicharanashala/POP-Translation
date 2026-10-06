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
from dashboard.display_id import (
    format_display_id,
    format_row_id,
    free_row_ids,
    parse_display_id,
    parse_row_id,
)
from dashboard.languages import LANGUAGES, TESSDATA_BEST
from dashboard.models import (
    COLL_DISTRICTS,
    COLL_DOCUMENTS,
    COLL_KVKS,
    COLL_LANGUAGES,
    COLL_ORGANIZATIONS,
    COLL_STATES,
    COLL_TRANSLATION_JOBS,
    COLL_UNIQUE_DOCUMENTS,
    link_placement,
    new_copy_link,
    new_document,
    normalize_format,
    unlink_placement,
    utcnow,
)
from dashboard.schemas import (
    CropOut,
    DistrictOut,
    DocumentOut,
    DocumentUpdate,
    FolderOut,
    KvkOut,
    LanguageOut,
    LocationDistrict,
    LocationKvk,
    LocationState,
    OrganizationOut,
    Paginated,
    PlacementCreate,
    StateOut,
    UniqueDocumentOut,
    UniqueDocumentUpdate,
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
PHRASE = "phrase"      # case-insensitive substring of the WHOLE value, commas included
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


# The team works in IST, so a date picked on a real timestamp means that IST
# day. Stored datetimes are naive UTC, so the IST day starts 5h30 earlier.
IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist_day_bounds(value: str):
    bounds = _day_bounds(value)
    return None if bounds is None else (bounds[0] - IST_OFFSET, bounds[1] - IST_OFFSET)


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
    if kind == PHRASE:
        # A typed search box: one phrase, trimmed only at the ends. Not split on
        # commas -- a shareable name can contain one ("Paddy, Kharif").
        phrase = raw.strip()
        return _icontains(phrase) if phrase else _NO_MATCH

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
        bounds = _ist_day_bounds(values[0])
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
            bounds = _ist_day_bounds(bound)
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


# Every filter key the API accepts, across both tables. A `filter[...]` outside
# this set is a 400 rather than something ignored.
#
# Ignoring one is how a caller gets a WRONG ANSWER instead of an error: the name
# filters (`filter[state]`, `filter[crop]`, `filter[district]`, `filter[kvk]`)
# were removed in favour of their _id forms, and a frontend still sending
# `filter[state]=Kerala` would otherwise be handed all 9,811 placements as
# though it had asked for no filter at all.
_VOCABULARY_FILTER_KEYS = frozenset({
    "state_id", "crop_id", "organization_id", "crop_kind", "district_id", "kvk_id",
})
# Accepted by GET /unique-documents only; harmless to allow on both.
_EXTRA_FILTER_KEYS = frozenset({"multi_placement"})


def _reject_unknown_filters(request: Request) -> None:
    plain = (_VOCABULARY_FILTER_KEYS | _EXTRA_FILTER_KEYS
             | set(_DOCUMENT_FILTERS) | set(_JOINED_FILTERS))
    # A range end only exists where the column has an order: _min/_max on
    # numbers, _from/_to on dates. `filter[state_id_from]` is not a filter, and
    # saying so beats answering it with the unfiltered table.
    known = set(plain)
    for table in (_DOCUMENT_FILTERS, _JOINED_FILTERS):
        for key, (_field, kind) in table.items():
            if kind == INT:
                known |= {f"{key}_min", f"{key}_max"}
            elif kind in (DATE, DATETIME):
                known |= {f"{key}_from", f"{key}_to"}
    for param in request.query_params:
        if not (param.startswith("filter[") and param.endswith("]")):
            continue
        key = param[len("filter["):-1]
        if key in known:
            continue
        removed = {"state": "state_id", "crop": "crop_id (or organization_id)",
                   "district": "district_id", "kvk": "kvk_id"}
        if key in removed:
            raise HTTPException(
                400, f"filter[{key}] no longer exists -- send filter[{removed[key]}] with the id "
                     f"from the dropdown. Vocabularies are matched by id, never by name.")
        raise HTTPException(400, f"unknown filter: filter[{key}]")


def _vocabulary_filter(db, request: Request) -> dict | None:
    """The folder filters, as a clause on the placement's id fields. None when
    nothing can match.

      filter[state_id]=<ids>             exact, comma = any of
      filter[crop_id]=<ids>              crop master ids
      filter[organization_id]=<ids>      organisation ids
      filter[crop_kind]=crop|organization

    district and kvk are NOT here. They belong to the document, so they are
    filtered with the other document fields -- see _location_filter.

    IDS, NOT NAMES. `filter[state]=kerala` and its siblings for crop, district
    and kvk are gone. A column filter is a SELECTION like every other form here:
    the dropdown lists exactly the entries that can match -- the frontend
    fetches them from /dashboard/states, /crops, /organizations, /districts,
    /kvks -- so the caller is choosing a row it already holds the id of. Matching
    a substring across names and `raw_names` on top of that gave the same
    question two answers, and the one that guessed was the one that silently
    picked up Jaipur twice (agriai lists two) or lost Keralam's rows to a filter
    still saying "Kerala".

    A malformed id is not an error here: it matches nothing, so a filter naming
    only malformed ids answers an empty page.
    """
    def ids_param(name: str) -> set | None:
        raw = request.query_params.get(f"filter[{name}]")
        if not raw:
            return None
        ids = {vocabulary.to_object_id(v) for v in _split(raw)}
        ids.discard(None)
        return ids

    clauses: list[dict] = []
    for kind in ("state",):
        ids = ids_param(f"{kind}_id")
        if ids is not None:
            if not ids:
                return None
            clauses.append({vocabulary.field(kind): {"$in": sorted(ids)}})

    # The folder: crop or organisation, the one "Crop" column in the table.
    kind = request.query_params.get("filter[crop_kind]")
    if kind:
        if kind not in ("crop", "organization"):
            return None
        clauses.append({vocabulary.field(kind): {"$exists": True}})
    folder_clauses = []
    for name in ("crop_id", "organization_id"):
        ids = ids_param(name)
        if ids is not None:
            if not ids:
                return None
            folder_clauses.append({name: {"$in": sorted(ids)}})
    if len(folder_clauses) == 2:
        # Both sent means "either of these", because they are one column: a
        # crop and an organisation can never both be set on one placement, so
        # AND-ing them would match nothing at all.
        clauses.append({"$or": folder_clauses})
    else:
        clauses.extend(folder_clauses)

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
    "sha256": ("sha256", PHRASE),
    "shareable_name": ("shareable_name", PHRASE),
    "shareable_link": ("shareable_link", PHRASE),
    "language": ("language", EXACT),
    "language_source": ("language_source", EXACT),
    "num_pages": ("num_pages", INT),
    "format_original": ("format_original", EXACT),
    "translation_status": ("translation_status", EXACT),
    "review_status": ("review_status", EXACT),
    "advisory_type": ("advisory_type", TEXT),
    "advisory_scope": ("advisory_scope", TEXT),
    "advisory_name": ("advisory_name", PHRASE),
    "advisory_released_org": ("advisory_released_org", PHRASE),
    "advisory_org_address": ("advisory_org_address", PHRASE),
    "edition_revision_volume": ("edition_revision_volume", PHRASE),
    "live_source_link": ("live_source_link", PHRASE),
    "season": ("season", TEXT),
    "domain": ("domain", TEXT),
    "verification_status": ("verification_status", TEXT),
    "uploaded_by": ("uploaded_by", TEXT),
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
_ROW_FIELDS = {"state_id", "crop_id", "organization_id"}


# The vocabulary references stored on the DOCUMENT rather than on a placement.
_LOCATION_FIELDS = ("district_id", "kvk_id")


def _location_filter(db, request: Request, prefix: str = "") -> dict | None:
    """filter[district_id] / filter[kvk_id], as a clause on the DOCUMENT.

    `prefix` is "doc." on the main table, where the document is joined in under
    that alias, and "" on the documents listing, which IS the document. The two
    listings therefore filter the same stored field, which is the point: before
    the move these lived on the placement, so "documents in Wayanad" and
    "placements in Wayanad" were different questions with no good answer for a
    document filed in three folders.

    {} when neither is asked for, None when nothing can match.
    """
    clauses: list[dict] = []
    for f in _LOCATION_FIELDS:
        raw = request.query_params.get(f"filter[{f}]")
        if not raw:
            continue
        ids = {vocabulary.to_object_id(v) for v in _split(raw)}
        ids.discard(None)
        if not ids:
            return None
        clauses.append({f"{prefix}{f}": {"$in": sorted(ids)}})
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


# -- ?sort= --------------------------------------------------------------------
#
# `sort=<column>` / `sort=-<column>`. ANY column the filters accept can also be
# sorted on -- the whitelist is derived from the filter tables above rather than
# written out again, so the two cannot drift apart. No param keeps the default
# order (_PAGE_SORT).
#
# Fields stored on the PLACEMENT, sortable only on the main table.
_SORT_ROW_FIELDS = {"row_id": "row_id", "created_at": "created_at", "updated_at": "updated_at"}
# ... and on the document, sortable only on the documents listing. (On the main
# table `created_at` means the placement's, which is what its column shows.)
_SORT_DOC_FIELDS = {"created_at": "created_at", "updated_at": "updated_at"}
# The columns whose values are NAMES the placement does not store -- it stores
# an id into a vocabulary. See _vocabulary_sort_stages.
_VOCABULARY_SORT = ("state", "crop", "district", "kvk")


def _sortable(scope: str) -> dict[str, str]:
    """Sort key -> the path to sort on, for one listing.

    `scope` is "placements" (GET /documents, where the document is joined in
    under `doc.`) or "documents" (GET /unique-documents, which IS the document).
    """
    document = {key: field for key, (field, _kind) in _JOINED_FILTERS.items()}
    if scope == "placements":
        return {**{k: f"doc.{f}" for k, f in document.items()}, **_SORT_ROW_FIELDS}
    return {**document, **_SORT_DOC_FIELDS}


def _ranked_ids(db, kinds: tuple[str, ...]) -> list:
    """Every id in these vocabularies, ordered by the name the table shows.

    Position in this list IS the sort key. Resolving the order here rather than
    with a $lookup is not an optimisation: in production the crop master lives
    in a DIFFERENT DATABASE (dashboard/config.py), and $lookup cannot cross one.
    A few hundred entries all told, so the list is small enough to hand to the
    pipeline whole.
    """
    entries = [(name, oid) for kind in kinds
               for oid, name in vocabulary.name_map(db, kind).items()]
    return [oid for _name, oid in sorted(entries, key=lambda pair: (pair[0] or "").lower())]


def _vocabulary_sort_stages(db, key: str, scope: str, descending: bool) -> list[dict]:
    """Sort by state, folder, district or KVK NAME. The placement stores an id,
    so each row's id is looked up in a rank array built from the vocabulary.

    The folder is one column over two vocabularies -- a crop or an organisation
    -- so both are ranked together and whichever id the row has is used.

    District and KVK are the document's own fields, so on the documents listing
    they are read straight off it and on the main table off the joined document.

    State and folder belong to the PLACEMENT, and a document has no single one.
    The documents listing therefore sorts by the placement holding the copy the
    document IS -- found by matching `representative_file_id` against the copy
    entries, each of which names its own placement. That row is DERIVED here
    rather than read from a stored `representative_row_id`: the stored field said
    the same thing, had five writers to keep it in step, and drifted into naming
    a deleted placement. It is also the same placement a reader displays, so the
    column still sorts by what it shows.
    """
    stages: list[dict] = []
    base = ""
    if scope == "placements" and key in ("district", "kvk"):
        base = "doc."
    elif scope == "documents" and key in ("state", "crop"):
        # The copy entries whose file IS the document -- one of them, unless the
        # document has no copies left, in which case the row sorts as a blank.
        anchor_copies = {"$filter": {
            "input": {"$ifNull": ["$duplicate_links", []]},
            "as": "l",
            "cond": {"$eq": ["$$l.zoho_file_id", "$representative_file_id"]},
        }}
        stages += [
            {"$addFields": {"_anchor_row": {"$first": anchor_copies}}},
            {"$lookup": {"from": COLL_DOCUMENTS, "localField": "_anchor_row.row_id",
                         "foreignField": "row_id", "as": "_anchor"}},
            {"$unwind": {"path": "$_anchor", "preserveNullAndEmptyArrays": True}},
        ]
        base = "_anchor."
    if key == "crop":
        # One column, two vocabularies: a folder is a crop or an organisation.
        order = _ranked_ids(db, ("crop", "organization"))
        value = {"$ifNull": [f"${base}crop_id", f"${base}organization_id"]}
    else:
        order, value = _ranked_ids(db, (key,)), f"${base}{vocabulary.field(key)}"
    return stages + [
        # -1 is $indexOfArray's "not in the list", which covers both a row with
        # no folder and an id whose entry has since gone -- either way there is
        # no name to sort by, so it goes last like any other blank.
        {"$addFields": {"_sort_rank": {"$indexOfArray": [order, value]}}},
        {"$addFields": {"_sort_missing": {"$cond": [{"$eq": ["$_sort_rank", -1]}, 1, 0]}}},
        {"$sort": {"_sort_missing": 1, "_sort_rank": -1 if descending else 1, "_id": 1}},
        {"$project": {"_sort_rank": 0, "_sort_missing": 0, "_anchor": 0}},
    ]


def _sort_stages(db, request: Request, scope: str) -> list[dict]:
    """The aggregation stages for ?sort=, or [] for the default order.

    Rows with no value sort LAST in both directions -- most documents have never
    been translated, and "oldest first" should start at the oldest translation,
    not at thousands of blanks. `_id` breaks ties so skip/limit stays stable.
    """
    raw = (request.query_params.get("sort") or "").strip()
    if not raw:
        return []
    key, descending = raw.lstrip("-"), raw.startswith("-")
    allowed = _sortable(scope)
    if key in _VOCABULARY_SORT:
        return _vocabulary_sort_stages(db, key, scope, descending)
    if key not in allowed:
        raise HTTPException(
            400, f"sort must be one of {', '.join(sorted(set(allowed) | set(_VOCABULARY_SORT)))} "
                 f"(prefix '-' for descending)")
    path = allowed[key]
    return [
        # Missing means ABSENT, tested by type -- not falsy. `$ifNull` would put
        # POP_00000, a 0-page document and an empty string at the bottom with
        # the blanks, because $cond reads 0 and "" as false.
        {"$addFields": {"_sort_missing": {
            "$cond": [{"$in": [{"$type": f"${path}"}, ["missing", "null"]]}, 1, 0]}}},
        {"$sort": {"_sort_missing": 1, path: -1 if descending else 1, "_id": 1}},
        {"$project": {"_sort_missing": 0}},
    ]


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
        # On the DOCUMENT, not the row: one district per document, shown on
        # every placement of it.
        district=names["district"].get(doc.get("district_id")),
        kvk=names["kvk"].get(doc.get("kvk_id")),
        district_id=str(doc["district_id"]) if doc.get("district_id") else None,
        kvk_id=str(doc["kvk_id"]) if doc.get("kvk_id") else None,
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


def _copy_places(db, docs: list[dict]) -> dict[int, dict]:
    """row_id -> where that placement files the copy: state and folder, by name.

    A copy entry names its placement by row_id and carries none of this itself,
    so it is read from the placement. One query for a whole page.

    No district or KVK: those belong to the document now, so they are the same
    for every copy and the document already carries them.

    This is how the documents listing shows a state and a folder at all: a
    document has no single one, so a reader takes the entry whose `zoho_file_id`
    is the document's `representative_file_id` -- the copy the document IS.
    """
    row_ids = {c.get("row_id") for d in docs for c in (d.get("duplicate_links") or [])}
    row_ids.discard(None)
    if not row_ids:
        return {}
    names = _names(db)
    return {
        r["row_id"]: {
            "state": names["state"].get(r.get("state_id"), ""),
            "crop": _folder_of(r, names)[0],
        }
        for r in db[COLL_DOCUMENTS].find(
            {"row_id": {"$in": list(row_ids)}},
            {"row_id": 1, "state_id": 1, "crop_id": 1, "organization_id": 1})
    }


def _unique_out(doc: dict, places: dict[int, tuple[str, str]], names: dict) -> UniqueDocumentOut:
    """`places` is _copy_places() for a set of documents including this one;
    `names` is _names() for resolving the document's own district and KVK."""
    data = dict(doc)
    # The document's location, by name and by id. Its own fields since the
    # anchor placement went, so the modal reads them here rather than off
    # whichever placement happened to be the anchor.
    for kind in ("district", "kvk"):
        f = vocabulary.field(kind)
        data[kind] = names[kind].get(data.get(f))
        data[f] = str(data[f]) if data.get(f) else None
    links = []
    for link in data.get("duplicate_links") or []:
        links.append({**link, **places.get(link.get("row_id"), {})})
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
    return _unique_out(doc, _copy_places(db, [doc]), _names(db))


# -- the main table ------------------------------------------------------------


@router.get("/documents", response_model=Paginated[DocumentOut])
def list_documents(request: Request, db=Depends(get_db)):
    page, page_size = _pagination(request)
    sort = _sort_stages(db, request, "placements") or [{"$sort": _PAGE_SORT_AGG}]
    _reject_unknown_filters(request)
    row_query = _build_filter(request, _DOCUMENT_FILTERS)
    names_query = _vocabulary_filter(db, request)
    doc_query = _build_filter(request, _JOINED_FILTERS, prefix="doc.")
    where = _location_filter(db, request, prefix="doc.")
    if row_query is None or names_query is None or doc_query is None or where is None:
        return _empty_page(page, page_size)
    row_query.update(names_query)
    doc_query.update(where)

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


# The placement references a PATCH may set on either endpoint -- on the row
# directly, or on a document's anchor row.



def _location_refs(db, submitted: dict) -> tuple[dict, dict]:
    """($set, $unset) for the district/kvk references in a PATCH body.

    Each arrives as an ID and must name an existing entry. An EMPTY STRING
    clears the reference, and is the only way to say "no district after all",
    since a null here means "not sent", as it does for every other field.

    The name forms (`{"district": "Wayanad"}`) are gone with every other name
    field: these are synced vocabularies picked from a dropdown, so the caller
    has the id. "" is checked before the lookup, which would otherwise try to
    read it as an ObjectId and answer 400 -- the form sends the field it edits,
    and "the user picked nothing" is the one thing it cannot say with an id.

    Both are fields of the DOCUMENT, so only PATCH /unique-documents/{id}
    carries them. PATCH /documents/{row_id} edits a placement, and a placement
    has no district of its own to edit.
    """
    move: dict = {}
    unset: dict = {}
    for kind in ("district", "kvk"):
        f = vocabulary.field(kind)
        sent = submitted.get(f)
        if sent is None:
            continue
        if not str(sent).strip():
            unset[f] = ""
            continue
        entry = vocabulary.get(db, kind, sent)
        if entry is None:
            raise HTTPException(400, f"{f} does not name an existing {kind}")
        move[f] = entry["_id"]
    return move, unset


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

    # Moving a row: IDS ONLY. Every vocabulary is read-only and picked from a
    # dropdown, so the caller holds the id -- `{"state": "State Kerala"}` and
    # its siblings are gone. `state_raw` / `crop_raw` follow as provenance, so
    # the row stays traceable to a folder, but nothing reads them. Nothing on
    # the document needs touching -- its copy entries read their folder here.
    move: dict = {}
    unset: dict = {}
    if row_updates.get("state_id") is not None:
        state = vocabulary.get(db, "state", row_updates["state_id"])
        if state is None:
            raise HTTPException(400, "state_id does not name an existing state")
        move["state_id"] = state["_id"]
        move["state_raw"] = state["name"]

    folder = None  # (kind, entry)
    asked = [k for k in ("crop_id", "organization_id") if row_updates.get(k) is not None]
    if len(asked) > 1:
        raise HTTPException(400, "send only one of crop_id, organization_id")
    if asked:
        key = asked[0]
        kind = "crop" if key == "crop_id" else "organization"
        entry = vocabulary.get(db, kind, row_updates[key])
        if entry is None:
            raise HTTPException(400, f"{key} does not name an existing {kind}")
        folder = (kind, entry)

    if folder is not None:
        kind, entry = folder
        other = "organization" if kind == "crop" else "crop"
        move[vocabulary.field(kind)] = entry["_id"]
        move["crop_raw"] = entry["name"]
        unset[vocabulary.field(other)] = ""

    if doc_updates:
        _apply_document_updates(db, row["unique_document_id"], doc_updates)
    if move or unset:
        # `unset` alone is a real change -- moving a placement from a crop to
        # an organisation clears the other field -- so this cannot be gated on
        # `move` alone.
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


def _apply_document_updates(db, unique_document_id, updates: dict, unset: dict | None = None) -> None:
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
        # Re-anchoring moves the document's own link with it; leaving the old
        # one would point the document at a copy it no longer claims.
        updates["shareable_link"] = match.get("shareable_link")
        updates.setdefault("shareable_name", match.get("shareable_name"))
    updates["updated_at"] = utcnow()
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": unique_document_id},
        {"$set": updates, **({"$unset": unset} if unset else {})})


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


def _documents_placed_like(db, request: Request) -> dict | None:
    """The clause restricting the documents listing to documents PLACED a given
    way -- filter[state_id] and the folder ids, which are otherwise
    main-table-only because they live on the placement rather than on the
    document. District and kvk are NOT here: the document owns those, so
    _location_filter matches them directly.

    Matches on ANY placement: "the documents filed in Kerala" is every document
    with a Kerala placement, the same set the main table's filter describes,
    lifted from placements to documents. Note the row still DISPLAYS the state
    of the copy it IS, and 59 of 8,749 documents are filed in more than one
    state -- for those the filter can match on a placement other than the one
    shown.

    {} when nothing is being filtered, None when nothing can match.
    """
    clause = _vocabulary_filter(db, request)
    if clause is None:
        return None
    if not clause:
        return {}
    # One hop: which documents those placements belong to. The corpus is ~9,800
    # placements, so this reads at most that many ids and usually far fewer.
    return {"_id": {"$in": db[COLL_DOCUMENTS].distinct("unique_document_id", clause)}}


@router.get("/unique-documents", response_model=Paginated[UniqueDocumentOut])
def list_unique_documents(request: Request, db=Depends(get_db)):
    """The documents themselves, paginated 100/page.

    The counterpart of GET /documents: that lists PLACEMENTS (9,811 rows, the
    same document appearing once per folder it is filed in), this lists
    DOCUMENTS (8,748). A caller cannot derive one from the other by
    de-duplicating a page -- pagination is server-side, so a page of placements
    collapses to an unpredictable number of documents and the total is wrong.

    Same filter names as the main table's document-level filters, plus the
    placement filters (state, crop, district, kvk and their _id forms) and
    `filter[multi_placement]=true` for the documents filed in more than one
    place.
    """
    _reject_unknown_filters(request)
    page, page_size = _pagination(request)
    sort = _sort_stages(db, request, "documents")
    query = _build_filter(request, _UNIQUE_FILTERS)
    if query is None:
        return _empty_page(page, page_size)

    placed = _documents_placed_like(db, request)
    where = _location_filter(db, request)
    if placed is None or where is None:
        return _empty_page(page, page_size)
    query.update(placed)
    query.update(where)

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
    names = _names(db)
    return Paginated(items=[_unique_out(row, places, names) for row in rows], total=total,
                     page=page, page_size=page_size)


@router.get("/unique-documents/{document_id}", response_model=UniqueDocumentOut)
def get_unique_document(document_id: str, db=Depends(get_db)):
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": _to_object_id(document_id)})
    if doc is None:
        raise HTTPException(404, "unique document not found")
    return _unique_one(db, doc)


@router.patch("/unique-documents/{document_id}", response_model=UniqueDocumentOut)
def update_unique_document(document_id: str, body: UniqueDocumentUpdate, db=Depends(get_db)):
    """Edit a document. EVERYTHING lands on the document, and therefore shows on
    all of its placements -- district and kvk included.

    District and kvk used to be the exception: they were stored on a placement,
    so a document-level edit had to choose one placement to write to (its anchor)
    and refused when that placement had gone. They are the document's own fields
    now, so there is no exception left and nothing to choose.
    """
    oid = _to_object_id(document_id)
    if db[COLL_UNIQUE_DOCUMENTS].count_documents({"_id": oid}, limit=1) == 0:
        raise HTTPException(404, "unique document not found")
    updates = body.model_dump(exclude_unset=True)
    # "" clears a reference, which `_apply_document_updates` would otherwise
    # store as the empty string -- so these two are resolved to ids here and
    # applied as a $set/$unset pair.
    refs, unset = _location_refs(db, updates)
    for f in _LOCATION_FIELDS:
        updates.pop(f, None)
    updates.update(refs)
    if updates or unset:
        _apply_document_updates(db, oid, updates, unset=unset)
    return _unique_one(db, db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid}))


# NO ANCHOR PLACEMENT. _apply_anchor_refs() used to write a document-level
# district or kvk onto one chosen placement -- the document's anchor row --
# because those two fields were stored on the placement while the form that
# edits them is per document. It needed a stored `representative_row_id`, it
# refused with a 409 when that row had been deleted, and it wrote a Kerala
# district onto a Kerala placement while the document's Karnataka placement
# showed nothing. Moving both fields onto the document removed the question.


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


def _document_for_placement(db, raw: str) -> dict:
    """The unique document an id names, however the caller spells it.

    FOUR FORMS, because the main table and the document modal hold different
    ids for the same thing and either is a reasonable thing to send: a document
    by ObjectId or ANNAM id, or a PLACEMENT by ObjectId or POP id, which is
    resolved to the document it points at. A row id is accepted precisely
    because the row is what the person clicked.

    ORDER MATTERS, and getting it wrong is not cosmetic. parse_display_id()
    accepts a BARE number as well as "ANNAM_00042", so the 24-zero ObjectId --
    the one every test reaches for as "an id that names nothing" -- parses as
    display id 0 and finds ANNAM_00000, a real corpus document. Trying the
    ObjectId first is not enough on its own, because that string IS a valid
    ObjectId that matches nothing; the human forms are therefore accepted only
    with their prefix, where no digits-only string can collide with them.
    """
    oid = vocabulary.to_object_id(raw)
    if oid is not None:
        doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
        if doc is not None:
            return doc
        row = db[COLL_DOCUMENTS].find_one({"_id": oid})
        if row is not None:
            doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": row["unique_document_id"]})
            if doc is not None:
                return doc
    prefixed = str(raw or "").strip().upper()
    if prefixed.startswith("ANNAM_"):
        display = parse_display_id(prefixed)
        if display is not None:
            doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"display_id": display})
            if doc is not None:
                return doc
    if prefixed.startswith("POP_"):
        row_number = parse_row_id(prefixed)
        if row_number is not None:
            row = db[COLL_DOCUMENTS].find_one({"row_id": row_number})
            if row is not None:
                doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": row["unique_document_id"]})
                if doc is not None:
                    return doc
    raise HTTPException(404, "unique document not found")


@router.post("/unique-documents/{document_id}/placements",
             response_model=DocumentOut, status_code=201)
def create_placement(document_id: str, body: PlacementCreate, db=Depends(get_db)):
    """File a document that already exists under one more state + folder.

    The counterpart to PATCH /documents/{row_id}, which MOVES a placement: this
    adds one, leaving the others alone. Until now the only way to file an
    existing document somewhere else was to upload the file again and take the
    /uploads/{id}/add branch, which needed the bytes in hand for a document the
    database already had.

    NO SECOND FILE IS CREATED, and that is the whole reason this can exist. A
    dashboard upload writes one file into one flat WorkDrive folder and files it
    in several places, so several placements already share one `zoho_file_id`;
    the new row reuses the document's anchor copy, and link_placement() adds no
    `duplicate_links` entry because that file is already listed. So the new
    row's "Original" download serves the same file as the anchor's -- which is
    the truth, not an approximation.

    A document with NO copy links is still filed, and this used to refuse it. A
    placement is an association -- (document, state, folder) -- and needs no
    file of its own to exist; the "Original" download reads
    `representative_file_id` off the DOCUMENT (routes_files._NAMED_KINDS), not
    off the row and not off `duplicate_links`. The refusal was reachable too
    easily: deleting a document's only placement used to empty its
    `duplicate_links`, so a freshly uploaded document whose placement was
    removed could never be filed again, while its file sat in WorkDrive the
    whole time. unlink_placement no longer does that, and this restores the
    entry from the anchor file id for a document already left in that state.

    Ids only, and exactly one folder. No district or kvk: those are the
    DOCUMENT's fields, so a new placement neither carries nor changes them --
    it inherits whatever the document already says. 409 if the document is
    already filed under this exact state + folder.
    """
    doc = _document_for_placement(db, document_id)

    state = vocabulary.get(db, "state", body.state_id)
    if state is None:
        raise HTTPException(400, "state_id does not name an existing state")

    sent = [k for k in ("crop_id", "organization_id") if getattr(body, k)]
    if len(sent) != 1:
        raise HTTPException(400, "send exactly one of crop_id, organization_id")
    kind = "crop" if sent[0] == "crop_id" else "organization"
    folder = vocabulary.get(db, kind, getattr(body, sent[0]))
    if folder is None:
        raise HTTPException(400, f"{sent[0]} does not name an existing {kind}")

    clash = db[COLL_DOCUMENTS].find_one({
        "unique_document_id": doc["_id"],
        "state_id": state["_id"],
        vocabulary.field(kind): folder["_id"],
    })
    if clash is not None:
        raise HTTPException(
            409,
            f"{format_display_id(doc.get('display_id'))} is already filed under "
            f"{state['name']} / {folder['name']} as {format_row_id(clash.get('row_id'))}",
        )

    # The copy the document IS, if it lists one. With no links at all the new
    # row carries none either -- unless the anchor FILE is still known, in which
    # case the link is rebuilt from it and the document gets its download and
    # its translation source back.
    anchor = next((c for c in (doc.get("duplicate_links") or [])
                   if c.get("zoho_file_id") == doc.get("representative_file_id")), None)
    if anchor is None:
        anchor = next(iter(doc.get("duplicate_links") or []), None)
    if anchor is None and doc.get("representative_file_id"):
        anchor = {"zoho_file_id": doc["representative_file_id"],
                  "shareable_link": doc.get("shareable_link"),
                  "shareable_name": doc.get("shareable_name")}

    row_id = free_row_ids(db, 1)[0]
    row = new_document(
        row_id=row_id,
        unique_document_id=doc["_id"],
        state_id=state["_id"],
        # The folder names as filed. Not read by anything that infers -- the OCR
        # language keys off the state's LGD code -- but a placement still has to
        # be findable by the name its folder has.
        state_raw=state["name"],
        crop_raw=folder["name"],
        **{vocabulary.field(kind): folder["_id"]},
    )
    row["_id"] = db[COLL_DOCUMENTS].insert_one(row).inserted_id
    link_placement(
        db, doc["_id"],
        row_obj_id=row["_id"],
        row_id=row_id,
        copy_link=None if anchor is None else new_copy_link(
            zoho_file_id=anchor.get("zoho_file_id"),
            shareable_link=anchor.get("shareable_link"),
            shareable_name=anchor.get("shareable_name"),
        ),
    )
    return _joined_one(db, db[COLL_DOCUMENTS].find_one({"_id": row["_id"]}))


# -- lookups -------------------------------------------------------------------


# The extra fields each vocabulary's output model carries beyond name and count:
# the LGD code and the parent reference, read straight off the stored entry.
_ENTRY_EXTRAS = {
    "DistrictOut": (("code", "district_code"), ("state_id", "state_id")),
    "KvkOut": (("code", "kvk_code"), ("address", "address"),
               ("district_id", "district_id"), ("state_id", "state_id")),
    "StateOut": (("code", "state_code"),),
}


def _entry_out(model, row: dict, counts: dict):
    """One vocabulary entry as the dropdown needs it: id, name, code, parent,
    and how many placements use it.

    No `raw_names`. They are still stored -- provenance for the folder spellings
    the crawl found -- but nothing resolves them any more, so serving them
    invited a caller to match on one.
    """
    extras = {}
    for out_name, stored in _ENTRY_EXTRAS.get(model.__name__, ()):
        value = row.get(stored)
        extras[out_name] = str(value) if isinstance(value, ObjectId) else value
    return model(id=str(row["_id"]), name=row["name"],
                 document_count=counts.get(row["_id"], 0), **extras)


def _narrowed_by_state(db, request: Request, kind: str) -> dict | None:
    """`?state_id=<id>`: only entries actually used under that state -- what the
    upload form needs. {} when not narrowed, None when the state does not exist.

    By id. `?state=<name>` is gone: the caller picked the state from
    /dashboard/states, so it holds the id.
    """
    state_id = request.query_params.get("state_id")
    if not state_id:
        return {}
    state = vocabulary.get(db, "state", state_id)
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


def _names_in(db, field: str) -> list[str]:
    """Every distinct non-empty name in one audit field, A-Z ignoring case --
    the options for that column's filter dropdown. Read from the documents
    themselves, so it lists exactly the names that can match."""
    names = {n.strip() for n in db[COLL_UNIQUE_DOCUMENTS].distinct(field) if isinstance(n, str) and n.strip()}
    return sorted(names, key=str.casefold)


@router.get("/uploaded-by", response_model=list[str])
def list_uploaders(db=Depends(get_db)):
    return _names_in(db, "uploaded_by")


@router.get("/translated-by", response_model=list[str])
def list_translators(db=Depends(get_db)):
    return _names_in(db, "translated_by")


@router.get("/reviewed-by", response_model=list[str])
def list_reviewers(db=Depends(get_db)):
    return _names_in(db, "reviewed_by")


@router.get("/crops", response_model=list[CropOut])
def list_crops(request: Request, db=Depends(get_db)):
    """The crop master's crops (never its pesticides), for the dropdown.
    `?state_id=` narrows to the crops filed under that state."""
    query = _narrowed_by_state(db, request, "crop")
    if query is None:
        return []
    counts = vocabulary.usage_counts(db, "crop")
    return sorted((_entry_out(CropOut, row, counts)
                   for row in vocabulary.entries(db, "crop", query)),
                  key=lambda c: c.name.lower())


@router.get("/organizations", response_model=list[OrganizationOut])
def list_organizations(request: Request, db=Depends(get_db)):
    """Folders that are not crops: organisations, departments, groupings.
    `?state_id=` narrows to those filed under that state."""
    query = _narrowed_by_state(db, request, "organization")
    if query is None:
        return []
    counts = vocabulary.usage_counts(db, "organization")
    return sorted((_entry_out(OrganizationOut, row, counts) for row in db[COLL_ORGANIZATIONS].find(query)),
                  key=lambda o: o.name.lower())


def _by_parent(db, request: Request, kind: str) -> list[dict] | None:
    """The entries a dropdown should offer, narrowed to one parent if asked.

    Narrowed by PARENT REFERENCE, never by what placements already use: the Add
    Document and Edit forms need the whole official list, and "already used" is
    empty until people start tagging documents -- which is exactly why the
    dropdowns were empty before the LGD sync.

    Unnarrowed returns everything, which is what the column FILTER dropdowns
    want: a filter should offer every value that could match, whatever state is
    selected elsewhere. None when the named parent does not exist.
    """
    parent_kind, _f = vocabulary.PARENT[kind]
    by_id = request.query_params.get(f"{parent_kind}_id")
    if not by_id:
        return list(vocabulary.entries(db, kind, None))
    parent = vocabulary.get(db, parent_kind, by_id)
    if parent is None:
        return None
    return vocabulary.children_of(db, kind, parent["_id"])


@router.get("/districts", response_model=list[DistrictOut])
def list_districts(request: Request, db=Depends(get_db)):
    """Districts, synced from LGD. `?state_id=` narrows to that
    state's districts -- what the Add Document and Edit forms use. Unnarrowed
    returns all of them, for the column filter dropdown."""
    rows = _by_parent(db, request, "district")
    if rows is None:
        return []
    counts = vocabulary.usage_counts(db, "district")
    return [_entry_out(DistrictOut, row, counts) for row in rows]


@router.get("/kvks", response_model=list[KvkOut])
def list_kvks(request: Request, db=Depends(get_db)):
    """KVKs, synced from LGD. `?district_id=` narrows to that
    district's KVKs -- what the forms use. Unnarrowed returns all of them, for
    the column filter dropdown."""
    rows = _by_parent(db, request, "kvk")
    if rows is None:
        return []
    counts = vocabulary.usage_counts(db, "kvk")
    return [_entry_out(KvkOut, row, counts) for row in rows]


@router.get("/locations", response_model=list[LocationState])
def list_locations(db=Depends(get_db)):
    """Every state with its districts, and every district with its KVKs, in ONE
    request -- fetched once when the frontend loads, like the audit name lists.

    Stored flat (a district holds its state_id, a KVK its district_id) and
    assembled here, so there is one copy of the relationship rather than a
    nested one that can drift out of step with the references placements use.
    Five queries whatever the size: 37 states, ~790 districts, 727 KVKs, plus
    the two shared "All" rows, which hang under no parent and are added to every
    node here.
    """
    # The shared "All" rows are parentless, so they group under no key and have
    # to be added to every node by hand. Held out of the grouping first, or they
    # would land under a `None` parent and be lost.
    all_district = vocabulary.all_entry(db, "district")
    all_kvk = vocabulary.all_entry(db, "kvk")
    kvks_by_district: dict = {}
    for k in vocabulary.entries(db, "kvk", None):
        if k.get(vocabulary.IS_ALL):
            continue
        kvks_by_district.setdefault(k.get("district_id"), []).append(k)
    districts_by_state: dict = {}
    for d in vocabulary.entries(db, "district", None):
        if d.get(vocabulary.IS_ALL):
            continue
        districts_by_state.setdefault(d.get("state_id"), []).append(d)

    def ordered(rows):
        # "All" first, then A-Z -- the order a dropdown wants.
        return sorted(rows, key=lambda e: (e["name"] != vocabulary.ALL, (e.get("name") or "").lower()))

    def with_all(rows, shared):
        return ordered(rows + ([shared] if shared is not None else []))

    out = []
    for state in ordered(db[COLL_STATES].find()):
        districts = []
        for d in with_all(districts_by_state.get(state["_id"], []), all_district):
            districts.append(LocationDistrict(
                id=str(d["_id"]), name=d["name"], code=d.get("district_code"),
                kvks=[LocationKvk(id=str(k["_id"]), name=k["name"], code=k.get("kvk_code"),
                                  address=k.get("address"))
                      for k in with_all(kvks_by_district.get(d["_id"], []), all_kvk)]))
        out.append(LocationState(id=str(state["_id"]), name=state["name"],
                                 code=state.get("state_code"), districts=districts))
    return out


@router.get("/folders", response_model=list[FolderOut])
def list_folders(request: Request, db=Depends(get_db)):
    """The Folder dropdown -- for the Add Document form and the table's Folder
    column filter -- driven by the advisory type:

        Comprehensive, Crop Advisory   crops (crop master)
        Non-Crop Advisory              organisations
        General, blank or other        both

    `?advisory_type=` picks the rule; `?state_id=` narrows to
    folders actually used under that state, as for /crops.
    """
    out: list[FolderOut] = []
    for kind in vocabulary.folder_kinds_for_advisory(request.query_params.get("advisory_type")):
        query = _narrowed_by_state(db, request, kind)
        if query is None:
            return []
        counts = vocabulary.usage_counts(db, kind)
        for row in vocabulary.entries(db, kind, query):
            e = _entry_out(CropOut if kind == "crop" else OrganizationOut, row, counts)
            out.append(FolderOut(kind=kind, **e.model_dump()))
    return sorted(out, key=lambda f: (f.name.lower(), f.kind))


# NO VOCABULARY WRITE ROUTES. There used to be twenty -- create, rename, merge
# and delete for states, organisations, districts, KVKs and crops -- and every
# one of them is gone rather than left to answer 403.
#
# Every vocabulary comes from somewhere else: states, districts and KVKs from
# the LGD tables in `agriai` (scripts/sync_lgd.py), crops from the crop master,
# organisations from the WorkDrive crawl. An edit here would be undone by the
# next sync, and a create turned every typo into a new folder. A request names
# an entry by ID and nothing else, so there is no name for a form to invent.
#
# The mechanism survives in vocabulary.rename()/merge()/delete() for a future
# admin tool; it refuses every kind while the fourth column of KINDS is False,
# which is the seam to reopen if folder standardisation needs organisation
# merges back.


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
