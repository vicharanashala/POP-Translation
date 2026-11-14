"""Download every unique document's file from Zoho into data/pdfs/<doc_id>.<fmt>.

Idempotent: files already on disk are skipped, so rerun freely after a failure.
    python anara/download_pdfs.py [--limit N]
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from common import PDF_DIR, load_documents, pdf_path, zoho  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    docs = load_documents()
    if args.limit:
        docs = docs.head(args.limit)
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    wd = zoho()
    todo = [r for r in docs.itertuples() if not pdf_path(r.doc_id, r.fmt).exists()]
    print(f"{len(docs)} documents, {len(todo)} to download")

    def fetch(r):
        try:
            data = wd.download_file(r.file_id)
            pdf_path(r.doc_id, r.fmt).write_bytes(data)
            return r.doc_id, len(data), None
        except Exception as e:  # keep going; failures are listed at the end
            return r.doc_id, 0, repr(e)[:200]

    failed = []
    with ThreadPoolExecutor(args.workers) as pool:
        for i, (doc_id, size, err) in enumerate(pool.map(fetch, todo), 1):
            if err:
                failed.append((doc_id, err))
            if i % 25 == 0 or err:
                print(f"[{i}/{len(todo)}] {doc_id} {size} {err or ''}", flush=True)
    print(f"done, {len(failed)} failed")
    for f in failed:
        print("FAILED", *f)


if __name__ == "__main__":
    main()
