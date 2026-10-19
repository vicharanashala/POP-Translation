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

PAGE BY PAGE. Each page is translated, made into its own one-page .docx and
uploaded to the translations folder as soon as it is done; the job row keeps
`page_links` (one per page, "" until that page is done). A page that fails
leaves its slot empty and the rest carry on, so the job ends one of three ways:
done (every page filled -> merged into the document's translation, then the
page files are trashed), failed (some slots empty, the page files KEPT), or
cancelled (the page files KEPT too). There is no redo endpoint: translating a
document whose last job failed or was cancelled resumes that job and does only
the empty pages. A job's pages are given up only by deleting the job.
It used to be all or nothing -- one bad page out of 146 threw away the other
145.

THREE MODES, a tag on the job (`mode`): normal (one Gemini call a page, now),
flex (the same calls on Google's half-price best-effort tier -- each page can
take 1-15 minutes, and it still counts against the daily request limit) and
batch (every page in ONE Gemini Batch API job: half price, outside the daily
limit, but Google takes up to 24 hours). A batch job goes through
_run_translation twice. The first pass cuts the pages, submits them and
returns, leaving the job "running" with `batch_name` set and no thread holding
it. The batch poller (_poll_batches: every 2 minutes, and on every
GET /translation-jobs, i.e. the frontend's refresh) asks Google how it is going;
once the batch has finished it claims the job ("finishing") and runs the second
pass, which writes each page's HTML where the normal call would have, so the
rest -- images, .docx, upload, merge -- is the same code for all three modes.

Gated by GET /config's translation_available flag, which requires both an LLM
API key and TRANS=on (dashboard/config.py).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from pymongo import ASCENDING, DESCENDING

from dashboard import events, zoho_layout
from dashboard.config import DASHBOARD_TRANSLATE_CONCURRENCY, REPO_ROOT, TRANS_ENABLED
from dashboard.access import require_admin
from dashboard.db import get_db, get_session
from dashboard.display_id import format_display_id
from dashboard.models import (
    COLL_UNIQUE_DOCUMENTS,
    COLL_TRANSLATION_JOBS,
    ReviewStatus,
    TranslationJobKind,
    TranslationJobStatus,
    TranslationMode,
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

PAGE_ATTEMPTS = 3
# Flex is often turned away (503, no spare capacity) and waits double between
# tries: 15s, 30s ... 4 min, about 8 minutes in all before a page gives up.
FLEX_PAGE_ATTEMPTS = 6

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
        mode=row.get("mode") or TranslationMode.normal.value,
        batch_state=row.get("batch_state"),
        status=row["status"],
        progress_pct=row["progress_pct"],
        pages_done=row.get("pages_done"),
        total_pages=row.get("total_pages"),
        error_message=row.get("error_message"),
        page_links=row.get("page_links") or [],
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


@router.post("/unique-documents/{document_id}/translate", status_code=202, dependencies=[Depends(require_admin)])
def start_translation(document_id: str, body: dict | None = Body(default=None), db=Depends(get_db)):
    """Queue the pipeline for this document. Optional JSON body
    `{"translated_by": "<display name>", "mode": "normal" | "flex" | "batch"}`
    -- translated_by is recorded on the document when the translation lands;
    mode defaults to normal (see the module docstring).

    There is no separate redo: if the document's last job FAILED or was
    CANCELLED, this picks that same job up again and translates only the pages
    it has no link for.
    """
    from pop_server import LLM_API_KEY_ENV, LLM_PROVIDER

    if not TRANS_ENABLED:
        raise HTTPException(503, "translation is currently out of order")
    if not os.environ.get(LLM_API_KEY_ENV):
        raise HTTPException(400, f"{LLM_API_KEY_ENV} is not set -- translation unavailable")
    try:
        mode = TranslationMode((body or {}).get("mode") or TranslationMode.normal.value)
    except ValueError:
        raise HTTPException(422, "mode must be one of: normal, flex, batch")
    if mode is not TranslationMode.normal and LLM_PROVIDER != "gemini":
        raise HTTPException(400, f"{mode.value} translation needs Gemini; this server uses {LLM_PROVIDER}")

    oid = _to_object_id(document_id, "document")
    _expire_lost_jobs(db)
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid})
    if doc is None:
        raise HTTPException(404, "document not found")
    if _source_file_id(doc) is None:
        raise HTTPException(409, "document has no stored original file")
    if doc["translation_status"] == TranslationStatus.in_progress.value or db[COLL_TRANSLATION_JOBS].find_one(
        {"document_id": oid, "status": {"$in": [TranslationJobStatus.queued.value, TranslationJobStatus.running.value]}}
    ):
        raise HTTPException(409, "translation already in progress")

    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": oid},
        {"$set": {"translation_status": TranslationStatus.in_progress.value, "updated_at": utcnow()}},
    )
    requested_by = _person((body or {}).get("translated_by"))
    failed = list(db[COLL_TRANSLATION_JOBS].find({
        "document_id": oid,
        "status": {"$in": [TranslationJobStatus.failed.value, TranslationJobStatus.cancelled.value]},
    }).sort([("created_at", DESCENDING), ("_id", DESCENDING)]))
    if failed:
        # The newest failed/cancelled job carries on; any older one still
        # holding pages (two documents merged, each with its own run) is given
        # up, its pages trashed. Older ones with nothing left are history.
        job_id = failed[0]["_id"]
        db[COLL_TRANSLATION_JOBS].update_one({"_id": job_id}, {"$set": {
            "status": TranslationJobStatus.queued.value, "error_message": None, "mode": mode.value,
            "batch_name": None, "batch_state": None,
            "requested_by": requested_by, "heartbeat_at": utcnow(), "updated_at": utcnow(),
        }})
        for old in failed[1:]:
            if old["status"] != TranslationJobStatus.failed.value and not any(old.get("page_file_ids") or []):
                continue
            _end_job_pages(db, old)
            db[COLL_TRANSLATION_JOBS].update_one({"_id": old["_id"]}, {"$set": {
                "status": TranslationJobStatus.cancelled.value,
                "error_message": "replaced by a newer translation of the same document", "updated_at": utcnow(),
            }})
            events.translation_changed(old["_id"])
    else:
        job = new_translation_job(document_id=oid, kind=TranslationJobKind.translate, mode=mode)
        job["requested_by"] = requested_by
        job_id = db[COLL_TRANSLATION_JOBS].insert_one(job).inserted_id
    events.translation_changed(job_id)
    events.document_changed(oid)
    # pymongo writes apply immediately -- already visible to the background
    # thread's own connection before handoff.

    # Created here (before handoff), not inside the background thread -- a
    # cancel request arriving between submit() and the thread actually
    # starting must still find an event to set.
    ctl.make_event(str(job_id))
    _held_jobs.add(job_id)
    _start_heartbeat()
    _executor.submit(_run_translation, job_id, oid)
    return {"job_id": str(job_id)}


@router.get("/translation-jobs", response_model=list[TranslationJobOut], dependencies=[Depends(require_admin)])
def list_translation_jobs(status: TranslationJobStatus | None = None, db=Depends(get_db)):
    _expire_lost_jobs(db)
    _poll_batches(db, min_gap=15)  # the frontend's refresh asks Google too
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


@router.post("/translation-jobs/{job_id}/cancel", status_code=202, dependencies=[Depends(require_admin)])
def cancel_translation_job(job_id: str, db=Depends(get_db)):
    """Real cancellation, not a soft un-flag: this sets the same _job_ctl
    cancel event pop_server.py's own /run endpoint uses, which the runner
    checks before every page and every step of a page -- so a running job stops
    within roughly one page's worth of work. The job's status flips to
    "cancelled" asynchronously once the background thread observes it; poll
    GET /translation-jobs to see it land.

    The pages already done are KEPT, as on a failed job: translating the
    document again resumes this job. A FAILED job can be cancelled too; it
    just turns cancelled. Deleting the job is what gives its pages up."""
    oid = _to_object_id(job_id, "job")
    _expire_lost_jobs(db)
    job = db[COLL_TRANSLATION_JOBS].find_one({"_id": oid})
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] == TranslationJobStatus.failed.value:
        db[COLL_TRANSLATION_JOBS].update_one({"_id": oid}, {"$set": {
            "status": TranslationJobStatus.cancelled.value, "error_message": "Cancelled by user", "updated_at": utcnow(),
        }})
        events.translation_changed(oid)
        return {"cancelling": True}
    if job["status"] not in (TranslationJobStatus.queued.value, TranslationJobStatus.running.value):
        raise HTTPException(409, f"job is already {job['status']!r}")
    if job.get("batch_name") and db[COLL_TRANSLATION_JOBS].update_one(
        {"_id": oid, "status": TranslationJobStatus.running.value, "batch_state": {"$ne": _FINISHING}},
        {"$set": {"status": TranslationJobStatus.cancelled.value, "error_message": "Cancelled by user",
                  "updated_at": utcnow()}},
    ).modified_count:
        # A batch Google is still working on: no thread here to signal, so it
        # is cancelled at Google and marked cancelled at once.
        _cancel_batch(job["batch_name"])
        db[COLL_UNIQUE_DOCUMENTS].update_one({"_id": job["document_id"]}, {"$set": {
            "translation_status": TranslationStatus.not_started.value, "updated_at": utcnow()}})
        events.translation_changed(oid)
        events.document_changed(job["document_id"])
        return {"cancelling": True}
    ctl.cancel(str(oid))
    return {"cancelling": True}


@router.delete("/translation-jobs/{job_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_translation_job(job_id: str, db=Depends(get_db)):
    """Clear one finished job off the queue.

    The queue is a view of work in flight, not a record of what was translated
    -- that lives on the document (`translation_status`, `translation_file_id`),
    and deleting the job does not touch it. So this only removes the queue
    entry, and only for a job that has stopped: a queued or running one has to
    be cancelled first, otherwise the background thread would go on writing
    progress to a row that no longer exists. A failed job's page files go with
    it -- nothing could reach them afterwards.
    """
    oid = _to_object_id(job_id, "job")
    _expire_lost_jobs(db)
    job = db[COLL_TRANSLATION_JOBS].find_one({"_id": oid})
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] in (TranslationJobStatus.queued.value, TranslationJobStatus.running.value):
        raise HTTPException(409, f"job is {job['status']} -- cancel it first")
    _end_job_pages(db, job)
    db[COLL_TRANSLATION_JOBS].delete_one({"_id": oid})
    events.publish("translation", {"id": str(oid), "deleted": True})


# -- Jobs lost to a restart ----------------------------------------------------
# The executor lives in this process, so a restart silently drops every queued
# and running job and leaves its row saying "running" forever. Each process
# heartbeats the jobs it holds; a job whose heartbeat is stale belonged to a
# process that is gone. It is marked FAILED -- its finished pages are in
# WorkDrive and their links on the row, so translating again carries on. A
# heartbeat rather than a sweep at startup, because staging is one database
# served by several servers at once (the local container, ad-hoc test servers),
# and one starting up must not fail another's live jobs.

_HEARTBEAT_EVERY = 30  # seconds
_LOST_AFTER = 180  # seconds without a heartbeat
_held_jobs: set[ObjectId] = set()
_heartbeat_lock = threading.Lock()
_heartbeat_thread: threading.Thread | None = None


def _start_heartbeat() -> None:
    global _heartbeat_thread
    with _heartbeat_lock:
        if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
            return
        _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="translate-heartbeat")
        _heartbeat_thread.start()


def _heartbeat_loop() -> None:
    while True:
        time.sleep(_HEARTBEAT_EVERY)
        held = list(_held_jobs)
        if not held:
            continue
        try:
            with get_session() as db:
                db[COLL_TRANSLATION_JOBS].update_many({"_id": {"$in": held}}, {"$set": {"heartbeat_at": utcnow()}})
        except Exception as e:  # noqa: BLE001
            print(f"[translate] heartbeat failed: {type(e).__name__}: {e}", flush=True)


def _expire_lost_jobs(db) -> None:
    cutoff = utcnow() - timedelta(seconds=_LOST_AFTER)
    active = [TranslationJobStatus.queued.value, TranslationJobStatus.running.value]
    lost = list(db[COLL_TRANSLATION_JOBS].find({
        "status": {"$in": active},
        "_id": {"$nin": list(_held_jobs)},
        # A batch Google is working on is held by no thread; the poller has it.
        "$nor": [{"batch_name": {"$type": "string"}, "batch_state": {"$ne": _FINISHING}}],
        # Rows from before the heartbeat existed have only updated_at.
        "$or": [{"heartbeat_at": {"$lt": cutoff}},
                {"heartbeat_at": {"$exists": False}, "updated_at": {"$lt": cutoff}}],
    }, {"document_id": 1}))
    for job in lost:
        changed = db[COLL_TRANSLATION_JOBS].update_one({"_id": job["_id"], "status": {"$in": active}}, {"$set": {
            "status": TranslationJobStatus.failed.value,
            "error_message": "interrupted -- the server stopped during this translation; translate again to carry on",
            "updated_at": utcnow(),
        }}).modified_count
        if changed:
            db[COLL_UNIQUE_DOCUMENTS].update_one(
                {"_id": job["document_id"], "translation_status": TranslationStatus.in_progress.value},
                {"$set": {"translation_status": TranslationStatus.not_started.value, "updated_at": utcnow()}},
            )
            events.translation_changed(job["_id"])
            events.document_changed(job["document_id"])


# -- Page files ----------------------------------------------------------------

def _trash_files(file_ids) -> None:
    """Best effort: a page file that will not trash is left in WorkDrive rather
    than holding the job's state hostage."""
    file_ids = [f for f in file_ids if f]
    if not file_ids:
        return
    from pop_server import _get_zoho

    wd = _get_zoho()
    for file_id in file_ids:
        try:
            wd.delete(file_id)
        except Exception as e:  # noqa: BLE001
            print(f"[translate] could not trash page file {file_id}: {type(e).__name__}: {e}", flush=True)


def _end_job_pages(db, job: dict) -> None:
    """Trash a job's page files and empty its page lists."""
    _trash_files(job.get("page_file_ids") or [])
    db[COLL_TRANSLATION_JOBS].update_one({"_id": job["_id"]}, {"$set": {"page_links": [], "page_file_ids": []}})


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


class _PagesFailed(Exception):
    pass


def _translate_one_page(pdf_path: Path, docx_path: Path, doc_name: str, req, api_key: str, prompt: str,
                        tier: str | None = None) -> str | None:
    """Translate -> images -> one-page .docx for one page. Returns None on
    success, else why it failed. A page whose translated.html is already there
    (a batch result) skips the translate step."""
    from pop_server import LLM_PROVIDER
    import run_pop_to_docx_updated_pagewise_docx as pop

    ctl.check_cancel()
    result = pop.process_translation_for_page(
        page_pdf_path=pdf_path, prompt=prompt, model=req.model, api_key=api_key,
        overwrite_existing=False, max_retries=req.max_retries, retry_wait_seconds=req.retry_wait_seconds,
        enable_google_search=not req.disable_google_search, provider=LLM_PROVIDER, gemini_service_tier=tier,
    )
    if result.get("status") == "failed":
        return f"translation: {result.get('error') or 'failed'}"
    ctl.check_cancel()
    result = pop.process_image_injection_for_page(page_pdf_path=pdf_path, overwrite_existing=False)
    if result.get("status") == "failed":
        return f"images: {result.get('error') or 'failed'}"
    ctl.check_cancel()
    result = pop.convert_page_to_docx(page_pdf_path=pdf_path, output_docx_path=docx_path,
                                      doc_title=doc_name, overwrite_existing=False)
    if result.get("status") == "failed":
        return f"docx: {result.get('error') or 'failed'}"
    return None


def _run_translation(job_id: ObjectId, document_id: ObjectId) -> None:
    """Every page is its own unit of work: translated, given its images, made
    into a one-page .docx and uploaded to WorkDrive the moment it is done, its
    link written into the job's page_links at that page's index. A page that
    fails leaves its slot "" and the others carry on. Only when every slot is
    filled are the pages merged into the document's translation -- and then the
    page files are trashed. So a failed job loses nothing: translating the
    document again (start_translation) resumes this job and does only the ""
    pages.

    Not pop_server._run_one_doc, which throws the whole document away when one
    page fails and builds the .docx only after every page has translated.
    """
    from pop_server import DEFAULT_MODEL, LLM_API_KEY_ENV, POP_WORK, PROMPT_FILE, PopRequest, _get_zoho, _tl_stdout
    import run_pop_to_docx_updated_pagewise_docx as pop

    ctl.set_job_id(str(job_id))
    doc_root = POP_WORK / "Workdir" / "_dashboard" / str(job_id)
    tmp_pdf = _DASHBOARD_WORK_ROOT / f"{job_id}.pdf"
    try:
        ctl.check_cancel()
        _set_job(job_id, status=TranslationJobStatus.running.value)
        with get_session() as db:
            doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": document_id})
            job = db[COLL_TRANSLATION_JOBS].find_one({"_id": job_id}) or {}
        if doc is None:
            raise RuntimeError("the document no longer exists")
        zoho_file_id = _source_file_id(doc)
        doc_name = (doc.get("shareable_name") or str(document_id)).rsplit(".", 1)[0]
        safe_name = pop.safe_docx_name(doc_name)

        wd = _get_zoho()
        _DASHBOARD_WORK_ROOT.mkdir(parents=True, exist_ok=True)
        # The script's log file is one process-wide global. It must not live in
        # a job's workdir: that is deleted when the job ends, and every other
        # job's next log line would then fail to open it.
        pop.set_log_file(_DASHBOARD_WORK_ROOT / "pipeline_runtime.log")
        tmp_pdf.write_bytes(wd.download_file(zoho_file_id))
        page_pdfs, total = pop.split_pdf_to_page_folders(
            source_pdf_path=tmp_pdf, output_root=doc_root, start_page=1, end_page=None)

        links = list(job.get("page_links") or [])
        file_ids = list(job.get("page_file_ids") or [])
        if len(links) != total or len(file_ids) != total or job.get("source_file_id") != zoho_file_id:
            # Not this file's pages (first run, or the original was replaced).
            _trash_files(file_ids)
            links, file_ids = [""] * total, [""] * total
        _set_job(job_id, page_links=links, page_file_ids=file_ids, source_file_id=zoho_file_id,
                 total_pages=total, pages_done=sum(1 for x in links if x),
                 progress_pct=int(sum(1 for x in links if x) / total * 100) if total else 0)

        pop.check_pandoc_available()
        page_dir = doc_root / "page_docx"
        page_dir.mkdir(parents=True, exist_ok=True)
        docx_paths = [page_dir / f"{safe_name}_{pop.page_name(i + 1)}.docx" for i in range(total)]
        todo = [i for i in range(total) if not links[i]]
        mode = job.get("mode") or TranslationMode.normal.value
        errors: dict[int, str] = {}

        # Three attempts a page, each cut off after two minutes without a full
        # reply (GEMINI_ATTEMPT_TIMEOUT_SECONDS in the script); then the page is
        # left "" and the job moves on to the next one.
        req = PopRequest(state="_dashboard", crop=str(document_id), model=DEFAULT_MODEL,
                         max_retries=FLEX_PAGE_ATTEMPTS if job.get("mode") == TranslationMode.flex.value
                         else PAGE_ATTEMPTS)
        api_key = os.environ[LLM_API_KEY_ENV]
        prompt = PROMPT_FILE.read_text(encoding="utf-8")
        if mode == TranslationMode.batch.value and todo:
            if not job.get("batch_name"):
                _submit_batch(job_id, todo, page_pdfs, req, api_key, prompt, doc_root)
                return  # Google has the pages now; the batch poller takes it from here
            # Second pass: the batch is done. Each result becomes the page's
            # translated.html, exactly where the normal call would put it.
            htmls, errors = _collect_batch(job["batch_name"], api_key)
            for i in todo:
                if i in htmls:
                    (page_pdfs[i].parent / "translated.html").write_text(htmls[i], encoding="utf-8")
                else:
                    errors.setdefault(i, "translation: no result in the Gemini batch")
            todo = [i for i in todo if i in htmls]
        tier = "flex" if mode == TranslationMode.flex.value else None
        stdout_buf = getattr(_tl_stdout, "buf", None)
        lock = threading.Lock()

        def one(i: int) -> None:
            ctl.set_job_id(str(job_id))
            _tl_stdout.buf = stdout_buf
            try:
                why = _translate_one_page(page_pdfs[i], docx_paths[i], doc_name, req, api_key, prompt, tier)
                if why is None:
                    ctl.check_cancel()
                    fid = zoho_layout.upload_translation(
                        wd, f"{safe_name}_{pop.page_name(i + 1)}.docx", docx_paths[i].read_bytes())
                    with lock:
                        links[i], file_ids[i] = zoho_layout.shareable_link(fid), fid
                        done = sum(1 for x in links if x)
                        _set_job(job_id, **{f"page_links.{i}": links[i], f"page_file_ids.{i}": fid},
                                 pages_done=done, progress_pct=int(done / total * 100))
                else:
                    errors[i] = why
            except ctl.JobCancelled:
                raise
            except Exception as e:  # noqa: BLE001 -- one page's failure is that page's, not the job's
                errors[i] = f"{type(e).__name__}: {e}"
            finally:
                _tl_stdout.buf = None

        pool = ThreadPoolExecutor(max_workers=DASHBOARD_TRANSLATE_CONCURRENCY, thread_name_prefix="translate-page")
        try:
            for future in [pool.submit(one, i) for i in todo]:
                future.result()  # re-raises JobCancelled from a page thread
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        ctl.check_cancel()

        if errors:
            first = min(errors)
            raise _PagesFailed(
                f"{len(errors)} of {total} page(s) failed (first: page {first + 1}, {errors[first][:200]}) "
                f"-- translate again to redo only those pages"
            )

        # Every page is in WorkDrive; those from an earlier run are fetched back.
        for i in range(total):
            if not docx_paths[i].exists():
                docx_paths[i].write_bytes(wd.download_file(file_ids[i]))
        final_docx = doc_root / f"{safe_name}_translated_pages_001_to_{total:03d}.docx"
        pop.merge_docx_files_in_order(input_docx_files=docx_paths, output_docx_path=final_docx)
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
        _trash_files(file_ids)
        _set_job(job_id, status=TranslationJobStatus.done.value, progress_pct=100,
                 page_links=[], page_file_ids=[])
    except ctl.JobCancelled:
        # The pages done so far stay, like a failed job's: translating again resumes.
        with get_session() as db:
            job = db[COLL_TRANSLATION_JOBS].find_one({"_id": job_id}, {"batch_name": 1})
        if (job or {}).get("batch_name"):
            _cancel_batch(job["batch_name"])
        _set_job(job_id, status=TranslationJobStatus.cancelled.value, error_message="Cancelled by user")
        _set_doc(document_id, translation_status=TranslationStatus.not_started.value)
    except (Exception, pop.ApiLimitReached) as e:  # noqa: BLE001 -- out of credits/daily limit: stop, say why
        _set_job(job_id, status=TranslationJobStatus.failed.value, error_message=str(e))
        _set_doc(document_id, translation_status=TranslationStatus.not_started.value)
    finally:
        _held_jobs.discard(job_id)
        ctl.cleanup(str(job_id))
        shutil.rmtree(doc_root, ignore_errors=True)
        tmp_pdf.unlink(missing_ok=True)


# -- Batch mode ----------------------------------------------------------------
# Submitting, collecting and cancelling a Gemini batch, and the poller that
# watches the batches in flight. Several servers share the staging database, so
# every one polls; the "finishing" claim is a conditional update, and only the
# server whose update lands runs the second pass.

_FINISHING = "finishing"
_BATCH_DONE = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
_BATCH_DEAD = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
_POLL_EVERY = 120  # seconds
_poll_lock = threading.Lock()
_last_poll = 0.0


def _gemini(api_key: str):
    from google import genai

    return genai.Client(api_key=api_key)


def _batch_state(batch) -> str | None:
    return getattr(batch.state, "value", batch.state) if batch.state is not None else None


def _submit_batch(job_id: ObjectId, todo: list[int], page_pdfs, req, api_key: str, prompt: str, workdir: Path) -> None:
    """One JSONL line per page still to do, keyed by its index; uploaded, then
    submitted as one batch. Images go inline, so the file can run to a few
    hundred MB for a long document -- far under the 2 GB limit."""
    from google.genai import types
    import run_pop_to_docx_updated_pagewise_docx as pop

    lines = workdir / "batch_input.jsonl"
    with lines.open("w", encoding="utf-8") as f:
        for i in todo:
            ctl.check_cancel()
            request = pop.gemini_batch_request(page_pdfs[i], prompt, not req.disable_google_search)
            f.write(json.dumps({"key": str(i), "request": request}) + "\n")
    client = _gemini(api_key)
    uploaded = client.files.upload(file=lines, config=types.UploadFileConfig(
        display_name=f"pop-translate-{job_id}", mime_type="jsonl"))
    ctl.check_cancel()
    try:
        batch = client.batches.create(model=req.model, src=uploaded.name,
                                      config=types.CreateBatchJobConfig(display_name=f"pop-translate-{job_id}"))
    except Exception as e:
        # Out of credits / spending cap: the same plain message the page calls give.
        if (why := pop._api_limit_message(e)) is not None:
            raise pop.ApiLimitReached(why) from e
        raise
    # Written before anything else can fail, so a cancel can always reach it.
    _set_job(job_id, batch_name=batch.name, batch_state=_batch_state(batch))
    print(f"[translate] job {job_id}: {len(todo)} page(s) submitted as {batch.name}", flush=True)
    ctl.check_cancel()


def _collect_batch(batch_name: str, api_key: str) -> tuple[dict[int, str], dict[int, str]]:
    """The finished batch's pages: {index: html} and {index: why it failed}."""
    import run_pop_to_docx_updated_pagewise_docx as pop

    client = _gemini(api_key)
    batch = client.batches.get(name=batch_name)
    if batch.dest is None or not batch.dest.file_name:
        raise RuntimeError(f"the Gemini batch {batch_name} has no results file")
    htmls: dict[int, str] = {}
    errors: dict[int, str] = {}
    for line in client.files.download(file=batch.dest.file_name).decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        i = int(row["key"])
        try:
            if row.get("error"):
                raise RuntimeError((row["error"] or {}).get("message") or row["error"])
            htmls[i] = pop.gemini_batch_html(row.get("response") or {})
        except Exception as e:  # noqa: BLE001 -- one page's failure is that page's
            errors[i] = f"translation: {e}"
    return htmls, errors


def _cancel_batch(batch_name: str) -> None:
    """Best effort: a batch that already finished cannot be cancelled, and that
    must not stop the job being marked cancelled here."""
    from pop_server import LLM_API_KEY_ENV

    try:
        # Held in a variable: a Client nobody references closes its connection
        # before the call it was created for is sent.
        client = _gemini(os.environ[LLM_API_KEY_ENV])
        client.batches.cancel(name=batch_name)
    except Exception as e:  # noqa: BLE001
        print(f"[translate] could not cancel {batch_name}: {type(e).__name__}: {e}", flush=True)


def start_batch_poller() -> None:
    """Called once at startup (pop_server's lifespan), so a batch submitted
    before a restart is still collected after it."""
    threading.Thread(target=_poll_loop, daemon=True, name="translate-batch-poll").start()


def _poll_loop() -> None:
    while True:
        try:
            with get_session() as db:
                _poll_batches(db)
        except Exception as e:  # noqa: BLE001
            print(f"[translate] batch poll failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(_POLL_EVERY)


def _poll_batches(db, *, min_gap: float = 0) -> None:
    """Ask Google about every batch in flight. Done -> claim and run the second
    pass; failed/expired/cancelled at Google -> the job fails (translate again
    to resubmit the pages still missing). Each poll also refreshes the job's
    heartbeat, so a server that does not know about batches does not expire it."""
    global _last_poll
    from pop_server import LLM_API_KEY_ENV

    api_key = os.environ.get(LLM_API_KEY_ENV)
    if not api_key:
        return
    with _poll_lock:
        if time.monotonic() - _last_poll < min_gap:
            return
        _last_poll = time.monotonic()
        running = TranslationJobStatus.running.value
        waiting = list(db[COLL_TRANSLATION_JOBS].find({
            "status": running, "batch_name": {"$type": "string"}, "batch_state": {"$ne": _FINISHING}}))
        if not waiting:
            return
        client = _gemini(api_key)
        for job in waiting:
            try:
                batch = client.batches.get(name=job["batch_name"])
            except Exception as e:  # noqa: BLE001 -- try again next poll
                print(f"[translate] could not check {job['batch_name']}: {type(e).__name__}: {e}", flush=True)
                continue
            state = _batch_state(batch)
            unclaimed = {"_id": job["_id"], "status": running, "batch_state": {"$ne": _FINISHING}}
            if state in _BATCH_DONE:
                if db[COLL_TRANSLATION_JOBS].update_one(unclaimed, {"$set": {
                    "batch_state": _FINISHING, "heartbeat_at": utcnow(), "updated_at": utcnow(),
                }}).modified_count:
                    events.translation_changed(job["_id"])
                    ctl.make_event(str(job["_id"]))
                    _held_jobs.add(job["_id"])
                    _start_heartbeat()
                    _executor.submit(_run_translation, job["_id"], job["document_id"])
            elif state in _BATCH_DEAD:
                why = getattr(batch.error, "message", None) if batch.error else None
                if db[COLL_TRANSLATION_JOBS].update_one(unclaimed, {"$set": {
                    "status": TranslationJobStatus.failed.value, "batch_state": state,
                    "error_message": f"the Gemini batch ended {state.removeprefix('JOB_STATE_').lower()}"
                                     f"{f' ({why})' if why else ''} -- translate again to resubmit",
                    "updated_at": utcnow(),
                }}).modified_count:
                    db[COLL_UNIQUE_DOCUMENTS].update_one({"_id": job["document_id"]}, {"$set": {
                        "translation_status": TranslationStatus.not_started.value, "updated_at": utcnow()}})
                    events.translation_changed(job["_id"])
                    events.document_changed(job["document_id"])
            else:
                fields = {"heartbeat_at": utcnow()}
                if state != job.get("batch_state"):
                    fields.update(batch_state=state, updated_at=utcnow())
                db[COLL_TRANSLATION_JOBS].update_one(unclaimed, {"$set": fields})
                if state != job.get("batch_state"):
                    events.translation_changed(job["_id"])


@router.post("/unique-documents/{document_id}/translation", status_code=201, dependencies=[Depends(require_admin)])
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


@router.delete("/unique-documents/{document_id}/translation", status_code=204, dependencies=[Depends(require_admin)])
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


@router.post("/unique-documents/{document_id}/review", status_code=201, dependencies=[Depends(require_admin)])
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


@router.delete("/unique-documents/{document_id}/review", status_code=204, dependencies=[Depends(require_admin)])
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


@router.post("/documents/{row_id}/translate", status_code=202, dependencies=[Depends(require_admin)])
def start_translation_for_row(row_id: str, body: dict | None = Body(default=None), db=Depends(get_db)):
    return start_translation(_document_id_for_row(db, row_id), body=body, db=db)


@router.delete("/documents/{row_id}/translation", status_code=204, dependencies=[Depends(require_admin)])
def delete_translation_for_row(row_id: str, db=Depends(get_db)):
    return delete_translation(_document_id_for_row(db, row_id), db=db)


@router.post("/documents/{row_id}/translation", status_code=201, dependencies=[Depends(require_admin)])
async def upload_translation_for_row(row_id: str, file: UploadFile = File(...),
                                     translated_by: str | None = Form(None), db=Depends(get_db)):
    return await upload_translation(_document_id_for_row(db, row_id), file,
                                    translated_by=translated_by, db=db)


@router.post("/documents/{row_id}/review", status_code=201, dependencies=[Depends(require_admin)])
async def upload_review_for_row(row_id: str, file: UploadFile = File(...),
                                reviewed_by: str | None = Form(None), db=Depends(get_db)):
    return await upload_review(_document_id_for_row(db, row_id), file, reviewed_by=reviewed_by, db=db)


@router.delete("/documents/{row_id}/review", status_code=204, dependencies=[Depends(require_admin)])
def delete_review_for_row(row_id: str, db=Depends(get_db)):
    return delete_review(_document_id_for_row(db, row_id), db=db)
