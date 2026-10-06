"""Upload endpoints.

POST enqueues and returns immediately; GET polls for the frontend's progress
box; the three decision endpoints resolve an item a person is looking at.

    POST   /uploads              queued -> hashing -> checking_duplicate
                                 -> awaiting_review          (waits, indefinitely)
    POST   /uploads/{id}/add     file the NEW placements onto an existing
                                 document  {"document_id": "ANNAM_00321"}
    POST   /uploads/{id}/new     create a separate document
    POST   /uploads/{id}/cancel  discard; nothing was uploaded
    DELETE /uploads/{id}         same as cancel, also clears finished rows

Every item stops at `awaiting_review` whether or not anything matched -- the
duplicate check is a placeholder (exact sha256 only, see dashboard/queue_worker)
so the person is the real check. The pause happens before the Zoho upload, so
cancelling leaves nothing behind in WorkDrive.

WHICH DECISIONS ARE AVAILABLE is not the frontend's guess: each candidate in
`GET /uploads/{id}` carries `can_add` and `can_create_new`, and this module
enforces the same rules.

  - `add` needs a candidate with at least one new placement. Filing a document
    where it already is would create a second row for the same folder.
  - `new` is refused when an exact sha256 candidate exists, because sha256 is
    unique on `unique_documents`: a second document for byte-identical bytes
    cannot be stored, only filed under more places.

PLACEMENTS. `placements_json` is a list of per-state folder groups, which is how
the form is actually filled in -- a state, then the folders for that state, then
another state. IDS, never names:

    placements_json='[{"state_id":"6a9d...53","crop_ids":["6a9d...a1","6a9d...b2"]},
                      {"state_id":"6a9d...60","organization_ids":["6a9d...c3"]}]'

A group names `crop_ids` and/or `organization_ids`, and nothing else -- see
_parse_placements. Every id must already name an entry: no vocabulary is
writable here, so an upload cannot introduce a state, crop, organisation,
district or KVK, and an id that names nothing is refused at submit rather than
at approval.

`district_id` and `kvk_id` are form fields of the upload itself, NOT of a
placement group. They say where the DOCUMENT applies, so one upload carries one
of each however many folders it goes in; a group that still sends them is
refused rather than quietly ignored.

Wire format: JSON-array-encoded strings rather than native repeated Form fields
-- list-typed Form() parameters aren't reliably supported across the FastAPI
version range this repo targets (>=0.110.0).

`language` is required -- one of the codes from `GET /dashboard/languages`.
Everything else on the form is optional.
"""
from __future__ import annotations

import json

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pymongo import ASCENDING, DESCENDING

from dashboard import events, queue_worker, vocabulary
from dashboard.db import get_db
from dashboard.display_id import parse_display_id
from dashboard.models import (
    COLL_LANGUAGES,
    COLL_UNIQUE_DOCUMENTS,
    COLL_UPLOAD_QUEUE_ITEMS,
    MANUAL_METADATA_FIELDS,
    UploadQueueStatus,
    new_upload_queue_item,
    normalize_format,
    utcnow,
)
from dashboard.schemas import UploadQueueItemOut

router = APIRouter()


def _to_object_id(value: str) -> ObjectId:
    """A malformed id returns 422, matching what FastAPI produces automatically
    for a typed path param."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(422, "malformed upload id")


def _stringify_ids(placements: list[dict] | None) -> list[dict]:
    return [{**p, **{k: str(p[k]) for k in ("state_id", "crop_id", "organization_id")
                     if p.get(k) is not None}}
            for p in placements or []]


def _item_out(item: dict) -> UploadQueueItemOut:
    data = dict(item)
    data["id"] = str(data.pop("_id"))
    data["placements"] = _stringify_ids(data.get("placements"))
    # `metadata` is served as a plain dict, so an ObjectId in it reaches the
    # JSON serializer raw and fails the whole response with a 500. The location
    # refs are stored as ObjectIds -- they are references, and the queue worker
    # re-reads them as such -- so they are stringified HERE and nowhere else.
    # They used to live on the placements, which _stringify_ids already covered.
    meta = dict(data.get("metadata") or {})
    for f in _LOCATION_REFS:
        if meta.get(f) is not None:
            meta[f] = str(meta[f])
    data["metadata"] = meta
    data["candidates"] = [{**c, "new_placements": _stringify_ids(c.get("new_placements"))}
                          for c in data.get("candidates") or []]
    return UploadQueueItemOut(**data)


def _parse_placements(db, placements_json: str | None) -> list[dict]:
    """The (state, folder) pairs the form is asking for, resolved and deduped.

    IDS ONLY:

        [{"state_id": "<id>",
          "crop_ids": ["<id>"],           crop master ids
          "organization_ids": ["<id>"],   pop_organizations ids
          "organization_ids": ["<id>"]}]  pop_organizations ids

    Every id must name an existing entry, or the upload is refused at SUBMIT --
    not carried through the queue to fail when someone approves it hours later.

    WHAT WENT. `"state"`, `"crops"` and `"organizations"` took NAMES, and
    `states_json` + `crops_json` took two name lists and crossed them. All of it
    is gone: the form is a selection from lists this API serves, so it holds the
    ids, and a name had to be guessed at -- `"crops": ["X"]` even fell back to
    matching an organisation, so one field could mean either vocabulary. With
    ids the kind is in the field name and nothing is ambiguous. A caller that
    wants the old cross product sends the pairs it means.

    NO DISTRICT AND NO KVK HERE. They were accepted per state group, and they
    are facts about the DOCUMENT, not about one folder it goes in -- so they are
    form fields of their own (`district_id` / `kvk_id` on the upload) and apply
    to the document however many folders it is filed in. Sending them in a group
    is refused rather than ignored, so a caller still on the old shape is told.

    Deduped, because the same pair arriving twice would create two rows for one
    folder.
    """
    if not placements_json:
        raise HTTPException(400, "placements_json is required")
    try:
        parsed = json.loads(placements_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"invalid placements_json: {e}")
    if not isinstance(parsed, list) or not parsed:
        raise HTTPException(400, "placements_json must be a non-empty JSON array")

    pairs, seen = [], set()
    for group in parsed:
        if not isinstance(group, dict) or not group.get("state_id"):
            raise HTTPException(
                400, 'each placement needs {"state_id": ..., "crop_ids"/"organization_ids": [...]}')
        state = vocabulary.get(db, "state", group["state_id"])
        if state is None:
            raise HTTPException(400, f"state_id {group['state_id']!r} does not name an existing state")
        lists = {k: group.get(k) or [] for k in _FOLDER_LISTS}
        if any(not isinstance(v, list) for v in lists.values()):
            raise HTTPException(
                400, f"state {state['name']!r}: {', '.join(_FOLDER_LISTS)} must be arrays")
        if not any(lists.values()):
            raise HTTPException(400, f"state {state['name']!r} has no folders")
        for gone in _LOCATION_REFS:
            if group.get(gone):
                raise HTTPException(
                    400, f"{gone} is not a placement field any more -- send it as a form field "
                         f"on the upload, where it applies to the document")
        for kind, key in (("crop", "crop_ids"), ("organization", "organization_ids")):
            for raw_id in lists[key]:
                entry = vocabulary.get(db, kind, raw_id)
                if entry is None:
                    raise HTTPException(400, f"{key}: {raw_id!r} does not name an existing {kind}")
                key_tuple = (state["_id"], kind, entry["_id"])
                if key_tuple in seen:
                    continue
                seen.add(key_tuple)
                pairs.append({
                    # The names ride along as LABELS for the queue's "files it
                    # under ..." note. Read from the entry, never matched on.
                    "state": state["name"], "state_id": state["_id"],
                    "crop": entry["name"], "crop_kind": kind,
                    vocabulary.field(kind): entry["_id"],
                })
    return pairs


_FOLDER_LISTS = ("crop_ids", "organization_ids")
# Where the document applies: the district and the KVK. Fields of the DOCUMENT,
# so they arrive once per upload rather than once per state group. Absent from
# every upload the form has ever sent, and that stays valid -- neither is
# required, and absence already means "not specific to one".
_LOCATION_REFS = ("district_id", "kvk_id")


def _checked_ref(db, source: dict, key: str):
    """An optional district_id / kvk_id, verified at SUBMIT. A blank or missing
    value is None: not every upload knows a district, and most do not.

    Checked now rather than at approval, which is where it used to end up: an
    id that names nothing is the submitter's mistake to see, not something to
    carry through the queue and fail on hours later when someone approves it."""
    raw = source.get(key)
    if raw is None or not str(raw).strip():
        return None
    kind = key[: -len("_id")]
    entry = vocabulary.get(db, kind, raw)
    if entry is None:
        raise HTTPException(400, f"{key} {raw!r} does not name an existing {kind}")
    return entry["_id"]


@router.post("/uploads", response_model=UploadQueueItemOut, status_code=201)
async def create_upload(
    file: UploadFile = File(...),
    language: str = Form(...),
    placements_json: str = Form(...),
    advisory_type: str | None = Form(None),
    advisory_scope: str | None = Form(None),
    season: str | None = Form(None),
    edition_revision_volume: str | None = Form(None),
    date_of_release: str | None = Form(None),
    month_of_release: int | None = Form(None),
    year_of_release: int | None = Form(None),
    date_of_collection: str | None = Form(None),
    month_of_collection: int | None = Form(None),
    year_of_collection: int | None = Form(None),
    advisory_name: str | None = Form(None),
    advisory_released_org: str | None = Form(None),
    advisory_org_address: str | None = Form(None),
    live_source_link: str | None = Form(None),
    domain: str | None = Form(None),
    verification_status: str | None = Form(None),
    uploaded_by: str | None = Form(None),
    document_status: str | None = Form(None),
    # Optional. Blank means "derive it from the file's extension", as before;
    # set, it wins -- e.g. a PDF that is a scan of a printed document.
    format_original: str | None = Form(None),
    # Where the document applies. One each per upload, because they belong to
    # the document -- not to a state group, which is where they used to arrive.
    district_id: str | None = Form(None),
    kvk_id: str | None = Form(None),
    db=Depends(get_db),
):
    placements = _parse_placements(db, placements_json)
    where = {k: _checked_ref(db, {"district_id": district_id, "kvk_id": kvk_id}, k)
             for k in _LOCATION_REFS}
    if db[COLL_LANGUAGES].count_documents({"code": language}, limit=1) == 0:
        raise HTTPException(400, "language must be a code from GET /dashboard/languages")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "uploaded file is empty")

    local = locals()
    metadata = {"language": language, **{name: local.get(name) for name in MANUAL_METADATA_FIELDS},
                "format_original": normalize_format(format_original),
                **{k: v for k, v in where.items() if v is not None}}

    item = new_upload_queue_item(filename=file.filename, placements=placements, metadata=metadata)
    item_id = db[COLL_UPLOAD_QUEUE_ITEMS].insert_one(item).inserted_id
    item["_id"] = item_id
    # Staged before the worker is told about it, so the worker can never race
    # ahead and find no file. pymongo writes apply immediately, so the item is
    # already visible to the worker's own connection.
    queue_worker.stage(item_id, pdf_bytes)
    queue_worker.enqueue_check(item_id)
    events.upload_changed(item_id)
    return _item_out(item)


@router.get("/uploads", response_model=list[UploadQueueItemOut])
def list_uploads(db=Depends(get_db)):
    """The queue: uploads still being checked, waiting on a decision, or failed.

    Nothing finished is here. Once a person picks add/new and the work
    completes, the item deletes itself -- the outcome is the document and its
    rows, which the main table already shows. Cancel deletes it too. So an item
    disappearing from this list IS the success signal; poll it, and refresh the
    main table when something goes.
    """
    rows = db[COLL_UPLOAD_QUEUE_ITEMS].find().sort([("created_at", DESCENDING), ("_id", ASCENDING)])
    return [_item_out(item) for item in rows]


@router.get("/uploads/{item_id}", response_model=UploadQueueItemOut)
def get_upload(item_id: str, db=Depends(get_db)):
    item = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": _to_object_id(item_id)})
    if item is None:
        raise HTTPException(404, "upload not found")
    return _item_out(item)


def _decidable(db, item_id: str) -> tuple[ObjectId, dict]:
    """The item, if it is at a point where a decision is meaningful.

    `failed` is decidable too: that is a retry, and it is safe because a failure
    before the Zoho upload leaves the staged file in place, while one after it
    has already finished its upload.
    """
    oid = _to_object_id(item_id)
    item = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": oid})
    if item is None:
        raise HTTPException(404, "upload not found")
    if item["status"] not in (UploadQueueStatus.awaiting_review.value, UploadQueueStatus.failed.value):
        raise HTTPException(409, f"upload is {item['status']}, not awaiting_review")
    return oid, item


def _start(db, oid: ObjectId, document_id: ObjectId | None) -> UploadQueueItemOut:
    db[COLL_UPLOAD_QUEUE_ITEMS].update_one(
        {"_id": oid},
        {"$set": {"status": UploadQueueStatus.uploading.value, "error_message": None,
                  "progress_pct": 55, "updated_at": utcnow()}},
    )
    queue_worker.enqueue_decision(oid, document_id)
    events.upload_changed(oid)
    return _item_out(db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": oid}))


@router.post("/uploads/{item_id}/add", response_model=UploadQueueItemOut)
def add_to_existing(item_id: str, body: dict = Body(default=None), db=Depends(get_db)):
    """File this upload's NEW placements onto a document that already exists.

    `document_id` is an ObjectId hex or an ANNAM id, and must be one of the
    candidates the check offered -- so a person cannot attach an upload to an
    arbitrary document by typing its id. Nothing is uploaded to Zoho: the
    document already has the file, and the dashboard's storage is one flat
    folder rather than one per state/crop.
    """
    oid, item = _decidable(db, item_id)
    candidates = item.get("candidates") or []
    if not candidates:
        raise HTTPException(409, "nothing matched this upload -- use /new")

    wanted = (body or {}).get("document_id")
    if wanted is None and len(candidates) == 1:
        chosen = candidates[0]  # unambiguous: one candidate, no need to name it
    else:
        if not wanted:
            raise HTTPException(400, "document_id is required when more than one candidate matched")
        chosen = _match_candidate(candidates, str(wanted))
        if chosen is None:
            raise HTTPException(400, "document_id must be one of this upload's candidates")

    if not chosen.get("can_add"):
        raise HTTPException(
            409,
            f"{chosen['document_code']} is already filed under every place you selected -- "
            f"there is nothing to add",
        )
    return _start(db, oid, ObjectId(chosen["document_id"]))


def _match_candidate(candidates: list[dict], wanted: str) -> dict | None:
    """Accept either form of id, because the team works in ANNAM ids and the
    frontend works in ObjectIds."""
    display_id = parse_display_id(wanted)
    for candidate in candidates:
        if candidate["document_id"] == wanted:
            return candidate
        if display_id is not None and candidate.get("document_code") == f"ANNAM_{display_id:05d}":
            return candidate
    return None


@router.post("/uploads/{item_id}/new", response_model=UploadQueueItemOut)
def create_new_document(item_id: str, db=Depends(get_db)):
    """Upload the file and create a separate document for it.

    Refused when an exact sha256 candidate exists: sha256 is unique on
    `unique_documents`, so byte-identical content cannot become a second
    document. That is a storage fact, not a policy -- the answer there is /add.
    """
    oid, item = _decidable(db, item_id)
    exact = next((c for c in (item.get("candidates") or []) if c.get("match_type") == "sha"), None)
    if exact is not None:
        raise HTTPException(
            409,
            f"this exact file is already stored as {exact['document_code']}; it cannot become a "
            f"second document. Use /add to file it under more places, or /cancel.",
        )
    if not queue_worker.stage_path(oid).exists():
        raise HTTPException(409, "the staged file is gone -- submit the upload again")
    return _start(db, oid, None)


@router.post("/uploads/{item_id}/cancel", status_code=204)
def cancel_pending_upload(item_id: str, db=Depends(get_db)):
    """Discard the item and the staged file. Nothing was sent to Zoho and no
    rows exist, so there is nothing else to undo."""
    _discard(db, item_id)


@router.delete("/uploads/{item_id}", status_code=204)
def delete_upload(item_id: str, db=Depends(get_db)):
    """Same effect as /cancel, kept because clearing a `failed` row from the
    list is a delete, not a decision. A successful upload removes itself (see
    queue_worker._finish), so there is no `done` row to clear."""
    _discard(db, item_id)


def _discard(db, item_id: str) -> None:
    oid = _to_object_id(item_id)
    item = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": oid})
    if item is None:
        raise HTTPException(404, "upload not found")
    if item["status"] == UploadQueueStatus.uploading.value:
        # Its Zoho upload and row inserts are already in flight; deleting the
        # item would not stop them.
        raise HTTPException(409, "cannot cancel an upload that is already uploading")
    queue_worker.unstage(oid)
    db[COLL_UPLOAD_QUEUE_ITEMS].delete_one({"_id": oid})
    events.upload_removed(oid)
