import asyncio
import json
import os

from dotenv import load_dotenv
load_dotenv()
import re
import shutil
import sys
import tempfile
import threading
import time as _time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests as _requests
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
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

import run_pop_to_docx_updated_pagewise_docx as _pop_script  # noqa: E402
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
# Paths — POP_WORK / Workdir is local scratch; all persistent data lives in Zoho
# ---------------------------------------------------------------------------

POP_DATA = Path(__file__).resolve().parent / "pop-data"
POP_WORK = POP_DATA / "POP_Work"
PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "page_to_pdf.txt"
_CHUNK_TMP = Path(tempfile.gettempdir()) / "pop_chunks"

# ---------------------------------------------------------------------------
# Zoho WorkDrive singleton
# ---------------------------------------------------------------------------

_zoho_instance = None
_zoho_lock = threading.Lock()


def _get_zoho():
    global _zoho_instance
    if _zoho_instance is None:
        with _zoho_lock:
            if _zoho_instance is None:
                from helpers.zoho_workdrive import ZohoWorkDrive
                _zoho_instance = ZohoWorkDrive()
    return _zoho_instance


# ---------------------------------------------------------------------------
# Master JSON cache — single source of truth for all state-table data
# ---------------------------------------------------------------------------

_master_ready = threading.Event()
_master_lock = threading.Lock()
_master_data: dict = {}


def _upload_master_json():
    # Must be called under _master_lock.
    # NEVER raises — upload failures are logged but must not roll back the
    # in-memory _master_data that the caller already updated.  The next
    # successful upload will persist the correct state.
    try:
        zwd = _get_zoho()
        # Snapshot existing IDs before uploading so we can prune stale duplicates
        # after.  Zoho sometimes creates a new copy instead of replacing in-place
        # even with override-name-exist=true; we delete only IDs that differ from
        # the new file's ID so we never delete the file we just wrote.
        old_ids = {i["id"] for i in zwd.list_folder(zwd.root_folder_id) if i["name"] == "master.json"}
        payload = json.dumps({
            "built_at": datetime.now(timezone.utc).isoformat(),
            "data": _master_data,
        }).encode()
        new_id = zwd.upload_file("master.json", payload, zwd.root_folder_id)
        for old_id in old_ids:
            if old_id != new_id:
                try:
                    zwd.delete(old_id)
                except Exception as e:
                    print(f"[MASTER] cleanup of old master.json copy failed: {e}")
    except Exception as e:
        print(f"[MASTER] upload_master_json failed (in-memory state is still correct): {e}")


def _update_master_crop(state: str, crop: str, stem: str, **updates):
    with _master_lock:
        entry = _master_data.setdefault(state, {}).setdefault(crop, {}).setdefault(stem, {})
        entry.update(updates)
        _upload_master_json()


def _remove_master_crop(state: str, crop: str, stem: str):
    with _master_lock:
        if state in _master_data and crop in _master_data[state]:
            _master_data[state][crop].pop(stem, None)
            if not _master_data[state][crop]:
                del _master_data[state][crop]
            if not _master_data[state]:
                del _master_data[state]
            _upload_master_json()


def _master_sync_deleted_file(path: str):
    """Update master.json when a file is deleted via the generic DELETE /files/ endpoint."""
    if not _master_ready.is_set():
        return
    parts = path.strip("/").split("/")
    # Data/<state>/<crop>/<stem>.pdf
    if len(parts) == 4 and parts[0] == "Data" and parts[3].lower().endswith(".pdf"):
        _remove_master_crop(parts[1], parts[2], Path(parts[3]).stem)
        return
    # Workdir/<state>/<crop>/<stem>/final_output/<file>
    # Audit files can be any extension (.csv, .docx, etc.) — only output files are always .docx.
    if len(parts) == 6 and parts[0] == "Workdir" and parts[4] == "final_output":
        state, crop, stem, fname = parts[1], parts[2], parts[3], parts[5]
        if fname.startswith("audit_"):
            _update_master_crop(state, crop, stem, audited=False, audit_file=None)
        elif fname.lower().endswith(".docx"):
            _update_master_crop(state, crop, stem, processed=False, output_file=None)


def _master_sync_deleted_folder(path: str):
    """Update master.json when a folder is deleted via the generic DELETE /folders/ endpoint."""
    if not _master_ready.is_set():
        return
    parts = path.strip("/").split("/")
    # Data/<state>/<crop>
    if len(parts) == 3 and parts[0] == "Data":
        state, crop = parts[1], parts[2]
        with _master_lock:
            if state in _master_data and crop in _master_data[state]:
                del _master_data[state][crop]
                if not _master_data[state]:
                    del _master_data[state]
                _upload_master_json()
        return
    # Data/<state>
    if len(parts) == 2 and parts[0] == "Data":
        state = parts[1]
        with _master_lock:
            if state in _master_data:
                del _master_data[state]
                _upload_master_json()
        return
    # Workdir/<state>/<crop>/<stem>  — entire doc workdir removed
    if len(parts) == 4 and parts[0] == "Workdir":
        state, crop, stem = parts[1], parts[2], parts[3]
        with _master_lock:
            # Only update if the stem is already tracked — don't create phantom entries
            # for orphaned Workdir folders that have no corresponding Data PDF.
            if state in _master_data and crop in _master_data[state] and stem in _master_data[state][crop]:
                _master_data[state][crop][stem].update(
                    processed=False, output_file=None, audited=False, audit_file=None
                )
                _upload_master_json()
        return
    # Workdir/<state>/<crop>  — all doc workdirs under a crop removed
    if len(parts) == 3 and parts[0] == "Workdir":
        state, crop = parts[1], parts[2]
        with _master_lock:
            crop_entries = _master_data.get(state, {}).get(crop)
            if not crop_entries:
                return
            for entry in crop_entries.values():
                entry.update(processed=False, output_file=None, audited=False, audit_file=None)
            _upload_master_json()


def _rebuild_master():
    global _master_data
    try:
        zwd = _get_zoho()
        new_data: dict = {}

        data_result = zwd.resolve_path("Data")
        if data_result:
            for state_item in zwd.list_folder(data_result[0]):
                if state_item["type"] != "folder":
                    continue
                state = state_item["name"]
                for crop_item in zwd.list_folder(state_item["id"]):
                    if crop_item["type"] != "folder":
                        continue
                    crop = crop_item["name"]
                    # Always register the crop (even if empty) so the frontend can show it
                    new_data.setdefault(state, {}).setdefault(crop, {})
                    for doc_item in zwd.list_folder(crop_item["id"]):
                        if not doc_item["name"].lower().endswith(".pdf"):
                            continue
                        stem = Path(doc_item["name"]).stem
                        new_data[state][crop].setdefault(stem, {
                            "output_file": None,
                            "audit_file": None,
                            "downloaded": False,
                            "audited": False,
                            "processed": False,
                        })

        workdir_result = zwd.resolve_path("Workdir")
        if workdir_result:
            for state_item in zwd.list_folder(workdir_result[0]):
                if state_item["type"] != "folder":
                    continue
                state = state_item["name"]
                for crop_item in zwd.list_folder(state_item["id"]):
                    if crop_item["type"] != "folder":
                        continue
                    crop = crop_item["name"]
                    for doc_item in zwd.list_folder(crop_item["id"]):
                        if doc_item["type"] != "folder":
                            continue
                        stem = doc_item["name"]
                        final_out = zwd.find_child(doc_item["id"], "final_output")
                        if not final_out or final_out["type"] != "folder":
                            continue
                        docx_items = [
                            i for i in zwd.list_folder(final_out["id"])
                            if i["name"].lower().endswith(".docx")
                        ]
                        audit_items = sorted(
                            [i for i in docx_items if i["name"].startswith("audit_")],
                            key=lambda x: x["name"],
                        )
                        non_audit_items = sorted(
                            [i for i in docx_items if not i["name"].startswith("audit_")],
                            key=lambda x: x["name"],
                        )
                        entry = new_data.setdefault(state, {}).setdefault(crop, {}).setdefault(stem, {
                            "output_file": None,
                            "audit_file": None,
                            "downloaded": False,
                            "audited": False,
                            "processed": False,
                        })
                        workdir_path = f"Workdir/{state}/{crop}/{stem}/final_output"
                        if non_audit_items:
                            entry["output_file"] = f"{workdir_path}/{non_audit_items[-1]['name']}"
                            entry["processed"] = True
                        if audit_items:
                            entry["audit_file"] = f"{workdir_path}/{audit_items[-1]['name']}"
                            entry["audited"] = True

        with _master_lock:
            _master_data = new_data
            _upload_master_json()
    except Exception as e:
        print(f"[MASTER] rebuild failed: {e}")
    finally:
        _master_ready.set()


def _load_or_build_master():
    global _master_data
    try:
        zwd = _get_zoho()
        # Take the LAST match — Zoho lists in insertion order (oldest first), so the
        # last entry is the most recently uploaded copy (mirrors the upload_file fallback).
        matches = [i for i in zwd.list_folder(zwd.root_folder_id) if i["name"] == "master.json"]
        if matches:
            found = matches[-1]
            # Prune any stale duplicates that accumulated before this fix
            for stale in matches[:-1]:
                try:
                    zwd.delete(stale["id"])
                except Exception:
                    pass
            try:
                payload = json.loads(zwd.download_file(found["id"]))
                with _master_lock:
                    _master_data = payload.get("data", {})
                _master_ready.set()
                return
            except Exception as e:
                print(f"[MASTER] Failed to parse existing master.json, rebuilding: {e}")
        _rebuild_master()
    except Exception as e:
        print(f"[MASTER] load_or_build failed: {e}")
        _master_ready.set()


def _master_to_rows() -> list:
    with _master_lock:
        snapshot = {
            state: {crop: dict(entries) for crop, entries in crops.items()}
            for state, crops in _master_data.items()
        }

    rows = []
    for state in sorted(snapshot):
        for crop in sorted(snapshot[state]):
            if not snapshot[state][crop]:
                rows.append({
                    "state": state,
                    "crop": crop,
                    "doc_name": None,
                    "doc_path": f"Data/{state}/{crop}",
                    "is_empty": True,
                    "processed": False,
                    "downloaded": False,
                    "output_path": None,
                    "output_dir": None,
                    "audit_file": None,
                    "audited": False,
                    "has_docx": False,
                    "page_count": 0,
                    "status": "not_started",
                })
                continue
            for stem in sorted(snapshot[state][crop]):
                entry = snapshot[state][crop][stem]
                doc_name = stem + ".pdf"
                output_file = entry.get("output_file")
                audit_file = entry.get("audit_file")
                audited = entry.get("audited", False)
                processed = entry.get("processed", False)
                downloaded = entry.get("downloaded", False)

                output_dir = None
                if output_file:
                    parts = output_file.split("/")
                    if len(parts) >= 4:
                        output_dir = "/".join(parts[:4])

                if audited:
                    status = "audited"
                elif processed:
                    status = "done"
                else:
                    status = "not_started"

                rows.append({
                    "state": state,
                    "crop": crop,
                    "doc_name": doc_name,
                    "doc_path": f"Data/{state}/{crop}/{doc_name}",
                    "is_empty": False,
                    "processed": processed,
                    "downloaded": downloaded,
                    "output_path": output_file,
                    "output_dir": output_dir,
                    "audit_file": audit_file,
                    "audited": audited,
                    "has_docx": processed,
                    "page_count": 0,
                    "status": status,
                })
    return rows


# ---------------------------------------------------------------------------
# Path validation for Zoho paths (no traversal, no absolute)
# ---------------------------------------------------------------------------


def _validate_zoho_path(path: str) -> str:
    if not path:
        raise ValueError("empty path")
    if path.startswith("/"):
        raise ValueError(f"absolute paths not allowed: {path!r}")
    for part in path.split("/"):
        if part == "..":
            raise ValueError(f"path traversal not allowed: {path!r}")
        if "\x00" in part:
            raise ValueError(f"null bytes not allowed: {path!r}")
    return path


# ---------------------------------------------------------------------------
# Streaming PDF from Zoho to temp file (for pipeline)
# ---------------------------------------------------------------------------


@contextmanager
def _stream_zoho_pdf(zoho_path: str):
    """Stream a Zoho file to a local temp PDF and yield its Path. Auto-deletes on exit."""
    zwd = _get_zoho()
    result = zwd.resolve_path(zoho_path)
    if result is None:
        raise FileNotFoundError(f"{zoho_path!r} not found in Zoho WorkDrive")
    file_id = result[0]
    resp = zwd.download_file_stream(file_id)
    fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(chunk_size=4 << 20):
                f.write(chunk)
    finally:
        resp.close()
    try:
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Zoho tree builder with 60-second TTL cache
# ---------------------------------------------------------------------------

_tree_cache: dict = {"data": None, "workdir": None, "ts_data": 0.0, "ts_workdir": 0.0}
_TREE_TTL = 60.0


def _build_zoho_tree(zoho_path: str) -> dict:
    """Recursively build a tree dict from a Zoho folder path."""
    zwd = _get_zoho()

    def _node(folder_id: str, name: str, path: str) -> dict:
        children = []
        for item in sorted(zwd.list_folder(folder_id), key=lambda x: x["name"]):
            child_path = f"{path}/{item['name']}"
            if item["type"] == "folder":
                children.append(_node(item["id"], item["name"], child_path))
            else:
                children.append({
                    "name": item["name"],
                    "path": child_path,
                    "type": "file",
                    "size": item.get("size", 0),
                })
        return {"name": name, "path": path, "type": "directory", "children": children}

    result = zwd.resolve_path(zoho_path)
    root_name = zoho_path.rsplit("/", 1)[-1]
    if result is None:
        return {"name": root_name, "path": zoho_path, "type": "directory", "children": []}
    folder_id, _ = result
    return _node(folder_id, root_name, zoho_path)


def _cached_data_tree() -> dict:
    now = _time.monotonic()
    if _tree_cache["data"] is not None and now - _tree_cache["ts_data"] < _TREE_TTL:
        return _tree_cache["data"]
    result = _build_zoho_tree("Data")
    _tree_cache["data"] = result
    _tree_cache["ts_data"] = now
    return result


def _cached_workdir_tree() -> dict:
    now = _time.monotonic()
    if _tree_cache["workdir"] is not None and now - _tree_cache["ts_workdir"] < _TREE_TTL:
        return _tree_cache["workdir"]
    result = _build_zoho_tree("Workdir")
    _tree_cache["workdir"] = result
    _tree_cache["ts_workdir"] = now
    return result


def _invalidate_data_cache():
    _tree_cache["data"] = None
    _tree_cache["ts_data"] = 0.0


def _invalidate_workdir_cache():
    _tree_cache["workdir"] = None
    _tree_cache["ts_workdir"] = 0.0


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
        # Only parse the most recent run — previous runs may have stale
        # "completed X/Y" entries that corrupt progress for the current run.
        last_idx = text.rfind("Pipeline run started")
        if last_idx != -1:
            text = text[last_idx:]
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
# Pipeline runner — workdir intermediates stay local; only final DOCX goes to Zoho
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
            import time as _t
            _stage_start = _t.perf_counter()
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
            log_translation_summary(translation_summary, _t.perf_counter() - _stage_start)
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


def _run_pop_sync(req: PopRequest):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key and not req.skip_translation:
        raise RuntimeError("GEMINI_API_KEY is not set")

    zwd = _get_zoho()
    data_folder_path = f"Data/{req.state}/{req.crop}"
    result = zwd.resolve_path(data_folder_path)
    if result is None:
        raise FileNotFoundError(f"Zoho folder not found: {data_folder_path!r}")
    folder_id = result[0]

    items = zwd.list_folder(folder_id)
    all_pdf_items = sorted(
        [i for i in items if i["name"].lower().endswith(".pdf")],
        key=lambda x: x["name"],
    )

    if req.docs:
        name_to_item = {i["name"]: i for i in all_pdf_items}
        pdf_items = []
        missing = []
        for doc_filename in req.docs:
            if doc_filename in name_to_item:
                pdf_items.append(name_to_item[doc_filename])
            else:
                missing.append(doc_filename)
        if missing:
            raise FileNotFoundError(f"PDFs not found in Zoho: {', '.join(missing)}")
    else:
        pdf_items = all_pdf_items
        if not pdf_items:
            raise FileNotFoundError(f"No PDFs found in Zoho: {data_folder_path!r}")

    prompt = load_prompt(PROMPT_FILE)

    job_id = ctl.current_job_id()
    if job_id and job_id in _jobs:
        _jobs[job_id]["_pop_docs"] = [
            {
                "name": item["name"],
                "workdir": str(POP_WORK / "Workdir" / req.state / req.crop / Path(item["name"]).stem),
                "status": "pending",
            }
            for item in pdf_items
        ]

    for i, pdf_item in enumerate(pdf_items):
        ctl.check_cancel()
        if job_id and job_id in _jobs:
            _jobs[job_id]["_pop_docs"][i]["status"] = "running"

        doc_name = Path(pdf_item["name"]).stem
        print(f"[batch] processing: {pdf_item['name']}")

        # Stream PDF from Zoho to temp file, run pipeline, then clean up temp
        with _stream_zoho_pdf(f"{data_folder_path}/{pdf_item['name']}") as tmp_pdf:
            _run_one_doc(
                source_pdf_path=tmp_pdf,
                doc_name=doc_name,
                req=req,
                api_key=api_key,
                prompt=prompt,
            )

        # Upload final DOCX to Zoho
        workdir_root = POP_WORK / "Workdir" / req.state / req.crop / doc_name
        final_output_dir = workdir_root / "final_output"
        docx_files = sorted(
            f for f in final_output_dir.glob("*.docx")
            if f.is_file() and not f.name.startswith("audit_")
        )
        if docx_files:
            zoho_output_path = f"Workdir/{req.state}/{req.crop}/{doc_name}/final_output"
            zwd.upload_local_file(docx_files[-1], zoho_output_path)
            print(f"[batch] uploaded DOCX to Zoho: {zoho_output_path}/{docx_files[-1].name}")
            _invalidate_workdir_cache()
            _update_master_crop(
                req.state, req.crop, doc_name,
                output_file=f"{zoho_output_path}/{docx_files[-1].name}",
                processed=True,
            )

        # Clean up local scratch — all persistent data is now in Zoho
        shutil.rmtree(workdir_root, ignore_errors=True)
        _pop_script.LOG_FILE_PATH = None  # prevent stale path from crashing next job's log()
        print(f"[batch] cleaned local workdir: {workdir_root}")

        if job_id and job_id in _jobs:
            _jobs[job_id]["_pop_docs"][i]["status"] = "done"


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    (POP_WORK / "Workdir").mkdir(parents=True, exist_ok=True)
    _CHUNK_TMP.mkdir(parents=True, exist_ok=True)
    try:
        _get_zoho()
    except Exception as e:
        print(f"[WARN] Zoho WorkDrive init failed at startup: {e}")
    threading.Thread(target=_load_or_build_master, daemon=True).start()
    yield


app = FastAPI(title="POP Translation Server", lifespan=lifespan, docs_url="/api-docs", redoc_url="/api-redoc")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(_requests.exceptions.RetryError)
@app.exception_handler(_requests.exceptions.ConnectionError)
async def zoho_unavailable(request, exc):
    return JSONResponse(
        status_code=503,
        content={"detail": f"Zoho WorkDrive unavailable: {exc}"},
    )


# Health
@app.get("/")
def health():
    return {"status": "ok", "service": "pop-server"}


@app.get("/test-zoho")
def test_zoho():
    """End-to-end Zoho connectivity test: creates a temp file, verifies it exists, then deletes it."""
    zwd = _get_zoho()
    test_path = "Data/_zoho_test_"
    test_filename = "_pop_connectivity_test_.txt"
    results: dict = {}

    try:
        parent_id = zwd.ensure_path(test_path)
        results["ensure_path"] = "ok"
    except Exception as e:
        return {"status": "error", "step": "ensure_path", "error": str(e)}

    try:
        file_id = zwd.upload_file(test_filename, b"ping", parent_id)
        results["upload"] = "ok" if file_id else "uploaded_but_no_id"
    except Exception as e:
        return {"status": "error", "step": "upload", "error": str(e), "results": results}

    try:
        found = zwd.find_child(parent_id, test_filename)
        results["find_after_upload"] = "ok" if found else "not_found"
    except Exception as e:
        results["find_after_upload"] = f"error: {e}"

    try:
        resolve_result = zwd.resolve_path(f"{test_path}/{test_filename}")
        results["resolve_path"] = "ok" if resolve_result else "not_found"
        resolved_id = resolve_result[0] if resolve_result else (file_id or "")
    except Exception as e:
        results["resolve_path"] = f"error: {e}"
        resolved_id = file_id or ""

    try:
        ok = zwd.delete(resolved_id) if resolved_id else False
        results["delete"] = "ok" if ok else "failed"
    except Exception as e:
        results["delete"] = f"error: {e}"

    try:
        folder_result = zwd.resolve_path(test_path)
        if folder_result:
            zwd.delete(folder_result[0])
        results["cleanup_folder"] = "ok"
    except Exception as e:
        results["cleanup_folder"] = f"error: {e}"

    all_ok = all(v in ("ok", "uploaded_but_no_id") for v in results.values())
    return {"status": "ok" if all_ok else "partial", "results": results}


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


@app.get("/states")
def list_states():
    zwd = _get_zoho()
    result = zwd.resolve_path("Data")
    if result is None:
        return []
    items = zwd.list_folder(result[0])
    return sorted(i["name"] for i in items if i["type"] == "folder")


@app.get("/crops")
def list_crops(state: str = Query(...)):
    zwd = _get_zoho()
    result = zwd.resolve_path(f"Data/{state}")
    if result is None:
        raise HTTPException(404, f"State not found: {state!r}")
    items = zwd.list_folder(result[0])
    return sorted(i["name"] for i in items if i["type"] == "folder")


@app.get("/docs")
def list_docs(state: str = Query(...), crop: str = Query(...)):
    zwd = _get_zoho()
    result = zwd.resolve_path(f"Data/{state}/{crop}")
    if result is None:
        raise HTTPException(404, f"Crop not found: {state!r}/{crop!r}")
    items = zwd.list_folder(result[0])
    return sorted(i["name"] for i in items if i["name"].lower().endswith(".pdf"))


@app.get("/data/tree")
def data_tree():
    return _cached_data_tree()


@app.get("/output/tree")
def output_tree():
    return _cached_workdir_tree()


@app.get("/state-table")
def state_table(refresh: bool = False):
    if refresh:
        _master_ready.clear()
        threading.Thread(target=_rebuild_master, daemon=True).start()
        return {"status": "loading", "rows": []}

    if not _master_ready.is_set():
        return {"status": "loading", "rows": []}

    return {"status": "ready", "rows": _master_to_rows()}


# ---------------------------------------------------------------------------
# Semantic state / crop / doc management
# ---------------------------------------------------------------------------


@app.post("/state")
def create_state(body: StateBody):
    state = body.state.strip()
    if not state:
        raise HTTPException(400, "state is required")
    zwd = _get_zoho()
    zwd.ensure_path(f"Data/{state}")
    _invalidate_data_cache()
    return {"state": state}


@app.post("/crop")
def create_crop(body: CropBody):
    state, crop = body.state.strip(), body.crop.strip()
    if not state or not crop:
        raise HTTPException(400, "state and crop are required")
    zwd = _get_zoho()
    zwd.ensure_path(f"Data/{state}/{crop}")
    if _master_ready.is_set():
        with _master_lock:
            _master_data.setdefault(state, {}).setdefault(crop, {})
            _upload_master_json()
    _invalidate_data_cache()
    return {"state": state, "crop": crop}


@app.delete("/state")
def delete_state(state: str = Query(...)):
    """Delete a state folder. Returns 409 if any crop folders still exist inside."""
    zwd = _get_zoho()
    result = zwd.resolve_path(f"Data/{state}")
    if result is None:
        raise HTTPException(404, f"State not found: {state!r}")
    folder_id = result[0]
    crops = [i for i in zwd.list_folder(folder_id) if i["type"] == "folder"]
    if crops:
        raise HTTPException(409, f"State has {len(crops)} crop(s). Delete all crops first.")
    zwd.delete(folder_id)
    if _master_ready.is_set():
        with _master_lock:
            if state in _master_data:
                del _master_data[state]
                _upload_master_json()
    _invalidate_data_cache()
    return {"deleted": state}


@app.delete("/crop")
def delete_crop(state: str = Query(...), crop: str = Query(...)):
    """Delete a crop folder. Returns 409 if PDFs still exist — delete docs first."""
    zwd = _get_zoho()
    result = zwd.resolve_path(f"Data/{state}/{crop}")
    if result is None:
        raise HTTPException(404, f"Crop not found: {state!r}/{crop!r}")
    folder_id = result[0]
    pdfs = [i for i in zwd.list_folder(folder_id) if i["name"].lower().endswith(".pdf")]
    if pdfs:
        raise HTTPException(409, f"Crop has {len(pdfs)} PDF(s). Delete all documents first.")
    zwd.delete(folder_id)
    if _master_ready.is_set():
        with _master_lock:
            if state in _master_data and crop in _master_data[state]:
                del _master_data[state][crop]
                if not _master_data[state]:
                    del _master_data[state]
                _upload_master_json()
    _invalidate_data_cache()
    return {"deleted": f"{state}/{crop}"}


@app.post("/upload-doc")
async def upload_doc(file: UploadFile = File(...), state: str = Form(...), crop: str = Form(...)):
    """Upload a PDF into Zoho at Data/<state>/<crop>/."""
    state, crop = state.strip(), crop.strip()
    zwd = _get_zoho()
    parent_id = zwd.ensure_path(f"Data/{state}/{crop}")

    filename = file.filename or "upload.pdf"
    content = await file.read()
    zwd.upload_file(filename, content, parent_id)

    stem = Path(filename).stem
    if _master_ready.is_set():
        _update_master_crop(state, crop, stem,
            output_file=None, audit_file=None,
            downloaded=False, audited=False, processed=False,
        )
    _invalidate_data_cache()
    return {"path": f"Data/{state}/{crop}/{filename}", "size": len(content)}


@app.delete("/doc")
def delete_doc(state: str = Query(...), crop: str = Query(...), doc_name: str = Query(...)):
    """Delete a PDF, its meta.json, and its Zoho Workdir output folder."""
    zwd = _get_zoho()

    # Delete PDF
    pdf_result = zwd.resolve_path(f"Data/{state}/{crop}/{doc_name}")
    if pdf_result is None:
        raise HTTPException(404, f"Document not found: {doc_name!r}")
    if not zwd.delete(pdf_result[0]):
        raise HTTPException(500, f"Zoho rejected delete of {doc_name!r} — check server logs for details")

    stem = Path(doc_name).stem

    # Delete Workdir output folder from Zoho (best-effort)
    workdir_result = zwd.resolve_path(f"Workdir/{state}/{crop}/{stem}")
    if workdir_result:
        if not zwd.delete(workdir_result[0]):
            print(f"[WARN] Failed to delete Zoho workdir for {doc_name!r}")

    # Also clean up local workdir scratch (if it exists from a prior pipeline run)
    local_workdir = POP_WORK / "Workdir" / state / crop / stem
    if local_workdir.exists():
        shutil.rmtree(local_workdir, ignore_errors=True)

    if _master_ready.is_set():
        _remove_master_crop(state, crop, stem)
    _invalidate_data_cache()
    _invalidate_workdir_cache()
    return {"deleted": doc_name, "state": state, "crop": crop}


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


@app.post("/upload")
async def upload_file_endpoint(dest: str = Query(...), file: UploadFile = File(...)):
    try:
        _validate_zoho_path(dest)
    except ValueError as e:
        raise HTTPException(400, str(e))
    zwd = _get_zoho()
    filename = file.filename or "upload"
    content = await file.read()
    parent_id = zwd.ensure_path(dest)
    zwd.upload_file(filename, content, parent_id)
    return {"path": f"{dest}/{filename}", "size": len(content)}


@app.post("/upload-chunk")
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
        _validate_zoho_path(dest)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Assemble locally then stream-upload to Zoho
    assembled = chunk_dir / filename
    with assembled.open("wb") as out:
        for i in range(total_chunks):
            out.write((chunk_dir / f"chunk_{i:06d}").read_bytes())

    try:
        zwd = _get_zoho()
        zwd.upload_local_file(assembled, dest)
    finally:
        shutil.rmtree(chunk_dir, ignore_errors=True)

    _invalidate_data_cache()
    return {"status": "complete", "path": f"{dest}/{filename}"}


@app.post("/upload-audited")
async def upload_audited(
    file: UploadFile = File(...),
    state: str = Form(...),
    crop: str = Form(...),
    doc_name: str = Form(...),
):
    stem = Path(doc_name).stem
    zwd = _get_zoho()

    # Ensure the translated output exists before accepting the audit file
    workdir_path = f"Workdir/{state}/{crop}/{stem}/final_output"
    workdir_result = zwd.resolve_path(workdir_path)
    if workdir_result is None:
        raise HTTPException(404, f"No processed output found for {doc_name!r} — run translation first.")

    if workdir_result[1] != "folder":
        raise HTTPException(404, f"Output path is not a folder for {doc_name!r}")

    original_filename = file.filename or (stem + "_audited.docx")
    filename = original_filename if original_filename.startswith("audit_") else ("audit_" + original_filename)

    # Get old audit path from master cache to clean up the old file
    with _master_lock:
        old_audit = _master_data.get(state, {}).get(crop, {}).get(stem, {}).get("audit_file")

    # Upload new file first — then clean up old one so a failed upload doesn't lose data
    parent_id = workdir_result[0]
    content = await file.read()
    file_id = zwd.upload_file(filename, content, parent_id)
    if not file_id:
        # Zoho returned 200 but gave no ID — verify by listing
        found = zwd.find_child(parent_id, filename)
        if not found:
            raise HTTPException(500, "Audit file upload to Zoho failed — file not found after upload")

    zoho_audit_path = f"{workdir_path}/{filename}"

    # Remove old audit file (best-effort — don't fail the request if this errors)
    if old_audit and old_audit != zoho_audit_path:
        try:
            old_result = zwd.resolve_path(old_audit)
            if old_result:
                zwd.delete(old_result[0])
        except Exception as e:
            print(f"[WARN] Could not delete old audit file {old_audit!r}: {e}")

    _update_master_crop(state, crop, stem, audited=True, audit_file=zoho_audit_path)
    _invalidate_workdir_cache()
    return {"path": zoho_audit_path, "size": len(content)}


@app.delete("/audit")
def delete_audit(state: str = Query(...), crop: str = Query(...), doc_name: str = Query(...)):
    """Delete the audited file and clear audit metadata."""
    stem = Path(doc_name).stem
    zwd = _get_zoho()
    with _master_lock:
        audit_file = _master_data.get(state, {}).get(crop, {}).get(stem, {}).get("audit_file")
    if not audit_file:
        raise HTTPException(404, "No audit file found for this document")
    try:
        result = zwd.resolve_path(audit_file)
        if result:
            zwd.delete(result[0])
    except Exception as e:
        print(f"[WARN] Could not delete audit file from Zoho {audit_file!r}: {e}")
    # Always clear master regardless of whether the Zoho delete succeeded —
    # _upload_master_json never raises so this is guaranteed to run.
    _update_master_crop(state, crop, stem, audited=False, audit_file=None)
    _invalidate_workdir_cache()
    return {"deleted": audit_file, "state": state, "crop": crop, "doc_name": doc_name}


@app.delete("/files/{path:path}")
def delete_file(path: str):
    try:
        _validate_zoho_path(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    zwd = _get_zoho()
    result = zwd.resolve_path(path)
    if result is None:
        raise HTTPException(404, "Not found")
    file_id, ftype = result
    if ftype == "folder":
        raise HTTPException(400, "Path is not a file")
    zwd.delete(file_id)
    _master_sync_deleted_file(path)
    return {"deleted": path}


@app.delete("/folders/{path:path}")
def delete_folder(path: str):
    try:
        _validate_zoho_path(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    zwd = _get_zoho()
    result = zwd.resolve_path(path)
    if result is None:
        raise HTTPException(404, "Not found")
    file_id, ftype = result
    if ftype != "folder":
        raise HTTPException(400, "Path is not a directory")
    zwd.delete(file_id)
    _master_sync_deleted_folder(path)
    return {"deleted": path}


@app.get("/download/{path:path}")
def download_file(path: str):
    try:
        _validate_zoho_path(path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    zwd = _get_zoho()
    result = zwd.resolve_path(path)
    if result is None:
        raise HTTPException(404, "File not found")
    file_id, ftype = result
    if ftype == "folder":
        raise HTTPException(400, "Path is a folder, not a file")
    filename = path.rsplit("/", 1)[-1]

    def _stream():
        resp = zwd.download_file_stream(file_id)
        try:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                yield chunk
        finally:
            resp.close()

    return StreamingResponse(
        _stream(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/output")
def download_output(state: str = Query(...), crop: str = Query(...), doc_name: str = Query(...)):
    stem = Path(doc_name).stem
    zwd = _get_zoho()
    workdir_path = f"Workdir/{state}/{crop}/{stem}/final_output"
    result = zwd.resolve_path(workdir_path)
    if result is None:
        raise HTTPException(404, "Output directory not found in Zoho")
    folder_id = result[0]
    docx_items = sorted(
        [
            i for i in zwd.list_folder(folder_id)
            if i["name"].lower().endswith(".docx") and not i["name"].startswith("audit_")
        ],
        key=lambda x: x["name"],
    )
    if not docx_items:
        raise HTTPException(404, "No DOCX found for this document")
    docx_item = docx_items[-1]

    _update_master_crop(state, crop, stem, downloaded=True)

    def _stream():
        resp = zwd.download_file_stream(docx_item["id"])
        try:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                yield chunk
        finally:
            resp.close()

    return StreamingResponse(
        _stream(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{docx_item["name"]}"'},
    )


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------


@app.get("/jobs")
def list_jobs():
    return [_job_view(j) for j in _jobs.values()]


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_view(job)


@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    ctl.cancel(job_id)
    _jobs[job_id]["status"] = "stopped"
    return {"job_id": job_id, "status": "stopped"}


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    ctl.cleanup(job_id)
    del _jobs[job_id]
    return {"deleted": job_id}
