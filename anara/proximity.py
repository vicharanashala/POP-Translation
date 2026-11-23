"""Bare-bones keyword proximity search: BM25 over 5-page windows, no crop zones, no embeddings.

    python anara/proximity.py build      page-level BM25 matrix -> data/prox_matrix.npz + data/prox_meta.json (~1 min)
    python anara/proximity.py "query"    search from the command line

Index: the keywords.py method with WINDOW 5. A page's term score is idf * tf*(K1+1)/(tf + K1*(1-B+B*len/avg)),
idf over 5-page windows; a term is important on a page when the score is >= THRESHOLD (1.0). Terms seen once in
the whole corpus are left out (same as data/keyword_importance_w5.csv).

Search: the query's words (keywords.tokenize, exact words, no stemming, no fuzzy) that are important on at least
one page; the crop is a keyword like any other. A document passes when it holds important pages for at least 75%
of those words. If no document passes, the bar drops to min(4, that number) words. Passing documents are ranked
by proximity: the fewest pages that hold the needed number of different words (a stretch), then more words in
the stretch, then the sum of the words' best page scores.
"""
from __future__ import annotations

import functools
import json
import math
import re
import sys
from collections import Counter

import numpy as np
import scipy.sparse as sp

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
import keywords as kw  # noqa: E402

WINDOW, THRESHOLD = 5, 1.0
COVER, FALLBACK_WORDS, MAX_DOCS, SNIPPETS = 0.75, 4, 50, 5   # SNIPPETS: best pages kept per document
NEAR = 2            # bm25near: neighbouring pages counted either side of a page
CROP_RADIUS, CROP_UP, CROP_DOWN = 5, 0.3, 0.3   # bm25near: crop within this many pages: score x(1+0.3), else x(1-0.3)
CROP_DOWN_SCALED = 0.25   # bm25nearscaled: no crop within the radius
CROP_PER_SCORE = 1.0      # bm25nearscaled: percent of bump per BM25 point of the crop, minus 1 point per page of distance
NEIGHBOUR_WEIGHT = 0.5   # bm25nearown: a neighbouring page's score counts this fraction of the page's own
CROP_DOWN_MAX = 0.15  # bm25nearmax: the no-crop cut is smaller, since a group counts only once
SEED_SLACK = 2      # windows up to this many pages longer than a document's tightest one are also seeds
STOP_BELOW = 1      # a cluster grows over pages holding at least this many groups
CLUSTERS = 3        # clusters shown per document
MATRIX = common.DATA / "prox_matrix.npz"
META = common.DATA / "prox_meta.json"
RAW = common.DATA / "prox_raw.npz"        # every BM25 score of terms seen twice or more (no threshold)


_NUM = re.compile(r"(?<![a-z0-9])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![a-z0-9])")


def tokenize(text: str) -> list[str]:
    """keywords.tokenize words plus the numbers (120, 0.5, 1,000 -> 1000), which keywords.tokenize drops. Used for
    the index and the query alike."""
    low = text.lower()
    return kw.tokenize(low) + [m.replace(",", "") for m in _NUM.findall(low)]


def build() -> None:
    import pandas as pd
    import pagetext
    ids = pd.read_csv(common.DATA / "text_report.csv").query("verdict == 'english_text'")["doc_id"].tolist()
    vocab: dict[str, int] = {}
    rows, cols, vals = [], [], []
    docs, offsets, lengths, n_pages = [], [], [], 0
    for d in ids:
        try:
            pgs = pagetext.pages(d)
        except Exception:
            continue
        docs.append(d), offsets.append(n_pages), lengths.append(len(pgs))
        for p in pgs:
            for t, f in Counter(tokenize(p)).items():
                rows.append(n_pages), cols.append(vocab.setdefault(t, len(vocab))), vals.append(f)
            n_pages += 1
    V = len(vocab)
    m = sp.csr_matrix((np.array(vals, np.float32), (np.array(rows, np.int32), np.array(cols, np.int32))), shape=(n_pages, V))
    m.sum_duplicates()
    total = np.asarray(m.sum(axis=0)).ravel()
    plen = np.asarray(m.sum(axis=1)).ravel()
    coo = m.tocoo()
    page, term, tf = coo.row, coo.col, coo.data.astype(np.float64)
    nw = -(-n_pages // WINDOW)
    df = np.bincount(np.unique((page // WINDOW).astype(np.int64) * V + term) % V, minlength=V)
    idf = np.log(1 + (nw - df + 0.5) / (df + 0.5))
    norm = kw.K1 * (1 - kw.B + kw.B * plen / plen.mean())
    score = idf[term] * tf * (kw.K1 + 1) / (tf + norm[page])
    keep = (score >= THRESHOLD) & (total[term] >= 2)
    inv = [None] * V
    for t, i in vocab.items():
        inv[i] = t
    # term x page, only important pairs, the score kept
    tp = sp.csr_matrix((score[keep].astype(np.float32), (term[keep], page[keep])), shape=(V, n_pages))
    sp.save_npz(MATRIX, tp)
    rawk = total[term] >= 2                  # every score, no importance threshold (mode bm25sum)
    raw = sp.csr_matrix((score[rawk].astype(np.float32), (term[rawk], page[rawk])), shape=(V, n_pages))
    sp.save_npz(RAW, raw)
    META.write_text(json.dumps({"vocab": inv, "docs": docs, "offsets": offsets, "lengths": lengths}))
    print(f"{len(docs)} documents, {n_pages:,} pages, {nw:,} windows, {V:,} terms, {tp.nnz:,} important term/page pairs, {raw.nnz:,} term/page pairs in the raw matrix")


_S: dict = {}


def _load() -> dict:
    if _S:
        return _S
    meta = json.loads(META.read_text())
    _S.update(m=sp.load_npz(MATRIX).tocsr(), word_no={w: i for i, w in enumerate(meta["vocab"])}, docs=meta["docs"],
              offsets=np.array(meta["offsets"]), lengths=np.array(meta["lengths"]),
              table=common.load_documents().set_index("doc_id"))
    return _S


def _raw() -> sp.csr_matrix:
    _load()
    if "raw" not in _S:
        _S["raw"] = sp.load_npz(RAW).tocsr()
    return _S["raw"]


_CROPS: list = []


def _crop_mention(tokens: list[str]):
    """(crop name, the query tokens naming it, all its forms): the longest crop-master form found in the query, own name over alias."""
    import cropmaster
    if not _CROPS:
        _CROPS.extend(cropmaster.crops())
    best, best_key = None, None
    for c in _CROPS:
        for fi, form in enumerate(c["forms"]):
            for i in range(len(tokens) - len(form) + 1):
                if all(kw._bases(tokens[i + k]) & kw._bases(form[k]) for k in range(len(form))):
                    if best_key is None or (len(form), fi == 0) > best_key:
                        best, best_key = (c["name"], tokens[i:i + len(form)], c["forms"]), (len(form), fi == 0)
    return best


@functools.lru_cache(maxsize=24)
def _stored_pages(doc_id: str) -> tuple:
    import pagetext
    return tuple(pagetext.stored_pages(doc_id) or ())


def _page_text(doc_id: str, page: int) -> str:
    """One page: straight from the PDF when it is on disk (one page, not the whole file), else the stored text."""
    if common.pdf_path(doc_id).exists():
        import fitz
        with fitz.open(common.pdf_path(doc_id)) as pdf:
            return pdf[page - 1].get_text()
    return _stored_pages(doc_id)[page - 1]


def _snippet(doc_id: str, page: int, forms: list[str]) -> str:
    try:
        text = " ".join(_page_text(doc_id, page).split())
    except Exception:
        return ""
    pat = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    m = re.search(rf"\b(?:{pat})", text, re.I) if pat else None
    a = max(0, (m.start() if m else 0) - 110)
    return ("..." if a else "") + text[a:a + 300]


def _variants(t: str) -> set[str]:
    """Spelling variants of a word, kept later only if they are in the index: -ish both ways (yellowish <-> yellow)
    and British/American -our- <-> -or- (colour <-> color)."""
    out = {t}
    if t.endswith("ish") and len(t) - 3 >= 4:
        out.add(t[:-3])
    else:
        out.add(t + "ish")
    for v in list(out):
        out |= {v[:i.start()] + "or" + v[i.end():] for i in re.finditer("our", v)}
        out |= {v[:i.start()] + "our" + v[i.end():] for i in re.finditer("or", v)}
    return out


def _groups(query: str, m: sp.csr_matrix):
    """(tokens, groups, snip, rows, crop_info, skip) of a query over term x page matrix `m` (its row is empty for a
    term not seen). groups: label -> forms shown; snip: label -> words to highlight; rows: label -> score per page,
    the best of the group's forms (the crop group: the best alias, all words of an alias on one page)."""
    s = _load()
    tokens = list(dict.fromkeys(tokenize(query)))
    ptr, wn = m.indptr, s["word_no"]

    def forms_of(t: str) -> list[str]:        # suffix forms (+ the word without its s) that are important somewhere
        if t[0].isdigit():
            return [t] if t in wn and ptr[wn[t] + 1] > ptr[wn[t]] else []
        cand = set().union(*(kw.fuzzy_match(v, wn) for v in _variants(t)))
        if t.endswith("s") and len(t) >= 4:       # ears -> ear
            cand.add(t[:-1])
        return sorted(f for f in cand if f in wn and ptr[wn[f] + 1] > ptr[wn[f]])

    def row_of(fs: list[str]) -> np.ndarray:
        return m[[wn[f] for f in fs]].max(axis=0).toarray()[0]

    groups: dict[str, list[str]] = {}         # label -> what the page shows
    snip: dict[str, list[str]] = {}           # label -> words to highlight
    rows: dict[str, np.ndarray] = {}
    mention, crop_info = _crop_mention(tokens), None
    if mention:                               # the crop is ONE group: any alias of it (rice -> paddy, oryza sativa, ...)
        best, shown, words_ = np.zeros(m.shape[1], np.float32), [], []
        for form in mention[2]:
            per = [forms_of(t) for t in form]
            if all(per):
                r = np.min([row_of(f) for f in per], axis=0)      # all words of the alias on one page
                if r.any():
                    best = np.maximum(best, r)
                    shown.append(" ".join(form))
                    words_ += [x for f in per for x in f]
        if best.any():
            groups[mention[0]], snip[mention[0]], rows[mention[0]] = shown, words_, best
            crop_info = {"name": mention[0], "words": mention[1], "aliases": shown}
    skip = set(mention[1]) if crop_info else set()
    for t in tokens:
        if t in skip:
            continue
        forms = forms_of(t)
        if forms and forms not in snip.values():                      # two query words with the same forms: one group
            groups[t], snip[t], rows[t] = forms, forms, row_of(forms)
    return tokens, groups, snip, rows, crop_info, skip


def search_proximity(query: str, cover: float = COVER, scope: str = "all", limit: int = MAX_DOCS,
                     content: bool = False) -> list[dict]:
    """All documents at once on the term x page matrix (row per keyword group, 0/1 per page, score kept for ties).
    1. Groups: each query word + its suffix forms (+ the word without its s). The crop named in the query (crop
       master) must be on at least one page of a document, else the document is not looked at.
    2. need = 75% of the groups (rounded up); if no document has that many, 4 groups (or fewer); if still none, the
       most groups any document has (best available).
    3. Seeds: for stretch length L = 0, 1, 2, ... a keyword is in window [s, s+L] when it was in [s, s+L-1] or is on
       page s+L; the row sums count the groups in each window. A document's L* is the first L where a window of it
       holds `need` groups; windows of length up to L* + SEED_SLACK that hold `need` are its seeds.
    4. Clusters: from a seed, pages are added outwards in both directions while the page holds at least one group
       (STOP_BELOW); where it hits 0 that side stops. Seeds in the same run of pages are one cluster.
    Documents rank by their best cluster's seed: smallest L, then the larger SUM of the groups' best BM25 scores in the window (a group's row is the max over its forms; the number of groups only gates, via `need`), then more groups. `content` adds snippets."""
    s = _load()
    s = _load()
    tokens, groups, snip, rows, crop_info, skip = _groups(query, s["m"])
    words = list(groups)
    n = len(words)
    used = {t for w in words for t in ([w] + snip[w])} | skip
    info = {"proximity": True, "words": words, "groups": groups, "need": 0, "level": "75%", "crop": None,
            "ignored": [t for t in tokens if t not in used]}
    if not n:
        return []
    score0 = np.stack([rows[w] for w in words])                       # n x pages
    present = score0 > 0
    off, ln = s["offsets"], s["lengths"]
    npg = score0.shape[1]
    doc_of = np.repeat(np.arange(len(off)), ln)
    found = np.add.reduceat(present.astype(np.int32), off, axis=1) > 0        # n x docs
    nfound = found.sum(axis=0)
    cand = np.array([common.in_scope(d, scope) for d in s["docs"]])
    if crop_info:                             # the crop (any alias) must be on some page of the document
        has_crop = found[0]                   # the crop group is words[0]
        info["crop"] = {**crop_info, "docs_with": int((has_crop & cand).sum()), "docs_in_scope": int(cand.sum())}
        cand &= has_crop
    if not cand.any():
        return []
    need = max(1, math.ceil(cover * n - 1e-9))
    if not (cand & (nfound >= need)).any():
        need, info["level"] = min(FALLBACK_WORDS, need), "%d words" % min(FALLBACK_WORDS, need)
        if not (cand & (nfound >= need)).any():
            need, info["level"] = int(nfound[cand].max()), "best available"
    info["need"] = need
    cand &= nfound >= need
    cand_page = cand[doc_of]
    end = (off + ln)[doc_of]                                          # first page after each page's document
    start = np.arange(npg)
    lstar = np.full(len(off), -1)                                     # L* per document
    seeds: dict[int, dict] = {}                                       # doc -> {(first, last): (L, -score, -k)}
    cur, cur_s = present.copy(), score0.copy()
    for L in range(int(ln.max())):
        if L:
            cur[:, :npg - L] |= present[:, L:]
            np.maximum(cur_s[:, :npg - L], score0[:, L:], out=cur_s[:, :npg - L])
        cnt = cur.sum(axis=0)
        hit = np.nonzero((cnt >= need) & (start + L < end) & cand_page)[0]
        if len(hit):
            dh = doc_of[hit]
            new = np.unique(dh[lstar[dh] < 0])
            lstar[new] = L
            keep = L <= lstar[dh] + SEED_SLACK
            hit, dh = hit[keep], dh[keep]
            sc = np.where(cur[:, hit], cur_s[:, hit], 0).sum(axis=0)
            for pg, d, c, v in zip(hit.tolist(), dh.tolist(), cnt[hit].tolist(), sc.tolist()):
                seeds.setdefault(d, {})[(pg, pg + L)] = (L, -v, -c)
        if (lstar[cand] >= 0).all() and L > lstar[cand].max() + SEED_SLACK:
            break
    # runs of pages holding at least one group, never across a document boundary
    mask = present.sum(axis=0) >= STOP_BELOW
    doc_first, doc_last = start == off[doc_of], start == (off + ln - 1)[doc_of]
    run_start = mask & (~np.r_[False, mask[:-1]] | doc_first)
    run_end = mask & (~np.r_[mask[1:], False] | doc_last)
    rid = np.cumsum(run_start) - 1
    starts_, ends_ = np.nonzero(run_start)[0], np.nonzero(run_end)[0]
    idx_arr = np.where(mask, start, npg)
    nxt = np.minimum.accumulate(idx_arr[::-1])[::-1]                  # first masked page at or after i
    prv = np.maximum.accumulate(np.where(mask, start, -1))            # last masked page at or before i
    ranked = []
    for d, sd in seeds.items():
        cl: dict[tuple, tuple] = {}
        for (a, b), key in sd.items():
            f, l = int(nxt[a]), int(prv[b])                           # first and last page of the window holding a group
            rng = (int(starts_[rid[f]]), int(ends_[rid[l]]))
            if rng not in cl or key < cl[rng][0]:
                cl[rng] = (key, (f, l))
        order = sorted(cl.items(), key=lambda kv: kv[1][0])[:CLUSTERS]
        ranked.append((order[0][1][0], d, order))
    ranked.sort(key=lambda x: x[0])
    out = []
    for key, di, order in ranked[:limit]:
        d, o = s["docs"][di], int(off[di])
        row = s["table"].loc[d]
        cluster_info, page_groups, best_pages = [], [], []
        for (lo, hi), (_, (f, l)) in order:
            by_page = {p: [words[k] for k in np.nonzero(present[:, p])[0]] for p in range(f, l + 1) if present[:, p].any()}
            covered = [w for w in words if any(w in g for g in by_page.values())]
            rk = sorted(by_page.items(), key=lambda kv: (-score0[:, kv[0]].sum(), kv[0]))
            pg = [{"page": p - o + 1, "score": round(float(score0[:, p].sum()), 2), "groups": g,
                   "snippet": _snippet(d, p - o + 1, [x for w in g for x in snip[w]]) if content else ""}
                  for p, g in rk[:SNIPPETS]]
            cluster_info.append({"range": [lo - o + 1, hi - o + 1], "core": [f - o + 1, l - o + 1], "covered": covered})
            page_groups.append(pg)
            best_pages += [p - o + 1 for p, _ in rk[:SNIPPETS] if p - o + 1 not in best_pages]   # best cluster's pages first, then the next clusters'
        c0 = cluster_info[0]
        out.append({"doc_id": d, "name": row["name"], "link": row["link"], "mode": "proximity",
                    "total_pages": int(ln[di]), "stretch": c0["core"], "range": c0["range"], "clusters": cluster_info,
                    "covered": c0["covered"], "missing": [w for w in words if w not in c0["covered"]], "n_groups": n,
                    "fraction": round(len(c0["covered"]) / n, 3), "strength": round(-key[1], 2),
                    "passed": info["level"] != "best available", "pages": best_pages[:SNIPPETS], "page_groups": page_groups,
                    "keywords": info})
    return out


def _strong() -> sp.csr_matrix:
    """The raw matrix without the scores <= 1.0 (cached)."""
    _load()
    if "strong" not in _S:
        m = _raw().copy()
        m.data[m.data <= 1.0] = 0
        m.eliminate_zeros()
        _S["strong"] = m
    return _S["strong"]


def search_bm25sum(query: str, scope: str = "all", limit: int = MAX_DOCS, content: bool = False) -> list[dict]:
    """Raw BM25 sums, nothing else (see _page_sum_search): own page only, every score counts."""
    return _page_sum_search(query, _raw(), 0, scope, limit, content)


def search_bm25near(query: str, scope: str = "all", limit: int = MAX_DOCS, content: bool = False,
                    radius: int = NEAR, crop_radius: int = CROP_RADIUS) -> list[dict]:
    """Raw BM25 sums over a page and its +/-`radius` neighbours in the same document; scores <= 1.0 are left out. When
    the query names a crop, a page's sum is raised by CROP_UP if the crop (any alias) is on a page within
    +/-`crop_radius` of it in the same document, and cut by CROP_DOWN if not (`crop_radius` 0 switches this off)."""
    return _page_sum_search(query, _strong(), radius, scope, limit, content, crop_radius)


def search_bm25nearscaled(query: str, scope: str = "all", limit: int = MAX_DOCS, content: bool = False,
                          radius: int = NEAR, crop_radius: int = CROP_RADIUS) -> list[dict]:
    """bm25nearmax with a crop factor that follows the crop's own score: for a page, the bump is the best over the crop's
    pages within +/-`crop_radius` of (its BM25 score x CROP_PER_SCORE percent - its distance in pages, in percent),
    never below 0; no crop page within the radius cuts the score by CROP_DOWN_SCALED."""
    return _page_sum_search(query, _strong(), radius, scope, limit, content, crop_radius, "max", CROP_DOWN_SCALED, True)


def search_bm25nearown(query: str, scope: str = "all", limit: int = MAX_DOCS, content: bool = False,
                       radius: int = NEAR, crop_radius: int = CROP_RADIUS) -> list[dict]:
    """Like bm25nearscaled, but a neighbouring page's score counts NEIGHBOUR_WEIGHT of a score on the page itself: per
    group the best of (own score, NEIGHBOUR_WEIGHT x best neighbour score within +/-`radius`), then summed. Words
    that sit together on the page beat words scattered over the neighbours. Same crop factor as bm25nearscaled."""
    return _page_sum_search(query, _strong(), radius, scope, limit, content, crop_radius, "wmax", CROP_DOWN_SCALED, True)


def search_bm25nearcov(query: str, scope: str = "all", limit: int = MAX_DOCS, content: bool = False,
                       radius: int = NEAR, crop_radius: int = CROP_RADIUS) -> list[dict]:
    """bm25nearown x a group-coverage factor: (groups on the page itself + NEIGHBOUR_WEIGHT x groups found only on the
    +/-`radius` neighbours) / all groups, so words together on one page beat words spread over the neighbours."""
    return _page_sum_search(query, _strong(), radius, scope, limit, content, crop_radius, "wmax", CROP_DOWN_SCALED, True, True)


def search_bm25nearmax(query: str, scope: str = "all", limit: int = MAX_DOCS, content: bool = False,
                       radius: int = NEAR, crop_radius: int = CROP_RADIUS) -> list[dict]:
    """Like bm25near, but each group takes its BEST score over the page and its +/-`radius` neighbours (not the sum
    of them), so a group present on several of the pages counts once; then the groups are summed. The crop factor is
    x(1+CROP_UP) with the crop within `crop_radius` pages, x(1-CROP_DOWN_MAX) without."""
    return _page_sum_search(query, _strong(), radius, scope, limit, content, crop_radius, "max", CROP_DOWN_MAX)


def _page_sum_search(query: str, m: sp.csr_matrix, radius: int, scope: str, limit: int, content: bool,
                     crop_radius: int = 0, combine: str = "sum", crop_down: float = CROP_DOWN, scaled: bool = False,
                     coverage: bool = False) -> list[dict]:
    """A page's score is the SUM over the query's groups of the group's best BM25 score (matrix `m`) on the page and,
    when `radius` > 0, on the `radius` pages either side inside the same document (so a word on a neighbouring page
    counts as many times as the page sum adds it). No 75%/4 bar, no window, no crop filter; the crop is one group like
    the others. A document ranks by its best page; its CLUSTERS entries are its best pages, each at least `radius` + 1
    pages from a better one (SNIPPETS of them), not runs of pages."""
    s = _load()
    tokens, groups, snip, rows, crop_info, skip = _groups(query, m)
    words = list(groups)
    n = len(words)
    used = {t for w in words for t in ([w] + snip[w])} | skip
    info = {"proximity": True, "rawsum": True, "near": radius, "combine": combine, "crop_down": crop_down, "scaled": scaled, "coverage": coverage,
            "crop_radius": crop_radius if crop_info else 0, "words": words, "groups": groups, "need": 0,
            "level": "raw BM25 sum", "crop": crop_info, "ignored": [t for t in tokens if t not in used]}
    if not n:
        return []
    score0 = np.stack([rows[w] for w in words])                       # n x pages
    own = score0.sum(axis=0)
    off, ln = s["offsets"], s["lengths"]
    npg = len(own)
    if radius:
        doc_of = np.repeat(np.arange(len(off)), ln)
        cs = np.concatenate([[0.0], np.cumsum(own, dtype=np.float64)])
        idx = np.arange(npg)
        lo, hi = np.maximum(idx - radius, off[doc_of]), np.minimum(idx + radius, (off + ln)[doc_of] - 1)
        if combine == "max":                  # best of each group over p-radius..p+radius, then the groups added
            mx = np.zeros_like(score0)
            for d in range(-radius, radius + 1):
                src = idx + d
                valid = (src >= off[doc_of]) & (src < (off + ln)[doc_of])
                mx = np.maximum(mx, np.where(valid, score0[:, np.clip(src, 0, npg - 1)], 0))
            page = mx.sum(axis=0)
        elif combine == "wmax":               # own score in full, neighbours at NEIGHBOUR_WEIGHT, best per group
            mx = score0.copy()
            for d in range(-radius, radius + 1):
                if d:
                    src = idx + d
                    valid = (src >= off[doc_of]) & (src < (off + ln)[doc_of])
                    mx = np.maximum(mx, NEIGHBOUR_WEIGHT * np.where(valid, score0[:, np.clip(src, 0, npg - 1)], 0))
            page = mx.sum(axis=0)
        else:
            page = cs[hi + 1] - cs[lo]
    else:
        page = own
        lo = hi = np.arange(npg)
    near_crop = np.zeros(npg, dtype=bool)
    crop_bump = np.zeros(npg)
    cover = np.ones(npg)
    if coverage:                              # (groups on the page + 0.5 x groups only on the neighbours) / all groups
        doc_of = np.repeat(np.arange(len(off)), ln)
        idx = np.arange(npg)
        here = score0 > 0
        nb = np.zeros_like(here)
        for d in range(-radius, radius + 1):
            if d:
                src = idx + d
                valid = (src >= off[doc_of]) & (src < (off + ln)[doc_of])
                nb |= valid & here[:, np.clip(src, 0, npg - 1)]
        cover = (here.sum(axis=0) + NEIGHBOUR_WEIGHT * (nb & ~here).sum(axis=0)) / n
        page = page * cover
    if crop_info and crop_radius:             # the crop group is words[0]; its pages within +/-crop_radius, same document
        doc_of = np.repeat(np.arange(len(off)), ln)
        cc = np.concatenate([[0], np.cumsum(score0[0] > 0)])
        idx = np.arange(npg)
        a, b = np.maximum(idx - crop_radius, off[doc_of]), np.minimum(idx + crop_radius, (off + ln)[doc_of] - 1)
        near_crop = (cc[b + 1] - cc[a]) > 0
        if scaled:
            cs0 = score0[0]
            bump = np.full(npg, -np.inf)          # percent: best over d of crop score x CROP_PER_SCORE - |d|
            for d in range(-crop_radius, crop_radius + 1):
                src = idx + d
                valid = (src >= off[doc_of]) & (src < (off + ln)[doc_of])
                v = np.where(valid, cs0[np.clip(src, 0, npg - 1)], 0)
                bump = np.maximum(bump, np.where(v > 0, v * CROP_PER_SCORE - abs(d), -np.inf))
            factor = np.where(near_crop, 1 + np.maximum(bump, 0) / 100, 1 - crop_down)
            crop_bump = np.where(near_crop, np.maximum(bump, 0), 0)
            page = page * factor
        else:
            page = page * np.where(near_crop, 1 + CROP_UP, 1 - crop_down)
    ok = np.array([common.in_scope(d, scope) for d in s["docs"]])
    best = np.where(ok, np.maximum.reduceat(page, off), 0)
    out = []
    for di in np.argsort(-best, kind="stable")[:limit]:
        if best[di] <= 0:
            break
        d, o = s["docs"][di], int(off[di])
        seg = page[o:o + int(ln[di])]
        top: list[int] = []
        for p in np.argsort(-seg, kind="stable"):
            if seg[p] <= 0 or len(top) == SNIPPETS:
                break
            if all(abs(int(p) - q) > radius for q in top):
                top.append(int(p))
        clusters, page_groups = [], []
        for p in top:
            g = [words[k] for k in np.nonzero(score0[:, lo[o + p]:hi[o + p] + 1].max(axis=1) > 0)[0]]
            clusters.append({"range": [int(lo[o + p]) - o + 1, int(hi[o + p]) - o + 1], "core": [p + 1, p + 1], "covered": g})
            page_groups.append([{"page": p + 1, "score": round(float(seg[p]), 2), "own": round(float(own[o + p]), 2), "crop_near": bool(near_crop[o + p]), "cover": round(float(cover[o + p]), 3) if coverage else None,
                                 "crop_bump": round(float(crop_bump[o + p]), 1) if scaled and crop_info and crop_radius else None,
                                 "groups": g, "snippet": _snippet(d, p + 1, [x for w in g for x in snip[w]]) if content else ""}])
        c0 = clusters[0]
        row = s["table"].loc[d]
        out.append({"doc_id": d, "name": row["name"], "link": row["link"], "mode": "proximity",
                    "total_pages": int(ln[di]), "stretch": c0["core"], "range": c0["range"], "clusters": clusters,
                    "covered": c0["covered"], "missing": [w for w in words if w not in c0["covered"]], "n_groups": n,
                    "fraction": round(len(c0["covered"]) / n, 3), "strength": round(float(best[di]), 2), "passed": True,
                    "pages": [p + 1 for p in top], "page_groups": page_groups, "keywords": info})
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    if sys.argv[1] == "build":
        build()
    else:
        res = search_proximity(" ".join(sys.argv[1:]))
        print(res[0]["keywords"] if res else "no result")
        for r in res[:10]:
            print(f"{r['name'][:60]:60} p{r['stretch'][0]}-{r['stretch'][1]} {len(r['covered'])}/{r['n_groups']}")
