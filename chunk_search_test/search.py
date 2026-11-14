"""Query -> ranked documents (and pages) by how many of their chunks match.

    python chunk_search_test/search.py create-index          # Atlas vector index (once)
    python chunk_search_test/search.py "your question" [-k 50] [--local]

Library use:  search(query, k=50, local=False) -> list of documents, best first:
    {"doc_id", "name", "link", "matches", "score_sum", "score_max", "pages": [page, ...],
    "page_hits": [{"page", "score", "snippet"}, ...]}  # best page first

rank="top5" (default) orders by the mean score of a document's 5 best chunks (k=500 pool);
rank="count" orders by matching chunks (k=50); rank="best" orders by the single best chunk score.
Ranking: take the K nearest chunks, group them by document, order by number of
matching chunks, ties broken by summed similarity. `local=True` does the same
exact search in numpy instead of Atlas (for checking the index against ground truth).
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from db import EMBEDDINGS, UNIQUE_DOCS, get_db  # noqa: E402
from embedder import DIMS, embed_query  # noqa: E402

INDEX_NAME = "chunk_vector_index"


def create_index(db) -> None:
    from pymongo.operations import SearchIndexModel
    model = SearchIndexModel(
        name=INDEX_NAME, type="vectorSearch",
        definition={"fields": [{"type": "vector", "path": "embedding",
                                "numDimensions": DIMS, "similarity": "cosine"}]})
    if any(i["name"] == INDEX_NAME for i in db[EMBEDDINGS].list_search_indexes()):
        print("index already exists")
        return
    db[EMBEDDINGS].create_search_index(model)
    print("index requested; building (poll until queryable)")
    while True:
        idx = next(i for i in db[EMBEDDINGS].list_search_indexes() if i["name"] == INDEX_NAME)
        if idx.get("queryable"):
            print("index ready")
            return
        time.sleep(5)


def _nearest_atlas(db, qvec, k: int) -> list[dict]:
    return list(db[EMBEDDINGS].aggregate([
        {"$vectorSearch": {"index": INDEX_NAME, "path": "embedding", "queryVector": qvec,
                           "numCandidates": max(k * 10, 100), "limit": k}},
        {"$project": {"_id": 1, "document_id": 1, "page": 1, "chunk_index": 1,
                      "score": {"$meta": "vectorSearchScore"}}}]))


_local_cache = None


def _nearest_local(db, qvec, k: int) -> list[dict]:
    import numpy as np
    global _local_cache
    if _local_cache is None:
        rows = list(db[EMBEDDINGS].find({}, {"_id": 1, "document_id": 1, "page": 1, "chunk_index": 1,
                                             "embedding": 1}))
        _local_cache = (rows, np.array([r["embedding"] for r in rows], dtype="float32"))
    rows, mat = _local_cache
    sims = mat @ np.array(qvec, dtype="float32")
    top = np.argsort(-sims)[:k]
    return [{"_id": rows[i]["_id"], "document_id": rows[i]["document_id"], "page": rows[i]["page"],
             "chunk_index": rows[i]["chunk_index"], "score": float(sims[i])} for i in top]


TOP_N = 5           # chunks per document averaged by rank="top5"
TOP5_K = 500       # chunks fetched for it: a document's 5 best must be inside the pool


def search(query: str, k: int | None = None, local: bool = False, db=None,
           rank: str = "top5") -> list[dict]:
    db = get_db() if db is None else db
    k = k or (TOP5_K if rank == "top5" else 50)
    qvec = embed_query(query)
    hits = (_nearest_local if local else _nearest_atlas)(db, qvec, k)
    texts = {t["_id"]: t["text"] for t in
             db[EMBEDDINGS].find({"_id": {"$in": [h["_id"] for h in hits]}}, {"text": 1})}
    groups: dict[str, dict] = defaultdict(lambda: {"matches": 0, "score_sum": 0.0,
                                                   "score_max": 0.0, "pages": {}, "snips": {},
                                                   "scores": []})
    for h in hits:
        g = groups[h["document_id"]]
        if h["score"] >= g["pages"].get(h["page"], 0):
            g["snips"][h["page"]] = texts[h["_id"]]
        g["matches"] += 1
        g["scores"].append(h["score"])
        g["score_sum"] += h["score"]
        g["score_max"] = max(g["score_max"], h["score"])
        g["pages"][h["page"]] = max(g["pages"].get(h["page"], 0), h["score"])
    names = {d["_id"]: d for d in db[UNIQUE_DOCS].find({"_id": {"$in": list(groups)}})}
    # A document with fewer than TOP_N chunks in the pool is padded with the pool's lowest
    # score: any chunk not retrieved scored at most that, so the mean is an upper bound and
    # a document with one lucky chunk cannot beat one with five good ones.
    floor = min(h["score"] for h in hits) if hits else 0.0
    ranked = []
    for doc_id, g in groups.items():
        top = sorted(g["scores"], reverse=True)[:TOP_N]
        top_mean = (sum(top) + floor * (TOP_N - len(top))) / TOP_N
        ranked.append({"top5_mean": round(top_mean, 4), "doc_id": doc_id, "name": names[doc_id]["name"], "link": names[doc_id]["link"],
                       "matches": g["matches"], "score_sum": round(g["score_sum"], 4),
                       "score_max": round(g["score_max"], 4),
                       "pages": sorted(g["pages"], key=lambda p: -g["pages"][p]),
                       "page_hits": [{"page": p, "score": round(g["pages"][p], 4),
                                      "snippet": g["snips"][p]}
                                     for p in sorted(g["pages"], key=lambda p: -g["pages"][p])]})
    if rank == "top5":
        ranked.sort(key=lambda r: (-r["top5_mean"], -r["matches"]))
    elif rank == "best":
        ranked.sort(key=lambda r: (-r["score_max"], -r["matches"]))
    else:
        ranked.sort(key=lambda r: (-r["matches"], -r["score_sum"]))
    return ranked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help='a question, or "create-index"')
    ap.add_argument("-k", type=int, default=None)
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--rank", choices=["top5", "count", "best"], default="top5")
    ap.add_argument("-n", type=int, default=10, help="documents to show")
    args = ap.parse_args()
    if args.query == "create-index":
        create_index(get_db())
        return
    for i, r in enumerate(search(args.query, args.k, args.local, rank=args.rank)[: args.n], 1):
        print(f"{i:2}. top5 {r['top5_mean']:.3f} {r['matches']:3} chunks  sum {r['score_sum']:.2f}  max {r['score_max']:.3f}  "
              f"pages {r['pages'][:6]}  {r['name'][:70]}")


if __name__ == "__main__":
    main()
