"""
Single script, two phases, both over results/state_language_report/report_true.csv:

Phase 1 -- SHA: for each unique pdf NAME, SHA-256 its file bytes (downloaded
via its doc_link column). Cached by name: if a name already has a sha
anywhere in the CSV (from an earlier run), it's propagated to every row
sharing that name with NO download at all -- not even a re-check. Adds/
fills the `sha` column in report_true.csv (in place).

Phase 2 -- embed: for each unique SHA (not name -- this is the point: two
different filenames that happen to be byte-identical collapse to one sha
and only ever get embedded once), render its middle page at 300 DPI, run
Tesseract layout+OCR (same tessdata_best mechanism as
cropped_files_2/route_translate.py) into a reading-order transcript, and
embed it with a local multilingual sentence-transformer. Cached by sha: a
sha already present as a key in the output JSON is skipped entirely.

Nothing is persisted except the sha column and the final embeddings file --
no page images, no transcripts, no layout JSON, no per-doc directories.
Output is a single file, results/report_true_embeddings.json, mapping
sha -> embedding (list of floats, rounded to 6 decimals --
intfloat/multilingual-e5-base, 768-d). Downloaded PDF bytes and rendered
images exist only transiently in memory for the duration of one doc's
processing, never written to disk.

OCR language per sha (phase 2) comes from ONE representative row for that
sha (the first one encountered) -- report_true.csv's own `language` column
== "English" (exact) -> "eng", otherwise the row's state, looked up in
STATE_LANG below ("best available" tessdata_best model per state; Central
Advisories -> Hindi per explicit instruction; several Northeast/UT states
have no matching-script model at all and fall back to "eng").

Phase 1 uses one thread pool for the actual download (8 workers, I/O-bound,
cheap SHA-256 pass -- no CPU-heavy work involved, so concurrent downloads
don't meaningfully contend with each other).

Phase 2 splits download and OCR into two SEPARATE thread pools chained via
callbacks (download finishes -> hands bytes to the OCR pool -> OCR result
lands on a queue the main thread drains for embedding). This split matters:
OCR is CPU-bound and scales with worker count up to the core count (8
workers measured as the sweet spot on this machine; OMP_THREAD_LIMIT=1 is
force-set before any Tesseract call to stop Tesseract's own internal
OpenMP threading from oversubscribing on top of the worker pool --
without this, 6 workers were measured 5x SLOWER than sequential from CPU
contention). Download is bandwidth-bound, where MORE concurrency doesn't
add throughput -- it just splits the same pipe more ways, making each
individual download slower. Confirmed live: one ~300MB outlier file took
141.9s to download alone but repeatedly blew past a 400s hard_timeout once
8 concurrent downloads (an earlier single-pool design) were sharing
bandwidth. Download workers therefore default to 1 (fully serial); OCR
workers default to 8, fully decoupled from the network once bytes are in
memory. See run_embed_phase's docstring for the full reasoning.

Only the embedding call itself runs on the main thread, one at a time --
cheap, and SentenceTransformer isn't guaranteed safe for concurrent
.encode() calls from multiple threads sharing one model instance.

Both phases checkpoint (full rewrite) every 20 newly-processed items.

Usage:
    .venv/bin/python3 scripts/hash_and_embed_report_true.py
    .venv/bin/python3 scripts/hash_and_embed_report_true.py --limit 5   # smoke test, both phases
    .venv/bin/python3 scripts/hash_and_embed_report_true.py --hash-workers 8 --download-workers 1 --ocr-workers 8
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import queue
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # must be set before tesserocr's first call

import fitz  # PyMuPDF
import pytesseract
from dotenv import load_dotenv
from PIL import Image
from tesserocr import RIL, PyTessBaseAPI

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT))

from helpers.zoho_workdrive import ZohoWorkDrive  # noqa: E402
from scripts.ocr_layout_common import (  # noqa: E402
    IMAGE_BLOCK_TYPES,
    IGNORE_BLOCK_TYPES,
    MIN_BLOCK_DIM_PX,
    TABLE_BLOCK_TYPES,
    _drop_blocks_nested_in_image,
)

REPORT_TRUE_CSV = REPO_ROOT / "results" / "state_language_report" / "report_true.csv"
OUT_JSON = REPO_ROOT / "results" / "report_true_embeddings.json"
TESSDATA_PATH = str(REPO_ROOT / "tessdata_best")
EMBED_MODEL_NAME = "intfloat/multilingual-e5-base"
DPI = 300
CHECKPOINT_EVERY = 5
FILE_ID_RE = re.compile(r"/file/([A-Za-z0-9]+)")

STATE_LANG = {
    "Central Advisories": "hin",
    "State  Jammu and Kashmir": "urd",
    "State Andaman and Nicobar": "hin",
    "State Andhra Pradesh": "tel",
    "State Arunachal Pradesh": "eng",
    "State Assam": "asm",
    "State Bihar": "hin",
    "State Chattisgarh": "hin",
    "State Delhi": "hin",
    "State Goa": "mar",
    "State Gujarat": "guj",
    "State Haryana": "hin",
    "State Himachal Pradesh": "hin",
    "State Jharkhand": "hin",
    "State Karnataka": "kan",
    "State Kerala": "mal",
    "State Madhya Pradesh": "hin",
    "State Maharashtra": "mar",
    "State Manipur": "eng",
    "State Meghalaya": "eng",
    "State Mizoram": "eng",
    "State Nagaland": "eng",
    "State Odisha": "ori",
    "State Puducherry": "tam",
    "State Punjab": "pan",
    "State Rajasthan": "hin",
    "State Sikkim": "nep",
    "State Tamilnadu": "tam",
    "State Telangana": "tel",
    "State Tripura": "ben",
    "State Uttar Pradesh": "hin",
    "State Uttarakhand": "hin",
    "State West Bengal": "ben",
}


def extract_file_id(permalink: str) -> str | None:
    m = FILE_ID_RE.search(permalink or "")
    return m.group(1) if m else None


def ocr_lang_for_row(row: dict) -> str:
    if (row.get("language") or "").strip() == "English":
        return "eng"
    lang = STATE_LANG.get(row["state"])
    if lang is None:
        print(f"    [WARNING] unmapped state '{row['state']}' -- falling back to eng.")
        return "eng"
    return lang


def compute_sha256(wd: ZohoWorkDrive, file_id: str) -> str:
    resp = wd.download_file_stream(file_id)
    try:
        h = hashlib.sha256()
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                h.update(chunk)
        return h.hexdigest()
    finally:
        resp.close()


def render_middle_page(pdf_bytes: bytes) -> Image.Image:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        if len(doc) == 0:
            raise ValueError("PDF has 0 pages (malformed/corrupt file)")
        mid = (len(doc) - 1) // 2
        pix = doc[mid].get_pixmap(dpi=DPI)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def ocr_transcript(img: Image.Image, ocr_lang: str) -> str:
    tesseract_lang = f"{ocr_lang}+eng"
    raw_blocks = []
    with PyTessBaseAPI(path=TESSDATA_PATH, lang=tesseract_lang) as api:
        api.SetImage(img)
        api.Recognize()
        it = api.AnalyseLayout()
        while it and not it.Empty(RIL.BLOCK):
            bbox = it.BoundingBox(RIL.BLOCK)
            btype = it.BlockType()
            if bbox and btype not in IGNORE_BLOCK_TYPES:
                x1, y1, x2, y2 = bbox
                if (x2 - x1) >= MIN_BLOCK_DIM_PX and (y2 - y1) >= MIN_BLOCK_DIM_PX:
                    kind = "image" if btype in IMAGE_BLOCK_TYPES else "table" if btype in TABLE_BLOCK_TYPES else "text"
                    raw_blocks.append((bbox, kind))
            if not it.Next(RIL.BLOCK):
                break
    raw_blocks = _drop_blocks_nested_in_image(raw_blocks)

    parts = []
    for bbox, _kind in raw_blocks:
        text = pytesseract.image_to_string(
            img.crop(bbox), lang=tesseract_lang, config=f'--tessdata-dir "{TESSDATA_PATH}"',
        ).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_embeddings(path: Path) -> dict[str, list[float]]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def write_embeddings(path: Path, data: dict[str, list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def run_hash_phase(wd: ZohoWorkDrive, rows: list[dict], fieldnames: list[str], workers: int, limit: int | None) -> float:
    t0 = time.time()
    if "sha" not in fieldnames:
        fieldnames.append("sha")
        for row in rows:
            row["sha"] = ""

    rows_by_name: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_name.setdefault(row["pdf"], []).append(row)

    todo_names = []
    for name, grp in rows_by_name.items():
        existing_sha = next((r["sha"] for r in grp if r["sha"]), "")
        if existing_sha:
            for r in grp:
                r["sha"] = existing_sha
        else:
            todo_names.append(name)

    if limit is not None:
        todo_names = todo_names[:limit]

    print(f"[hash] {len(rows_by_name)} unique name(s), {len(todo_names)} to process this run "
          f"({workers} worker(s))")

    def task(name: str) -> tuple[str, str | None, str | None]:
        row0 = rows_by_name[name][0]
        file_id = extract_file_id(row0.get("doc_link", ""))
        if not file_id:
            return name, None, f"no file id in doc_link {row0.get('doc_link')!r}"
        try:
            return name, compute_sha256(wd, file_id), None
        except Exception as e:
            return name, None, str(e)

    hashed = failed = since_checkpoint = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(task, name): name for name in todo_names}
        for i, future in enumerate(as_completed(futures), start=1):
            name, sha, error = future.result()
            if error:
                print(f"[hash] [{i}/{len(todo_names)}] [WARNING] '{name}': {error} -- will retry next run.")
                failed += 1
                continue
            for row in rows_by_name[name]:
                row["sha"] = sha
            print(f"[hash] [{i}/{len(todo_names)}] {name} -> {sha}", flush=True)
            hashed += 1
            since_checkpoint += 1
            if since_checkpoint >= CHECKPOINT_EVERY:
                write_csv_rows(REPORT_TRUE_CSV, fieldnames, rows)
                since_checkpoint = 0

    write_csv_rows(REPORT_TRUE_CSV, fieldnames, rows)
    elapsed = time.time() - t0
    print(f"[hash] done: {hashed} newly hashed, {failed} failed, {elapsed:.1f}s "
          f"({elapsed / hashed:.2f}s/doc)" if hashed else f"[hash] done: nothing new, {elapsed:.1f}s")
    return elapsed


def download_task(wd: ZohoWorkDrive, row: dict) -> bytes:
    file_id = extract_file_id(row.get("doc_link", ""))
    if not file_id:
        raise ValueError(f"no file id in doc_link {row.get('doc_link')!r}")
    return wd.download_file(file_id)


def ocr_task(pdf_bytes: bytes, row: dict) -> str:
    ocr_lang = ocr_lang_for_row(row)
    img = render_middle_page(pdf_bytes)
    return ocr_transcript(img, ocr_lang)


def run_embed_phase(wd: ZohoWorkDrive, rows: list[dict], download_workers: int, ocr_workers: int, limit: int | None) -> float:
    """Download and OCR run on two separate thread pools, chained via
    callbacks: a download finishing hands its bytes straight to the OCR
    pool, whose result lands on `result_queue` for the main thread (which
    also owns the embedding model and all disk writes) to consume.

    This split exists because the two stages have opposite concurrency
    needs: OCR is CPU-bound and scales with worker count up to the core
    count (measured: 8 workers is the sweet spot on this machine). Download
    concurrency ACROSS DIFFERENT FILES doesn't help and can actively hurt --
    confirmed live: one ~300MB outlier file took 141.9s to download
    completely alone, but repeatedly blew past a 400s hard_timeout once 8
    concurrent downloads of DIFFERENT files (the old single-pool design)
    were sharing bandwidth/contending for the same physical connection.
    Download workers therefore default to 1 (fully serial across files).

    That's separate from parallelism WITHIN one file's download, which DOES
    help a lot: ZohoWorkDrive.download_file() now Range-chunks any file over
    20MB into 8 concurrent requests internally (see its docstring) --
    confirmed ~4.3x faster in aggregate for that same 300MB file (~140s on
    one connection -> ~30s with 8 parallel chunks, reproduced twice). Since
    that already uses 8 connections for one file, running more than 1 file
    at a time at this outer level would just recreate the same
    over-subscription problem this design fixed in the first place.
    """
    t0 = time.time()
    row_by_sha: dict[str, dict] = {}
    for row in rows:
        sha = row.get("sha", "")
        if sha:
            row_by_sha.setdefault(sha, row)  # first row seen for this sha is representative

    embeddings = load_embeddings(OUT_JSON)
    todo_shas = [sha for sha in row_by_sha if sha not in embeddings]
    if limit is not None:
        todo_shas = todo_shas[:limit]

    print(f"[embed] {len(row_by_sha)} unique sha(s), {len(embeddings)} already embedded, "
          f"{len(todo_shas)} to process this run ({download_workers} download worker(s), "
          f"{ocr_workers} OCR worker(s))")
    if not todo_shas:
        return time.time() - t0

    print(f"[embed] loading {EMBED_MODEL_NAME}...")
    import torch
    torch.set_num_threads(1)
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(EMBED_MODEL_NAME, device="cpu")

    result_queue: "queue.Queue[tuple[str, str, str | None]]" = queue.Queue()

    processed = failed = since_checkpoint = 0
    with ThreadPoolExecutor(max_workers=download_workers, thread_name_prefix="download") as dl_exec, \
            ThreadPoolExecutor(max_workers=ocr_workers, thread_name_prefix="ocr") as ocr_exec:

        def on_ocr_done(fut, sha: str) -> None:
            try:
                result_queue.put((sha, fut.result(), None))
            except Exception as e:
                result_queue.put((sha, "", str(e)))

        def on_download_done(fut, sha: str, row: dict) -> None:
            try:
                pdf_bytes = fut.result()
            except Exception as e:
                result_queue.put((sha, "", str(e)))
                return
            ocr_fut = ocr_exec.submit(ocr_task, pdf_bytes, row)
            ocr_fut.add_done_callback(lambda f, sha=sha: on_ocr_done(f, sha))

        for sha in todo_shas:
            row = row_by_sha[sha]
            dl_fut = dl_exec.submit(download_task, wd, row)
            dl_fut.add_done_callback(lambda f, sha=sha, row=row: on_download_done(f, sha, row))

        for i in range(1, len(todo_shas) + 1):
            sha, transcript, error = result_queue.get()
            if error:
                print(f"[embed] [{i}/{len(todo_shas)}] [WARNING] {sha}: {error} -- will retry next run.")
                failed += 1
                continue

            vector = model.encode(f"passage: {transcript}" if transcript else "passage: ", convert_to_numpy=True)
            embeddings[sha] = [round(float(x), 6) for x in vector]
            print(f"[embed] [{i}/{len(todo_shas)}] {sha} ({len(transcript)} chars)", flush=True)
            processed += 1
            since_checkpoint += 1
            if since_checkpoint >= CHECKPOINT_EVERY:
                write_embeddings(OUT_JSON, embeddings)
                since_checkpoint = 0

    write_embeddings(OUT_JSON, embeddings)
    elapsed = time.time() - t0
    print(f"[embed] done: {processed} newly embedded, {failed} failed, {elapsed:.1f}s "
          f"({elapsed / processed:.2f}s/doc)" if processed else f"[embed] done: nothing new, {elapsed:.1f}s")
    return elapsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Only process this many NEW items per phase (smoke test).")
    parser.add_argument("--hash-workers", type=int, default=8)
    parser.add_argument("--download-workers", type=int, default=1,
                         help="Concurrent PDF downloads in the embed phase (default 1 -- serial, avoids "
                              "self-inflicted bandwidth contention on large files; see run_embed_phase docstring).")
    parser.add_argument("--ocr-workers", type=int, default=8,
                         help="Concurrent OCR workers in the embed phase (CPU-bound, default 8 -- matches core count).")
    args = parser.parse_args()

    if not REPORT_TRUE_CSV.exists():
        print(f"Missing {REPORT_TRUE_CSV}", file=sys.stderr)
        sys.exit(1)

    with REPORT_TRUE_CSV.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    wd = ZohoWorkDrive()

    hash_elapsed = run_hash_phase(wd, rows, fieldnames, args.hash_workers, args.limit)
    embed_elapsed = run_embed_phase(wd, rows, args.download_workers, args.ocr_workers, args.limit)

    print(f"\nTotal this run: {hash_elapsed + embed_elapsed:.1f}s "
          f"(hash {hash_elapsed:.1f}s + embed {embed_elapsed:.1f}s)")


if __name__ == "__main__":
    main()
