"""Embedding-free retrieval experiments: topic-zone words, persistence zones, window scoring.

    zone_words(stats)                  -> words that look like section topics (crops, pests...)
    zones(counters, forms)             -> [(start, end)] page zones with a 2-of-3 persistence rule
    lexical_search(query, stats)       -> documents ranked from keyword groups alone (no vectors)

All of it works on the per-page word counts in data/keyword_stats.pkl (keywords.build_stats()).
    python anara/lexical.py zones | crops | eval
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import keywords as kw  # noqa: E402

KERNEL = np.array([0.5, 1.0, 0.5])
MIN_PAGES_LONG = 20


def _runs(pages: list[int], gap: int = 2) -> list[tuple[int, int]]:
    """Group sorted page numbers into runs, merging gaps of up to `gap` missing pages."""
    out = []
    for p in pages:
        if out and p - out[-1][1] <= gap + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def zone_words(stats: dict, min_docs: int = 3, min_pages: int = 5, max_span: float = 0.4,
               min_share: float = 0.6) -> dict[str, int]:
    """{word: number of long documents where it behaves like a section topic}. In a document of
    >= MIN_PAGES_LONG pages a word is a topic there when it is on >= min_pages pages and ONE run
    (gaps of 1 page merged) holds at least min_share of those pages while covering at most
    max_span of the document. A generic word is scattered (no dominant run) or spans everything;
    a crop name in a crop-by-crop guide sits in one chapter."""
    hits: dict[str, int] = defaultdict(int)
    for doc, counters in stats["pages"].items():
        n = len(counters)
        if n < MIN_PAGES_LONG:
            continue
        where: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(counters, 1):
            for w in c:
                where[w].append(i)
        for w, pages in where.items():
            if len(pages) < min_pages:
                continue
            runs = _runs(pages, gap=1)
            best = max(runs, key=lambda r: sum(r[0] <= p <= r[1] for p in pages))
            inside = sum(best[0] <= p <= best[1] for p in pages)
            if inside / len(pages) >= min_share and (best[1] - best[0] + 1) / n <= max_span:
                hits[w] += 1
    return {w: c for w, c in hits.items() if c >= min_docs}


def zones(counters: list, forms) -> list[tuple[int, int]]:
    """Page zones of a topic word (all `forms`) in one document. A zone OPENS at a page where
    the word is present on >= 2 of 3 consecutive pages and CLOSES after 2 pages without it,
    so one cross-reference ('unlike potato blight') does not open a zone."""
    forms = set(forms)
    has = np.array([any(f in c for f in forms) for c in counters], dtype=int)
    n = len(has)
    ok = np.zeros(n, dtype=bool)
    for i in range(n):
        w = has[max(0, i - 1):i + 2]
        ok[i] = has[i] and w.sum() >= 2
    out, start, miss = [], None, 0
    for i in range(n):
        if ok[i]:
            if start is None:
                start = i + 1
            last, miss = i + 1, 0
        elif start is not None:
            miss += 1
            if miss >= 2:
                out.append((start, last))
                start = None
    if start is not None:
        out.append((start, last))
    return out


def group_hits(counters: list, forms) -> np.ndarray:
    forms = set(forms)
    return np.array([any(f in c for f in forms) for c in counters], dtype=float)


def lexical_search(query: str, stats: dict, groups: dict[str, list[str]] | None = None,
                   top: int = 10) -> list[dict]:
    """Rank documents with no vectors: one 0/1 row per keyword group over the pages, summed
    across groups, smoothed with a [0.5, 1, 0.5] kernel; a document's score is its best
    smoothed page. Only documents holding every group are considered (as in restrict mode)."""
    vocab = set(stats["df"])
    if groups is None:
        groups = {}
        for w in dict.fromkeys(kw.tokenize(query)):
            f = kw.fuzzy_match(w, vocab)
            if f:
                groups[w] = f
    res = []
    for doc, counters in stats["pages"].items():
        rows = [group_hits(counters, f) for f in groups.values()]
        if not rows or any(r.sum() == 0 for r in rows):
            continue
        s = np.sum(rows, axis=0)
        sm = np.convolve(s, KERNEL)[1:-1]   # full conv trimmed: always len(s), even for 1-2 page documents
        best = int(sm.argmax())
        res.append({"doc_id": doc, "score": float(sm[best]), "page": best + 1,
                    "groups_on_page": int(s[best]), "pages": [int(p) + 1 for p in np.argsort(-sm)[:5]]})
    res.sort(key=lambda r: (-r["score"], -r["groups_on_page"]))
    return res[:top]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "crops"
    stats = kw.build_stats()
    zw = zone_words(stats)
    if cmd == "crops":
        import re
        from crops import CROPS
        names = {w for n in CROPS for w in re.findall(r"[a-z]+", n.lower()) if len(w) > 3}
        ranked = sorted(zw.items(), key=lambda x: -x[1])
        print(f"{len(zw)} zone words")
        for N in (50, 100, 200, 400):
            top = [w for w, _ in ranked[:N]]
            print(f"top {N}: {sum(w in names or w.rstrip('s') in names for w in top)} match a crop name")
        print([f"{w}:{c}" for w, c in ranked[:60]])
