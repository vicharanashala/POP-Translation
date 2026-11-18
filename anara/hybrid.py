"""Keyword-narrowed embedding search ("blast radius").

    search_hybrid(query) -> same result shape as search.search(), plus "mode" and "radius"

Steps:
  1. query words (keywords.tokenize) -> vocabulary words (keywords.fuzzy_match), keeping only
     words present in the keyword index
  2. their (document, page) lists are unioned and each page widened by +/- RADIUS pages inside
     the same document: the blast radius
  3. the query is embedded and scored exactly (numpy) against only the chunks on those pages;
     documents are ranked by the mean of their TOP_N best chunks
  4. no query word in the index -> falls back to the full search (mode "full-fallback")

All chunk vectors (about 43 MB) are cached in data/chunk_matrix.npz and held in memory.
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
import keywords as kw  # noqa: E402
from db import EMBEDDINGS, UNIQUE_DOCS, get_db  # noqa: E402
from embedder import embed_query  # noqa: E402

RADIUS = 2
TOP_N = 5
CACHE = common.DATA / "chunk_matrix.npz"
_state: dict = {}


def _load() -> dict:
    if _state:
        return _state
    if CACHE.exists():
        z = np.load(CACHE, allow_pickle=True)
        ids, docs, pages, mat = list(z["ids"]), list(z["docs"]), z["pages"], z["mat"]
    else:
        rows = list(get_db()[EMBEDDINGS].find({}, {"document_id": 1, "page": 1, "embedding": 1}))
        ids = [str(r["_id"]) for r in rows]
        docs = [r["document_id"] for r in rows]
        pages = np.array([r["page"] for r in rows], dtype="int32")
        mat = np.array([r["embedding"] for r in rows], dtype="float32")
        np.savez(CACHE, ids=np.array(ids), docs=np.array(docs), pages=pages, mat=mat)
    doc_names = sorted(set(docs))
    doc_no = {d: i for i, d in enumerate(doc_names)}
    _state.update(ids=ids, docs=docs, pages=pages, mat=mat, doc_names=doc_names, doc_no=doc_no,
                  key=np.array([doc_no[d] for d in docs], dtype="int64") * 100000 + pages,
                  index=kw.load_index())
    _state["vocab"] = list(_state["index"])
    return _state


def query_keywords(query: str) -> dict[str, list[str]]:
    """{query word: [index words it stands for]}; words with no match are left out."""
    s = _load()
    out = {}
    for w in dict.fromkeys(kw.tokenize(query)):
        m = kw.fuzzy_match(w, s["vocab"])
        if m:
            out[w] = m
    return out


def blast_radius(matched: dict[str, list[str]], radius: int = RADIUS) -> set[tuple[str, int]]:
    s = _load()
    hit = set()
    for forms in matched.values():
        for f in forms:
            for doc, pages in s["index"][f].items():
                for p in pages:
                    hit.update((doc, q) for q in range(max(1, p - radius), p + radius + 1))
    return hit


def search_hybrid(query: str, radius: int = RADIUS, db=None, fallback=None) -> list[dict]:
    from bson import ObjectId
    s = _load()
    db = get_db() if db is None else db
    matched = query_keywords(query)
    if not matched:
        from search import search as full_search
        res = (fallback or full_search)(query, db=db)
        for r in res:
            r["mode"], r["radius"] = "full-fallback", None
        return res
    cells = blast_radius(matched, radius)
    keys = np.array([s["doc_no"][d] * 100000 + p for d, p in cells if d in s["doc_no"]], dtype="int64")
    idx = np.nonzero(np.isin(s["key"], keys))[0]
    qvec = np.array(embed_query(query), dtype="float32")
    sims = s["mat"][idx] @ qvec
    floor = float(sims.min()) if len(sims) else 0.0
    by_doc: dict[str, list] = defaultdict(list)
    for i, sc in zip(idx, sims):
        by_doc[s["docs"][i]].append((float(sc), int(s["pages"][i]), s["ids"][i]))
    top_ids = [ObjectId(max(v)[2]) for v in by_doc.values()]
    texts = {t["_id"]: t["text"] for t in db[EMBEDDINGS].find({"_id": {"$in": top_ids}}, {"text": 1})}
    names = {d["_id"]: d for d in db[UNIQUE_DOCS].find({"_id": {"$in": list(by_doc)}})}
    out = []
    for doc, v in by_doc.items():
        v.sort(reverse=True)
        top = [x[0] for x in v[:TOP_N]]
        mean = (sum(top) + floor * (TOP_N - len(top))) / TOP_N
        page_best: dict[int, float] = {}
        for sc, p, _ in v:
            page_best[p] = max(page_best.get(p, 0.0), sc)
        order = sorted(page_best, key=lambda p: -page_best[p])
        best_id = ObjectId(v[0][2])
        out.append({"top5_mean": round(mean, 4), "doc_id": doc, "name": names[doc]["name"],
                    "link": names[doc]["link"], "matches": len(v), "score_sum": round(sum(x[0] for x in v), 4),
                    "score_max": round(v[0][0], 4), "pages": order,
                    "page_hits": [{"page": p, "score": round(page_best[p], 4),
                                   "snippet": texts.get(best_id, "") if p == v[0][1] else ""}
                                  for p in order],
                    "mode": "keyword-radius", "radius": len(idx)})
    out.sort(key=lambda r: (-r["top5_mean"], -r["matches"]))
    return out


if __name__ == "__main__":
    import time
    q = " ".join(sys.argv[1:]) or "How do I control pink bollworm in cotton?"
    t = time.time()
    m = query_keywords(q)
    print({k: v[:6] for k, v in m.items()})
    for r in search_hybrid(q)[:8]:
        print(f"{r['top5_mean']:.3f} {r['mode']} chunks_in_radius={r['radius']} pages={r['pages'][:4]} {r['name'][:60]}")
    print(f"{time.time() - t:.1f}s")
