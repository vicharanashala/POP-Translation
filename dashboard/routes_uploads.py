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

THE APPROVAL CHAIN. Since 2026-10, an upload passes through people before an
admin files it. Who may act is checked against dashboard/access.py:

    expert uploads ----------> upload approvers ----------> admins
      ^                          PATCH   edit                PATCH   edit
      |                          /approve -> admins          /add, /new, /cancel
      |                          /decline -> uploader        /decline -> approvers
      |                          (note required)             (note required)
      +-- declined: PATCH edit, /resubmit -> approvers, or DELETE

An admin's or an upload approver's own upload starts at the admins, and is
never declined below the level it came from (`level` / `upload_level`, see
_stage). A declined item goes back one level with a note; at the uploader it can be corrected
(PATCH) and resubmitted to the approvers, or removed (DELETE) and uploaded
again -- experts never add or discard. Each item says
which stage it is at and what THIS caller may do with it (`stage`, `actions`),
and `GET /uploads` lists only the caller's own uploads plus what waits on them.

    PATCH  /uploads/{id}           whoever holds it corrects the form fields
    POST   /uploads/{id}/approve   approver -> admins
    POST   /uploads/{id}/decline   back one stage, {"note": "..."}
    POST   /uploads/{id}/resubmit  uploader -> approvers, after a decline
    GET    /uploads/{id}/file      the staged file, for the queue's preview

The same file cannot be queued twice: POST /uploads answers 409 when an item
with its sha256 is already in the queue, at any stage (_refuse_if_queued). The
catalogue is checked later, by the worker; the queue has to be checked here,
because nothing in it is in the catalogue yet.

Every item stops at `awaiting_review` whether or not anything matched -- the
duplicate check is a placeholder (exact sha256 only, see dashboard/queue_worker)
so the person is the real check. The pause happens before the Zoho upload, so
cancelling leaves nothing behind in WorkDrive.

WHO APPROVED IT. /approve, /add and /new take an optional body
`{"approved_by": "<display name>"}` (/add also its `document_id`), like
`translated_by` on a translation. The names wait on the item as
`approved_by_approver` / `approved_by_admin`, each with the person's account id
beside it (`..._id`, from X-User-Id), and are written onto the document when
/new creates it, together with the uploader's id (`expert`) as `uploaded_by_id`. /add files more places for a document that already
exists, and leaves its approvals as they were -- as it leaves `uploaded_by`.

ONE DECISION PER ITEM. Requests run in parallel, not in turn, so every move
is a conditional write -- "only if it is still where I saw it" -- and the
loser of a race gets a 409 to refresh on: approve, decline and edit (_move),
add and new (_start claims the item before the worker sees it), discard
(_discard). The worker re-checks the document's folders before filing, so even
a retry files nothing twice.

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

import hashlib
import json

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import TypeAdapter, ValidationError
from pymongo import ASCENDING, DESCENDING

from dashboard import events, queue_worker, vocabulary
from dashboard.access import UPLOAD_LIST, Caller, caller
from dashboard.db import get_db
from dashboard.display_id import parse_display_id
from dashboard.models import (
    COLL_APPROVERS,
    COLL_LANGUAGES,
    COLL_UNIQUE_DOCUMENTS,
    COLL_UPLOAD_QUEUE_ITEMS,
    MANUAL_METADATA_FIELDS,
    UploadQueueStatus,
    new_upload_queue_item,
    normalize_format,
    utcnow,
)
from dashboard.schemas import Domains, UploadActions, UploadDecline, UploadEdit, UploadQueueItemOut, UploadStage

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


# -- the approval chain ----------------------------------------------------------
#
# Stored as two numbers on the item:
#
#     level          where it is NOW:     0 the uploader, 1 the upload
#                                         approvers, 2 the admins
#     upload_level   who UPLOADED it:     0 anyone else, 1 an upload
#                                         approver, 2 an admin
#
# An upload starts one level above its uploader's own -- an approver's or an
# admin's upload skips the approvers, who would only be approving themselves --
# and a decline moves it down one. It never goes below `upload_level`: there is
# nobody down there who ever saw it, so at `level == upload_level` there is no
# decline. That replaced two booleans (`approver`, `admin`) which could not say
# where an admin's own upload should go when declined, and sent it to approvers
# who had never been part of its path.
#
# An item with no `level` was queued before the chain existed; it was waiting on
# an admin then, so it is still with the admins, uploaded by nobody in
# particular (upload_level 0). Read that way rather than rewritten.
#
# Who sees an item: its uploader always (their own uploads, at any level), the
# upload approvers while it is at 1, the admins while it is at 2. A person can
# be several of these, and sees the union.

_LEVEL = {UploadStage.expert: 0, UploadStage.approver: 1, UploadStage.admin: 2}
_STAGE_AT = {n: stage for stage, n in _LEVEL.items()}


def _level(item: dict) -> int:
    return item.get("level", 2)


def _upload_level(item: dict) -> int:
    return item.get("upload_level", 0)


def _stage(item: dict) -> UploadStage:
    return _STAGE_AT[_level(item)]


def upload_level_of(who: Caller) -> int:
    """The level a person's own upload counts as uploaded from."""
    return 2 if who.is_admin else 1 if who.can_approve_uploads else 0


# The Mongo spelling of each stage, for conditional updates: a decision only
# lands if the item is still where the person saw it.
_AT_STAGE = {
    UploadStage.admin: {"$or": [{"level": 2}, {"level": {"$exists": False}}]},
    UploadStage.approver: {"level": 1},
    UploadStage.expert: {"level": 0},
}

_REVIEWABLE = (UploadQueueStatus.awaiting_review.value, UploadQueueStatus.failed.value)


def _holds(who: Caller, stage: UploadStage, item: dict) -> bool:
    """Whether `who` is one of the people the item is waiting on. At the
    uploader's stage that is the uploader alone."""
    return ((stage is UploadStage.approver and who.can_approve_uploads)
            or (stage is UploadStage.admin and who.is_admin)
            or (stage is UploadStage.expert and item.get("expert") == who.user_id))


def _actions(item: dict, who: Caller | None) -> UploadActions:
    if who is None:
        return UploadActions()
    stage, status = _stage(item), item.get("status")
    holds = _holds(who, stage, item)
    waiting = status == UploadQueueStatus.awaiting_review.value
    not_filing = status != UploadQueueStatus.uploading.value
    return UploadActions(
        can_edit=holds and waiting,
        can_approve=holds and stage is UploadStage.approver and waiting,
        # Down one level, never below where it was uploaded from.
        can_decline=holds and _level(item) > _upload_level(item) and status in _REVIEWABLE,
        can_decide=holds and stage is UploadStage.admin and status in _REVIEWABLE,
        can_discard=holds and stage is UploadStage.admin and not_filing,
        can_resubmit=holds and stage is UploadStage.expert and waiting,
        # The uploader may drop their own upload once it is back at their own
        # level: an expert's declined upload, or an approver's own upload that
        # an admin sent back. (An admin's is the admins' to discard anyway.)
        can_remove=(item.get("expert") == who.user_id and _level(item) == _upload_level(item)
                    and not_filing),
    )


def _visible_filter(who: Caller) -> dict:
    clauses = [{"expert": who.user_id}]
    if who.can_approve_uploads:
        clauses.append(_AT_STAGE[UploadStage.approver])
    if who.is_admin:
        clauses.append(_AT_STAGE[UploadStage.admin])
    return {"$or": clauses}


def _visible_item(db, item_id: str, who: Caller) -> tuple[ObjectId, dict]:
    """The item, if this caller may see it. Someone else's item is a 404, not a
    403: whether it exists is not theirs to know either."""
    oid = _to_object_id(item_id)
    item = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"$and": [{"_id": oid}, _visible_filter(who)]})
    if item is None:
        raise HTTPException(404, "upload not found")
    return oid, item


def _item_out(item: dict, who: Caller | None = None) -> UploadQueueItemOut:
    data = dict(item)
    data["id"] = str(data.pop("_id"))
    data["placements"] = _stringify_ids(data.get("placements"))
    # The uploader by name, from the form's own `uploaded_by` -- `expert` is an
    # account id, which nobody reading the queue can place (UM-002).
    data["uploaded_by"] = ((data.get("metadata") or {}).get("uploaded_by") or "").strip() or None
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
    stage = _stage(item)
    data["level"] = _level(item)
    data["upload_level"] = _upload_level(item)
    data["stage"] = stage
    data["actions"] = _actions(item, who)
    return UploadQueueItemOut(**data)


def _parse_domains(domain_json: str | None) -> list[str]:
    """The upload form's `domain_json`, checked like a PATCH's `domain`."""
    try:
        return TypeAdapter(Domains).validate_python(json.loads(domain_json) if domain_json else None)
    except json.JSONDecodeError:
        raise HTTPException(400, "domain_json must be a JSON list of names")
    except ValidationError as e:
        raise HTTPException(400, f"domain_json: {e.errors()[0]['msg']}")


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


_STAGE_WORDS = {
    UploadStage.expert: "declined back to its uploader",
    UploadStage.approver: "waiting on the upload approvers",
    UploadStage.admin: "waiting on the admins",
}


def _refuse_if_queued(db, sha: str) -> None:
    """409 when this exact file is already in the upload queue.

    The duplicate check (queue_worker.find_candidates) only looks at the
    CATALOGUE, and an upload is not in the catalogue until an admin files it --
    which, with the approval chain, can be days. Without this, the same file
    sent twice (by two experts, or by one who did not see the first go through)
    became two queue items, each reviewed by the approvers and the admins in
    turn, and the second one's add then found the first's document.

    Any status counts, `failed` included: the item is still there, and removing
    it is the way to send the file again. Matched on the exact sha256, like the
    catalogue check -- a re-scan is a different file to both.
    """
    queued = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"sha256": sha}, {"filename": 1, "level": 1,
                                                                    "status": 1})
    if queued is not None:
        raise HTTPException(
            409,
            f"This exact file is already in the upload queue as \"{queued.get('filename')}\" "
            f"({_STAGE_WORDS[_stage(queued)]}, status {queued.get('status')}). "
            "It cannot be queued twice -- wait for that one to be decided, or have it removed first.",
        )


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
    # The domains as a JSON list of names from GET /dashboard/domains -- JSON
    # because several names contain a comma.
    domain_json: str | None = Form(None),
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
    who: Caller = Depends(caller),
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
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    _refuse_if_queued(db, sha)

    domain = _parse_domains(domain_json)
    local = locals()
    metadata = {"language": language, **{name: local.get(name) for name in MANUAL_METADATA_FIELDS},
                "format_original": normalize_format(format_original),
                **{k: v for k, v in where.items() if v is not None}}

    # Starts one level above the uploader's own: an admin's or an upload
    # approver's upload skips the approvers, who would only approve themselves.
    item = new_upload_queue_item(filename=file.filename, placements=placements, metadata=metadata,
                                 expert=who.user_id, upload_level=upload_level_of(who))
    # Stored now rather than when the worker gets to it, so a second copy sent
    # a moment later is already refused by _refuse_if_queued.
    item["sha256"] = sha
    item_id = db[COLL_UPLOAD_QUEUE_ITEMS].insert_one(item).inserted_id
    item["_id"] = item_id
    # Staged before the worker is told about it, so the worker can never race
    # ahead and find no file. pymongo writes apply immediately, so the item is
    # already visible to the worker's own connection.
    queue_worker.stage(item_id, pdf_bytes)
    queue_worker.enqueue_check(item_id)
    events.upload_changed(item_id)
    return _item_out(item, who)


@router.get("/uploads", response_model=list[UploadQueueItemOut])
def list_uploads(who: Caller = Depends(caller), db=Depends(get_db)):
    """The queue: uploads still being checked, waiting on a decision, or failed.

    Nothing finished is here. Once a person picks add/new and the work
    completes, the item deletes itself -- the outcome is the document and its
    rows, which the main table already shows. Cancel deletes it too. So an item
    disappearing from this list IS the success signal; poll it, and refresh the
    main table when something goes.

    Only what THIS caller should see: their own uploads, plus whatever is
    waiting on them (see _visible_filter).
    """
    rows = db[COLL_UPLOAD_QUEUE_ITEMS].find(_visible_filter(who)).sort(
        [("created_at", DESCENDING), ("_id", ASCENDING)])
    return [_item_out(item, who) for item in rows]


@router.get("/uploads/{item_id}", response_model=UploadQueueItemOut)
def get_upload(item_id: str, who: Caller = Depends(caller), db=Depends(get_db)):
    return _item_out(_visible_item(db, item_id, who)[1], who)


@router.get("/uploads/{item_id}/file")
def get_upload_file(item_id: str, who: Caller = Depends(caller), db=Depends(get_db)):
    """The uploaded file itself, for the queue's preview. It has not reached Zoho
    yet -- it waits on local disk until an admin decides -- so it is served from
    there, to the same people who can see the item."""
    oid, item = _visible_item(db, item_id, who)
    path = queue_worker.stage_path(oid)
    if not path.exists():
        raise HTTPException(404, "the staged file is gone")
    filename = item.get("filename") or path.name
    media = "application/pdf" if filename.lower().endswith(".pdf") else None
    return FileResponse(path, media_type=media, filename=filename, content_disposition_type="inline")


def _held_item(db, item_id: str, who: Caller, *stages: UploadStage,
               statuses: tuple = _REVIEWABLE) -> tuple[ObjectId, dict]:
    """The item, if it is waiting on `who` at one of `stages` and in a status
    where acting on it means something.

    When it is not, the answer names the reason, because the button that sent
    the request was on the caller's screen a moment ago. A blanket "upload not
    found" told an approver taken off the list, and one who lost a race to
    another approver, the same untrue thing (UM-009):

        404  it is gone (filed or discarded), or the caller never had any
             business with it -- whether it exists is not theirs to know
        409  someone else acted first: it moved on to another level
        403  the caller can see it but it is not theirs to act on, or it is
             still with the approvers and the caller is no longer one
        409  (below) it is mid-work
    """
    oid = _to_object_id(item_id)
    item = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": oid})
    if item is None:
        raise HTTPException(404, "this upload is no longer in the queue -- it was filed or discarded")
    stage = _stage(item)
    if stage not in stages or not _holds(who, stage, item):
        if ((UploadStage.approver in stages and who.can_approve_uploads)
                or (UploadStage.admin in stages and who.is_admin)):
            # The caller holds one of the stages this action is for, so the
            # item was there when they saw it and someone else moved it.
            raise HTTPException(409, _moved_on(item))
        if (item.get("expert") == who.user_id
                or (stage is UploadStage.approver and who.can_approve_uploads)
                or (stage is UploadStage.admin and who.is_admin)):
            raise HTTPException(403, f"this upload is with the {stage.value}s -- not yours to act on")
        if stage is UploadStage.approver and UploadStage.approver in stages:
            # Still where an approver would have seen it: it is the caller who
            # changed, taken off the approval list since the page loaded.
            raise HTTPException(403, "you are no longer an upload approver")
        raise HTTPException(404, "upload not found")
    if item["status"] not in statuses:
        raise HTTPException(409, f"upload is {item['status']}, not {' or '.join(statuses)}")
    return oid, item


def _moved_on(item: dict) -> str:
    """Why an item is no longer where the caller saw it -- someone else acted
    first. Names the approver where the item says who it was."""
    stage = _stage(item)
    if stage is UploadStage.admin:
        name = item.get("approved_by_approver")
        return (f"already approved by {name}" if name else "already approved") + \
            " -- it is now with the admins; refresh"
    if stage is UploadStage.expert:
        return "it has been declined back to its uploader -- refresh"
    return "it is with the upload approvers now -- refresh"


def _decidable(db, item_id: str, who: Caller) -> tuple[ObjectId, dict]:
    """The item, if an admin may add / new it now.

    `failed` is decidable too: that is a retry, and it is safe because a failure
    before the Zoho upload leaves the staged file in place, while one after it
    has already finished its upload.
    """
    return _held_item(db, item_id, who, UploadStage.admin)


def _move(db, oid: ObjectId, stage: UploadStage, who: Caller, **fields) -> UploadQueueItemOut:
    """Apply a stage change only if the item is still at `stage` -- two people
    acting on one item at once must not both land."""
    result = db[COLL_UPLOAD_QUEUE_ITEMS].update_one(
        {"$and": [{"_id": oid}, _AT_STAGE[stage]]},
        {"$set": {**fields, "updated_at": utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "this upload was just acted on by someone else -- refresh")
    events.upload_changed(oid)
    return _item_out(db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": oid}), who)


def _approver_name(db, body: dict | None, who: Caller, list_name: str | None = None) -> str | None:
    """Who is approving, by display name -- `{"approved_by": "<name>"}`, sent
    with the action like `translated_by` on a translation. Without it, the
    name stored when the person was added to the approval list, where there is
    one; an admin is on no list, so an admin who sends nothing is recorded as
    unknown (null) rather than as an id nobody can read."""
    name = str((body or {}).get("approved_by") or "").strip()
    if name or list_name is None:
        return name or None
    entry = db[COLL_APPROVERS].find_one({"list": list_name, "user_id": who.user_id}, {"name": 1})
    return ((entry or {}).get("name") or "").strip() or None


@router.post("/uploads/{item_id}/approve", response_model=UploadQueueItemOut)
def approve_upload(item_id: str, body: dict = Body(default=None), who: Caller = Depends(caller),
                   db=Depends(get_db)):
    """An upload approver passes it on to the admins, with any edits they made.
    Optional body `{"approved_by": "<display name>"}` -- see _approver_name."""
    oid, _ = _held_item(db, item_id, who, UploadStage.approver,
                        statuses=(UploadQueueStatus.awaiting_review.value,))
    # The approvers' own note asked the uploader for something that has now
    # been passed; left on, the admins would read a settled reason as current.
    return _move(db, oid, UploadStage.approver, who, level=2, approver_note=None,
                 approved_by_approver=_approver_name(db, body, who, UPLOAD_LIST),
                 approved_by_approver_id=who.user_id)


@router.post("/uploads/{item_id}/resubmit", response_model=UploadQueueItemOut)
def resubmit_upload(item_id: str, who: Caller = Depends(caller), db=Depends(get_db)):
    """The uploader sends a declined item back to the upload approvers, after
    correcting it with PATCH. The approvers' note is cleared: the uploader has
    answered it, and an approver opening the item again should not read it as
    a current objection (UM-005). A wrong FILE cannot be corrected this way --
    that is still remove and upload again."""
    oid, _ = _held_item(db, item_id, who, UploadStage.expert,
                        statuses=(UploadQueueStatus.awaiting_review.value,))
    return _move(db, oid, UploadStage.expert, who, level=1, approver_note=None)


@router.post("/uploads/{item_id}/decline", response_model=UploadQueueItemOut)
def decline_upload(item_id: str, body: UploadDecline, who: Caller = Depends(caller),
                   db=Depends(get_db)):
    """Send it back one level, with a note saying why.

    From the approvers it goes back to the uploader, who reads the note and
    either corrects the item and resubmits it, or removes it and uploads again.
    From the admins it goes back to the approvers, who either fix it and approve
    again or decline it on to the uploader. The note is required: a bare "no"
    tells nobody what to change.

    Never below the level it was uploaded from (409): an admin's own upload has
    no approvers to go back to, and an approver's own has no expert. Those are
    corrected where they are, or discarded / removed.
    """
    note = (body.note or "").strip()
    if not note:
        raise HTTPException(400, "a note is required when declining")
    oid, item = _held_item(db, item_id, who, UploadStage.approver, UploadStage.admin)
    if _level(item) <= _upload_level(item):
        raise HTTPException(409, "this upload was uploaded at this level -- there is nobody to decline "
                                 "it back to; correct it here, or discard it")
    stage = _stage(item)
    if stage is UploadStage.approver:
        # Back to the uploader unapproved: an approval from an earlier pass
        # (before an admin sent it back) no longer stands.
        return _move(db, oid, stage, who, level=0, approver_note=note,
                     approved_by_approver=None, approved_by_approver_id=None)
    return _move(db, oid, stage, who, level=1, admin_note=note)


@router.patch("/uploads/{item_id}", response_model=UploadQueueItemOut)
def edit_upload(item_id: str, body: UploadEdit, who: Caller = Depends(caller), db=Depends(get_db)):
    """Correct what the uploader filled in, before passing it on.

    Whoever holds the item -- an approver, an admin, or the uploader once it
    has been declined back to them -- may change any form field
    except `uploaded_by`, under the same rules as the form: ids only, every id
    checked now. Changing the placements re-reads which of them each candidate
    already has, since that is what decides whether "add" means anything.
    """
    oid, item = _held_item(db, item_id, who, UploadStage.approver, UploadStage.admin,
                           UploadStage.expert, statuses=(UploadQueueStatus.awaiting_review.value,))
    sent = body.model_dump(include=body.model_fields_set)
    meta, unset, update = dict(item.get("metadata") or {}), [], {}

    if "language" in sent:
        if db[COLL_LANGUAGES].count_documents({"code": sent["language"]}, limit=1) == 0:
            raise HTTPException(400, "language must be a code from GET /dashboard/languages")
        meta["language"] = sent.pop("language")
    if "placements" in sent:
        update["placements"] = _parse_placements(db, json.dumps(sent.pop("placements")))
    for key in _LOCATION_REFS:
        if key in sent:
            ref = _checked_ref(db, sent, key)
            sent.pop(key)
            if ref is None:
                meta.pop(key, None)
            else:
                meta[key] = ref
    if "format_original" in sent:
        meta["format_original"] = normalize_format(sent.pop("format_original"))
    meta.update(sent)  # the plain metadata fields; UploadEdit forbids anything else
    update["metadata"] = meta

    if "placements" in update and item.get("sha256"):
        candidates = queue_worker.find_candidates(db, item["sha256"], update["placements"])
        update["candidates"] = candidates
        update["note"] = queue_worker.describe_candidates(candidates)
    return _move(db, oid, _stage(item), who, **update)


def _start(db, oid: ObjectId, document_id: ObjectId | None, who: Caller,
           approved_by: str | None) -> UploadQueueItemOut:
    """CLAIM the item, then hand it to the worker.

    The claim is one conditional write: `uploading` only if it is still with
    the admins and still reviewable. Two admins clicking add/new at the same
    moment both pass _decidable's read, but only one of these writes matches;
    the other gets the 409 and nothing runs twice. Without it both were queued:
    a second `new` uploaded the file to WorkDrive again and filed the document
    in every folder a second time.
    """
    result = db[COLL_UPLOAD_QUEUE_ITEMS].update_one(
        {"$and": [{"_id": oid, "status": {"$in": list(_REVIEWABLE)}}, _AT_STAGE[UploadStage.admin]]},
        {"$set": {"status": UploadQueueStatus.uploading.value, "error_message": None,
                  "progress_pct": 55, "approved_by_admin": approved_by,
                  "approved_by_admin_id": who.user_id, "updated_at": utcnow()}},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "this upload was just acted on by someone else -- refresh")
    queue_worker.enqueue_decision(oid, document_id)
    events.upload_changed(oid)
    return _item_out(db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": oid}), who)


@router.post("/uploads/{item_id}/add", response_model=UploadQueueItemOut)
def add_to_existing(item_id: str, body: dict = Body(default=None), who: Caller = Depends(caller),
                    db=Depends(get_db)):
    """File this upload's NEW placements onto a document that already exists.

    `document_id` is an ObjectId hex or an ANNAM id, and must be one of the
    candidates the check offered -- so a person cannot attach an upload to an
    arbitrary document by typing its id. Nothing is uploaded to Zoho: the
    document already has the file, and the dashboard's storage is one flat
    folder rather than one per state/crop.
    """
    oid, item = _decidable(db, item_id, who)
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
    return _start(db, oid, ObjectId(chosen["document_id"]), who, _approver_name(db, body, who))


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
def create_new_document(item_id: str, body: dict = Body(default=None), who: Caller = Depends(caller),
                        db=Depends(get_db)):
    """Upload the file and create a separate document for it.

    Refused when an exact sha256 candidate exists: sha256 is unique on
    `unique_documents`, so byte-identical content cannot become a second
    document. That is a storage fact, not a policy -- the answer there is /add.
    """
    oid, item = _decidable(db, item_id, who)
    exact = next((c for c in (item.get("candidates") or []) if c.get("match_type") == "sha"), None)
    if exact is not None:
        raise HTTPException(
            409,
            f"this exact file is already stored as {exact['document_code']}; it cannot become a "
            f"second document. Use /add to file it under more places, or /cancel.",
        )
    if not queue_worker.stage_path(oid).exists():
        raise HTTPException(409, "the staged file is gone -- submit the upload again")
    return _start(db, oid, None, who, _approver_name(db, body, who))


@router.post("/uploads/{item_id}/cancel", status_code=204)
def cancel_pending_upload(item_id: str, who: Caller = Depends(caller), db=Depends(get_db)):
    """Discard the item and the staged file. Nothing was sent to Zoho and no
    rows exist, so there is nothing else to undo."""
    _discard(db, item_id, who)


@router.delete("/uploads/{item_id}", status_code=204)
def delete_upload(item_id: str, who: Caller = Depends(caller), db=Depends(get_db)):
    """Same effect as /cancel, kept because clearing a `failed` row from the
    list is a delete, not a decision. A successful upload removes itself (see
    queue_worker._finish), so there is no `done` row to clear."""
    _discard(db, item_id, who)


def _discard(db, item_id: str, who: Caller) -> None:
    """Two people may drop an item: an admin, while it is with the admins, and
    its uploader, once it is back at the level it was uploaded from. Nobody
    else -- an expert can no longer withdraw an upload that is still in review,
    and an approver's "no" is a decline with a note, not a discard."""
    oid, item = _visible_item(db, item_id, who)
    if not (_actions(item, who).can_discard or _actions(item, who).can_remove):
        if item["status"] == UploadQueueStatus.uploading.value:
            raise HTTPException(409, "cannot cancel an upload that is already uploading")
        raise HTTPException(403, f"this upload is with the {_stage(item).value}s -- not yours to remove")
    if item["status"] == UploadQueueStatus.uploading.value:
        # Its Zoho upload and row inserts are already in flight; deleting the
        # item would not stop them.
        raise HTTPException(409, "cannot cancel an upload that is already uploading")
    # Deleted only if it is still at the stage the check above saw and still
    # not being filed: an admin's add landing between that read and this
    # delete must win, not lose its item mid-flight. Not pinned to the exact
    # status -- the worker moves a fresh upload through queued/hashing/checking
    # on its own, and that is not someone else acting on it.
    result = db[COLL_UPLOAD_QUEUE_ITEMS].delete_one({"$and": [
        {"_id": oid, "status": {"$ne": UploadQueueStatus.uploading.value}},
        _AT_STAGE[_stage(item)]]})
    if result.deleted_count == 0:
        raise HTTPException(409, "this upload was just acted on by someone else -- refresh")
    queue_worker.unstage(oid)
    events.upload_removed(oid)
