# Architecture

## High-level component map

```
┌──────────────────────────────────────────────────────────┐
│                     Clients / UI                         │
└────────────────────────┬─────────────────────────────────┘
                         │ HTTP (port 8032)
┌────────────────────────▼─────────────────────────────────┐
│                   FastAPI  pop_server.py                  │
│                                                           │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │  REST API   │  │  Job store   │  │  Master JSON    │  │
│  │  endpoints  │  │  _jobs dict  │  │  cache          │  │
│  └──────┬──────┘  └──────┬───────┘  └────────┬────────┘  │
│         │                │                   │           │
│         └────────────────┴───────────────────┘           │
│                          │                               │
│              ┌───────────▼────────────┐                  │
│              │  ThreadPoolExecutor    │                  │
│              │  (max_workers=4)       │                  │
│              └───────────┬────────────┘                  │
└──────────────────────────┼───────────────────────────────┘
                           │
          ┌────────────────▼────────────────┐
          │   Pipeline  (scripts/)          │
          │   run_pop_to_docx_…_docx.py     │
          │                                 │
          │   Stage 1: split_pdf            │
          │   Stage 2: translate (Gemini)   │
          │   Stage 3: inject_images        │
          │   Stage 4: convert_to_docx      │
          │   Stage 5: merge_docx           │
          └──────┬──────────────┬───────────┘
                 │              │
    ┌────────────▼──┐    ┌──────▼───────────────────┐
    │  Gemini API   │    │  Zoho WorkDrive           │
    │  (translate)  │    │  helpers/zoho_workdrive.py│
    └───────────────┘    └──────────────────────────-┘
```

## Module inventory

| File | Role |
|------|------|
| `pop_server.py` | Entry point; FastAPI app, all route handlers, master JSON management, pipeline orchestration |
| `_job_ctl.py` | Thread-safe job cancellation primitives (Events, proc registry) |
| `scripts/run_pop_to_docx_updated_pagewise_docx.py` | Self-contained pipeline; also usable as a CLI |
| `helpers/zoho_workdrive.py` | Zoho WorkDrive REST client (CRUD, upload, download, token refresh) |
| `helpers/__init__.py` | Empty package marker |
| `prompts/page_to_pdf.txt` | Translation system prompt for Gemini |

---

## Data-flow: one translation job

```
POST /run/pop  { state, crop, docs?, ... }
  │
  ├─ _submit() creates job record, registers threading.Event
  │
  └─ BackgroundTasks → _run_job_bg → _run_pop_sync (in thread)
       │
       ├─ resolve Zoho folder  Data/<state>/<crop>/
       ├─ list PDF items
       │
       └─ for each PDF:
            │
            ├─ _stream_zoho_pdf  → stream to /tmp/<uuid>.pdf
            │
            └─ _run_one_doc(tmp_pdf, ...)
                 │
                 ├─ [Stage 1] split_pdf_to_page_folders
                 │     creates pop-data/POP_Work/Workdir/<state>/<crop>/<stem>/page_NNN/page_NNN.pdf
                 │
                 ├─ [Stage 2] translate_pages / process_translation_for_page
                 │     calls Gemini → saves page_NNN/translated.html
                 │     writes translation_summary.json
                 │
                 ├─ [Stage 3] inject_images_for_pages
                 │     extracts images from PDF → page_NNN/images/image_N.png
                 │     merges into page_NNN/final_with_images.html
                 │     writes image_injection_summary.json
                 │
                 ├─ [Stage 4] convert_pages_to_individual_docx
                 │     Pandoc: final_with_images.html → final_output/page_docx/<stem>_page_NNN.docx
                 │     writes page_docx_summary.json
                 │
                 ├─ [Stage 5] merge_docx_files_in_order
                 │     docxcompose → final_output/<stem>_translated_pages_001_to_NNN.docx
                 │
                 ├─ upload final DOCX → Zoho Workdir/<state>/<crop>/<stem>/final_output/
                 ├─ _update_master_crop(..., processed=True)
                 └─ shutil.rmtree(local workdir)   ← scratch cleaned up
```

---

## Storage layout

### Zoho WorkDrive (persistent)

```
<ZOHO_ROOT_FOLDER_ID>/
├── master.json                          ← state table (rebuilt on startup)
│
├── Data/
│   └── <State>/                         ← e.g. Karnataka
│       └── <Crop>/                      ← e.g. Ginger
│           └── <document>.pdf           ← source PDFs
│
└── Workdir/
    └── <State>/
        └── <Crop>/
            └── <document_stem>/
                └── final_output/
                    ├── <stem>_translated_pages_001_to_NNN.docx
                    └── audit_<stem>_audited.docx  ← optional reviewed version
```

### Local disk (temporary scratch, mounted volume)

```
/app/pop-data/                           ← Docker volume mount point
└── POP_Work/
    └── Workdir/
        └── <State>/<Crop>/<stem>/       ← created on job start, deleted when done
            ├── page_001/
            │   ├── page_001.pdf
            │   ├── translated.html
            │   ├── images/
            │   │   └── image_1.png
            │   └── final_with_images.html
            ├── page_002/ ...
            ├── pipeline_runtime.log
            ├── translation_summary.json
            ├── image_injection_summary.json
            ├── page_docx_summary.json
            └── final_output/
                ├── page_docx/
                │   ├── <stem>_page_001.docx
                │   └── ...
                └── <stem>_translated_pages_001_to_NNN.docx
```

> Local workdir is deleted after DOCX is uploaded to Zoho. The `pop-data/` directory is a Docker named volume so it survives container restarts during active long-running jobs.

---

## master.json

`master.json` is uploaded to the Zoho root folder and is the authoritative state table. Structure:

```json
{
  "built_at": "2026-06-08T10:00:00+00:00",
  "data": {
    "<State>": {
      "<Crop>": {
        "<stem>": {
          "output_file":  "Workdir/<State>/<Crop>/<stem>/final_output/<file>.docx",
          "audit_file":   "Workdir/<State>/<Crop>/<stem>/final_output/audit_<file>.docx",
          "downloaded":   false,
          "audited":      false,
          "processed":    true,
          "finished_at":  "2026-06-08T10:30:00+00:00"
        }
      }
    }
  }
}
```

On startup, `pop_server.py` tries to load the cached `master.json` from Zoho before falling back to a full `_rebuild_master()` walk. All mutations (`_update_master_crop`, `_remove_master_crop`) hold `_master_lock` and call `_upload_master_json()` inline.

---

## Thread safety model

| Resource | Guard |
|----------|-------|
| `_master_data` dict | `_master_lock` (threading.Lock) |
| `_jobs` dict | Reads/writes happen from the event loop thread or single background worker; no explicit lock needed for job creation, but jobs are isolated per-thread |
| Zoho access token | `ZohoWorkDrive._lock` (threading.Lock) |
| Log file writes | `LOG_LOCK` (threading.Lock) in pipeline script |
| Job stdout capture | `_tl_stdout` (threading.local) — per-thread buffer |

The `ThreadPoolExecutor` runs pipeline jobs with `max_workers=4`, meaning up to 4 documents can be processed simultaneously across all active jobs. A single `/run/pop` job processes its documents sequentially within the one worker thread it occupies.
