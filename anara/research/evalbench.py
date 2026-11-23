"""Scratch: score retrieval methods on research/bench.json (synthetic, from headings). NOT part of the app.
    python anara/research/evalbench.py [n_questions] [methods,comma,separated]
Each method: query -> [(doc_id, page 1-based)] best page per document, best first.
"""
import json, re, sys, time, collections
sys.path.insert(0, "/home/paulose610/repos/annam/containers/POP-Translation/anara")
import numpy as np
import zonesearch as z, keywords as kw

S = z._load()
DOCNO = {d: i for i, d in enumerate(S["docs"])}
OFF, LEN = S["offsets"], S["lengths"]
doc_of_page = np.repeat(np.arange(len(S["docs"])), LEN)
BAD_HEAD = {"published by", "maharashtra", "sorghum", "no of", "editors", "publisher", "contact", "country", "email", "phone"}
BAD_CROP = {"Char Magaz", "Shah Marich"}
RRF_K = 60


def load_bench(n=None):
    b = [o for o in json.load(open("/home/paulose610/repos/annam/containers/POP-Translation/anara/research/bench.json"))
         if o["key"][0] not in BAD_HEAD and o["crop"] not in BAD_CROP]
    return b[:n] if n else b


def page_scores(q):
    """(tokens, kept words, bm layers (k x P), crop index or None, crop token span)"""
    toks = list(dict.fromkeys(kw.tokenize(q)))
    hit = z.find_crop(toks)
    ci = span = None
    if hit:
        ci, span = hit
    words, bm, present, forms, miss = z.keyword_layers(toks)
    return toks, words, bm, ci, span


def best_per_doc(score, topn=60):
    """score over the flat page axis -> [(doc_id, page)] best page per doc, best first."""
    idx = np.argpartition(-score, min(2000, len(score) - 1))[:2000]
    idx = idx[np.argsort(-score[idx])]
    seen, out = set(), []
    for g in idx:
        if score[g] <= 0:
            break
        di = int(doc_of_page[g])
        if di in seen:
            continue
        seen.add(di)
        out.append((S["docs"][di], int(g - OFF[di]) + 1, float(score[g])))
        if len(out) >= topn:
            break
    return out


# ------------------------------------------------------------ methods
def m_zone(q):
    r = z.search_zone(q)
    return [(x["doc_id"], x["pages"][0] if x["pages"] else x["stretch"][0], 0.0) for x in r]


def m_bm25(q):
    toks, words, bm, ci, span = page_scores(q)
    if not len(words):
        return []
    return best_per_doc(bm.sum(0))


def m_bm25cov(q):
    """coverage first (number of query words on the page), then BM25"""
    toks, words, bm, ci, span = page_scores(q)
    if not len(words):
        return []
    return best_per_doc((bm > 0).sum(0) * 1000.0 + bm.sum(0))


def m_bm25win(q):
    """pages scored on a +-1 page window (answer often spills over a page break): sum of own + 0.5*neighbours"""
    toks, words, bm, ci, span = page_scores(q)
    if not len(words):
        return []
    s = bm.sum(0)
    t = s.copy()
    t[1:] += 0.5 * s[:-1]
    t[:-1] += 0.5 * s[1:]
    return best_per_doc(t)


def m_soft(q, lam=1.0):
    """soft crop zone: non-crop words BM25 x (crop evidence). crop evidence = smoothed normalised crop score, capped at 1."""
    toks, words, bm, ci, span = page_scores(q)
    if ci is None:
        return m_bm25cov(q)
    other = [i for i, w in enumerate(words) if not (span[0] <= toks.index(w) < span[1]) and w in toks] if False else None
    ctoks = set(toks[span[0]:span[1]])
    keep = [i for i, w in enumerate(words) if w not in ctoks]
    if not keep:
        return best_per_doc(S["sm"][ci].copy())
    sc = bm[keep].sum(0)
    cov = (bm[keep] > 0).sum(0)
    crop = np.minimum(S["sm"][ci], 1.0)
    return best_per_doc((cov * 1000.0 + sc) * (0.02 + crop ** lam))


def _rrf(lists, w=None):
    sc = collections.defaultdict(float)
    page = {}
    for li, L in enumerate(lists):
        for r, (d, p, *_ ) in enumerate(L):
            sc[d] += (w[li] if w else 1.0) / (RRF_K + r)
            page.setdefault(d, p) if li == 0 else None
    out = sorted(sc, key=lambda d: -sc[d])
    return [(d, page.get(d) or next(p for L in lists for (dd, p, *_ ) in L if dd == d), 0.0) for d in out]


def m_rrf_zone_bm25(q):
    a, b = m_zone(q), m_bm25cov(q)
    return _rrf([a, b])


def m_rrf_zone_soft(q):
    return _rrf([m_zone(q), m_soft(q)])


METHODS = {k[2:]: v for k, v in globals().items() if k.startswith("m_")}


# ------------------------------------------------------------ scoring
def score(method, bench, label=None):
    n = len(bench)
    hits = collections.Counter()
    mrr = 0.0
    t0 = time.time()
    lat = []
    for o in bench:
        t = time.time()
        try:
            res = method(o["q"])
        except Exception as e:      # a crash counts as a miss, reported below
            hits["error"] += 1
            res = []
        lat.append(time.time() - t)
        pool = o["pool"]
        def in_pool(d, p):
            return any(d == pd and p0 - 1 <= p <= p1 + 1 for pd, p0, p1 in pool)
        src_rank = next((i for i, (d, p, *_ ) in enumerate(res) if d == o["doc"]), None)
        any_rank = next((i for i, (d, p, *_ ) in enumerate(res) if in_pool(d, p)), None)
        for k in (1, 5, 10):
            if src_rank is not None and src_rank < k:
                hits[f"doc@{k}"] += 1
            if any_rank is not None and any_rank < k:
                hits[f"answerpage@{k}"] += 1
        if src_rank is not None:
            hits["source_found"] += 1
            if o["p0"] - 1 <= res[src_rank][1] <= o["p1"] + 1:
                hits["source_page_ok"] += 1
        if src_rank == 0 and o["p0"] - 1 <= res[0][1] <= o["p1"] + 1:
            hits["top1_source_and_page"] += 1
        if any_rank is not None:
            mrr += 1 / (any_rank + 1)
    row = {k: round(v / n, 3) for k, v in sorted(hits.items())}
    row["mrr_answerpage"] = round(mrr / n, 3)
    row["sec_per_q"] = round(sum(lat) / n, 3)
    return row


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    names = sys.argv[2].split(",") if len(sys.argv) > 2 else list(METHODS)
    bench = load_bench()
    import random
    random.Random(1).shuffle(bench)
    bench = bench[:n]
    print("questions", len(bench), "docs", len({o['doc'] for o in bench}), flush=True)
    for nm in names:
        print("RESULT", nm, json.dumps(score(METHODS[nm], bench)), flush=True)
