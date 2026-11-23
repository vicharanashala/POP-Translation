"""Catalogued hin/mar central PDFs, streamed: download -> text check -> keep text only -> delete the PDF.

    python anara/central_stream.py [BATCH]
Documents already in text_report.csv are skipped (resumable). Rows are appended to text_report.csv; for an
english_text document the page text and heading lines go to data/pagetext (pagetext.py). The PDF is deleted
afterwards if this script downloaded it (ids listed in data/stream_downloaded.txt); a PDF that was already on
disk stays. Stops when free disk space is below MIN_FREE.
"""
from __future__ import annotations

import csv
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
import headings  # noqa: E402
import pagetext  # noqa: E402
from central_ingest import COLS, REPORT  # noqa: E402
from textcheck import check, extract_pages  # noqa: E402

MIN_FREE = 3 * 1024 ** 3
MANIFEST = common.DATA / "stream_downloaded.txt"


def main() -> None:
    batch = int(sys.argv[1]) if len(sys.argv) > 1 else 16
    docs = pd.read_csv(common.CENTRAL, keep_default_na=False)
    docs = docs[(docs.fmt == "pdf") & docs.language.isin(["hin", "mar"])]
    seen = set(pd.read_csv(REPORT)["doc_id"]) | set(common.load_documents(central=False)["doc_id"])
    todo = list(docs[~docs.doc_id.isin(seen)].itertuples())
    mine = set(MANIFEST.read_text().split()) if MANIFEST.exists() else set()
    print(f"{len(todo)} documents to do; free {shutil.disk_usage(common.DATA).free / 1e9:.1f} GB", flush=True)
    wd = common.zoho()
    common.PDF_DIR.mkdir(parents=True, exist_ok=True)

    def fetch(r):
        p = common.pdf_path(r.doc_id)
        if p.exists() and p.stat().st_size:
            return r.doc_id, None
        err = ""
        for attempt in range(3):
            try:
                p.write_bytes(wd.download_file(r.file_id))
                return r.doc_id, None
            except Exception as e:
                err = repr(e)[:150]
                time.sleep(2 * (attempt + 1))
        p.unlink(missing_ok=True)
        return r.doc_id, err

    counts: dict[str, int] = {}
    failed = 0
    for i in range(0, len(todo), batch):
        free = shutil.disk_usage(common.DATA).free
        if free < MIN_FREE:
            print(f"STOP: free {free / 1e9:.2f} GB < {MIN_FREE / 1e9:.0f} GB", flush=True)
            break
        part = todo[i:i + batch]
        with MANIFEST.open("a") as f:     # recorded before the download: a crash leaves them deletable
            for r in part:
                if not (common.pdf_path(r.doc_id).exists()):
                    f.write(r.doc_id + "\n")
                    mine.add(r.doc_id)
        with ThreadPoolExecutor(4) as pool:
            res = dict(pool.map(fetch, part))
        rows = []
        for r in part:
            p = common.pdf_path(r.doc_id)
            if res[r.doc_id]:
                failed += 1
                print("FAILED", r.doc_id, res[r.doc_id], flush=True)
                continue
            row = {"doc_id": r.doc_id, "name": r.name, "fmt": r.fmt, "catalogued_lang": r.language, **check(p, r.fmt)}
            if row["verdict"] == "english_text":
                pages = extract_pages(p)
                heads = [[h["page"], h["text"], h["ratio"], h["bold"], h["y"]] for h in headings.extract(p)]
                pagetext.save(r.doc_id, pages, heads)
            rows.append(row)
            counts[row["verdict"].split(":")[0]] = counts.get(row["verdict"].split(":")[0], 0) + 1
            if r.doc_id in mine:
                p.unlink(missing_ok=True)
        with open(REPORT, "a", newline="") as f:
            w = csv.DictWriter(f, COLS, extrasaction="ignore")
            for row in rows:
                w.writerow(row)
        print(f"[{min(i + batch, len(todo))}/{len(todo)}] {counts} failed={failed} free={shutil.disk_usage(common.DATA).free / 1e9:.1f}GB", flush=True)
    print("done", counts, "failed", failed)


if __name__ == "__main__":
    main()
