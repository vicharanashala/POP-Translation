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

PLACEMENTS. `placements_json` is a list of per-state crop groups, which is how
the form is actually filled in -- a state, then the crops for that state, then
another state:

    placements_json='[{"state":"State Karnataka","crops":["Paddy","Ragi"]},
                      {"state":"State Kerala","crops":["Coconut"]}]'

`states_json` + `crops_json` remain accepted for the simple case, and mean the
cross product of the two. Crops that do not exist yet are created; the form can
introduce a new one.

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

from dashboard import queue_worker
from dashboard.db import CI_COLLATION, get_db
from dashboard.display_id import parse_display_id
from dashboard.models import (
    COLL_CROPS,
    COLL_LANGUAGES,
    COLL_STATES,
    COLL_UNIQUE_DOCUMENTS,
    COLL_UPLOAD_QUEUE_ITEMS,
    MANUAL_METADATA_FIELDS,
    UploadQueueStatus,
    new_upload_queue_item,
    normalize_crop_name,
    normalize_state_name,
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


def _item_out(item: dict) -> UploadQueueItemOut:
    data = dict(item)
    data["id"] = str(data.pop("_id"))
    return UploadQueueItemOut(**data)


def _canonical(db, collection, name: str) -> str:
    """The vocabulary's spelling of a name, if it already knows one.

    normalize_state_name() strips noise words but does not re-case: "State
    Karnataka" gives "Karnataka" while "state karnataka" gives "karnataka", and
    title-casing everything is not an option either ("Jammu and Kashmir" would
    become "Jammu And Kashmir"). So the vocabulary decides. A name it has never
    seen is stored as typed and becomes the canonical spelling itself.
    """
    existing = db[collection].find_one({"name": name}, collation=CI_COLLATION)
    return existing["name"] if existing else name


def _parse_placements(db, placements_json: str | None, states_json: str | None,
                      crops_json: str | None) -> list[dict]:
    """The (state, crop) pairs the form is asking for, normalised and deduped.

    Deduped because the same pair arriving twice would otherwise create two rows
    for one folder, and a form that lists a crop under two states is a normal
    thing for a person to do. Deduped AFTER the vocabulary has had its say, so
    two spellings of one state collapse rather than becoming two placements.
    """
    groups: list[dict] = []
    if placements_json:
        try:
            parsed = json.loads(placements_json)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"invalid placements_json: {e}")
        if not isinstance(parsed, list):
            raise HTTPException(400, "placements_json must be a JSON array")
        for group in parsed:
            if not isinstance(group, dict) or not group.get("state"):
                raise HTTPException(400, 'each placement needs {"state": ..., "crops": [...]}')
            crops = group.get("crops") or []
            if not isinstance(crops, list) or not crops:
                raise HTTPException(400, f"state {group['state']!r} has no crops")
            groups.append({"state": group["state"], "crops": crops})
    else:
        try:
            states = json.loads(states_json or "[]")
            crops = json.loads(crops_json or "[]")
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"invalid states_json/crops_json: {e}")
        if not isinstance(states, list) or not isinstance(crops, list):
            raise HTTPException(400, "states_json/crops_json must be JSON arrays")
        if not states:
            raise HTTPException(400, "at least one state must be selected")
        if not crops:
            raise HTTPException(400, "at least one crop must be selected")
        groups = [{"state": s, "crops": crops} for s in states]

    if not groups:
        raise HTTPException(400, "at least one state must be selected")

    pairs, seen = [], set()
    for group in groups:
        state = _canonical(db, COLL_STATES, normalize_state_name(str(group["state"]).strip()))
        for crop in group["crops"]:
            crop = _canonical(db, COLL_CROPS, normalize_crop_name(str(crop).strip()))
            if not state or not crop:
                raise HTTPException(400, "state and crop names cannot be empty")
            if (state, crop) in seen:
                continue
            seen.add((state, crop))
            pairs.append({"state": state, "crop": crop})
    return pairs


@router.post("/uploads", response_model=UploadQueueItemOut, status_code=201)
async def create_upload(
    file: UploadFile = File(...),
    language: str = Form(...),
    placements_json: str | None = Form(None),
    states_json: str | None = Form(None),
    crops_json: str | None = Form(None),
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
    verified_by: str | None = Form(None),
    document_status: str | None = Form(None),
    db=Depends(get_db),
):
    placements = _parse_placements(db, placements_json, states_json, crops_json)
    if db[COLL_LANGUAGES].count_documents({"code": language}, limit=1) == 0:
        raise HTTPException(400, "language must be a code from GET /dashboard/languages")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "uploaded file is empty")

    local = locals()
    metadata = {"language": language, **{name: local.get(name) for name in MANUAL_METADATA_FIELDS}}

    item = new_upload_queue_item(filename=file.filename, placements=placements, metadata=metadata)
    item_id = db[COLL_UPLOAD_QUEUE_ITEMS].insert_one(item).inserted_id
    item["_id"] = item_id
    # Staged before the worker is told about it, so the worker can never race
    # ahead and find no file. pymongo writes apply immediately, so the item is
    # already visible to the worker's own connection.
    queue_worker.stage(item_id, pdf_bytes)
    queue_worker.enqueue_check(item_id)
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
