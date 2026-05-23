import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Thread-local stdout capture: routes print() output per job thread
# ---------------------------------------------------------------------------

_tl_stdout = threading.local()
_real_stdout = sys.stdout


class _ThreadLocalStdout:
    def write(self, s: str):
        buf = getattr(_tl_stdout, "buf", None)
        if buf is not None:
            buf.append(s)
        else:
            _real_stdout.write(s)

    def flush(self):
        if getattr(_tl_stdout, "buf", None) is None:
            _real_stdout.flush()

    def __getattr__(self, name):
        return getattr(_real_stdout, name)


sys.stdout = _ThreadLocalStdout()

# ---------------------------------------------------------------------------
# Pipeline imports
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent / "scripts"))

from run_pop_to_docx_updated_pagewise_docx import (  # noqa: E402
    convert_pages_to_individual_docx,
    inject_images_for_pages,
    load_prompt,
    log as pipeline_log,
    log_translation_summary,
    merge_docx_files_in_order,
    process_translation_for_page,
    safe_docx_name,
    set_log_file,
    split_pdf_to_page_folders,
    translate_pages,
)

import _job_ctl as ctl  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

POP_DATA = Path(__file__).resolve().parent / "pop-data"
POP_WORK = POP_DATA / "POP_Work"
POP_STORE = POP_WORK / "Data"   # canonical store: pop-data/POP_Work/Data/<state>/<crop>/
PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "page_to_pdf.txt"
_CHUNK_TMP = Path(tempfile.gettempdir()) / "pop_chunks"


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _resolve_safe(user_str: str) -> Path:
    p = Path(user_str)
    if p.is_absolute():
        raise ValueError(f"absolute paths not allowed: {user_str!r}")
    for part in p.parts:
        if part == "..":
            raise ValueError(f"path traversal not allowed: {user_str!r}")
        if "\x00" in part:
            raise ValueError(f"null bytes not allowed: {user_str!r}")
    resolved = (POP_DATA / p).resolve()
    if not resolved.is_relative_to(POP_DATA.resolve()):
        raise ValueError(f"path escapes sandbox: {user_str!r}")
    return resolved


# ---------------------------------------------------------------------------
# Meta helpers
# ---------------------------------------------------------------------------


def _meta_path(state: str, crop: str, stem: str) -> Path:
    return POP_STORE / state / crop / f"{stem}.meta.json"


def _read_meta(state: str, crop: str, stem: str) -> dict:
    p = _meta_path(state, crop, stem)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed": False, "audited": False, "audit_file": None}


def _write_meta(state: str, crop: str, stem: str, data: dict) -> None:
    _meta_path(state, crop, stem).write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PopRequest(BaseModel):
    state: str
    crop: str
    docs: Optional[List[str]] = None  # PDF filenames; None = all in Data/<state>/<crop>/
    concurrency: int = 1
    overwrite: bool = False
    skip_translation: bool = False
    skip_image_injection: bool = False
    model: str = "gemini-3.1-pro-preview"
    max_retries: int = 5
    retry_wait_seconds: int = 15
    disable_google_search: bool = False


class FolderBody(BaseModel):
    path: str


class StateBody(BaseModel):
    state: str


class CropBody(BaseModel):
    state: str
    crop: str


# ---------------------------------------------------------------------------
# Job store & runner
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_executor = ThreadPoolExecutor(max_workers=4)


def _run_job_sync(job_id: str, fn):
    _jobs[job_id]["status"] = "running"
    _jobs[job_id]["started_at"] = datetime.now(timezone.utc).isoformat()

    ctl.set_job_id(job_id)
    _tl_stdout.buf = _jobs[job_id]["_stdout_buf"]

    try:
        fn()
        _jobs[job_id]["status"] = "stopped" if ctl.is_cancelled(job_id) else "done"
        _jobs[job_id]["stderr"] = "stopped by user" if ctl.is_cancelled(job_id) else ""
    except ctl.JobCancelled:
        _jobs[job_id]["status"] = "stopped"
        _jobs[job_id]["stderr"] = "stopped by user"
    except Exception:
        _jobs[job_id]["status"] = "stopped" if ctl.is_cancelled(job_id) else "failed"
        _jobs[job_id]["stderr"] = "stopped by user" if ctl.is_cancelled(job_id) else traceback.format_exc()
    finally:
        _tl_stdout.buf = None
        ctl.cleanup(job_id)
        _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat()


async def _run_job_bg(job_id: str, fn):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _run_job_sync, job_id, fn)


def _submit(fn, background: BackgroundTasks, job_type: str = "pop") -> dict:
    job_id = str(uuid.uuid4())
    ctl.make_event(job_id)
    _jobs[job_id] = {
        "job_id": job_id,
        "job_type": job_type,
        "job_type_id": 5,
        "status": "pending",
        "stderr": "",
        "_stdout_buf": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "finished_at": None,
    }
    background.add_task(_run_job_bg, job_id, fn)
    return {"job_id": job_id, "job_type": job_type, "job_type_id": 5, "status": "pending"}


def _parse_log_pages(log_path: Path) -> tuple:
    """Return (total_pages, pages_done) parsed from a pipeline_runtime.log file."""
    if not log_path.exists():
        return None, 0
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
        total = None
        m = re.search(r'\bselected_pages=(\d+)', text)
        if m:
            total = int(m.group(1))
        matches = re.findall(r'Translation progress \| completed (\d+)/(\d+)', text)
        if matches:
            last = matches[-1]
            pages_done = int(last[0])
            if total is None:
                total = int(last[1])
        else:
            pages_done = 0
        return total, pages_done
    except Exception:
        return None, 0


def _build_pop_progress(job: dict) -> Optional[dict]:
    """Build the structured progress dict from per-doc status tracking and log files."""
    pop_docs = job.get("_pop_docs")
    if pop_docs is None:
        return None

    docs_info = []
    docs_done = 0
    current_doc = current_page = current_total_pages = None

    for doc in pop_docs:
        log_path = Path(doc["workdir"]) / "pipeline_runtime.log"
        status = doc["status"]
        if status == "done":
            total, _ = _parse_log_pages(log_path)
            docs_info.append({"name": doc["name"], "status": "done",
                               "pages_total": total, "pages_done": total})
            docs_done += 1
        elif status == "running":
            total, done = _parse_log_pages(log_path)
            docs_info.append({"name": doc["name"], "status": "running",
                               "pages_total": total, "pages_done": done})
            current_doc = doc["name"]
            current_page = done
            current_total_pages = total
        else:
            docs_info.append({"name": doc["name"], "status": "pending",
                               "pages_total": None, "pages_done": 0})

    return {
        "total_docs": len(pop_docs),
        "docs_done": docs_done,
        "current_doc": current_doc,
        "current_page": current_page,
        "current_total_pages": current_total_pages,
        "docs": docs_info,
    }


def _job_view(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "job_type": job["job_type"],
        "job_type_id": job.get("job_type_id", 5),
        "status": job["status"],
        "stdout": "".join(job["_stdout_buf"]),
        "stderr": job["stderr"],
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "progress": _build_pop_progress(job) if job.get("job_type") == "pop" else None,
    }


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def _run_one_doc(
    source_pdf_path: Path,
    doc_name: str,
    req: PopRequest,
    api_key: str,
    prompt: str,
):
    workdir_root = POP_WORK / "Workdir" / req.state / req.crop / doc_name
    workdir_root.mkdir(parents=True, exist_ok=True)

    safe_name = safe_docx_name(doc_name)
    set_log_file(workdir_root / "pipeline_runtime.log")

    # 1. Split PDF
    page_pdf_files, total_pages = split_pdf_to_page_folders(
        source_pdf_path=source_pdf_path,
        output_root=workdir_root,
        start_page=1,
        end_page=None,
        overwrite_existing=req.overwrite,
    )

    ctl.check_cancel()

    # 2. Translate
    if not req.skip_translation:
        if req.concurrency <= 1:
            # Sequential loop with per-page cancellation checks
            import time as _time
            _stage_start = _time.perf_counter()
            translation_summary = []
            total_pages_t = len(page_pdf_files)
            pipeline_log(
                f"Translation stage started | pages={total_pages_t} | "
                f"concurrency=1 | model={req.model} | "
                f"google_search={not req.disable_google_search}"
            )
            for idx, pdf_path in enumerate(page_pdf_files, start=1):
                ctl.check_cancel()
                pipeline_log(f"Translation progress | starting {idx}/{total_pages_t} | page={pdf_path.parent.name}")
                result = process_translation_for_page(
                    page_pdf_path=pdf_path,
                    prompt=prompt,
                    model=req.model,
                    api_key=api_key,
                    overwrite_existing=req.overwrite,
                    max_retries=req.max_retries,
                    retry_wait_seconds=req.retry_wait_seconds,
                    enable_google_search=not req.disable_google_search,
                )
                translation_summary.append(result)
                pipeline_log(f"Translation progress | completed {idx}/{total_pages_t} | page={pdf_path.parent.name}")
            log_translation_summary(translation_summary, _time.perf_counter() - _stage_start)
        else:
            translation_summary = translate_pages(
                page_pdf_files=page_pdf_files,
                prompt=prompt,
                model=req.model,
                api_key=api_key,
                overwrite_existing=req.overwrite,
                max_retries=req.max_retries,
                retry_wait_seconds=req.retry_wait_seconds,
                enable_google_search=not req.disable_google_search,
                concurrency=req.concurrency,
            )
        (workdir_root / "translation_summary.json").write_text(
            json.dumps(translation_summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        failed = [x for x in translation_summary if x["status"] == "failed"]
        if failed:
            raise RuntimeError(f"Translation failed for {len(failed)} page(s). See translation_summary.json")

    ctl.check_cancel()

    # 3. Image injection
    if not req.skip_image_injection:
        image_summary = inject_images_for_pages(
            page_pdf_files=page_pdf_files,
            overwrite_existing=req.overwrite,
        )
        (workdir_root / "image_injection_summary.json").write_text(
            json.dumps(image_summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        failed = [x for x in image_summary if x["status"] == "failed"]
        if failed:
            raise RuntimeError(f"Image injection failed for {len(failed)} page(s). See image_injection_summary.json")

    ctl.check_cancel()

    # 4. Page-wise DOCX conversion
    final_output_dir = workdir_root / "final_output"
    final_output_dir.mkdir(parents=True, exist_ok=True)

    page_docx_files, page_docx_summary = convert_pages_to_individual_docx(
        page_pdf_files=page_pdf_files,
        final_output_dir=final_output_dir,
        safe_name=safe_name,
        doc_title=doc_name,
        overwrite_existing=req.overwrite,
    )
    (workdir_root / "page_docx_summary.json").write_text(
        json.dumps(page_docx_summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    failed = [x for x in page_docx_summary if x.get("status") == "failed"]
    if failed:
        raise RuntimeError(f"Page-wise DOCX conversion failed for {len(failed)} page(s). See page_docx_summary.json")

    ctl.check_cancel()

    # 5. Merge into final DOCX
    final_docx_path = final_output_dir / f"{safe_name}_translated_pages_001_to_{total_pages:03d}.docx"
    merge_docx_files_in_order(input_docx_files=page_docx_files, output_docx_path=final_docx_path)

    # Mark processed in meta
    meta = _read_meta(req.state, req.crop, doc_name)
    meta["processed"] = True
    meta["processed_at"] = datetime.now(timezone.utc).isoformat()
    _write_meta(req.state, req.crop, doc_name, meta)


def _run_pop_sync(req: PopRequest):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key and not req.skip_translation:
        raise RuntimeError("GEMINI_API_KEY is not set")

    crop_data_dir = POP_STORE / req.state / req.crop
    if not crop_data_dir.exists():
        raise FileNotFoundError(f"Data directory not found: {crop_data_dir}")

    if req.docs:
        pdf_files = [crop_data_dir / filename for filename in req.docs]
        missing = [str(f) for f in pdf_files if not f.exists()]
        if missing:
            raise FileNotFoundError(f"PDFs not found: {', '.join(missing)}")
    else:
        pdf_files = sorted(f for f in crop_data_dir.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")
        if not pdf_files:
            raise FileNotFoundError(f"No PDFs found in {crop_data_dir}")

    prompt = load_prompt(PROMPT_FILE)

    job_id = ctl.current_job_id()
    if job_id and job_id in _jobs:
        _jobs[job_id]["_pop_docs"] = [
            {
                "name": f.name,
                "workdir": str(POP_WORK / "Workdir" / req.state / req.crop / f.stem),
                "status": "pending",
            }
            for f in pdf_files
        ]

    for i, pdf_path in enumerate(pdf_files):
        ctl.check_cancel()
        if job_id and job_id in _jobs:
            _jobs[job_id]["_pop_docs"][i]["status"] = "running"
        doc_name = pdf_path.stem
        print(f"[batch] processing: {pdf_path.name}")
        _run_one_doc(
            source_pdf_path=pdf_path,
            doc_name=doc_name,
            req=req,
            api_key=api_key,
            prompt=prompt,
        )
        if job_id and job_id in _jobs:
            _jobs[job_id]["_pop_docs"][i]["status"] = "done"


# ---------------------------------------------------------------------------
# Tree helper
# ---------------------------------------------------------------------------


def _build_tree(root: Path) -> dict:
    def _node(p: Path) -> dict:
        rel = str(p.relative_to(POP_DATA))
        if p.is_file():
            return {"name": p.name, "path": rel, "type": "file", "size": p.stat().st_size}
        children = [_node(c) for c in sorted(p.iterdir())]
        return {"name": p.name, "path": rel, "type": "directory", "children": children}

    if not root.exists():
        rel = str(root.relative_to(POP_DATA)) if root.is_relative_to(POP_DATA) else root.name
        return {"name": root.name, "path": rel, "type": "directory", "children": []}
    return _node(root)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    POP_STORE.mkdir(parents=True, exist_ok=True)
    (POP_WORK / "Workdir").mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="POP Translation Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Health
@app.get("/")
def health():
    return {"status": "ok", "service": "pop-server"}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@app.post("/run/pop")
async def run_pop(req: PopRequest, background: BackgroundTasks):
    def fn():
        _run_pop_sync(req)

    return _submit(fn, background, job_type="pop")


# ---------------------------------------------------------------------------
# Data browsing
# ---------------------------------------------------------------------------


@app.get("/pop/states")
def list_states():
    if not POP_STORE.exists():
        return []
    return sorted(d.name for d in POP_STORE.iterdir() if d.is_dir())


@app.get("/pop/crops")
def list_crops(state: str = Query(...)):
    state_dir = POP_STORE / state
    if not state_dir.exists():
        raise HTTPException(404, f"State not found: {state!r}")
    return sorted(d.name for d in state_dir.iterdir() if d.is_dir())


@app.get("/pop/docs")
def list_docs(state: str = Query(...), crop: str = Query(...)):
    crop_dir = POP_STORE / state / crop
    if not crop_dir.exists():
        raise HTTPException(404, f"Crop not found: {state!r}/{crop!r}")
    return sorted(f.name for f in crop_dir.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")


@app.get("/pop/data/tree")
def data_tree():
    return _build_tree(POP_STORE)


@app.get("/pop/output/tree")
def output_tree():
    return _build_tree(POP_WORK / "Workdir")


@app.get("/pop/state-table")
def state_table():
    workdir = POP_WORK / "Workdir"
    rows: dict[tuple, dict] = {}

    if POP_STORE.exists():
        for state_dir in sorted(POP_STORE.iterdir()):
            if not state_dir.is_dir():
                continue
            crop_dirs = sorted(d for d in state_dir.iterdir() if d.is_dir())
            if not crop_dirs:
                rows[(state_dir.name, None, None)] = {
                    "state": state_dir.name,
                    "crop": None,
                    "doc_name": None,
                    "doc_path": None,
                    "is_empty": True,
                    "processed": False,
                    "downloaded": False,
                    "output_path": None,
                    "output_dir": None,
                    "audit_file": None,
                    "audited": False,
                    "has_docx": False,
                    "page_count": 0,
                    "status": "empty",
                }
                continue
            for crop_dir in crop_dirs:
                pdfs = sorted(f for f in crop_dir.iterdir() if f.is_file() and f.suffix.lower() == ".pdf")
                if pdfs:
                    for pdf in pdfs:
                        meta = _read_meta(state_dir.name, crop_dir.name, pdf.stem)
                        rows[(state_dir.name, crop_dir.name, pdf.stem)] = {
                            "state": state_dir.name,
                            "crop": crop_dir.name,
                            "doc_name": pdf.name,
                            "doc_path": str(pdf.relative_to(POP_DATA)),
                            "is_empty": False,
                            "processed": meta.get("processed", False),
                            "downloaded": meta.get("downloaded", False),
                            "output_path": None,
                            "output_dir": None,
                            "audit_file": meta.get("audit_file"),
                            "audited": meta.get("audited", False),
                            "has_docx": False,
                            "page_count": 0,
                            "status": "not_started",
                        }
                else:
                    rows[(state_dir.name, crop_dir.name, "")] = {
                        "state": state_dir.name,
                        "crop": crop_dir.name,
                        "doc_name": None,
                        "doc_path": None,
                        "is_empty": True,
                        "processed": False,
                        "downloaded": False,
                        "output_path": None,
                        "output_dir": None,
                        "audit_file": None,
                        "audited": False,
                        "has_docx": False,
                        "page_count": 0,
                        "status": "empty",
                    }

    if workdir.exists():
        for state_dir in sorted(workdir.iterdir()):
            if not state_dir.is_dir():
                continue
            for crop_dir in sorted(state_dir.iterdir()):
                if not crop_dir.is_dir():
                    continue
                for doc_dir in sorted(crop_dir.iterdir()):
                    if not doc_dir.is_dir():
                        continue
                    final_output = doc_dir / "final_output"
                    docx_files = (
                        sorted(f for f in final_output.glob("*.docx") if f.is_file())
                        if final_output.exists()
                        else []
                    )
                    has_docx = bool(docx_files)
                    page_count = len([d for d in doc_dir.iterdir() if d.is_dir() and d.name.startswith("page_")])
                    status = "done" if has_docx else ("in_progress" if page_count > 0 else "not_started")
                    output_path = str(docx_files[-1].relative_to(POP_DATA)) if has_docx else None
                    output_dir_rel = str(doc_dir.relative_to(POP_DATA))

                    key = (state_dir.name, crop_dir.name, doc_dir.name)
                    if key in rows:
                        rows[key].update({
                            "has_docx": has_docx,
                            "page_count": page_count,
                            "status": status,
                            # processed and downloaded are authoritative from meta
                            "output_path": output_path,
                            "output_dir": output_dir_rel,
                        })
                    else:
                        pdf_path = POP_STORE / state_dir.name / crop_dir.name / (doc_dir.name + ".pdf")
                        meta = _read_meta(state_dir.name, crop_dir.name, doc_dir.name)
                        rows[key] = {
                            "state": state_dir.name,
                            "crop": crop_dir.name,
                            "doc_name": (doc_dir.name + ".pdf") if pdf_path.exists() else doc_dir.name,
                            "doc_path": str(pdf_path.relative_to(POP_DATA)) if pdf_path.exists() else None,
                            "is_empty": False,
                            "processed": meta.get("processed", False),
                            "downloaded": meta.get("downloaded", False),
                            "output_path": output_path,
                            "output_dir": output_dir_rel,
                            "audit_file": meta.get("audit_file"),
                            "audited": meta.get("audited", False),
                            "has_docx": has_docx,
                            "page_count": page_count,
                            "status": status,
                        }

    sorted_rows = sorted(
        rows.values(),
        key=lambda r: (r["state"] or "", r["crop"] or "", r["doc_name"] or ""),
    )
    return {"rows": sorted_rows}


# ---------------------------------------------------------------------------
# Semantic state / crop / doc management
# ---------------------------------------------------------------------------


@app.post("/pop/state")
def create_state(body: StateBody):
    state = body.state.strip()
    if not state:
        raise HTTPException(400, "state is required")
    (POP_STORE / state).mkdir(parents=True, exist_ok=True)
    return {"state": state}


@app.post("/pop/crop")
def create_crop(body: CropBody):
    state, crop = body.state.strip(), body.crop.strip()
    if not state or not crop:
        raise HTTPException(400, "state and crop are required")
    (POP_STORE / state / crop).mkdir(parents=True, exist_ok=True)
    return {"state": state, "crop": crop}


@app.delete("/pop/state")
def delete_state(state: str = Query(...)):
    """Delete a state folder. Returns 409 if any crop folders still exist inside."""
    state_dir = POP_STORE / state
    if not state_dir.exists():
        raise HTTPException(404, f"State not found: {state!r}")
    crops = [d for d in state_dir.iterdir() if d.is_dir()]
    if crops:
        raise HTTPException(409, f"State has {len(crops)} crop(s). Delete all crops first.")
    shutil.rmtree(state_dir)
    return {"deleted": state}


@app.delete("/pop/crop")
def delete_crop(state: str = Query(...), crop: str = Query(...)):
    """Delete a crop folder. Returns 409 if PDFs still exist — delete docs first."""
    crop_dir = POP_STORE / state / crop
    if not crop_dir.exists():
        raise HTTPException(404, f"Crop not found: {state!r}/{crop!r}")
    pdfs = [f for f in crop_dir.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"]
    if pdfs:
        raise HTTPException(409, f"Crop has {len(pdfs)} PDF(s). Delete all documents first.")
    shutil.rmtree(crop_dir)
    return {"deleted": f"{state}/{crop}"}


@app.post("/pop/upload-doc")
async def upload_doc(file: UploadFile = File(...), state: str = Form(...), crop: str = Form(...)):
    """Upload a PDF into POP_Work/Data/<state>/<crop>/."""
    state, crop = state.strip(), crop.strip()
    dest_dir = POP_STORE / state / crop
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "upload.pdf"
    dest_path = dest_dir / filename
    content = await file.read()
    dest_path.write_bytes(content)
    stem = Path(filename).stem
    _write_meta(state, crop, stem, {
        "doc_name": filename,
        "state": state,
        "crop": crop,
        "processed": False,
        "audited": False,
        "downloaded": False,
        "audit_file": None,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "processed_at": None,
        "audited_at": None,
        "downloaded_at": None,
    })
    return {"path": str(dest_path.relative_to(POP_DATA)), "size": len(content)}


@app.delete("/pop/doc")
def delete_doc(state: str = Query(...), crop: str = Query(...), doc_name: str = Query(...)):
    """Delete a PDF and its matching audit file and workdir output folder."""
    pdf_path = POP_STORE / state / crop / doc_name
    if not pdf_path.exists():
        raise HTTPException(404, f"Document not found: {doc_name!r}")
    stem = Path(doc_name).stem
    # Remove meta.json
    _meta_path(state, crop, stem).unlink(missing_ok=True)
    # Remove workdir output
    workdir_doc = POP_WORK / "Workdir" / state / crop / stem
    if workdir_doc.exists():
        shutil.rmtree(workdir_doc, ignore_errors=True)
    pdf_path.unlink()
    return {"deleted": doc_name, "state": state, "crop": crop}


# ---------------------------------------------------------------------------
# File operations (legacy — kept for compatibility)
# ---------------------------------------------------------------------------


@app.post("/pop/upload")
async def upload_file(dest: str = Query(...), file: UploadFile = File(...)):
    try:
        dest_path = _resolve_safe(dest)
    except ValueError as e:
        raise HTTPException(400, str(e))

    dest_path.mkdir(parents=True, exist_ok=True)
    filename = file.filename or "upload"
    out_path = dest_path / filename
    content = await file.read()
    out_path.write_bytes(content)
    return {"path": str(out_path.relative_to(POP_DATA)), "size": len(content)}


@app.post("/pop/upload-chunk")
async def upload_chunk(
    request: Request,
    upload_id: str = Query(...),
    chunk_index: int = Query(...),
    total_chunks: int = Query(...),
    filename: str = Query(...),
    dest: str = Query(...),
):
    body = await request.body()

    chunk_dir = _CHUNK_TMP / upload_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / f"chunk_{chunk_index:06d}").write_bytes(body)

    existing = list(chunk_dir.glob("chunk_*"))
    if len(existing) < total_chunks:
        return {"status": "ok", "chunk": chunk_index}

    try:
        dest_path = _resolve_safe(dest)
    except ValueError as e:
        raise HTTPException(400, str(e))

    dest_path.mkdir(parents=True, exist_ok=True)
    final_path = dest_path / filename

    with final_path.open("wb") as out:
        for i in range(total_chunks):
            out.write((chunk_dir / f"chunk_{i:06d}").read_bytes())

    shutil.rmtree(chunk_dir, ignore_errors=True)
    return {"status": "complete", "path": str(final_path.relative_to(POP_DATA))}


@app.post("/pop/upload-audited")
async def upload_audited(
    file: UploadFile = File(...),
    state: str = Form(...),
    crop: str = Form(...),
    doc_name: str = Form(...),
):
    stem = Path(doc_name).stem
    final_output_dir = POP_WORK / "Workdir" / state / crop / stem / "final_output"
    if not final_output_dir.exists():
        raise HTTPException(404, f"No processed output found for {doc_name!r} — run translation first.")
    original_filename = file.filename or (stem + "_audited.docx")
    filename = original_filename if original_filename.startswith("audit_") else ("audit_" + original_filename)

    # Delete previous audit file to avoid accumulation
    meta = _read_meta(state, crop, stem)
    old_audit = meta.get("audit_file")
    if old_audit:
        old_path = POP_DATA / old_audit
        old_path.unlink(missing_ok=True)

    dest_path = final_output_dir / filename
    content = await file.read()
    dest_path.write_bytes(content)
    meta["audited"] = True
    meta["audited_at"] = datetime.now(timezone.utc).isoformat()
    meta["audit_file"] = str(dest_path.relative_to(POP_DATA))
    _write_meta(state, crop, stem, meta)
    return {"path": str(dest_path.relative_to(POP_DATA)), "size": len(content)}


@app.delete("/pop/audit")
def delete_audit(state: str = Query(...), crop: str = Query(...), doc_name: str = Query(...)):
    """Delete the audited file and clear audit metadata."""
    stem = Path(doc_name).stem
    meta = _read_meta(state, crop, stem)
    audit_file = meta.get("audit_file")
    if not audit_file:
        raise HTTPException(404, "No audit file found for this document")
    audit_path = POP_DATA / audit_file
    audit_path.unlink(missing_ok=True)
    meta["audited"] = False
    meta["audit_file"] = None
    meta["audited_at"] = None
    _write_meta(state, crop, stem, meta)
    return {"deleted": audit_file, "state": state, "crop": crop, "doc_name": doc_name}


@app.post("/pop/folders")
def create_folder(body: FolderBody):
    try:
        path = _resolve_safe(body.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    path.mkdir(parents=True, exist_ok=True)
    return {"path": body.path}


@app.delete("/pop/files/{path:path}")
def delete_file(path: str):
    try:
        p = _resolve_safe(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not p.exists():
        raise HTTPException(404, "Not found")
    if not p.is_file():
        raise HTTPException(400, "Path is not a file")
    p.unlink()
    return {"deleted": path}


@app.delete("/pop/folders/{path:path}")
def delete_folder(path: str):
    try:
        p = _resolve_safe(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not p.exists():
        raise HTTPException(404, "Not found")
    if not p.is_dir():
        raise HTTPException(400, "Path is not a directory")
    shutil.rmtree(p)
    return {"deleted": path}


@app.get("/pop/download/{path:path}")
def download_file(path: str):
    try:
        file_path = _resolve_safe(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(str(file_path), filename=file_path.name)


@app.get("/pop/output")
def download_output(state: str = Query(...), crop: str = Query(...), doc_name: str = Query(...)):
    stem = Path(doc_name).stem
    final_output_dir = POP_WORK / "Workdir" / state / crop / stem / "final_output"
    if not final_output_dir.exists():
        raise HTTPException(404, "Output directory not found")
    docx_files = sorted(f for f in final_output_dir.glob("*.docx") if f.is_file())
    if not docx_files:
        raise HTTPException(404, "No DOCX found for this document")
    docx_path = docx_files[-1]
    meta = _read_meta(state, crop, stem)
    meta["downloaded"] = True
    meta["downloaded_at"] = datetime.now(timezone.utc).isoformat()
    _write_meta(state, crop, stem, meta)
    return FileResponse(
        str(docx_path),
        filename=docx_path.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------


@app.get("/pop/jobs")
def list_jobs():
    return [_job_view(j) for j in _jobs.values()]


@app.get("/pop/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_view(job)


@app.post("/pop/jobs/{job_id}/stop")
def stop_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    ctl.cancel(job_id)
    _jobs[job_id]["status"] = "stopped"
    return {"job_id": job_id, "status": "stopped"}


@app.delete("/pop/jobs/{job_id}")
def delete_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    ctl.cleanup(job_id)
    del _jobs[job_id]
    return {"deleted": job_id}
