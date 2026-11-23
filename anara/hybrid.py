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
    _state["vocab"] = set(_state["index"])
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


def _rank_chunks(query: str, idx, db, mode: str, info: dict) -> list[dict]:
    """Score the chunks at positions `idx` of the cached matrix exactly against the query and
    rank their documents by the mean of the TOP_N best chunks (shared by hybrid and restrict)."""
    from bson import ObjectId
    s = _load()
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
                    "mode": mode, "radius": len(idx), "keywords": info})
    out.sort(key=lambda r: (-r["top5_mean"], -r["matches"]))
    return out


def _doc_words() -> dict[str, set[str]]:
    """{word: {doc ids containing it anywhere}} from the raw per-page counts (no BM25
    threshold: 'contains' means the word is in the document at all). Built once, cached."""
    s = _load()
    if "doc_words" not in s:
        st = kw.build_stats()
        dw: dict[str, set[str]] = defaultdict(set)
        for doc, pages in st["pages"].items():
            for c in pages:
                for w in c:
                    dw[w].add(doc)
        s["doc_words"], s["all_words"] = dict(dw), set(st["df"])
    return s["doc_words"]


def keyword_groups(query: str) -> dict[str, list[str]]:
    """{query word: [every word of the corpus that is a suffix variant of it]}: one group per
    query word. A query word with no form in the corpus has no group."""
    dw = _doc_words()
    out = {}
    for w in dict.fromkeys(kw.tokenize(query)):
        forms = kw.fuzzy_match(w, _state["all_words"])
        if forms:
            out[w] = forms
    return out


def restrict_docs(groups: dict[str, list[str]]) -> tuple[set[str], dict[str, int]]:
    """Documents containing at least one form of EVERY group, and per-group document counts."""
    dw = _doc_words()
    per = {g: set().union(*(dw.get(f, set()) for f in forms)) for g, forms in groups.items()}
    docs = set.intersection(*per.values()) if per else set()
    return docs, {g: len(d) for g, d in per.items()}


def search_restrict(query: str, db=None, fallback=None) -> list[dict]:
    """Third mode. Keep only documents that contain, for every query keyword, at least one of
    its forms (any one form per group will do); then score ALL chunks of those documents
    exactly. No query keyword in the corpus -> full search (mode "full-fallback"). No document
    holds every group -> an empty list."""
    s = _load()
    db = get_db() if db is None else db
    groups = keyword_groups(query)
    info = {"used": groups, "unmatched": [w for w in dict.fromkeys(kw.tokenize(query)) if w not in groups]}
    if not groups:
        from search import search as full_search
        res = (fallback or full_search)(query, db=db)
        for r in res:
            r["mode"], r["radius"], r["keywords"] = "full-fallback", None, info
        return res
    docs, counts = restrict_docs(groups)
    info["doc_counts"], info["candidate_docs"] = counts, len(docs)
    if not docs:
        return []
    idx = np.nonzero(np.isin(np.array(s["docs"]), list(docs)))[0]
    return _rank_chunks(query, idx, db, "keyword-restriction", info)


def search_hybrid(query: str, radius: int = RADIUS, db=None, fallback=None) -> list[dict]:
    s = _load()
    db = get_db() if db is None else db
    matched = query_keywords(query)
    info = {"used": matched, "unmatched": [w for w in dict.fromkeys(kw.tokenize(query)) if w not in matched]}
    if not matched:
        from search import search as full_search
        res = (fallback or full_search)(query, db=db)
        for r in res:
            r["mode"], r["radius"], r["keywords"] = "full-fallback", None, info
        return res
    cells = blast_radius(matched, radius)
    keys = np.array([s["doc_no"][d] * 100000 + p for d, p in cells if d in s["doc_no"]], dtype="int64")
    idx = np.nonzero(np.isin(s["key"], keys))[0]
    return _rank_chunks(query, idx, db, "keyword-radius", info)


if __name__ == "__main__":
    import time
    q = " ".join(sys.argv[1:]) or "How do I control pink bollworm in cotton?"
    t = time.time()
    m = query_keywords(q)
    print({k: v[:6] for k, v in m.items()})
    for r in search_hybrid(q)[:8]:
        print(f"{r['top5_mean']:.3f} {r['mode']} chunks_in_radius={r['radius']} pages={r['pages'][:4]} {r['name'][:60]}")
    print(f"{time.time() - t:.1f}s")
