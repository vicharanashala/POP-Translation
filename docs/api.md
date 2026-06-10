# REST API Reference

Base URL: `http://<host>:8032`  
Interactive docs: `http://<host>:8032/api-docs` (Swagger UI)  
ReDoc: `http://<host>:8032/api-redoc`

---

## Health

### `GET /`

Returns service status.

```json
{ "status": "ok", "service": "pop-server" }
```

### `GET /test-zoho`

End-to-end Zoho connectivity test. Creates a temp file, verifies it is findable, then deletes it.

**Response:**

```json
{
  "status": "ok",
  "results": {
    "ensure_path": "ok",
    "upload": "ok",
    "find_after_upload": "ok",
    "resolve_path": "ok",
    "delete": "ok",
    "cleanup_folder": "ok"
  }
}
```

`status` is `"partial"` if any step failed.

---

## Translation jobs

### `POST /run/pop`

Start a translation job for one or more PDFs.

**Request body (JSON):**

```json
{
  "state": "Karnataka",
  "crop": "Ginger",
  "docs": ["doc1.pdf", "doc2.pdf"],   // optional; null = all PDFs in the folder
  "concurrency": 1,
  "overwrite": false,
  "skip_translation": false,
  "skip_image_injection": false,
  "model": "gemini-3.1-pro-preview",
  "max_retries": 5,
  "retry_wait_seconds": 15,
  "disable_google_search": false
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `state` | string | required | State folder name under `Data/` |
| `crop` | string | required | Crop folder name under `Data/<state>/` |
| `docs` | string[] \| null | null | Specific PDF filenames; null = all PDFs in the crop folder |
| `concurrency` | int | 1 | Parallel Gemini translation workers per document |
| `overwrite` | bool | false | Re-run completed stages |
| `skip_translation` | bool | false | Skip Gemini translation; reuse `translated.html` |
| `skip_image_injection` | bool | false | Skip image injection; reuse `final_with_images.html` |
| `model` | string | `gemini-3.1-pro-preview` | Gemini model ID |
| `max_retries` | int | 5 | Retries per page on Gemini failure |
| `retry_wait_seconds` | int | 15 | Seconds between retries |
| `disable_google_search` | bool | false | Disable Google Search grounding |

**Response (202-equivalent):**

```json
{
  "job_id": "b3f2c1a0-...",
  "job_type": "pop",
  "job_type_id": 5,
  "status": "pending"
}
```

---

## Job management

All jobs are in-memory. They are lost on server restart.

### `GET /jobs`

List all jobs.

**Response:** array of job objects (see shape below).

### `GET /jobs/{job_id}`

Get a single job.

**Job object:**

```json
{
  "job_id": "b3f2c1a0-...",
  "job_type": "pop",
  "job_type_id": 5,
  "status": "running",             // "pending" | "running" | "done" | "stopped" | "failed"
  "stdout": "...",                 // captured print() output from the pipeline thread
  "stderr": "",                    // error message or "stopped by user"
  "created_at": "2026-06-08T10:00:00+00:00",
  "started_at": "2026-06-08T10:00:01+00:00",
  "finished_at": null,
  "progress": {                    // null for non-"pop" jobs
    "total_docs": 3,
    "docs_done": 1,
    "current_doc": "doc2.pdf",
    "current_page": 7,
    "current_total_pages": 20,
    "docs": [
      { "name": "doc1.pdf", "status": "done",    "pages_total": 15, "pages_done": 15 },
      { "name": "doc2.pdf", "status": "running", "pages_total": 20, "pages_done": 7  },
      { "name": "doc3.pdf", "status": "pending", "pages_total": null, "pages_done": 0 }
    ]
  }
}
```

**Status values:**

| Status | Meaning |
|---|---|
| `pending` | Queued, not started yet |
| `running` | Actively processing |
| `done` | Completed successfully |
| `stopped` | Stopped by `POST /jobs/{id}/stop` |
| `failed` | Unhandled exception; `stderr` contains traceback |

### `POST /jobs/{job_id}/stop`

Request cancellation. Sets the cancellation event; the pipeline checks it between pages via `ctl.check_cancel()`. Any in-flight Gemini request finishes before the job actually stops.

**Response:**

```json
{ "job_id": "...", "status": "stopped" }
```

### `DELETE /jobs/{job_id}`

Remove a job record from memory. Safe to call on finished jobs to free memory.

**Response:**

```json
{ "deleted": "..." }
```

---

## Data browsing

### `GET /states`

List state folder names under `Data/`.

**Response:** `["Karnataka", "Tamil Nadu", ...]`

### `GET /crops?state=Karnataka`

List crop folder names under `Data/<state>/`.

**Response:** `["Ginger", "Onion", ...]`

### `GET /docs?state=Karnataka&crop=Ginger`

List PDF filenames under `Data/<state>/<crop>/`.

**Response:** `["doc1.pdf", "doc2.pdf"]`

### `GET /data/tree`

Full recursive tree of the `Data/` folder in Zoho. Cached for 60 seconds.

```json
{
  "name": "Data",
  "path": "Data",
  "type": "directory",
  "children": [
    {
      "name": "Karnataka",
      "path": "Data/Karnataka",
      "type": "directory",
      "children": [...]
    }
  ]
}
```

### `GET /output/tree`

Full recursive tree of the `Workdir/` folder in Zoho. Cached for 60 seconds.

### `GET /state-table?refresh=false`

Returns the in-memory master document state table.

| Query param | Description |
|---|---|
| `refresh=true` | Triggers a background `_rebuild_master()` walk of Zoho; immediately returns `{"status": "loading", "rows": []}` |

**Response (when ready):**

```json
{
  "status": "ready",
  "rows": [
    {
      "state": "Karnataka",
      "crop": "Ginger",
      "doc_name": "doc1.pdf",
      "doc_path": "Data/Karnataka/Ginger/doc1.pdf",
      "is_empty": false,
      "processed": true,
      "downloaded": false,
      "output_path": "Workdir/Karnataka/Ginger/doc1/final_output/doc1_translated_pages_001_to_015.docx",
      "output_dir": "Workdir/Karnataka/Ginger/doc1",
      "audit_file": null,
      "audited": false,
      "has_docx": true,
      "page_count": 0,
      "status": "done",             // "not_started" | "done" | "audited"
      "finished_at": "2026-06-08T10:30:00+00:00"
    }
  ]
}
```

`is_empty: true` rows appear when a crop folder exists in `Data/` but contains no PDFs.

---

## State and crop management

### `POST /state`

Create a state folder under `Data/`.

```json
{ "state": "Tamil Nadu" }
```

### `POST /crop`

Create a crop folder under `Data/<state>/`.

```json
{ "state": "Tamil Nadu", "crop": "Banana" }
```

### `DELETE /state?state=Karnataka`

Delete a state folder. Returns `409` if any crop folders exist inside.

### `DELETE /crop?state=Karnataka&crop=Ginger`

Delete a crop folder. Returns `409` if any PDFs exist inside.

---

## Document management

### `POST /upload-doc`

Upload a PDF to `Data/<state>/<crop>/`. Registers the document in master.json.

**Form fields:** `file` (PDF), `state`, `crop`

**Response:**

```json
{ "path": "Data/Karnataka/Ginger/doc1.pdf", "size": 204800 }
```

### `DELETE /doc?state=Karnataka&crop=Ginger&doc_name=doc1.pdf`

Delete a source PDF **and** its Zoho Workdir output folder. Also cleans up any local scratch. Updates master.json.

---

## File operations

### `POST /upload?dest=Data/Karnataka/Ginger`

Generic file upload. `dest` is the Zoho folder path.

**Form fields:** `file`

**Response:**

```json
{ "path": "Data/Karnataka/Ginger/filename.pdf", "size": 12345 }
```

### `POST /upload-chunk`

Chunked upload for large files. The server assembles chunks and streams the completed file to Zoho.

**Query params:** `upload_id`, `chunk_index`, `total_chunks`, `filename`, `dest`  
**Body:** raw binary chunk

Chunks can arrive in any order. When `len(received_chunks) == total_chunks`, the file is assembled and uploaded.

**Response while assembling:**

```json
{ "status": "ok", "chunk": 3 }
```

**Response on completion:**

```json
{ "status": "complete", "path": "Data/Karnataka/Ginger/large_file.pdf" }
```

### `POST /upload-audited`

Upload a reviewed/audited DOCX file. The filename is prefixed with `audit_` if not already.

**Requirements:** A translated DOCX must already exist at `Workdir/<state>/<crop>/<stem>/final_output/` before an audit file can be accepted (returns `404` otherwise).

**Form fields:** `file` (DOCX), `state`, `crop`, `doc_name`

Replaces the previous audit file (old file is deleted from Zoho after the new one is confirmed uploaded).

**Response:**

```json
{ "path": "Workdir/Karnataka/Ginger/doc1/final_output/audit_doc1_audited.docx", "size": 204800 }
```

### `DELETE /audit?state=Karnataka&crop=Ginger&doc_name=doc1.pdf`

Delete the audit file for a document. Clears `audited` and `audit_file` in master.json regardless of whether the Zoho delete succeeds.

### `DELETE /files/{path}`

Delete any file by Zoho path (e.g. `Data/Karnataka/Ginger/doc1.pdf`). Syncs master.json.  
Returns `400` if path resolves to a folder.

### `DELETE /folders/{path}`

Delete any folder by Zoho path. Syncs master.json.  
Returns `400` if path resolves to a file.

### `GET /download/{path}`

Stream any file from Zoho by path.

**Response:** `application/octet-stream` with `Content-Disposition: attachment`

### `GET /output?state=Karnataka&crop=Ginger&doc_name=doc1.pdf`

Stream the latest translated DOCX for a document (excludes `audit_` prefixed files). Marks `downloaded=true` in master.json.

**Response:** `.docx` file stream.

---

## Error responses

| HTTP status | Condition |
|---|---|
| `400` | Bad request — invalid path, missing required field |
| `404` | Resource not found in Zoho |
| `409` | Conflict — e.g. deleting a state that still has crops |
| `500` | Internal error — pipeline failure, Zoho upload rejection |
| `503` | Zoho WorkDrive unreachable (connection/retry error) |

All error responses have the shape: `{ "detail": "..." }`

---

## Path validation

All Zoho paths passed through endpoints are validated by `_validate_zoho_path()`:

- Empty paths → `400`
- Absolute paths (starting with `/`) → `400`
- Path traversal segments (`..`) → `400`
- Null bytes → `400`
