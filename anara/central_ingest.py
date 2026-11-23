"""Central documents: download, text check, append to text_report.csv.

    python anara/central_ingest.py eng            catalogued-eng pdf central documents
    python anara/central_ingest.py hin-sample [N] random N (default 60) catalogued-hin pdf documents, seed 0
Idempotent: files on disk are not downloaded again, documents already in text_report.csv are not
rechecked. Existing report rows are never rewritten. Index afterwards with ingest.py, central_bin.py,
then rebuild the keyword and zone files (see INDEX.md).
"""
from __future__ import annotations

import csv
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
from textcheck import check  # noqa: E402

REPORT = common.DATA / "text_report.csv"
COLS = ["doc_id", "name", "fmt", "catalogued_lang", "pdf_pages", "pages_text", "chars", "latin_ratio", "lang", "verdict"]


def main() -> None:
    group = sys.argv[1]
    docs = pd.read_csv(common.CENTRAL, keep_default_na=False)
    docs = docs[docs.fmt == "pdf"]
    if group == "eng":
        pick = docs[docs.language == "eng"]
    else:
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        hin = docs[docs.language == "hin"]
        pick = hin.iloc[sorted(random.Random(0).sample(range(len(hin)), n))]
    seen = set(pd.read_csv(REPORT)["doc_id"])
    sheet = set(common.load_documents(central=False)["doc_id"])
    pick = pick[~pick.doc_id.isin(seen | sheet)]
    print(f"{len(pick)} documents to do", flush=True)
    common.PDF_DIR.mkdir(parents=True, exist_ok=True)
    wd = common.zoho()

    def fetch(r):
        p = common.pdf_path(r.doc_id)
        if p.exists() and p.stat().st_size:
            return r.doc_id, None
        for attempt in range(3):
            try:
                p.write_bytes(wd.download_file(r.file_id))
                return r.doc_id, None
            except Exception as e:
                err = repr(e)[:150]
                time.sleep(2 * (attempt + 1))
        p.unlink(missing_ok=True)
        return r.doc_id, err

    failed = {}
    rows = []
    with ThreadPoolExecutor(3) as pool:
        for i, (doc_id, err) in enumerate(pool.map(fetch, list(pick.itertuples())), 1):
            if err:
                failed[doc_id] = err
                print("FAILED", doc_id, err, flush=True)
            if i % 25 == 0:
                print(f"[{i}/{len(pick)}] downloaded", flush=True)
    for r in pick.itertuples():
        if r.doc_id in failed:
            continue
        rows.append({"doc_id": r.doc_id, "name": r.name, "fmt": r.fmt, "catalogued_lang": r.language,
                     **check(common.pdf_path(r.doc_id), r.fmt)})
    with open(REPORT, "a", newline="") as f:
        w = csv.DictWriter(f, COLS, extrasaction="ignore")
        for row in rows:
            w.writerow(row)
    out = pd.DataFrame(rows)
    print(f"{len(rows)} checked, {len(failed)} download failures")
    print(out["verdict"].value_counts().to_string())
    ok = out[out.verdict == "english_text"]
    print("english_text pages (num_pages sum):", int(pick[pick.doc_id.isin(ok.doc_id)].pages.sum()))
    print("catalogued pages sum of the whole selection:", int(pick.pages.sum()))


if __name__ == "__main__":
    main()
