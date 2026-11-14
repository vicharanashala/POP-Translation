"""Chunk, embed and store every english_text document in Atlas.

For each document marked english_text in data/text_report.csv:
    unique_documents  <- {_id: doc_id, name, link, pages, states, num_chunks}
    doc_embeddings    <- {document_id, page, chunk_index, text, embedding}
Idempotent: a document that already has embeddings is skipped; --redo deletes and rebuilds.
    python chunk_search_test/ingest.py [--limit N] [--redo]
"""
from __future__ import annotations

import argparse
import sys
import time

import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
from chunker import chunk_pages  # noqa: E402
from db import EMBEDDINGS, UNIQUE_DOCS, ensure_collections, get_db  # noqa: E402
from embedder import embed_passages  # noqa: E402
from textcheck import extract_pages  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    db = get_db()
    ensure_collections(db)
    docs = common.load_documents().set_index("doc_id")
    report = pd.read_csv(common.DATA / "text_report.csv")
    ids = report.loc[report.verdict == "english_text", "doc_id"].tolist()
    done = set() if args.redo else set(db[EMBEDDINGS].distinct("document_id"))
    todo = [i for i in ids if i not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(ids)} english docs, {len(done)} already stored, {len(todo)} to do", flush=True)

    t0 = time.time()
    for n, doc_id in enumerate(todo, 1):
        d = docs.loc[doc_id]
        chunks = chunk_pages(extract_pages(common.pdf_path(doc_id)))
        if not chunks:
            print(f"[{n}] {doc_id} no chunks, skipped")
            continue
        vecs = embed_passages([c["text"] for c in chunks])
        db[EMBEDDINGS].delete_many({"document_id": doc_id})
        db[EMBEDDINGS].insert_many([{"document_id": doc_id, **c, "embedding": v}
                                    for c, v in zip(chunks, vecs)])
        db[UNIQUE_DOCS].replace_one({"_id": doc_id}, {
            "_id": doc_id, "name": d["name"], "link": d["link"], "pages": int(d["pages"]),
            "states": d["states"], "num_chunks": len(chunks)}, upsert=True)
        if n % 10 == 0 or n == len(todo):
            print(f"[{n}/{len(todo)}] {time.time() - t0:.0f}s  {len(chunks)} chunks  {d['name'][:50]}",
                  flush=True)
    print("documents:", db[UNIQUE_DOCS].count_documents({}),
          "chunks:", db[EMBEDDINGS].count_documents({}))


if __name__ == "__main__":
    main()
