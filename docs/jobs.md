# Job System

`pop_server.py` manages background jobs through a combination of an in-memory store, a `ThreadPoolExecutor`, and thread-local primitives.

---

## Job lifecycle

```
POST /run/pop
  │
  └─ _submit(fn, background, job_type="pop")
       │
       ├─ uuid4()  →  job_id
       ├─ ctl.make_event(job_id)     ← creates threading.Event for cancellation
       ├─ _jobs[job_id] = { status: "pending", ... }
       └─ background.add_task(_run_job_bg, job_id, fn)
                │
                └─ asyncio.loop.run_in_executor(_executor, _run_job_sync, job_id, fn)
                         │
                         ├─ status → "running"
                         ├─ ctl.set_job_id(job_id)          ← thread-local
                         ├─ _tl_stdout.buf = job["_stdout_buf"]  ← capture print()
                         │
                         ├─ fn()   ← _run_pop_sync(req)
                         │
                         ├─ on success:  status → "done"
                         ├─ on JobCancelled:  status → "stopped"
                         ├─ on Exception:     status → "failed", stderr = traceback
                         │
                         └─ ctl.cleanup(job_id), finished_at = now
```

---

## In-memory job record

```python
{
    "job_id":      "b3f2c1a0-...",
    "job_type":    "pop",
    "job_type_id": 5,
    "status":      "running",         # pending | running | done | stopped | failed
    "stderr":      "",                # traceback or "stopped by user"
    "_stdout_buf": [],                # list of strings (print() output)
    "created_at":  "...",
    "started_at":  "...",
    "finished_at": None,
    "_pop_docs": [                    # injected by _run_pop_sync; drives progress
        {
            "name":    "doc1.pdf",
            "workdir": "/app/pop-data/POP_Work/Workdir/Karnataka/Ginger/doc1",
            "status":  "running"      # pending | running | done
        }
    ]
}
```

`_job_view(job)` strips the private `_` fields before returning to callers.

---

## Stdout capture

`pop_server.py` replaces `sys.stdout` with `_ThreadLocalStdout` at module load time. This wrapper checks `threading.local()` for a `buf` list:

- If `buf` is set (job thread): all `print()` output is appended to the buffer.
- Otherwise: forwarded to the real stdout.

The buffer is attached to the job record as `_stdout_buf`. `GET /jobs/{id}` returns it as `"stdout": "..."` (joined string).

This means pipeline log output is isolated per job and never interleaved in the server's stdout.

---

## Cancellation

`_job_ctl.py` provides:

| Function | Description |
|---|---|
| `make_event(job_id)` | Creates a `threading.Event` keyed to the job |
| `cancel(job_id)` | Sets the event; kills any registered subprocess |
| `check_cancel()` | Raises `JobCancelled` if the event is set |
| `is_cancelled(job_id)` | Returns bool without raising |
| `register_proc(proc)` | Associates a `subprocess.Popen` with the current job |
| `deregister_proc()` | Removes the subprocess association |
| `cleanup(job_id)` | Removes event and proc records |

The pipeline calls `ctl.check_cancel()` at key checkpoints:
- After each PDF split stage
- Before each document in the batch loop
- Between translation and image injection stages
- Between image injection and DOCX conversion

`JobCancelled` inherits from `BaseException` (not `Exception`) so it bypasses broad `except Exception` handlers in the pipeline.

`POST /jobs/{id}/stop` immediately sets `status = "stopped"` in the job record and sets the event. The running thread will transition to `stopped` on its next `check_cancel()` call.

---

## Progress tracking

Progress data is built on-demand by `_build_pop_progress(job)` from two sources:

1. **`_pop_docs` list** — each document's `status` (`pending` / `running` / `done`), set by `_run_pop_sync`.
2. **`pipeline_runtime.log`** file — parsed by `_parse_log_pages()`:
   - `selected_pages=N` → total page count for current document
   - `Translation progress | completed N/M` → pages done so far

The log parser scans only from the most recent `Pipeline run started` banner to avoid stale counts from earlier runs.

```json
"progress": {
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
```

---

## Concurrency limits

`_executor = ThreadPoolExecutor(max_workers=4)`

At most 4 pipeline jobs can run simultaneously. Jobs beyond that queue in FastAPI's `BackgroundTasks` and start as workers become free. Within a single job, documents are processed sequentially.

---

## Persistence

Jobs are **in-memory only**. They are lost on server restart. Long-running jobs that survive a restart are reflected in `master.json` (partial state) — the output files exist in Zoho if stages completed before the crash, but there is no job record to query.

For reliable completion tracking, poll `GET /state-table` rather than individual job status.
