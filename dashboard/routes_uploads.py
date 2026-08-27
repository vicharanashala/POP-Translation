"""Upload queue endpoints. POST enqueues and returns immediately (a
queue_item_id), GET polls/lists status for the frontend's status box, DELETE
cancels a still-queued item. The actual hash/embed/dedup-check/upload work
happens off the request thread in dashboard.queue_worker.

Nothing is ever auto-created or auto-removed (changed 2026-08-26) -- every
upload, matched or not, lands at status=awaiting_review and sits there until
one of three actions resolves it: POST .../add (this IS the matched
document -- link the new state/crop placement(s) onto it), POST .../new
(create it as its own row -- used both when there was no candidate match at
all and when there was one but it's a false positive), or POST .../cancel
(don't append anything, discard the upload). `duplicate_of_id`/
`similarity_score`/`match_type` on the queue item tell the human what (if
anything) was found -- an exact sha256 hit (`match_type="sha"`,
similarity_score=1.0), the closest embedding match regardless of confidence
(`match_type="embedding"`), or nothing at all (`match_type=None`, nothing in
`unique_documents` to compare against).

Wire format: `states`/`crops` are sent as JSON-array-encoded strings
(`states_json`, `crops_json`) rather than native repeated Form fields --
list-typed Form() parameters aren't reliably supported across the FastAPI
version range this repo targets (>=0.110.0), so a single JSON string field
is used instead, matching what a frontend would send for e.g.
`states_json='["State Karnataka"]'`. `states_json` must decode to EXACTLY
ONE state (an upload is tagged to a single state); `crops_json` may hold
any number of crops.

`language` (a plain Form field, not JSON) is required -- one of the codes
returned by `GET /dashboard/languages` (a tessdata_best code, e.g. `"kan"`
for Kannada). This is not inferred from the selected state: it's the
language the middle-page OCR runs in, which directly feeds the
dedup-embedding, so an uploader must state it explicitly rather than have it
guessed from state.
"""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from dashboard import queue_worker
from dashboard.db import get_db
from dashboard.dedup import LANGUAGES
from dashboard.models import UploadQueueItem, UploadQueueStatus
from dashboard.schemas import UploadQueueItemOut

router = APIRouter()


@router.post("/uploads", response_model=UploadQueueItemOut, status_code=201)
async def create_upload(
    file: UploadFile = File(...),
    states_json: str = Form(...),
    crops_json: str = Form(...),
    language: str = Form(...),
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
    db: Session = Depends(get_db),
):
    try:
        state_names = json.loads(states_json)
        crop_names = json.loads(crops_json)
        if not isinstance(state_names, list) or not isinstance(crop_names, list):
            raise ValueError("states_json/crops_json must be JSON arrays")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"invalid states_json/crops_json: {e}")

    if len(state_names) != 1:
        raise HTTPException(400, "exactly one state must be selected per upload (multiple crops are allowed)")
    if not crop_names:
        raise HTTPException(400, "at least one crop must be selected")
    if language not in LANGUAGES:
        raise HTTPException(400, f"language must be one of {sorted(LANGUAGES)} (see GET /dashboard/languages)")

    pdf_bytes = await file.read()

    metadata_payload = {
        "state_names": state_names,
        "crop_names": crop_names,
        "language": language,
        "advisory_type": advisory_type,
        "advisory_scope": advisory_scope,
        "season": season,
        "edition_revision_volume": edition_revision_volume,
        "date_of_release": date_of_release,
        "month_of_release": month_of_release,
        "year_of_release": year_of_release,
        "date_of_collection": date_of_collection,
        "month_of_collection": month_of_collection,
        "year_of_collection": year_of_collection,
        "advisory_name": advisory_name,
        "advisory_released_org": advisory_released_org,
        "advisory_org_address": advisory_org_address,
        "live_source_link": live_source_link,
        "domain": domain,
        "verification_status": verification_status,
        "verified_by": verified_by,
        "document_status": document_status,
    }

    item = UploadQueueItem(filename=file.filename, metadata_payload=metadata_payload)
    db.add(item)
    db.flush()
    db.refresh(item)
    item_id = item.id
    # Commit explicitly (rather than waiting for get_db's post-yield commit)
    # so the row is visible to the queue worker's OWN session in a
    # background thread before we hand off to it.
    db.commit()

    queue_worker.enqueue(item_id, pdf_bytes)
    return item


@router.get("/uploads", response_model=list[UploadQueueItemOut])
def list_uploads(db: Session = Depends(get_db)):
    return db.query(UploadQueueItem).order_by(UploadQueueItem.created_at.desc()).all()


@router.get("/uploads/{item_id}", response_model=UploadQueueItemOut)
def get_upload(item_id: uuid.UUID, db: Session = Depends(get_db)):
    item = db.get(UploadQueueItem, item_id)
    if item is None:
        raise HTTPException(404, "upload not found")
    return item


@router.delete("/uploads/{item_id}", status_code=204)
def cancel_upload(item_id: uuid.UUID, db: Session = Depends(get_db)):
    item = db.get(UploadQueueItem, item_id)
    if item is None:
        raise HTTPException(404, "upload not found")
    if item.status != UploadQueueStatus.queued:
        raise HTTPException(409, f"cannot cancel an upload in status {item.status.value!r}")
    db.delete(item)


def _require_awaiting_review(db: Session, item_id: uuid.UUID) -> UploadQueueItem:
    item = db.get(UploadQueueItem, item_id)
    if item is None:
        raise HTTPException(404, "upload not found")
    if item.status != UploadQueueStatus.awaiting_review:
        raise HTTPException(409, f"upload is not awaiting review (status={item.status.value!r})")
    return item


@router.post("/uploads/{item_id}/add", status_code=200)
def add_upload_to_match(item_id: uuid.UUID, db: Session = Depends(get_db)):
    """This IS the matched document: link the upload's state/crop
    placement(s) onto it (skipping any that are already there -- e.g. if
    the match already has State X / Crop A,B and this upload is State X /
    Crop A,B,C, only C gets added), then remove the item from the queue.
    Requires a matched document to exist (duplicate_of_id set) -- if
    match_type is None (nothing to compare against at all), there's nothing
    to add to; use 'new' instead."""
    item = _require_awaiting_review(db, item_id)
    if item.duplicate_of_id is None:
        raise HTTPException(409, "no matched document to link to -- use 'new' instead")
    payload = item.metadata_payload
    linked = queue_worker.attach_new_placements(
        db, item.duplicate_of_id, payload.get("state_names", []), payload.get("crop_names", [])
    )
    linked_doc_id = item.duplicate_of_id
    queue_worker.discard_pending_upload(item_id)  # cached bytes only needed for a possible .../new call
    db.delete(item)
    db.flush()
    return {"linked_to": linked_doc_id, "placements_created": linked}


@router.post("/uploads/{item_id}/new", status_code=202)
def add_upload_as_new(item_id: uuid.UUID, db: Session = Depends(get_db)):
    """Create this upload as its own unique_documents row with its own
    placements -- used both when there was no candidate match at all
    (match_type is None) and when there was one but the human judges it a
    false positive (match_type="embedding"). Runs async (real Zoho upload
    involved), same pattern as POST /uploads itself; poll GET
    /uploads/{item_id} until it's gone (done) or status=failed."""
    item = _require_awaiting_review(db, item_id)
    if item.match_type == "sha":
        raise HTTPException(
            409,
            "cannot treat this as a new document -- it's byte-for-byte identical to the matched "
            "document (same sha256), not just similar",
        )
    queue_worker.enqueue_as_new(item_id)
    return {"processing": True}


@router.post("/uploads/{item_id}/cancel", status_code=204)
def cancel_upload_duplicate(item_id: uuid.UUID, db: Session = Depends(get_db)):
    """Don't append: nothing is linked and nothing new is created, the
    upload is simply discarded. Any matched document is untouched."""
    item = _require_awaiting_review(db, item_id)
    queue_worker.discard_pending_upload(item_id)
    db.delete(item)
