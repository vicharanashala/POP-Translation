"""Crop-zone search: no embeddings, no Atlas. Matrices and forward passes only.

INDEX (once, `python anara/zonesearch.py build`)
    words x pages sparse BM25 matrix. Every page of every document sits on ONE flat page axis
    (document i starts at offsets[i]). TF and length normalisation are per page, the idf comes
    from 10-page windows of the corpus (keywords.page_scores). A stored entry means the word is
    on the page (scores are floored at 1e-6, so "present" is exact). Also a crops x pages matrix:
    a crop's score on a page is the max over its surface forms (name, scientific name, English
    aliases from the crop master) of the min over the form's words of the word's best BM25 among
    its suffix variants -- a multi-word form needs all its words on the page.

QUERY (`search_zone(q)`)

    1. The crop in the query is found in the crop master (longest form wins, name before alias).
    2. Per document its ZONES. When the document has a heading that names the crop (headings.py:
       font size / bold; chapter-level headings preferred), the zone starts exactly on that page and
       ends before the next chapter-level heading that names another crop, or, if there is none, by the
       BM25 rule below.
       Without such a heading (or when the crop's scores do not support it) the zone is found from the
       scores alone: it starts at a stretch of pages where the crop scores near its
       document peak (>= SEED_FRAC of it, and it is the leading crop there) and runs FORWARD ONLY
       until another crop outscores it (and is itself present) on STOP_RUN consecutive pages, or the
       document ends. Scores are normalised per crop (95th percentile of its non-zero pages, so a
       one-word name and a three-word name are comparable) and smoothed over SMOOTH pages.
    3. The other query words become keyword groups: all suffix variants of a word OR-ed into one
       0/1 row per page; the rows stack into one layer per group over the same flat page axis.
    4. Inside each zone a forward pass finds the shortest stretch (<= MAX_SPAN pages) holding at
       least COVER (75%) of the groups OR NEED_CAP (4) groups, whichever is fewer, on one page or spread
       over the stretch. Documents are ranked
       by groups covered, then shortest stretch, then how dedicated the zone holding it is (the crop's mean score over the zone times the
       square root of its length, length capped at ZONE_CAP: a crop chapter beats a passing mention),
       then BM25 strength of the stretch.
    5. Documents with NO zone for the query's crop are not dropped. Their pages outside the zones of every
       crop are searched with the crop counted as one more keyword group and required in the stretch: a document needs
       COVER (75%) of the groups or NEED_CAP (4), whichever is fewer. They come after the zone documents, ranked by
       PROXIMITY (fewest pages holding that many groups), then groups held, then strength (_no_zone_docs).
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import numpy as np
import scipy.sparse as sp
from scipy.ndimage import uniform_filter1d

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
import cropmaster  # noqa: E402
import headings  # noqa: E402
import keywords as kw  # noqa: E402

MATRIX = common.DATA / "zone_bm25.npz"
CROPS = common.DATA / "zone_crops.npy"
META = common.DATA / "zone_meta.json"
LEVELS = common.DATA / "zone_levels.json"
WINDOW = 10

SMOOTH = 5         # crop scores are averaged over this many pages (inside one document) first
SEED_FRAC = 0.6    # a page seeds a zone when the crop is the leading crop there and scores >= this share of its document peak
MIN_CROP = 0.3     # smoothed, normalised score below which a crop is not "present" (also the bar for a rival)
STOP_RUN = 2       # a rival crop leading on this many consecutive pages ends the zone
MAJOR_RATIO = 1.4  # a heading this much larger than the body text is a chapter title
TOP_SHARE = 0.3    # a chapter heading above this share of the page height owns its page; lower down the page is shared
                   # and goes to the EARLIER chapter (a zone ends on it, the next zone starts on the following page)
LEVEL_MIN = 1.2     # a heading size level below this (ratio to the body text) is body text or a sub-heading, never the crop level
LEVEL_CROPS = 3     # a level is the crop level only if headings of it name at least this many different crops
SUPPORT = 0.3      # a heading start needs the crop's smoothed score near it to reach this share of its document peak
ZONE_CAP = 25      # zone length counts for at most this many pages in the ranking
COVER = 0.75        # share of the keyword groups a stretch must hold
NEED_CAP = 4       # ...or this many groups, whichever is fewer: a long query needs only 4 of its groups
MAX_SPAN = 6        # longest stretch of pages (the groups may be spread over it)
MAX_DOCS = 50


# ---------------------------------------------------------------- index

def build() -> None:
    stats = kw.build_stats(refresh=True, zone=True)
    docs = list(stats["pages"])
    offsets, total = {}, 0
    for d in docs:
        offsets[d] = total
        total += len(stats["pages"][d])
    vocab = sorted(stats["df"])
    word_no = {w: i for i, w in enumerate(vocab)}
    rows, cols, vals = [], [], []
    for term, doc, page, _, score in kw.page_scores(stats, WINDOW):
        rows.append(word_no[term])
        cols.append(offsets[doc] + page - 1)
        vals.append(max(score, 1e-6))
    m = sp.csr_matrix((np.array(vals, dtype="float32"), (rows, cols)), shape=(len(vocab), total))
    sp.save_npz(MATRIX, m)
    entries = cropmaster.crops()
    vocab_set = set(vocab)
    cache: dict[str, np.ndarray] = {}

    def word_vec(t: str) -> np.ndarray:
        if t not in cache:
            r = [word_no[f] for f in kw.fuzzy_match(t, vocab_set)]
            cache[t] = m[r].max(axis=0).toarray()[0] if r else np.zeros(total, dtype="float32")
        return cache[t]

    cm = np.zeros((len(entries), total), dtype="float32")
    for i, e in enumerate(entries):
        for form in e["forms"]:
            cm[i] = np.maximum(cm[i], np.min([word_vec(t) for t in form], axis=0))
    np.save(CROPS, cm)
    META.write_text(json.dumps({"vocab": vocab, "docs": docs, "offsets": [offsets[d] for d in docs],
                                "lengths": [len(stats["pages"][d]) for d in docs],
                                "crops": [e["name"] for e in entries]}))
    print(f"matrix {m.shape} nnz={m.nnz:,}; crop matrix {cm.shape}; pages {total:,}; docs {len(docs)}")


_S: dict = {}


def _load() -> dict:
    if _S:
        return _S
    meta = json.loads(META.read_text())
    entries = {e["name"]: e for e in cropmaster.crops()}
    crops = [entries[n] for n in meta["crops"]]
    names = [tuple(f) for c in crops for f in c["forms"][:1]]
    related = np.zeros((len(crops), len(crops)), dtype=bool)
    formsets = [{tuple(f) for f in c["forms"]} for c in crops]
    nameset = [set(c["forms"][0]) for c in crops]
    for i in range(len(crops)):
        for j in range(len(crops)):
            related[i, j] = (i == j or bool(formsets[i] & formsets[j])
                             or nameset[i] <= nameset[j] or nameset[j] <= nameset[i])
    st = dict(m=sp.load_npz(MATRIX).tocsr(), cm=np.load(CROPS), vocab=meta["vocab"], docs=meta["docs"],
              offsets=np.array(meta["offsets"]), lengths=np.array(meta["lengths"]), crops=crops,
              related=related, word_no={w: i for i, w in enumerate(meta["vocab"])},
              vocab_set=set(meta["vocab"]), names=names)
    fidx: dict[str, list] = {}
    for ci, c in enumerate(crops):
        for form in c["forms"]:
            for base in kw._bases(form[0]):
                fidx.setdefault(base, []).append((ci, tuple(form)))
    st["form_index"], st["heads"], st["marks"] = fidx, headings.load(), {}
    st["table"] = common.load_documents().set_index("doc_id")      # the sheet is slow to read: once
    cm = st["cm"]
    scale = np.array([np.percentile(r[r > 0], 95) if (r > 0).any() else 1.0 for r in cm], dtype="float32")[:, None]
    norm = cm / scale
    sm = np.empty_like(norm)
    for off, ln in zip(st["offsets"], st["lengths"]):
        sm[:, off:off + ln] = uniform_filter1d(norm[:, off:off + ln], SMOOTH, axis=1, mode="constant")
    st["sm"] = sm
    _S.update(st)      # only now: a failure above must not leave a half-built cache behind
    return _S


# ---------------------------------------------------------------- query parsing

def _same(a: str, b: str) -> bool:
    return bool(kw._bases(a) & kw._bases(b))


def find_crop(tokens: list[str]) -> tuple[int, tuple[int, int]] | None:
    """(crop index, (first, last+1) token span) of the best crop mention: longest form, then the
    crop's own name over an alias, then the crop with the most pages."""
    s = _load()
    best, best_key = None, None
    for ci, c in enumerate(s["crops"]):
        for fi, form in enumerate(c["forms"]):
            n = len(form)
            for i in range(len(tokens) - n + 1):
                if all(_same(tokens[i + k], form[k]) for k in range(n)):
                    key = (n, fi == 0, int((s["cm"][ci] > 0).sum()))
                    if best_key is None or key > best_key:
                        best, best_key = (ci, (i, i + n)), key
    return best


def keyword_layers(words: list[str]) -> tuple[list[str], np.ndarray, np.ndarray, list[list[str]], list[str]]:
    """(kept words, bm25 layers, presence layers, forms per word, unmatched words): one row per
    word over the flat page axis, the best BM25 among its suffix variants."""
    s = _load()
    kept, forms, lay, miss = [], [], [], []
    for w in words:
        f = kw.fuzzy_match(w, s["vocab_set"])
        if not f:
            miss.append(w)
            continue
        kept.append(w)
        forms.append(f)
        lay.append(s["m"][[s["word_no"][x] for x in f]].max(axis=0).toarray()[0])
    bm = np.array(lay, dtype="float32") if lay else np.zeros((0, s["m"].shape[1]), dtype="float32")
    return kept, bm, bm > 0, forms, miss


# ---------------------------------------------------------------- zones

def _head_rows(di: int) -> list[tuple[int, float, float, frozenset]]:
    """[(0-based page, size ratio, y share of the page, crop indices named)] for EVERY heading line of document
    `di` (the set is empty for a heading that names no crop). Cached."""
    s = _load()
    cache = s.setdefault("rows", {})
    if di in cache:
        return cache[di]
    out = []
    for page, text, ratio, _bold, y in s["heads"].get(s["docs"][di], []):
        toks = kw.tokenize(text)
        hit = set()
        for i, t in enumerate(toks):
            for base in kw._bases(t):
                for ci, form in s["form_index"].get(base, ()):
                    if i + len(form) <= len(toks) and all(_same(toks[i + k], form[k]) for k in range(len(form))):
                        hit.add(ci)
        out.append((page - 1, ratio, y, frozenset(hit)))
    cache[di] = out
    return out


def _heading_marks(di: int) -> list[tuple[int, float, float, frozenset]]:
    """The headings of document `di` that name at least one crop."""
    return [r for r in _head_rows(di) if r[3]]


def _bucket(ratio: float) -> float:
    return round(ratio * 10) / 10


def level_profile(di: int) -> dict[float, tuple[int, int, int]]:
    """{size level: (headings, headings naming a crop, different crops named)} of document `di`; the level is the
    heading's font size over the body size, to 0.1."""
    acc: dict[float, list] = {}
    for _pg, ratio, _y, hit in _head_rows(di):
        a = acc.setdefault(_bucket(ratio), [0, 0, set()])
        a[0] += 1
        if hit:
            a[1] += 1
            a[2] |= hit
    return {k: (v[0], v[1], len(v[2])) for k, v in sorted(acc.items())}


def crop_level(di: int) -> float | None:
    """The size level at which this document's crop headings sit: of the levels above body text / sub-headings
    (>= LEVEL_MIN), the one naming the most different crops (the larger on a tie), provided it names at least
    LEVEL_CROPS. None when there is none. Cached."""
    s = _load()
    cache = s.setdefault("levels", {})
    if di not in cache:
        prof = {k: v for k, v in level_profile(di).items() if k >= LEVEL_MIN and v[2] >= LEVEL_CROPS}
        cache[di] = max(prof, key=lambda k: (prof[k][2], k)) if prof else None
    return cache[di]


def levels_report() -> dict:
    """Write data/zone_levels.json: for every document its heading size levels and the chosen crop level."""
    s = _load()
    out = {}
    for di, d in enumerate(s["docs"]):
        out[d] = {"crop_level": crop_level(di),
                  "levels": {str(k): list(v) for k, v in level_profile(di).items()}}
    LEVELS.write_text(json.dumps(out))
    return out


def heading_info(di: int, ci: int) -> tuple[list[int], list[int], list[tuple[int, int]]]:
    """(starts at any heading level, starts at chapter level, chapter-level stops): 0-based pages of
    headings naming this crop (or crops of its family, e.g. Maize and Maize / Corn) and no other,
    and, as (page, last page of a zone reaching it), chapter-level headings naming only other crops:
    a heading in the top TOP_SHARE of its page starts a page that is wholly the next chapter, so the
    zone stops the page before; lower down the page is shared and still counts. Likewise a start
    heading below TOP_SHARE of its page moves the zone start to the next page."""
    rel = _load()["related"][ci]
    level = crop_level(di)      # None: no level found, the fixed MAJOR_RATIO decides
    allh, major, stops = [], [], []
    for page, ratio, y, hit in (_head_rows(di) if level is not None else _heading_marks(di)):
        high = _bucket(ratio) >= level if level is not None else ratio >= MAJOR_RATIO
        if ci in hit and all(rel[j] for j in hit):
            first = page + 1 if y >= TOP_SHARE else page    # a low heading: the page is shared, the earlier chapter keeps it
            allh.append(first)
            if high:
                major.append(first)
        elif high and not any(rel[j] for j in hit):         # a heading of the crop level (or above) that is not this crop
            stops.append((page, page - 1 if y < TOP_SHARE else page))
    return sorted(set(allh)), sorted(set(major)), sorted(set(stops))


def zones_in_doc(u: np.ndarray, rivals: np.ndarray, starts=None, stops=()) -> list[tuple[int, int]]:
    """Zones of one crop in one document, 0-based inclusive page ranges. `u` is the crop's
    smoothed, normalised score per page, `rivals` the same for every unrelated crop (crops x pages).
    `starts` fixes the zone starts (heading pages); otherwise they come from the scores. `stops` are
    (page, last page) of chapter-level headings of other crops: a zone ends at the first one's last page
    (and only without any, by the score rule)."""
    n = len(u)
    peak = u.max() if n else 0.0
    if peak < MIN_CROP:
        return []
    rival = rivals.max(axis=0) if len(rivals) else np.zeros(n, dtype=u.dtype)
    if starts is None:
        seeds = np.nonzero((u >= SEED_FRAC * peak) & (u >= rival))[0]
        starts, prev = [], -10
        for p in seeds:
            if p - prev > 2:
                starts.append(int(p))
            prev = p
    beaten = (rival > u) & (rival >= MIN_CROP)
    out = []
    for st in starts:
        if out and st <= out[-1][1]:
            continue
        later = [e for pg, e in stops if pg > st]
        end = n - 1
        if later:       # a chapter heading of another crop is the surer end (a pest called "tobacco
            end = later[0]           # caterpillar" makes Tobacco outscore Cotton inside the cotton chapter)
        else:
            for p in range(st + 1, n - STOP_RUN + 1):
                if beaten[p:p + STOP_RUN].all():
                    end = p - 1
                    break
        out.append((st, end))
    return out


def doc_zones(di: int, ci: int, comp_idx) -> tuple[list[tuple[int, int]], str]:
    """(zones, source) of crop `ci` in document `di`: "heading" when a heading names the crop and
    the crop's scores support it, else "bm25"."""
    s = _load()
    lo, ln = int(s["offsets"][di]), int(s["lengths"][di])
    u, rivals = s["sm"][ci, lo:lo + ln], s["sm"][comp_idx, lo:lo + ln]
    peak = u.max() if ln else 0.0
    anyh, major, stops = heading_info(di, ci)
    for cand in (major, anyh):
        ok = [p for p in cand if p < ln and peak >= MIN_CROP and u[p:p + 3].max() >= SUPPORT * peak]
        if ok:
            return zones_in_doc(u, rivals, ok, stops), "heading"
    return zones_in_doc(u, rivals), "bm25"


def best_stretch(present: np.ndarray, bm: np.ndarray, lo: int, hi: int, need: int, max_span: int = MAX_SPAN):
    """Forward pass over the layers inside pages lo..hi (global, inclusive). Returns
    (groups covered, first page, last page, strength) of the shortest stretch of <= max_span pages
    holding the most groups (at least `need`), or None. `max_span` caps the stretch length."""
    L = present[:, lo:hi + 1]
    g, n = L.shape
    INF = 10 ** 9
    nxt = np.full((g, n + 1), INF, dtype="int64")
    for p in range(n - 1, -1, -1):
        nxt[:, p] = np.where(L[:, p], p, nxt[:, p + 1])
    for k in range(g, need - 1, -1):
        best = None
        for i in range(n):
            if not L[:, i].any():
                continue
            e = np.sort(nxt[:, i])[k - 1]
            if e < INF and e - i + 1 <= max_span:
                span = int(e) - i + 1
                if best is None or span < best[0]:
                    best = (span, i, int(e))
        if best:
            _, i, e = best
            strength = float(bm[:, lo + i:lo + e + 1].max(axis=1).sum())
            return k, lo + i, lo + e, strength
    return None


def tightest_stretch(present: np.ndarray, bm: np.ndarray, lo: int, hi: int, need: int, max_span: int = MAX_SPAN,
                     required: int | None = None):
    """Like best_stretch, but PROXIMITY comes first: over pages lo..hi (global, inclusive) the stretch of
    <= max_span pages with the fewest pages that still holds at least `need` groups; on equal length the one
    holding more groups. `required` is the row of a group that MUST be in the stretch (it still counts
    towards `need`). Returns (groups covered, first page, last page, strength) or None."""
    L = present[:, lo:hi + 1]
    n = L.shape[1]
    INF = 10 ** 9
    nxt = np.full((L.shape[0], n + 1), INF, dtype="int64")
    for p in range(n - 1, -1, -1):
        nxt[:, p] = np.where(L[:, p], p, nxt[:, p + 1])
    if required is not None:
        others = np.delete(np.arange(L.shape[0]), required)
    best = None
    for i in range(n):
        if not L[:, i].any():
            continue
        if required is None:
            e = int(np.sort(nxt[:, i])[need - 1])
        else:   # the required group, plus the nearest need-1 of the others
            e = int(nxt[required, i])
            if need > 1:
                e = max(e, int(np.sort(nxt[others, i])[need - 2]))
        if e >= INF or e - i + 1 > max_span:
            continue
        key = (e - i + 1, -int((nxt[:, i] <= e).sum()))
        if best is None or key < best[0]:
            best = (key, i, e)
    if best is None:
        return None
    (span, negk), i, e = best
    return -negk, lo + i, lo + e, float(bm[:, lo + i:lo + e + 1].max(axis=1).sum())


def covered_pages(di: int) -> np.ndarray:
    """Boolean per page of document `di`: inside the zone of ANY crop (heading or score based). Cached."""
    s = _load()
    cache = s.setdefault("covered", {})
    if di not in cache:
        lo, ln = int(s["offsets"][di]), int(s["lengths"][di])
        mask = np.zeros(ln, dtype=bool)
        for ci in np.nonzero(s["sm"][:, lo:lo + ln].max(axis=1) >= MIN_CROP)[0]:
            zs, _ = doc_zones(di, int(ci), np.nonzero(~s["related"][ci])[0])
            for z0, z1 in zs:
                mask[z0:z1 + 1] = True
        cache[di] = mask
    return cache[di]


# ---------------------------------------------------------------- query

def _snippet(doc_id: str, page: int, forms: list[str]) -> str:
    import pagetext
    try:
        if common.pdf_path(doc_id).exists():
            import fitz
            with fitz.open(common.pdf_path(doc_id)) as pdf:
                text = " ".join(pdf[page - 1].get_text().split())
        else:
            text = " ".join(pagetext.pages(doc_id)[page - 1].split())
    except Exception:
        return ""
    pat = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    m = re.search(rf"\b(?:{pat})", text, re.I) if pat else None
    a = max(0, (m.start() if m else 0) - 110)
    return ("..." if a else "") + text[a:a + 300]


RELAXED_DOCS = 10    # documents returned when none meets the threshold


def search_zone(query: str, cover: float = COVER, max_span: int = MAX_SPAN,
                need_cap: int = NEED_CAP, relaxed: bool = False, scope: str = "all") -> list[dict]:
    """Zone search. When no document meets the threshold the search runs again with `relaxed`: no minimum,
    and the documents with the most keyword groups (then the closest together) are returned, `passed` False."""
    out = _search_zone(query, cover, max_span, need_cap, relaxed, scope)
    if not out and not relaxed:
        return _search_zone(query, cover, max_span, need_cap, True, scope)
    return out


def _search_zone(query, cover, max_span, need_cap, relaxed, scope="all") -> list[dict]:
    s = _load()
    docs = s["table"]
    tokens = list(dict.fromkeys(kw.tokenize(query)))
    hit = find_crop(tokens)
    if hit is None:     # no crop of the crop master in the query: keywords only, over every page
        words, bm, present, forms, missing = keyword_layers(tokens)
        info = {"crop": None, "crop_words": [], "groups": dict(zip(words, forms)), "unmatched": missing,
                "need": 0, "max_span": max_span}
        return _no_zone_docs(None, [], words, forms, present, bm, set(), info, cover, max_span, need_cap,
                             relaxed, scope) if words else []
    ci, (a, b) = hit
    crop = s["crops"][ci]
    others = [t for i, t in enumerate(tokens) if not a <= i < b]
    words, bm, present, forms, missing = keyword_layers(others)
    n = len(words)
    need = max(1, min(need_cap or n, math.ceil(round(cover * n, 6)))) if n else 0   # need_cap 0 = no cap
    need_run = 1 if relaxed else need
    info = {"crop": crop["name"], "crop_words": tokens[a:b], "groups": dict(zip(words, forms)),
            "unmatched": missing, "need": need, "max_span": max_span}
    comp_idx = np.nonzero(~s["related"][ci])[0]
    res, has_zone = [], set()
    for di, d in enumerate(s["docs"]):
        if not common.in_scope(d, scope):
            continue
        lo, ln = int(s["offsets"][di]), int(s["lengths"][di])
        cs = s["cm"][ci, lo:lo + ln]
        zs, src = doc_zones(di, ci, comp_idx)
        if zs:
            has_zone.add(di)
        best = None
        for z0, z1 in zs:
            if n == 0:
                pk = z0 + int(cs[z0:z1 + 1].argmax())   # the crop's own best page, not the smoothed start
                cand = (1, lo + pk, lo + pk, float(cs[pk]), (z0, z1))
            else:
                r = best_stretch(present, bm, lo + z0, lo + z1, need_run, max_span)
                cand = (*r, (z0, z1)) if r else None
            if cand:
                z0_, z1_ = cand[4]
                zq = float(np.mean(s["sm"][ci, lo + z0_:lo + z1_ + 1])) * min(z1_ - z0_ + 1, ZONE_CAP) ** 0.5
                cand = (*cand, zq)
            if cand and (best is None or (cand[0], -(cand[2] - cand[1]), cand[5], cand[3]) >
                         (best[0], -(best[2] - best[1]), best[5], best[3])):
                best = cand
        if best:
            res.append((di, d, zs, best, src))
    res.sort(key=lambda r: (-r[3][0], r[3][2] - r[3][1], -r[3][5], -r[3][3]))
    out = []
    for di, d, zs, (k, g0, g1, strength, (z0, z1), _zq), src in res[:MAX_DOCS]:
        lo = int(s["offsets"][di])
        p0, p1 = g0 - lo + 1, g1 - lo + 1
        covered = [w for w, row in zip(words, present[:, g0:g1 + 1]) if row.any()]
        row = docs.loc[d]
        pages = []
        for p in range(p0, p1 + 1):
            here = [w for w, r in zip(words, present[:, lo + p - 1]) if r]
            flat = [f for w in here for f in forms[words.index(w)]] or list(tokens[a:b])
            pages.append({"page": p, "score": len(here), "snippet": _snippet(d, p, flat), "groups": here})
        out.append({"doc_id": d, "name": row["name"], "link": row["link"], "mode": "crop-zone",
                    "total_pages": int(s["lengths"][di]), "zones": [[x + 1, y + 1] for x, y in zs],
                    "zone": [z0 + 1, z1 + 1], "zone_source": src, "stretch": [p0, p1], "covered": covered,
                    "missing": [w for w in words if w not in covered], "n_groups": n,
                    "fraction": round(k / n, 3) if n else 1.0, "strength": round(strength, 2),
                    "passed": k >= need, "pages": [p["page"] for p in pages],
                    "page_groups": [pages], "keywords": info})
    out += _no_zone_docs(ci, tokens[a:b], words, forms, present, bm, has_zone, info, cover, max_span, need_cap, relaxed,
                         scope)
    return _relaxed_order(out, info) if relaxed else out


def _relaxed_order(out: list[dict], info: dict) -> list[dict]:
    """Best effort: most keyword groups first, then the fewest pages; zone results before no-zone ones on a tie."""
    info["relaxed"] = True
    out.sort(key=lambda x: (-len(x["covered"]), x["stretch"][1] - x["stretch"][0]))
    return out[:RELAXED_DOCS]


def _no_zone_docs(ci, crop_words, words, forms, present, bm, has_zone, info, cover, max_span, need_cap,
                  relaxed=False, scope="all") -> list[dict]:
    """Documents with NO zone for the query's crop: look only at pages outside the zones of every crop,
    with the crop counted as one more keyword group AND REQUIRED in the stretch. A document needs the cover
    share (75%) of the groups OR need_cap (4), whichever is fewer, crop included. Ranked by proximity: fewest pages holding that many
    groups first, then more groups, then keyword strength (a stretch is counted in whole pages)."""
    s = _load()
    docs = s["table"]
    if ci is None:      # no crop: plain keyword groups, no required one, no pages excluded
        words_f, forms_f, present_f, bm_f = words, forms, present, bm
    else:
        crop_row = s["cm"][ci]
        words_f = words + [" ".join(crop_words)]
        forms_f = forms + [[" ".join(f) for f in s["crops"][ci]["forms"]] + list(crop_words)]
        present_f = np.vstack([present, (crop_row > 0)[None]])
        bm_f = np.vstack([bm, crop_row[None]])
    n_all = len(words_f)
    need = max(1, min(need_cap or n_all, math.ceil(round(cover * n_all, 6))))
    info["fallback_need"], info["fallback_groups"] = need, words_f
    if relaxed:
        need_run = 1
    else:
        need_run = need
    info["no_crop"] = ci is None
    found = []
    for di, d in enumerate(s["docs"]):
        if di in has_zone or not common.in_scope(d, scope):
            continue
        lo, ln = int(s["offsets"][di]), int(s["lengths"][di])
        if (ci is not None and not present_f[-1, lo:lo + ln].any()) or present_f[:, lo:lo + ln].any(axis=1).sum() < need_run:
            continue      # the crop word (last row) must occur, and enough groups overall
        free = ~covered_pages(di) if ci is not None else np.ones(ln, dtype=bool)
        best = None
        i = 0
        while i < ln:
            if not free[i]:
                i += 1
                continue
            j = i
            while j + 1 < ln and free[j + 1]:
                j += 1
            req = None if ci is None else n_all - 1
            if relaxed:     # the most groups that fit, then the tightest
                r = next((x for k in range(n_all, 0, -1)
                          if (x := tightest_stretch(present_f, bm_f, lo + i, lo + j, k, max_span, req))), None)
            else:
                r = tightest_stretch(present_f, bm_f, lo + i, lo + j, need, max_span, req)
            key = lambda x: (-x[0], x[2] - x[1], -x[3]) if relaxed else (x[2] - x[1], -x[0], -x[3])
            if r and (best is None or key(r) < key(best)):
                best = r
            i = j + 1
        if best:
            found.append((di, d, best))
    found.sort(key=lambda f: (-f[2][0], f[2][2] - f[2][1], -f[2][3]) if relaxed else
               (f[2][2] - f[2][1], -f[2][0], -f[2][3]))
    out = []
    for di, d, (k, g0, g1, strength) in found[:MAX_DOCS]:
        lo = int(s["offsets"][di])
        p0, p1 = g0 - lo + 1, g1 - lo + 1
        covered = [w for w, row in zip(words_f, present_f[:, g0:g1 + 1]) if row.any()]
        pages = []
        for p in range(p0, p1 + 1):
            here = [w for w, r in zip(words_f, present_f[:, lo + p - 1]) if r]
            flat = [f for w in here for f in forms_f[words_f.index(w)]] or list(crop_words)
            pages.append({"page": p, "score": len(here), "snippet": _snippet(d, p, flat), "groups": here})
        out.append({"doc_id": d, "name": docs.loc[d]["name"], "link": docs.loc[d]["link"], "mode": "crop-zone",
                    "fallback": True, "no_crop": ci is None, "total_pages": int(s["lengths"][di]), "zones": [],
                    "zone": [p0, p1], "zone_source": "no crop in query" if ci is None else "no crop zone", "stretch": [p0, p1], "covered": covered,
                    "missing": [w for w in words_f if w not in covered], "n_groups": n_all,
                    "fraction": round(k / n_all, 3), "strength": round(strength, 2), "passed": k >= need,
                    "pages": [p["page"] for p in pages], "page_groups": [pages], "keywords": info})
    return out


# ---------------------------------------------------------------- CLI

def _cli() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    if sys.argv[1] == "build":
        build()
        _S.clear()
        lv = levels_report()
        print(f"levels: {sum(1 for v in lv.values() if v['crop_level'] is not None)} of {len(lv)} documents have a crop level")
        return
    if sys.argv[1] == "levels":
        lv = levels_report()
        print(f"levels: {sum(1 for v in lv.values() if v['crop_level'] is not None)} of {len(lv)} documents have a crop level")
        return
    import time
    q = " ".join(sys.argv[1:])
    t = time.time()
    r = search_zone(q)
    if r:
        i = r[0]["keywords"]
        print(f"crop: {i['crop']} (from {i['crop_words']}); groups {list(i['groups'])}; unmatched {i['unmatched']}; need {i['need']}")
    for x in r[:10]:
        print(f"{x['fraction']:.2f} stretch p{x['stretch'][0]}-{x['stretch'][1]} zone p{x['zone'][0]}-{x['zone'][1]} "
              f"zones={x['zones'][:4]} missing={x['missing']} {x['name'][:55]}")
    print(f"{len(r)} docs, {time.time() - t:.1f}s")


if __name__ == "__main__":
    _cli()
