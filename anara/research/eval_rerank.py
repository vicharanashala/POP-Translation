"""Scratch: rerank the top candidate pages of zone/bm25cov with a local dense model or cross-encoder.
    python anara/research/eval_rerank.py N [methods]
"""
import sys, json, time, random, collections
sys.path.insert(0, "/home/paulose610/repos/annam/containers/POP-Translation/anara")
import numpy as np
import evalbench as E
import pagetext

TEXT = {}


def page_text(d, p, n=1800):
    if d not in TEXT:
        try:
            TEXT[d] = pagetext.pages(d)
        except Exception:
            TEXT[d] = []
    pg = TEXT[d]
    return (pg[p - 1] if 0 < p <= len(pg) else "")[:n]


_dense = None
_ce = {}
CACHE = {}


def dense_scores(q, cands):
    global _dense
    if _dense is None:
        from sentence_transformers import SentenceTransformer
        _dense = SentenceTransformer("BAAI/bge-small-en-v1.5", device="cpu")
        _dense.max_seq_length = 384
    need = [c for c in cands if c not in CACHE]
    if need:
        v = _dense.encode([page_text(*c) for c in need], batch_size=16, normalize_embeddings=True, show_progress_bar=False)
        for c, x in zip(need, v):
            CACHE[c] = x
    qv = _dense.encode(["Represent this sentence for searching relevant passages: " + q], normalize_embeddings=True)[0]
    return [float(CACHE[c] @ qv) for c in cands]


def ce_scores(q, cands, name):
    if name not in _ce:
        from sentence_transformers import CrossEncoder
        _ce[name] = CrossEncoder(name, device="cpu", max_length=512)
    return [float(x) for x in _ce[name].predict([(q, page_text(*c)) for c in cands], batch_size=16, show_progress_bar=False)]


def candidates(q, top=20):
    a = E.m_zone(q)[:top]
    b = E.m_bm25cov(q)[:top]
    fused = E._rrf([a, b])[:top]
    return fused


def make_rerank(kind, top=20, w_lex=1.0):
    def run(q):
        fused = candidates(q, top)
        if not fused:
            return []
        cands = [(d, p) for d, p, *_ in fused]
        s = dense_scores(q, cands) if kind == "dense" else ce_scores(q, cands, kind)
        order_dense = sorted(range(len(cands)), key=lambda i: -s[i])
        rank_d = {i: r for r, i in enumerate(order_dense)}
        fin = sorted(range(len(cands)), key=lambda i: -(w_lex / (60 + i) + 1.0 / (60 + rank_d[i])))
        pure = order_dense
        return [(cands[i][0], cands[i][1], s[i]) for i in (pure if w_lex == 0 else fin)]
    return run


METHODS = {
    "fused_only": lambda q: candidates(q, 20),
    "dense_rrf": make_rerank("dense"),
    "dense_only": make_rerank("dense", w_lex=0),
    "ce_minilm_rrf": make_rerank("cross-encoder/ms-marco-MiniLM-L-6-v2"),
    "ce_minilm_only": make_rerank("cross-encoder/ms-marco-MiniLM-L-6-v2", w_lex=0),
}

if __name__ == "__main__":
    n = int(sys.argv[1]); names = sys.argv[2].split(",") if len(sys.argv) > 2 else list(METHODS)
    bench = E.load_bench(); random.Random(1).shuffle(bench); bench = bench[:n]
    print("questions", len(bench), flush=True)
    for nm in names:
        print("RESULT", nm, json.dumps(E.score(METHODS[nm], bench)), flush=True)
