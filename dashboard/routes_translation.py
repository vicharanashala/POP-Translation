"""Translate / review-upload endpoints for main-table rows.

Reuses the EXISTING translation pipeline (pop_server._run_one_doc -- the same
split -> translate -> inject_images -> convert_to_docx -> merge flow already
used by the state/crop pipeline) rather than reimplementing it; the dashboard
only adds a new trigger point and a DB-backed status table (translation_jobs)
around it. _run_one_doc needs a PopRequest with `.state`/`.crop` purely to
build its LOCAL SCRATCH workdir path -- it has no relation to Zoho storage
locations -- so a synthetic pair ("_dashboard", <row id>) is passed; this never
collides with the real Data/<State>/<Crop> layout since it's local-only and
cleaned up afterward. The final DOCX is uploaded to the dashboard's own Zoho
mega-folder layout (translations/), not the pipeline's Workdir layout.

JOBS ARE SCOPED TO A DOCUMENT, not to a placement. Translating a document once
covers every folder it is filed in -- the corpus's most-placed document appears
in 66 places, and under the previous flat schema that was 66 separate
translations of the same file. `POST /documents/{row_id}/translate` resolves the
row to its document, so the button can live on a main-table row while the work
and the cost stay per document.

WHICH physical file is translated: a document grouped by sha256 owns one copy
per folder, all byte-identical by construction, so the first entry in
duplicate_links is as good as any other -- see _source_file_id().

Gated by GET /config's translation_available flag, which requires both an LLM
API key and TRANS=on (dashboard/config.py).
"""
from __future__ import annotations

import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from pymongo import ASCENDING, DESCENDING

from dashboard import events, zoho_layout
from dashboard.config import DASHBOARD_TRANSLATE_CONCURRENCY, REPO_ROOT, TRANS_ENABLED
from dashboard.db import get_db, get_session
from dashboard.display_id import format_display_id
from dashboard.models import (
    COLL_UNIQUE_DOCUMENTS,
    COLL_TRANSLATION_JOBS,
    ReviewStatus,
    TranslationJobKind,
    TranslationJobStatus,
    TranslationStatus,
    new_translation_job,
    utcnow,
)
from dashboard.schemas import ConfigOut, TranslationJobOut

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import _job_ctl as ctl  # noqa: E402 -- same cooperative-cancellation module pop_server.py's own /run uses

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-translate")

_DASHBOARD_WORK_ROOT = REPO_ROOT / "pop-data" / "POP_Work" / "Dashboard_Workdir"


def _to_object_id(value: str, what: str) -> ObjectId:
    """A malformed id returns 422 -- ObjectId has no FastAPI validator, so the
    status a typed path param would have produced is raised by hand."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(422, f"malformed {what} id")


def _translation_job_out(row: dict, doc: dict | None) -> TranslationJobOut:
    return TranslationJobOut(
        id=str(row["_id"]),
        document_id=str(row["document_id"]),
        unique_document_id=str(row["document_id"]),
        document_code=format_display_id(doc.get("display_id")) if doc else None,
        shareable_name=doc.get("shareable_name") if doc else None,
        kind=row["kind"],
        status=row["status"],
        progress_pct=row["progress_pct"],
        pages_done=row.get("pages_done"),
        total_pages=row.get("total_pages"),
        error_message=row.get("error_message"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _hydrate_jobs(db, rows: list[dict]) -> list[TranslationJobOut]:
    """Fetch the rows a page of jobs refers to in one batched query."""
    if not rows:
        return []
    doc_ids = {r["document_id"] for r in rows}
    docs = {
        d["_id"]: d
        for d in db[COLL_UNIQUE_DOCUMENTS].find(
            {"_id": {"$in": list(doc_ids)}}, {"display_id": 1, "shareable_name": 1}
        )
    }
    return [_translation_job_out(r, docs.get(r["document_id"])) for r in rows]


@router.get("/config", response_model=ConfigOut)
def get_config():
    from pop_server import LLM_API_KEY_ENV

    return ConfigOut(translation_available=TRANS_ENABLED and bool(os.environ.get(LLM_API_KEY_ENV)))


def _person(value) -> str | None:
    """A *_by name as sent, trimmed; blank means unknown."""
    return (str(value).strip() or None) if value is not None else None


@router.post("/unique-documents/{document_id}/translate", status_code=202)
def start_translation(document_id: str, body: dict | None = Body(default=None), db=Depends(get_db)):
    """Queue the pipeline for this document. Optional JSON body
    `{"translated_by": "<display name>"}` -- recorded on the document when the
    translation lands."""
    from pop_server import LLM_API_KEY_ENV

    if not TRANS_ENABLED:
        raise HTTPException(503, "translation is currently out of order")
    if not os.environ.get(LLM_API_KEY_ENV):
        raise HTTPException(400, f"{LLM_API_KEY_ENV} is not set -- translation unavailable")

    oid = _to_object_id(document_id, "document")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "document not found")
    if _source_file_id(doc) is None:
        raise HTTPException(409, "document has no stored original file")
    if doc["translation_status"] == TranslationStatus.in_progress.value:
        raise HTTPException(409, "translation already in progress")

    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": oid},
        {"$set": {"translation_status": TranslationStatus.in_progress.value, "updated_at": utcnow()}},
    )
    job = new_translation_job(document_id=oid, kind=TranslationJobKind.translate)
    job["requested_by"] = _person((body or {}).get("translated_by"))
    job_id = db[COLL_TRANSLATION_JOBS].insert_one(job).inserted_id
    events.translation_changed(job_id)
    events.document_changed(oid)
    # pymongo writes apply immediately -- already visible to the background
    # thread's own connection before handoff.

    # Created here (before handoff), not inside the background thread -- a
    # cancel request arriving between submit() and the thread actually
    # starting must still find an event to set.
    ctl.make_event(str(job_id))
    _executor.submit(_run_translation, job_id, oid)
    return {"job_id": str(job_id)}


@router.get("/translation-jobs", response_model=list[TranslationJobOut])
def list_translation_jobs(status: TranslationJobStatus | None = None, db=Depends(get_db)):
    if status is not None:
        query = {"status": status.value}
    else:
        # Default view is the "queue" -- still-active jobs. Pass an explicit
        # ?status= to see done/failed/cancelled history.
        query = {"status": {"$in": [TranslationJobStatus.queued.value, TranslationJobStatus.running.value]}}
    rows = list(
        db[COLL_TRANSLATION_JOBS].find(query).sort([("created_at", DESCENDING), ("_id", ASCENDING)])
    )
    return _hydrate_jobs(db, rows)


@router.post("/translation-jobs/{job_id}/cancel", status_code=202)
def cancel_translation_job(job_id: str, db=Depends(get_db)):
    """Real cancellation, not a soft un-flag: this sets the same _job_ctl
    cancel event pop_server.py's own /run endpoint uses, which _run_one_doc
    checks between pipeline stages AND before every individual page's
    translation call -- so a running job stops within roughly one page's worth
    of work, not just once it happens to finish on its own. The job's status
    flips to "cancelled" asynchronously once the background thread observes
    it; poll GET /translation-jobs to see it land."""
    oid = _to_object_id(job_id, "job")
    job = db[COLL_TRANSLATION_JOBS].find_one({"_id": oid})
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] not in (TranslationJobStatus.queued.value, TranslationJobStatus.running.value):
        raise HTTPException(409, f"job is already {job['status']!r}")
    ctl.cancel(str(oid))
    return {"cancelling": True}


@router.delete("/translation-jobs/{job_id}", status_code=204)
def delete_translation_job(job_id: str, db=Depends(get_db)):
    """Clear one finished job off the queue.

    The queue is a view of work in flight, not a record of what was translated
    -- that lives on the document (`translation_status`, `translation_file_id`),
    and deleting the job does not touch it. So this only removes the queue
    entry, and only for a job that has stopped: a queued or running one has to
    be cancelled first, otherwise the background thread would go on writing
    progress to a row that no longer exists.
    """
    oid = _to_object_id(job_id, "job")
    job = db[COLL_TRANSLATION_JOBS].find_one({"_id": oid})
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] in (TranslationJobStatus.queued.value, TranslationJobStatus.running.value):
        raise HTTPException(409, f"job is {job['status']} -- cancel it first")
    db[COLL_TRANSLATION_JOBS].delete_one({"_id": oid})
    events.publish("translation", {"id": str(oid), "deleted": True})


def _set_job(job_id: ObjectId, **fields) -> None:
    fields["updated_at"] = utcnow()
    with get_session() as db:
        db[COLL_TRANSLATION_JOBS].update_one({"_id": job_id}, {"$set": fields})
    events.translation_changed(job_id)


def _set_doc(document_id: ObjectId, **fields) -> None:
    fields["updated_at"] = utcnow()
    with get_session() as db:
        db[COLL_UNIQUE_DOCUMENTS].update_one({"_id": document_id}, {"$set": fields})
    events.document_changed(document_id)


def _progress_cb(job_id: ObjectId):
    def cb(pages_done: int, total_pages: int) -> None:
        _set_job(
            job_id,
            pages_done=pages_done,
            total_pages=total_pages,
            progress_pct=int(pages_done / total_pages * 100) if total_pages else 0,
        )

    return cb


def _run_translation(job_id: ObjectId, document_id: ObjectId) -> None:
    from pop_server import DEFAULT_MODEL, LLM_API_KEY_ENV, POP_WORK, PROMPT_FILE, PopRequest, _get_zoho, _run_one_doc

    ctl.set_job_id(str(job_id))

    _set_job(job_id, status=TranslationJobStatus.running.value)
    with get_session() as db:
        doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": document_id})
        zoho_file_id = _source_file_id(doc)
        doc_name = (doc.get("shareable_name") or str(document_id)).rsplit(".", 1)[0]
        job = db[COLL_TRANSLATION_JOBS].find_one({"_id": job_id}, {"requested_by": 1}) or {}

    # _run_one_doc builds its own workdir internally as
    # POP_WORK/Workdir/<req.state>/<req.crop>/<doc_name> -- doc_root here must
    # match that formula exactly (state="_dashboard", crop=str(document_id)) or
    # the final_dir lookup below finds nothing.
    doc_root = POP_WORK / "Workdir" / "_dashboard" / str(document_id) / doc_name
    tmp_pdf = _DASHBOARD_WORK_ROOT / f"{document_id}.pdf"
    try:
        wd = _get_zoho()
        pdf_bytes = wd.download_file(zoho_file_id)
        _DASHBOARD_WORK_ROOT.mkdir(parents=True, exist_ok=True)
        tmp_pdf.write_bytes(pdf_bytes)

        req = PopRequest(state="_dashboard", crop=str(document_id), model=DEFAULT_MODEL,
                         concurrency=DASHBOARD_TRANSLATE_CONCURRENCY)
        api_key = os.environ[LLM_API_KEY_ENV]
        prompt = PROMPT_FILE.read_text(encoding="utf-8")

        _run_one_doc(tmp_pdf, doc_name, req, api_key, prompt, on_progress=_progress_cb(job_id))

        final_dir = doc_root / "final_output"
        final_docx = next(final_dir.glob("*_translated_pages_*.docx"))
        translation_zoho_file_id = zoho_layout.upload_translation(
            wd, f"{doc_name}_translated.docx", final_docx.read_bytes()
        )

        _set_doc(
            document_id,
            translation_zoho_file_id=translation_zoho_file_id,
            translation_shareable_link=zoho_layout.shareable_link(translation_zoho_file_id),
            translation_status=TranslationStatus.done.value,
            translated_by=job.get("requested_by"),
            translated_at=utcnow(),
        )
        _set_job(job_id, status=TranslationJobStatus.done.value, progress_pct=100)
    except ctl.JobCancelled:
        _set_job(job_id, status=TranslationJobStatus.cancelled.value, error_message="Cancelled by user")
        _set_doc(document_id, translation_status=TranslationStatus.not_started.value)
    except Exception as e:  # noqa: BLE001
        _set_job(job_id, status=TranslationJobStatus.failed.value, error_message=str(e))
        _set_doc(document_id, translation_status=TranslationStatus.not_started.value)
    finally:
        ctl.cleanup(str(job_id))
        shutil.rmtree(doc_root, ignore_errors=True)
        tmp_pdf.unlink(missing_ok=True)


@router.post("/unique-documents/{document_id}/translation", status_code=201)
async def upload_translation(document_id: str, file: UploadFile = File(...),
                             translated_by: str | None = Form(None), db=Depends(get_db)):
    """Attach a translation produced somewhere else.

    The same end state the pipeline reaches on its own -- the file lands in the
    dashboard's translations folder and the document gets
    `translation_status: "done"` plus `translation_file_id` -- but reached by a
    person handing over a file instead. That is the point: a team comparing two
    candidate translations picks one and attaches it, rather than being forced
    to take whatever the pipeline produced.

    Refused while a job for this document is queued or running: that job would
    overwrite what you just uploaded when it finishes. Cancel it first.

    Replacing an existing translation deletes the old file from WorkDrive, so a
    document does not accumulate abandoned copies nobody can reach.
    """
    oid = _to_object_id(document_id, "document")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "document not found")

    live = db[COLL_TRANSLATION_JOBS].find_one({
        "document_id": oid,
        "status": {"$in": [TranslationJobStatus.queued.value, TranslationJobStatus.running.value]},
    })
    if live is not None:
        raise HTTPException(
            409,
            "a translation job is already running for this document -- cancel it "
            "before uploading a translation, or it will overwrite yours",
        )

    from pop_server import _get_zoho

    wd = _get_zoho()
    content = await file.read()
    translation_zoho_file_id = zoho_layout.upload_translation(wd, file.filename, content)

    superseded = doc.get("translation_zoho_file_id")
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": oid},
        {
            "$set": {
                "translation_zoho_file_id": translation_zoho_file_id,
                "translation_shareable_link": zoho_layout.shareable_link(translation_zoho_file_id),
                "translation_status": TranslationStatus.done.value,
                "translated_by": _person(translated_by),
                "translated_at": utcnow(),
                "updated_at": utcnow(),
            }
        },
    )
    events.document_changed(oid)
    # Only after the new one is recorded: a failed delete must not be able to
    # leave the document pointing at a file that is already gone.
    if superseded and superseded != translation_zoho_file_id:
        try:
            wd.delete(superseded)
        except Exception:  # noqa: BLE001
            pass
    return {"translation_file_id": translation_zoho_file_id}


@router.delete("/unique-documents/{document_id}/translation", status_code=204)
def delete_translation(document_id: str, db=Depends(get_db)):
    oid = _to_object_id(document_id, "document")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.get("translation_zoho_file_id"):
        from pop_server import _get_zoho

        try:
            _get_zoho().delete(doc["translation_zoho_file_id"])
        except Exception:
            pass
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": oid},
        {
            "$set": {
                "translation_zoho_file_id": None,
                "translation_shareable_link": None,
                "translation_status": TranslationStatus.not_started.value,
                "translated_by": None,
                "translated_at": None,
                "updated_at": utcnow(),
            }
        },
    )
    events.document_changed(oid)


@router.post("/unique-documents/{document_id}/review", status_code=201)
async def upload_review(document_id: str, file: UploadFile = File(...),
                        reviewed_by: str | None = Form(None), db=Depends(get_db)):
    oid = _to_object_id(document_id, "document")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "document not found")

    from pop_server import _get_zoho

    wd = _get_zoho()
    content = await file.read()
    review_zoho_file_id = zoho_layout.upload_review(wd, file.filename, content)

    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": oid},
        {
            "$set": {
                "review_zoho_file_id": review_zoho_file_id,
                "review_shareable_link": zoho_layout.shareable_link(review_zoho_file_id),
                "review_status": ReviewStatus.done.value,
                "reviewed_by": _person(reviewed_by),
                "reviewed_at": utcnow(),
                "updated_at": utcnow(),
            }
        },
    )
    events.document_changed(oid)
    # review_file_id is the name every read endpoint uses for this; the
    # older key is kept so an existing caller does not break.
    return {"review_file_id": review_zoho_file_id,
            "review_zoho_file_id": review_zoho_file_id}


@router.delete("/unique-documents/{document_id}/review", status_code=204)
def delete_review(document_id: str, db=Depends(get_db)):
    oid = _to_object_id(document_id, "document")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.get("review_zoho_file_id"):
        from pop_server import _get_zoho

        try:
            _get_zoho().delete(doc["review_zoho_file_id"])
        except Exception:
            pass
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": oid},
        {
            "$set": {
                "review_zoho_file_id": None,
                "review_shareable_link": None,
                "review_status": ReviewStatus.not_started.value,
                "reviewed_by": None,
                "reviewed_at": None,
                "updated_at": utcnow(),
            }
        },
    )
    events.document_changed(oid)


# -- row-addressed aliases -----------------------------------------------------
# The team works in the main table, so the buttons are on a row; the work is per
# document. These resolve one to the other and delegate -- there is no second
# implementation to keep in step.


def _source_file_id(doc: dict) -> str | None:
    """The Zoho file id to translate from: the document's ANCHOR copy.

    A document owns one physical copy per folder it is filed in (WorkDrive keeps
    separate files rather than linking one into many), and
    `representative_file_id` names which of them IS the document. Translation
    must always use that one, never "whichever copy is first".

    That is not fussiness about today's data -- everything grouped so far is
    byte-identical, so today any copy would do. It is about what the merge
    endpoint will start doing: absorbing NEAR-duplicates, whose copies are a
    re-scan or a re-export and are NOT the same bytes. Anchoring now means
    translation does not silently start working on the wrong artefact then.

    Falls back to the first copy only if the anchor is unset or has since been
    removed from duplicate_links.
    """
    links = doc.get("duplicate_links") or []
    anchor = doc.get("representative_file_id")
    if anchor and any(l.get("zoho_file_id") == anchor for l in links):
        return anchor
    for link in links:
        if link.get("zoho_file_id"):
            return link["zoho_file_id"]
    return None


def _document_id_for_row(db, row_id: str) -> str:
    from dashboard.models import COLL_DOCUMENTS

    row = db[COLL_DOCUMENTS].find_one({"_id": _to_object_id(row_id, "document")})
    if row is None:
        raise HTTPException(404, "document not found")
    return str(row["unique_document_id"])


@router.post("/documents/{row_id}/translate", status_code=202)
def start_translation_for_row(row_id: str, body: dict | None = Body(default=None), db=Depends(get_db)):
    return start_translation(_document_id_for_row(db, row_id), body=body, db=db)


@router.delete("/documents/{row_id}/translation", status_code=204)
def delete_translation_for_row(row_id: str, db=Depends(get_db)):
    return delete_translation(_document_id_for_row(db, row_id), db=db)


@router.post("/documents/{row_id}/translation", status_code=201)
async def upload_translation_for_row(row_id: str, file: UploadFile = File(...),
                                     translated_by: str | None = Form(None), db=Depends(get_db)):
    return await upload_translation(_document_id_for_row(db, row_id), file,
                                    translated_by=translated_by, db=db)


@router.post("/documents/{row_id}/review", status_code=201)
async def upload_review_for_row(row_id: str, file: UploadFile = File(...),
                                reviewed_by: str | None = Form(None), db=Depends(get_db)):
    return await upload_review(_document_id_for_row(db, row_id), file, reviewed_by=reviewed_by, db=db)


@router.delete("/documents/{row_id}/review", status_code=204)
def delete_review_for_row(row_id: str, db=Depends(get_db)):
    return delete_review(_document_id_for_row(db, row_id), db=db)
