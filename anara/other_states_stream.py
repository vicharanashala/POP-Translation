"""Other states' documents, streamed: download -> text check -> keep page text only -> delete the PDF.

    python anara/other_states_stream.py [--limit N] [--states "Haryana,Keralam"] [--workers 4]
Reads data/other_state_docs.csv (other_state_docs.py). Per document, in one worker: download to
data/other_states/tmp (never to data/pdfs), textcheck.check, and for an english_text document the page text and
heading rows go to the pagetext store (pagetext.py); the file is deleted at once. Verdict rows go to
data/other_states/text_report_other.csv (same columns as text_report.csv; other_states_finish.sh merges them in
at the swap, so the live text_report.csv is not touched while the server runs). Non-pdf formats are recorded as
not_pdf without a download. Documents without extractable text (scanned) or not English are recorded and not
stored: no OCR, no translation. Resumable: ids in data/other_states/done.txt are skipped; a failed download (3 tries,
backoff) is logged to failed.txt and not marked done, so the next run retries it. Stops when free disk < MIN_FREE.
Log: data/other_states/stream.log (run it detached).
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
import headings  # noqa: E402
import pagetext  # noqa: E402
from central_ingest import COLS  # noqa: E402
from textcheck import check, extract_pages  # noqa: E402

MIN_FREE = 3 * 1024 ** 3
OUTDIR = common.DATA / "other_states"
TMP = OUTDIR / "tmp"
REPORT = OUTDIR / "text_report_other.csv"
DONE = OUTDIR / "done.txt"
FAILED = OUTDIR / "failed.txt"
DOCS = common.DATA / "other_state_docs.csv"
lock = threading.Lock()
stop = threading.Event()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--states", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--docs", default=str(DOCS), help="csv of documents (default other_state_docs.csv; central_adv_docs.csv for the Central Advisories ones)")
    a = ap.parse_args()
    OUTDIR.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(exist_ok=True)
    for f in TMP.glob("*"):     # leftovers of a crashed run
        f.unlink()
    docs = pd.read_csv(a.docs, keep_default_na=False)
    if a.states:
        want = set(a.states.split(","))
        docs = docs[docs["states"].map(lambda s: bool(want & set(json.loads(s))))]
    done = set(DONE.read_text().split()) if DONE.exists() else set()
    todo = [r for r in docs.itertuples() if r.doc_id not in done]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(todo)} documents to do ({len(done)} done); free {shutil.disk_usage(common.DATA).free / 1e9:.1f} GB", flush=True)
    wd = common.zoho()
    new = not REPORT.exists()
    counts: dict[str, int] = {}
    n = {"i": 0, "failed": 0}
    t0 = time.time()

    def record(r, row, ok=True) -> None:
        with lock:
            if ok:
                with REPORT.open("a", newline="") as f:
                    w = csv.DictWriter(f, COLS, extrasaction="ignore")
                    if new and REPORT.stat().st_size == 0:
                        w.writeheader()
                    w.writerow(row)
                DONE.open("a").write(r.doc_id + "\n")
                v = row["verdict"].split(":")[0]
                counts[v] = counts.get(v, 0) + 1
            else:
                n["failed"] += 1
                FAILED.open("a").write(f"{r.doc_id}\t{row}\n")
                print("FAILED", r.doc_id, row, flush=True)
            n["i"] += 1
            if n["i"] % 25 == 0 or n["i"] == len(todo):
                el = time.time() - t0
                print(f"[{n['i']}/{len(todo)}] {counts} failed={n['failed']} {n['i'] / el * 60:.1f} docs/min "
                      f"free={shutil.disk_usage(common.DATA).free / 1e9:.1f}GB", flush=True)

    def work(r) -> None:
        if stop.is_set():
            return
        base = {"doc_id": r.doc_id, "name": r.name, "fmt": r.fmt, "catalogued_lang": r.language}
        if r.fmt != "pdf":
            return record(r, {**base, "verdict": "not_pdf"})
        free = shutil.disk_usage(common.DATA).free
        if free < MIN_FREE:
            if not stop.is_set():
                print(f"STOP: free {free / 1e9:.2f} GB < {MIN_FREE / 1e9:.0f} GB", flush=True)
            stop.set()
            return
        p = TMP / f"{r.doc_id}.pdf"
        err = ""
        try:
            for attempt in range(3):
                try:
                    p.write_bytes(wd.download_file(r.file_id))
                    err = ""
                    break
                except Exception as e:
                    err = repr(e)[:150]
                    time.sleep(4 * 2 ** attempt)
            if err:
                return record(r, err, ok=False)
            row = {**base, **check(p, r.fmt)}
            if row["verdict"] == "english_text":
                pages = extract_pages(p)
                heads = [[h["page"], h["text"], h["ratio"], h["bold"], h["y"]] for h in headings.extract(p)]
                pagetext.save(r.doc_id, pages, heads)
            record(r, row)
        except Exception as e:      # one bad file must not stop the run; it is retried next time
            record(r, repr(e)[:150], ok=False)
        finally:
            p.unlink(missing_ok=True)

    with ThreadPoolExecutor(a.workers) as pool:
        list(pool.map(work, todo))
    print("done", counts, "failed", n["failed"], f"{(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
