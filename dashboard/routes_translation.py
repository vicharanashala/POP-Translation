"""Translate / review-upload endpoints for unique_documents.

Reuses the EXISTING translation pipeline (pop_server._run_one_doc -- the
same split -> translate -> inject_images -> convert_to_docx -> merge flow
already used by the state/crop pipeline) rather than reimplementing it; the
dashboard only adds a new trigger point and a DB-backed status table
(translation_jobs) around it. _run_one_doc needs a PopRequest with
`.state`/`.crop` purely to build its LOCAL SCRATCH workdir path -- it has no
relation to Zoho storage locations -- so a synthetic state/crop pair
("_dashboard", <doc id>) is passed; this never collides with the real
Data/<State>/<Crop> layout since it's local-only and cleaned up afterward.
The final DOCX is uploaded to the dashboard's own Zoho mega-folder layout
(translations/), not the pipeline's Workdir/<State>/<Crop>/ layout.

Per the request: no separate upload box for translation/review -- these are
button-driven from the unique_documents columns, gated by GET /config's
translation_available flag (mirrors GEMINI_API_KEY/LLM_API_KEY_ENV not being
set yet).

translation_available is also gated on dashboard.config.TRANS_ENABLED (the
TRANS=on/off env var, 2026-08-27): this release deploys document management
only -- production is Gemini-only for now and the translate flow hasn't been
properly load-tested there yet, so TRANS=off makes the button read "out of
order" regardless of whether an LLM API key happens to be set. Proper
production-ready translation wiring is a later piece of work.
"""
from __future__ import annotations

import os
import shutil
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from dashboard import zoho_layout
from dashboard.config import REPO_ROOT, TRANS_ENABLED
from dashboard.db import get_db, get_session
from dashboard.display_id import format_display_id
from dashboard.models import (
    ReviewStatus,
    TranslationJob,
    TranslationJobKind,
    TranslationJobStatus,
    TranslationStatus,
    UniqueDocument,
)
from dashboard.schemas import ConfigOut, TranslationJobOut

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import _job_ctl as ctl  # noqa: E402 -- same cooperative-cancellation module pop_server.py's own /run uses

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-translate")

_DASHBOARD_WORK_ROOT = REPO_ROOT / "pop-data" / "POP_Work" / "Dashboard_Workdir"


def _translation_job_out(row: TranslationJob) -> TranslationJobOut:
    return TranslationJobOut(
        id=row.id,
        unique_document_id=row.unique_document_id,
        document_id=format_display_id(row.unique_document.display_id),
        shareable_name=row.unique_document.shareable_name,
        kind=row.kind,
        status=row.status,
        progress_pct=row.progress_pct,
        pages_done=row.pages_done,
        total_pages=row.total_pages,
        error_message=row.error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/config", response_model=ConfigOut)
def get_config():
    from pop_server import LLM_API_KEY_ENV

    return ConfigOut(translation_available=TRANS_ENABLED and bool(os.environ.get(LLM_API_KEY_ENV)))


@router.post("/unique-documents/{doc_id}/translate", status_code=202)
def start_translation(doc_id: uuid.UUID, db: Session = Depends(get_db)):
    from pop_server import LLM_API_KEY_ENV

    if not TRANS_ENABLED:
        raise HTTPException(503, "translation is currently out of order")
    if not os.environ.get(LLM_API_KEY_ENV):
        raise HTTPException(400, f"{LLM_API_KEY_ENV} is not set -- translation unavailable")

    doc = db.get(UniqueDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.zoho_file_id is None:
        raise HTTPException(409, "document has no stored original file")
    if doc.translation_status == TranslationStatus.in_progress:
        raise HTTPException(409, "translation already in progress")

    doc.translation_status = TranslationStatus.in_progress
    job = TranslationJob(unique_document_id=doc_id, kind=TranslationJobKind.translate)
    db.add(job)
    db.flush()
    db.refresh(job)
    job_id = job.id
    db.commit()  # visible to the background thread's own session before handoff

    # Created here (before handoff), not inside the background thread -- a
    # cancel request arriving between submit() and the thread actually
    # starting must still find an event to set.
    ctl.make_event(str(job_id))
    _executor.submit(_run_translation, job_id, doc_id)
    return {"job_id": job_id}


@router.get("/translation-jobs", response_model=list[TranslationJobOut])
def list_translation_jobs(status: TranslationJobStatus | None = None, db: Session = Depends(get_db)):
    q = db.query(TranslationJob)
    if status is not None:
        q = q.filter(TranslationJob.status == status)
    else:
        # Default view is the "queue" -- still-active jobs. Pass an explicit
        # ?status= to see done/failed/cancelled history.
        q = q.filter(TranslationJob.status.in_([TranslationJobStatus.queued, TranslationJobStatus.running]))
    rows = q.order_by(TranslationJob.created_at.desc()).all()
    return [_translation_job_out(r) for r in rows]


@router.post("/translation-jobs/{job_id}/cancel", status_code=202)
def cancel_translation_job(job_id: uuid.UUID, db: Session = Depends(get_db)):
    """Real cancellation, not a soft un-flag: this sets the same
    _job_ctl cancel event pop_server.py's own /run endpoint uses, which
    _run_one_doc checks between pipeline stages AND before every individual
    page's translation call -- so a running job stops within roughly one
    page's worth of work, not just once it happens to finish on its own.
    The job's status flips to "cancelled" asynchronously once the
    background thread observes it; poll GET /translation-jobs to see it
    land."""
    job = db.get(TranslationJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status not in (TranslationJobStatus.queued, TranslationJobStatus.running):
        raise HTTPException(409, f"job is already {job.status.value!r}")
    ctl.cancel(str(job_id))
    return {"cancelling": True}


def _progress_cb(job_id: uuid.UUID):
    def cb(pages_done: int, total_pages: int) -> None:
        with get_session() as session:
            job = session.get(TranslationJob, job_id)
            if job is None:
                return
            job.pages_done = pages_done
            job.total_pages = total_pages
            job.progress_pct = int(pages_done / total_pages * 100) if total_pages else 0

    return cb


def _run_translation(job_id: uuid.UUID, doc_id: uuid.UUID) -> None:
    from pop_server import DEFAULT_MODEL, LLM_API_KEY_ENV, POP_WORK, PROMPT_FILE, PopRequest, _get_zoho, _run_one_doc

    ctl.set_job_id(str(job_id))

    with get_session() as session:
        job = session.get(TranslationJob, job_id)
        job.status = TranslationJobStatus.running
        doc = session.get(UniqueDocument, doc_id)
        zoho_file_id = doc.zoho_file_id
        doc_name = (doc.shareable_name or str(doc_id)).rsplit(".", 1)[0]

    # _run_one_doc builds its own workdir internally as
    # POP_WORK/Workdir/<req.state>/<req.crop>/<doc_name> -- doc_root here
    # must match that formula exactly (state="_dashboard", crop=str(doc_id))
    # or the final_dir lookup below finds nothing.
    doc_root = POP_WORK / "Workdir" / "_dashboard" / str(doc_id) / doc_name
    tmp_pdf = _DASHBOARD_WORK_ROOT / f"{doc_id}.pdf"
    try:
        wd = _get_zoho()
        pdf_bytes = wd.download_file(zoho_file_id)
        _DASHBOARD_WORK_ROOT.mkdir(parents=True, exist_ok=True)
        tmp_pdf.write_bytes(pdf_bytes)

        req = PopRequest(state="_dashboard", crop=str(doc_id), model=DEFAULT_MODEL)
        api_key = os.environ[LLM_API_KEY_ENV]
        prompt = PROMPT_FILE.read_text(encoding="utf-8")

        _run_one_doc(tmp_pdf, doc_name, req, api_key, prompt, on_progress=_progress_cb(job_id))

        final_dir = doc_root / "final_output"
        final_docx = next(final_dir.glob("*_translated_pages_*.docx"))
        translation_zoho_file_id = zoho_layout.upload_translation(
            wd, f"{doc_name}_translated.docx", final_docx.read_bytes()
        )

        with get_session() as session:
            doc = session.get(UniqueDocument, doc_id)
            doc.translation_zoho_file_id = translation_zoho_file_id
            doc.translation_shareable_link = zoho_layout.shareable_link(translation_zoho_file_id)
            doc.translation_status = TranslationStatus.done
            job = session.get(TranslationJob, job_id)
            job.status = TranslationJobStatus.done
            job.progress_pct = 100
    except ctl.JobCancelled:
        with get_session() as session:
            job = session.get(TranslationJob, job_id)
            job.status = TranslationJobStatus.cancelled
            job.error_message = "Cancelled by user"
            doc = session.get(UniqueDocument, doc_id)
            doc.translation_status = TranslationStatus.not_started
    except Exception as e:  # noqa: BLE001
        with get_session() as session:
            job = session.get(TranslationJob, job_id)
            job.status = TranslationJobStatus.failed
            job.error_message = str(e)
            doc = session.get(UniqueDocument, doc_id)
            doc.translation_status = TranslationStatus.not_started
    finally:
        ctl.cleanup(str(job_id))
        shutil.rmtree(doc_root, ignore_errors=True)
        tmp_pdf.unlink(missing_ok=True)


@router.delete("/unique-documents/{doc_id}/translation", status_code=204)
def delete_translation(doc_id: uuid.UUID, db: Session = Depends(get_db)):
    doc = db.get(UniqueDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.translation_zoho_file_id:
        from pop_server import _get_zoho

        try:
            _get_zoho().delete(doc.translation_zoho_file_id)
        except Exception:
            pass
    doc.translation_zoho_file_id = None
    doc.translation_shareable_link = None
    doc.translation_status = TranslationStatus.not_started


@router.post("/unique-documents/{doc_id}/review", status_code=201)
async def upload_review(doc_id: uuid.UUID, file: UploadFile = File(...), db: Session = Depends(get_db)):
    doc = db.get(UniqueDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")

    from pop_server import _get_zoho

    wd = _get_zoho()
    content = await file.read()
    review_zoho_file_id = zoho_layout.upload_review(wd, file.filename, content)

    doc.review_zoho_file_id = review_zoho_file_id
    doc.review_shareable_link = zoho_layout.shareable_link(review_zoho_file_id)
    doc.review_status = ReviewStatus.done
    return {"review_zoho_file_id": review_zoho_file_id}


@router.delete("/unique-documents/{doc_id}/review", status_code=204)
def delete_review(doc_id: uuid.UUID, db: Session = Depends(get_db)):
    doc = db.get(UniqueDocument, doc_id)
    if doc is None:
        raise HTTPException(404, "document not found")
    if doc.review_zoho_file_id:
        from pop_server import _get_zoho

        try:
            _get_zoho().delete(doc.review_zoho_file_id)
        except Exception:
            pass
    doc.review_zoho_file_id = None
    doc.review_shareable_link = None
    doc.review_status = ReviewStatus.not_started
