"""Background worker for the upload queue (upload_queue_items).

Same ThreadPoolExecutor-driven async-job shape pop_server.py already uses for
the translation pipeline, but DB-backed so queue state survives a server restart
instead of living only in an in-memory dict.

TWO PHASES, with a human decision between them:

    POST /uploads          queued -> hashing -> checking_duplicate -> awaiting_review
    POST /uploads/{id}/add    file the upload's NEW placements onto an existing
                              document -- no second copy of the metadata, and no
                              second upload to Zoho
    POST /uploads/{id}/new    create a separate document
    POST /uploads/{id}/cancel discard; nothing was uploaded

The pause is BEFORE the Zoho upload on purpose: a cancelled upload must not leave
an orphan file in WorkDrive, so the bytes wait on local disk (see STAGING_DIR)
and only reach Zoho once a person says go -- and only on the `new` path, since
`add` reuses the document's existing file.

THE DUPLICATE CHECK IS A PLACEHOLDER. `find_candidates()` below does one cheap,
honest thing -- looks for an existing document with the same sha256 -- and
returns nothing otherwise. It does NOT do similarity, so a re-encoded or
re-scanned copy of an existing document is reported as new. It already returns a
RANKED LIST of up to three, because that is the shape the chunk-match algorithm
will fill: today the list is 0 or 1 long and every score is 1.0.

WHY THE THREE-WAY CHOICE. An upload of a document already in the catalogue is
usually not a mistake -- it is the same advisory being filed under another state
or crop. So the decision is not "duplicate or not" but "what should exist
afterwards", and the answer depends on the placements: `add` is only offered when
this upload names a (state, crop) the chosen document does not already have.

MongoDB note: there is no session/transaction to hold open -- pymongo writes
apply immediately -- so get_session() simply hands out the Database and the
`with` blocks below are kept purely for call-site symmetry with the rest of the
package.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bson import ObjectId

from dashboard import zoho_layout
from dashboard.db import get_session
from dashboard.display_id import format_display_id, format_row_id, free_display_ids, free_row_ids
from dashboard.models import (
    COLL_DOCUMENTS,
    COLL_UNIQUE_DOCUMENTS,
    COLL_UPLOAD_QUEUE_ITEMS,
    MANUAL_METADATA_FIELDS,
    UploadQueueStatus,
    link_placement,
    new_copy_link,
    new_document,
    new_unique_document,
    new_upload_candidate,
    normalize_crop_name,
    normalize_state_name,
    remember_vocabulary,
    utcnow,
)

# Two workers, as before: the work per item is one Zoho upload plus a handful
# of inserts, and keeping the pool small avoids competing for bandwidth with
# the translation pipeline's own pool.
_UPLOAD_WORKERS = 2
_executor = ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS, thread_name_prefix="upload")

_METADATA_FIELDS = MANUAL_METADATA_FIELDS

# Uploaded bytes wait here between the duplicate check and the human's
# decision. On disk rather than in memory because the wait is open-ended -- an
# item can sit at awaiting_review for as long as the person takes -- and
# holding every pending PDF in the process would be a slow leak.
#
# UPLOAD_STAGING_DIR overrides the location. In a container the default temp
# directory dies with the container, so an item sitting at awaiting_review when
# it restarts loses its bytes; docker-compose.yml points this at the
# mounted pop-data volume instead.
#
# A staged file that disappears anyway fails its own item loudly at decision
# time (see _finish) and is simply re-uploaded. It is never mistaken for a
# successful upload.
STAGING_DIR = Path(
    os.environ.get("UPLOAD_STAGING_DIR") or (Path(tempfile.gettempdir()) / "pop_render_uploads")
)


def stage_path(item_id: ObjectId) -> Path:
    return STAGING_DIR / f"{item_id}.pdf"


def stage(item_id: ObjectId, pdf_bytes: bytes) -> None:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    stage_path(item_id).write_bytes(pdf_bytes)


def unstage(item_id: ObjectId) -> None:
    stage_path(item_id).unlink(missing_ok=True)


def _set_status(item_id: ObjectId, **fields) -> None:
    fields["updated_at"] = utcnow()
    # Enum members are stored by value, matching what the API serialises.
    for key, value in list(fields.items()):
        if isinstance(value, UploadQueueStatus):
            fields[key] = value.value
    with get_session() as db:
        db[COLL_UPLOAD_QUEUE_ITEMS].update_one({"_id": item_id}, {"$set": fields})


def enqueue_check(item_id: ObjectId) -> None:
    """Phase 1: hash, page-count and duplicate-check, then park for review."""
    _executor.submit(_check, item_id)


def enqueue_decision(item_id: ObjectId, document_id: ObjectId | None) -> None:
    """Phase 2. `document_id` names an existing document to file this upload
    under (add), or None to create a new one (new)."""
    _executor.submit(_finish, item_id, document_id)


def _placement_key(placement: dict) -> tuple:
    """Compare placements on their NORMALISED names, so "State Karnataka" and
    "Karnataka" are the same placement rather than two."""
    return (
        normalize_state_name(placement.get("state") or ""),
        normalize_crop_name(placement.get("crop") or ""),
    )


def new_placements_for(db, document, placements: list[dict]) -> list[dict]:
    """Which of `placements` the document does not already have.

    Read from the `documents` collection rather than from the document's own
    duplicate_links, because the rows are the authority on where a document is
    filed -- links describe physical files, which is a different question.
    """
    have = {
        (row.get("state") or "", row.get("crop") or "")
        for row in db[COLL_DOCUMENTS].find(
            {"unique_document_id": document["_id"]}, {"state": 1, "crop": 1}
        )
    }
    out, seen = [], set()
    for placement in placements:
        key = _placement_key(placement)
        if key in have or key in seen:
            continue
        seen.add(key)
        out.append({"state": key[0], "crop": key[1]})
    return out


def find_candidates(db, sha: str, placements: list[dict], limit: int = 3) -> list[dict]:
    """Up to `limit` existing documents this upload might be, best first.

    PLACEHOLDER: exact sha256 only, so the list is 0 or 1 long and the score is
    always 1.0. An empty list means "this exact file is not in the catalogue",
    NOT "this document is new" -- a re-scan or re-export has a different hash.

    THE SEAM. The real check reads this file's chunk vectors from local disk
    (they are not in Mongo -- the cluster is a 512 MB free tier), compares them
    against the corpus, and returns the best few with real scores. Everything
    around it -- the queue item, the three-way decision, the endpoints -- already
    works against a list and does not care how it was produced.
    """
    matches = list(db[COLL_UNIQUE_DOCUMENTS].find({"sha256": sha}).limit(limit))
    out = []
    for document in matches:
        candidate = new_upload_candidate(
            document=document,
            score=1.0,
            match_type="sha",
            new_placements=new_placements_for(db, document, placements),
        )
        candidate["document_code"] = format_display_id(document.get("display_id"))
        out.append(candidate)
    return out


def describe_candidates(candidates: list[dict]) -> str:
    """The note shown next to the decision buttons."""
    if not candidates:
        return ("No existing document has this exact file, so this will be filed as a "
                "new document. Note that the similarity check is not implemented yet -- "
                "a re-scanned or re-exported copy of an existing advisory would not "
                "have been found.")
    best = candidates[0]
    if best["can_add"]:
        pairs = ", ".join(f"{p['state']}/{p['crop']}" for p in best["new_placements"][:4])
        return (f"This exact file is already in the catalogue as {best['document_code']}, "
                f"filed in {best['placement_count']} place(s). Adding files it under "
                f"{len(best['new_placements'])} new place(s): {pairs}.")
    return (f"This exact file is already in the catalogue as {best['document_code']}, and "
            f"already filed under every place you selected. There is nothing to add.")


def find_or_create_document(db, *, sha: str, fields: dict) -> dict:
    """The unique document for this file, creating it if it is new.

    Reused rather than duplicated when the sha already exists. That is a
    backstop, not the normal path -- an exact match is surfaced as a candidate
    and the person is expected to choose "add". It matters when two uploads of
    the same file race, where the second must join the first's document rather
    than fail on the unique sha256 index.
    """
    existing = db[COLL_UNIQUE_DOCUMENTS].find_one({"sha256": sha})
    if existing is not None:
        return existing
    display_id = free_display_ids(db, 1)[0]
    doc = new_unique_document(display_id=display_id, sha256=sha, **fields)
    doc["_id"] = db[COLL_UNIQUE_DOCUMENTS].insert_one(doc).inserted_id
    return doc


def create_placements(db, *, document, placements: list[dict], copy: dict) -> list[dict]:
    """Insert one `documents` row per placement, all pointing at `document`.

    row_ids are drawn in bulk from one scan rather than one at a time, so filing
    a document under 20 crops does not rescan the collection 20 times. The unique
    index on row_id is still the arbiter if another writer takes one first -- an
    insert that loses that race raises, which is correct: the caller reports a
    failed upload rather than silently creating fewer rows than asked for.

    `copy` is the physical file each new placement is backed by. On the `add`
    path that is the document's EXISTING anchor file: the dashboard stores
    uploads in one flat WorkDrive folder, not per state/crop, so filing the same
    document somewhere else needs no second upload and creates no second file.
    Several copy entries then share a zoho_file_id, which is the truth for
    dashboard uploads -- unlike the crawled corpus, where WorkDrive really does
    hold a separate file per folder.
    """
    if not placements:
        return []
    row_ids = free_row_ids(db, len(placements))
    rows = []
    for placement, row_id in zip(placements, row_ids):
        state, crop = placement["state"], placement["crop"]
        remember_vocabulary(db, state=state, crop=crop)
        rows.append(new_document(row_id=row_id, unique_document_id=document["_id"],
                                 state=state, crop=crop))
    result = db[COLL_DOCUMENTS].insert_many(rows)
    for row, inserted_id in zip(rows, result.inserted_ids):
        row["_id"] = inserted_id
        link_placement(
            db, document["_id"],
            row_obj_id=inserted_id,
            row_id=row["row_id"],
            copy_link=new_copy_link(state=row["state"], crop=row["crop"], **copy),
        )
    return rows


def _check(item_id: ObjectId) -> None:
    """Phase 1. Ends at awaiting_review for EVERY item that gets this far,
    whether or not anything matched -- the person is the real check."""
    try:
        path = stage_path(item_id)
        if not path.exists():
            _set_status(item_id, status=UploadQueueStatus.failed,
                        error_message="staged upload file is missing")
            return
        pdf_bytes = path.read_bytes()

        with get_session() as db:
            item = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": item_id})
        if item is None:
            return

        _set_status(item_id, status=UploadQueueStatus.hashing, progress_pct=10)
        # sha256 is both the placeholder check's only signal and the join key to
        # the on-disk chunk fingerprints the real check will use.
        sha = hashlib.sha256(pdf_bytes).hexdigest()

        num_pages = None
        if (item["filename"] or "").lower().endswith(".pdf"):
            import fitz

            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                num_pages = doc.page_count

        _set_status(item_id, status=UploadQueueStatus.checking_duplicate,
                    sha256=sha, num_pages=num_pages, progress_pct=30)
        with get_session() as db:
            candidates = find_candidates(db, sha, item.get("placements") or [])

        _set_status(item_id, status=UploadQueueStatus.awaiting_review, progress_pct=50,
                    candidates=candidates, note=describe_candidates(candidates))
    except Exception as e:  # noqa: BLE001
        _set_status(item_id, status=UploadQueueStatus.failed, error_message=str(e))


def _finish(item_id: ObjectId, document_id: ObjectId | None) -> None:
    """Phase 2, entered only from an explicit decision.

    `document_id` set  -> ADD: file this upload's new placements onto that
                          existing document. Nothing is uploaded to Zoho and no
                          metadata is written; the document already has both.
    `document_id` None -> NEW: upload the file and create a document for it.
    """
    try:
        with get_session() as db:
            item = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": item_id})
        if item is None:
            return
        metadata = item.get("metadata") or {}
        filename = item["filename"]
        placements = item.get("placements") or []

        with get_session() as db:
            if document_id is not None:
                document = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": document_id})
                if document is None:
                    _set_status(item_id, status=UploadQueueStatus.failed,
                                error_message="the chosen document no longer exists")
                    return
                # Only the placements it does not already have. Re-filing a
                # document where it already is would create a duplicate row for
                # the same folder.
                placements = new_placements_for(db, document, placements)
                copy = {
                    "zoho_file_id": document.get("representative_file_id"),
                    "shareable_link": document.get("shareable_link"),
                    "shareable_name": document.get("shareable_name"),
                }
                verb = "Filed"
            else:
                path = stage_path(item_id)
                if not path.exists():
                    _set_status(item_id, status=UploadQueueStatus.failed,
                                error_message="staged upload file is missing -- upload the file again")
                    return
                pdf_bytes = path.read_bytes()
                _set_status(item_id, status=UploadQueueStatus.uploading, progress_pct=60)
                from pop_server import _get_zoho

                zoho_file_id = zoho_layout.upload_original(_get_zoho(), filename, pdf_bytes)
                document = find_or_create_document(
                    db,
                    sha=item.get("sha256") or hashlib.sha256(pdf_bytes).hexdigest(),
                    fields={
                        "shareable_name": filename,
                        "shareable_link": zoho_layout.shareable_link(zoho_file_id),
                        "representative_file_id": zoho_file_id,
                        "num_pages": item.get("num_pages"),
                        "format_original": (filename.rsplit(".", 1)[-1] or "pdf").lower(),
                        # The uploader's tessdata choice is both the OCR setting
                        # and the document's language, and a person choosing it
                        # is its own provenance -- neither read off the file
                        # (detected) nor inferred from the state.
                        "language": metadata.get("language"),
                        "language_source": "manual",
                        **{k: metadata.get(k) for k in _METADATA_FIELDS},
                    },
                )
                copy = {
                    "zoho_file_id": zoho_file_id,
                    "shareable_link": zoho_layout.shareable_link(zoho_file_id),
                    "shareable_name": filename,
                }
                verb = "Created"

            rows = create_placements(db, document=document, placements=placements, copy=copy)
            code = format_display_id(document["display_id"])
            row_codes = [format_row_id(r["row_id"]) for r in rows]
            # The decision is carried out, so the item leaves the queue. The
            # queue holds work that is in flight or waiting on a person -- a
            # finished upload is not either, and leaving a `done` row behind
            # just makes someone clear it by hand. The result is not lost: it
            # is the document and the rows themselves, which the main table
            # shows at the top (it sorts created_at DESC).
            #
            # `failed` items are the one thing that stays: they carry the error
            # and are retryable via the same add/new endpoints.
            db[COLL_UPLOAD_QUEUE_ITEMS].delete_one({"_id": item_id})
            print(f"[upload] {verb} {code} in {len(rows)} place(s)"
                  + (f": {', '.join(row_codes)}" if row_codes else ""), flush=True)
        # Only once the rows exist: a failure above leaves the staged file in
        # place so the decision can be retried without re-uploading.
        unstage(item_id)

    except Exception as e:  # noqa: BLE001
        _set_status(item_id, status=UploadQueueStatus.failed, error_message=str(e))
