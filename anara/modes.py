"""The single registry of search modes: key, number (order of invention), short name, one-line description and the
exact configuration. app.py serves it as GET /api/modes; page.html, report.html and labelled_test.py read it from there.

Numbers and constants are read from the modules that implement the modes (proximity.py, hybrid.py, search.py,
zonesearch.py), so a changed constant changes the text. Order of invention follows METHODS_TRIED.md:
A (1-3), D zone, F proximity, G bm25sum, I+J bm25near, K bm25nearmax, L/L2 bm25nearscaled, M bm25nearown, N bm25nearcov, O bm25nearcov2.
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import hybrid  # noqa: E402
import proximity as px  # noqa: E402
import search as emb  # noqa: E402
import zonesearch as zs  # noqa: E402

_pct = lambda x: f"{round(x * 100)}%"
_R = px.NEAR
_CROP_BUMP_SCALED = (f"crop within ±{px.CROP_RADIUS} pages: x(1 + (crop BM25 score - pages from the centre)%), never below x1; "
                     f"no crop within ±{px.CROP_RADIUS}: x{1 - px.CROP_DOWN_SCALED:g}")


def _near(*rows):
    """bm25 near-family rows shared by modes 7-11."""
    return [("Matrix", f"raw BM25 per page (prox_raw), scores of {px.THRESHOLD:g} or less removed"),
            ("Window", f"the page and {_R} pages either side, same document"), *rows,
            ("Ranking", "a document ranks by its best page; the 5 best pages are shown"),
            ("Searches", "state + central documents"), ("Needs", "no embeddings")]


def _build() -> list[dict]:
    m = []

    def add(key, name, desc, config, built_on=None):
        m.append({"key": key, "number": len(m) + 1, "name": name, "description": desc, "config": [list(c) for c in config],
                  "built_on": built_on, "central": key not in ("full", "hybrid", "restrict"),
                  "needs_embeddings": key in ("full", "hybrid", "restrict")})

    add("full", "Embedding only",
        "Vector search of the question against every page chunk; a document ranks by its 5 best chunks.",
        [("Matrix", "chunk embeddings (BAAI/bge-small-en-v1.5, Atlas vector index)"),
         ("Pool", f"{emb.TOP5_K} nearest chunks"), ("Ranking", f"mean score of the document's {emb.TOP_N} best chunks"),
         ("Searches", "state documents only (central documents have no embeddings)"), ("Needs", "embeddings + Atlas")])
    add("hybrid", "Keywords + embedding",
        f"Keep only chunks on pages with a query keyword (±{hybrid.RADIUS} pages), then run the vector search inside them.",
        [("Matrix", "keyword index (10-page windows) to pick pages, then chunk embeddings"),
         ("Radius", f"±{hybrid.RADIUS} pages around every keyword page"),
         ("Ranking", f"mean score of the document's {hybrid.TOP_N} best chunks inside the radius"),
         ("Searches", "state documents only"), ("Needs", "embeddings + Atlas")], 1)
    add("restrict", "Keyword restriction",
        "Only documents holding a form of every keyword group are searched, by vector search.",
        [("Matrix", "keyword index to filter documents, then chunk embeddings"),
         ("Rule", "a document must contain at least one form of EVERY keyword group"),
         ("Ranking", "as Embedding only, inside the surviving documents"),
         ("Searches", "state documents only"), ("Needs", "embeddings + Atlas")], 2)
    add("zone", "Crop zone",
        "Finds the page zone of the query's crop in each document, then looks for the keyword groups inside it.",
        [("Matrix", f"crop zones (headings, crop scores) + keyword BM25 over {zs.WINDOW}-page windows"),
         ("Threshold", f"a stretch must hold {_pct(zs.COVER)} of the keyword groups, or {zs.NEED_CAP} if fewer"),
         ("Window", f"stretch of at most {zs.MAX_SPAN} pages inside the crop zone"),
         ("Fallback", f"no zone: keywords over all pages; nothing passes: best {zs.RELAXED_DOCS} documents"),
         ("Searches", "state + central documents"), ("Needs", "no embeddings")])
    add("proximity", "Keyword proximity",
        "Documents holding most keyword groups, ranked by how few pages hold them together.",
        [("Matrix", f"BM25 over {px.WINDOW}-page windows, scores of {px.THRESHOLD:g} or more kept"),
         ("Threshold", f"a document needs {_pct(px.COVER)} of the keyword groups, else {px.FALLBACK_WORDS}, else the best available"),
         ("Crop rule", "the crop (any alias) must be on a page of the document"),
         ("Ranking", "smallest stretch of pages, then larger summed BM25, then more groups"),
         ("Searches", "state + central documents"), ("Needs", "no embeddings")])
    add("bm25sum", "BM25 page sum",
        "Each page scores the sum over keyword groups of the group's best BM25 on that page; nothing else.",
        [("Matrix", "raw BM25 per page, every score of terms seen twice or more (prox_raw)"),
         ("Threshold", "none"), ("Window", "the page only, no neighbours"), ("Crop rule", "none"),
         ("Ranking", "a document ranks by its best page; the 5 best pages are shown"),
         ("Searches", "state + central documents"), ("Needs", "no embeddings")])
    add("bm25near", "BM25 near (sum ±2)",
        f"Page score = BM25 summed over the page and {_R} pages each side; crop nearby raises it, no crop cuts it.",
        _near(("Combine", "SUM of the page sums over the window (a word on several pages counts several times)"),
              ("Crop rule", f"crop within ±{px.CROP_RADIUS} pages: x{1 + px.CROP_UP:g}; otherwise x{1 - px.CROP_DOWN:g}")), 6)
    add("bm25nearmax", "BM25 near-max (best ±2)",
        "Each keyword group counts once, at its best score over the page and its neighbours; groups are summed.",
        _near(("Combine", "MAX per group over the window, then sum over groups (a group counts once)"),
              ("Crop rule", f"crop within ±{px.CROP_RADIUS} pages: x{1 + px.CROP_UP:g}; otherwise x{1 - px.CROP_DOWN_MAX:g}")), 7)
    add("bm25nearscaled", "BM25 near-scaled (crop by score)",
        "As near-max, but the crop bump grows with the crop's own BM25 score and shrinks with its distance.",
        _near(("Combine", "MAX per group over the window, then sum over groups"),
              ("Crop rule", _CROP_BUMP_SCALED)), 8)
    add("bm25nearown", "BM25 near-own (neighbours half)",
        f"As near-scaled, but a neighbouring page's score counts only {px.NEIGHBOUR_WEIGHT:g} of the page's own.",
        _near(("Combine", f"per group the best of: its score on the page, {px.NEIGHBOUR_WEIGHT:g} x its best score on a neighbour; then sum over groups"),
              ("Crop rule", _CROP_BUMP_SCALED)), 9)
    add("bm25nearcov", "BM25 near-cov (x coverage)",
        "As near-own, multiplied by the share of keyword groups that the page and its neighbours cover.",
        _near(("Combine", f"as near-own (own page full, neighbours x{px.NEIGHBOUR_WEIGHT:g}, best per group)"),
              ("Coverage factor", f"x (groups on the page + {px.NEIGHBOUR_WEIGHT:g} x groups only on neighbours) / all groups"),
              ("Crop rule", _CROP_BUMP_SCALED)), 10)
    add("bm25nearcov2", "BM25 near-cov 2 (distance-weighted)",
        "As near-cov, but a neighbouring page counts 0.9 one page away and 0.7 two pages away, both in the page score and in the coverage factor.",
        _near(("Combine", f"per group the best of: its score on the page, {px.COVER_WEIGHTS[1]:g} x its best score on a page 1 away, {px.COVER_WEIGHTS[2]:g} x its best score on a page 2 away; then sum over groups"),
              ("Coverage factor", f"x (groups on the page + sum over groups only on neighbours of {px.COVER_WEIGHTS[1]:g} at +/-1 page or {px.COVER_WEIGHTS[2]:g} at +/-2 pages, nearest page decides) / all groups"),
              ("Crop rule", _CROP_BUMP_SCALED)), 11)
    return m


MODES: list[dict] = _build()
KEYS = [m["key"] for m in MODES]
BY_KEY = {m["key"]: m for m in MODES}


def modes() -> list[dict]:
    """The registry as served by /api/modes: `built_on` is the number of the mode this one derives from (or None)."""
    return MODES
