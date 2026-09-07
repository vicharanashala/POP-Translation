"""Duplicate review: propose, then merge on a person's say-so.

Two endpoints, and only one of them does anything yet.

    POST /documents/{row_id}/find-duplicates
        The "find duplicates" button. Returns an EMPTY candidate list and says
        why -- the chunk-match algorithm is not wired up here. It exists now so
        the frontend can be built against its real shape, and so the seam is
        obvious when the algorithm lands: fill in `_find_candidates()` and
        nothing else moves.

    POST /unique-documents/{id}/merge
        The team's decision. Absorbs the named documents into this one.

WHAT A MERGE DOES, precisely:

  - every placement of an absorbed document is REPOINTED at the survivor
  - the absorbed documents' `duplicate_links` are appended to the survivor's,
    so every physical copy in WorkDrive stays reachable -- but the survivor's
    `representative_file_id` is left alone, so translation keeps acting on the
    survivor's own file rather than on an absorbed near-duplicate
  - the absorbed ANNAM ids are recorded in the survivor's `merged_from`
  - the absorbed DOCUMENTS are deleted

WHAT IT NEVER DOES: delete a `documents` row. Each one is a real file sitting in
a real folder in WorkDrive, and the main table's job is to say so. A merge
changes which document a folder entry is understood to hold; it does not claim
the folder entry stopped existing.

Merging is not reversible by this API, which is why `merged_from` exists: the
absorbed ANNAM ids and their copy links are all still on the survivor, so a
person can reconstruct the split by hand if a merge turns out to be wrong.
"""
from __future__ import annotations

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException

from dashboard.db import get_db
from dashboard.display_id import format_display_id, parse_display_id
from dashboard.models import COLL_DOCUMENTS, COLL_TRANSLATION_JOBS, COLL_UNIQUE_DOCUMENTS, utcnow
from dashboard.schemas import DuplicateCandidatesOut, MergeRequest, MergeResult

router = APIRouter()

_NOT_IMPLEMENTED_NOTE = (
    "The duplicate-finding algorithm is not connected yet, so this is an empty "
    "list rather than a statement that no duplicates exist. Byte-identical "
    "files are already grouped -- every placement of the same bytes shares one "
    "document. What is still to come is near-duplicates: a re-scan or a "
    "re-export of the same advisory, which has a different sha256 and will be "
    "found by comparing chunk embeddings."
)


def _resolve_document(db, value: str) -> dict | None:
    """Accept either an ObjectId hex or an ANNAM id, because the team works in
    ANNAM ids and the frontend works in ObjectIds."""
    display_id = parse_display_id(value)
    if display_id is not None and not _looks_like_object_id(value):
        return db[COLL_UNIQUE_DOCUMENTS].find_one({"display_id": display_id})
    try:
        return db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": ObjectId(value)})
    except (InvalidId, TypeError):
        return None


def _looks_like_object_id(value: str) -> bool:
    """A 24-char hex string. Checked explicitly because "000000000000000000000042"
    parses as both an ObjectId and (via parse_display_id) an integer."""
    value = str(value).strip()
    if len(value) != 24:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _to_object_id(value: str, what: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(422, f"malformed {what} id")


def _find_candidates(db, document: dict) -> list:
    """THE SEAM. Returns documents that may be the same as `document`.

    Empty today. When the chunk-match algorithm lands it goes here: read this
    document's chunk vectors from local disk by sha256 (they are NOT in Mongo --
    the cluster is a 512 MB free tier), compare against the rest, and return
    DuplicateCandidate entries with a real score. Everything downstream of this
    function -- the endpoint, the response shape, the merge -- already works.
    """
    return []


@router.post("/documents/{row_id}/find-duplicates", response_model=DuplicateCandidatesOut)
def find_duplicates_for_row(row_id: str, db=Depends(get_db)):
    """The button on a main-table row. Resolves the row to its document and
    asks for candidates."""
    row = db[COLL_DOCUMENTS].find_one({"_id": _to_object_id(row_id, "document")})
    if row is None:
        raise HTTPException(404, "document not found")
    document = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": row["unique_document_id"]})
    if document is None:
        raise HTTPException(404, "the row's unique document is missing")
    return DuplicateCandidatesOut(
        document_id=format_display_id(document.get("display_id")) or "",
        candidates=_find_candidates(db, document),
        note=_NOT_IMPLEMENTED_NOTE,
    )


@router.post("/unique-documents/{document_id}/find-duplicates", response_model=DuplicateCandidatesOut)
def find_duplicates_for_document(document_id: str, db=Depends(get_db)):
    """The same search, started from the document rather than a placement."""
    document = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": _to_object_id(document_id, "unique document")})
    if document is None:
        raise HTTPException(404, "unique document not found")
    return DuplicateCandidatesOut(
        document_id=format_display_id(document.get("display_id")) or "",
        candidates=_find_candidates(db, document),
        note=_NOT_IMPLEMENTED_NOTE,
    )


@router.post("/unique-documents/{document_id}/merge", response_model=MergeResult)
def merge_documents(document_id: str, body: MergeRequest, db=Depends(get_db)):
    """Absorb the named documents into this one.

    Every id in `absorb` must resolve and must not be the survivor itself; the
    whole request is validated before anything is written, so a typo in the
    fifth id does not leave the first four already merged.
    """
    survivor = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": _to_object_id(document_id, "unique document")})
    if survivor is None:
        raise HTTPException(404, "unique document not found")
    if not body.absorb:
        raise HTTPException(400, "nothing to absorb")

    absorbed: list[dict] = []
    seen: set = {survivor["_id"]}
    for value in body.absorb:
        other = _resolve_document(db, value)
        if other is None:
            raise HTTPException(404, f"unique document not found: {value}")
        if other["_id"] == survivor["_id"]:
            raise HTTPException(400, "a document cannot absorb itself")
        if other["_id"] in seen:
            continue  # the same document named twice -- harmless, absorb once
        seen.add(other["_id"])
        absorbed.append(other)

    absorbed_ids = [d["_id"] for d in absorbed]
    codes = [format_display_id(d.get("display_id")) for d in absorbed]

    # 1. Repoint the placements. Done first: if anything below fails, the rows
    #    point at a document that still exists, which is a recoverable state.
    #    Deleting first would leave them pointing at nothing.
    repointed = db[COLL_DOCUMENTS].update_many(
        {"unique_document_id": {"$in": absorbed_ids}},
        {"$set": {"unique_document_id": survivor["_id"], "updated_at": utcnow()}},
    ).modified_count

    # 2. Move the copy links and the placement ids across, so every physical
    #    file in WorkDrive is still reachable from the surviving document.
    #    THE ANCHOR IS NOT TOUCHED. representative_file_id keeps naming the
    #    survivor's own file, so translation goes on acting on the bytes this
    #    document's metadata describes -- absorbed copies are merely reachable,
    #    never promoted. That matters as soon as near-duplicates are merged,
    #    since those copies are a re-scan or re-export and not the same bytes.
    moved_links = [link for d in absorbed for link in (d.get("duplicate_links") or [])]
    moved_rows = [rid for d in absorbed for rid in (d.get("main_row_ids") or [])]
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": survivor["_id"]},
        {
            "$push": {"duplicate_links": {"$each": moved_links}},
            "$addToSet": {
                "main_row_ids": {"$each": moved_rows},
                "merged_from": {"$each": [c for c in codes if c]},
            },
            "$set": {"updated_at": utcnow()},
        },
    )

    # 3. Any translation job queued against an absorbed document now belongs to
    #    the survivor -- the work is the same work.
    db[COLL_TRANSLATION_JOBS].update_many(
        {"document_id": {"$in": absorbed_ids}},
        {"$set": {"document_id": survivor["_id"], "updated_at": utcnow()}},
    )

    # 4. Only now delete the absorbed documents.
    db[COLL_UNIQUE_DOCUMENTS].delete_many({"_id": {"$in": absorbed_ids}})

    after = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": survivor["_id"]})
    return MergeResult(
        document_id=format_display_id(survivor.get("display_id")) or "",
        absorbed=[c for c in codes if c],
        placements_repointed=repointed,
        placement_count=len(after.get("main_row_ids") or []),
    )
