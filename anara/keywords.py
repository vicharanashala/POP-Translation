"""TF-IDF keyword index: keyword -> {doc_id: [pages]}, for important terms only.

    tokenize(text)                      -> list of stemmed terms (lowercase, stop words out)
    build_stats()                       -> per-page term counts + document frequencies (cached)
    build_index(stats, threshold)       -> {term: {doc_id: [page, ...]}}
    save_index(index) / load_index()    -> data/keyword_index.json

Dictionary and page rules: see build_index.
    BM25 per page (see page_scores); idf counts DOCUMENTS containing the term.

    python anara/keywords.py stats | build | probe "query" ...
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import re
import sys
from collections import Counter, defaultdict

import pandas as pd
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402
import pagetext  # noqa: E402
from textcheck import extract_pages  # noqa: E402

STATS = common.DATA / "keyword_stats.pkl"
ZONE_STATS = common.DATA / "zone_stats.pkl"   # same as STATS plus the central documents (keyword-only, no embeddings)
INDEX = common.DATA / "keyword_index.json"

_word = re.compile(r"[a-z][a-z0-9]+(?:-[a-z0-9]+)*")
_vowel = re.compile(r"[aeiou]")  # a token with no vowel is OCR junk (qwk, hdf, mmt)


def tokenize(text: str) -> list[str]:
    """Lowercase raw words; stop words, tokens under 3 letters and tokens without a vowel (a,e,i,o,u) out. NO stemming: the index
    keeps real words, so counts and rarity are real; variants are matched at query time by
    fuzzy_match()."""
    return [w for w in _word.findall(text.lower())
            if len(w) >= 3 and w not in ENGLISH_STOP_WORDS and _vowel.search(w)]


SUFFIXES = ("s", "es", "d", "ed", "ing", "ly", "er", "ers", "est", "ion", "ions", "al", "ally",
            "ness", "ment", "ments", "able", "ful", "ive", "ize", "ized", "izing", "ise", "ised", "ity")
MIN_BASE = 4        # a word is only stripped down to a base of at least this many letters


def _bases(word: str) -> set[str]:
    """The word, and the word with one suffix taken off (undoing a doubled consonant, and
    turning -ies back into -y): controlling -> controll -> control; varieties -> variet -> variety."""
    out = {word}
    for suf in SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= MIN_BASE:
            base = word[: -len(suf)]
            out.add(base)
            if len(base) > MIN_BASE and base[-1] == base[-2] and base[-1] not in "aeiou":
                out.add(base[:-1])
            if suf == "es" and base.endswith("i"):
                out.add(base[:-1] + "y")
    if word.endswith("ies") and len(word) - 3 >= MIN_BASE:
        out.add(word[:-3] + "y")
    return out


def fuzzy_match(word: str, vocab) -> list[str]:
    """Index words a query word stands for. ONLY suffix changes: strip a suffix from the word
    and/or add one (control -> controlled, controlling, controls; bollworms -> bollworm). Single-
    letter slips, misspellings and unrelated words one edit away are NOT matched. Candidate
    forms are generated and kept if they are in `vocab`."""
    vocab = vocab if isinstance(vocab, (set, dict)) else set(vocab)
    forms = set()
    for base in _bases(word):
        forms.add(base)
        for suf in SUFFIXES:
            forms.add(base + suf)
            forms.add(base + "e" + suf)                 # make -> making: mak + e + ing
            if suf in ("ing", "ed", "er", "ers", "est") and base[-1] not in "aeiou":
                forms.add(base + base[-1] + suf)         # run -> running
        if base.endswith("y"):
            forms.add(base[:-1] + "ies")
            forms.add(base[:-1] + "ied")
    return sorted(f for f in forms if f in vocab)


def build_stats(refresh: bool = False, zone: bool = False) -> dict:
    """{'pages': {doc_id: [Counter per page]}, 'df': Counter, 'n_docs': int}

    zone=True: the file for zone mode, which also holds the central documents (they have no embeddings,
    so hybrid/restrict, which pair this data with Atlas chunks, keep reading the file without them)."""
    path = ZONE_STATS if zone else STATS
    if path.exists() and not refresh:
        return pickle.loads(path.read_bytes())
    report = pd.read_csv(common.DATA / "text_report.csv")
    ids = report.loc[report.verdict == "english_text", "doc_id"].tolist()
    if not zone:
        ids = [i for i in ids if i in set(common.load_documents(central=False)["doc_id"])]
    pages, df = {}, Counter()
    for n, doc_id in enumerate(ids, 1):
        counts = [Counter(tokenize(p)) for p in pagetext.pages(doc_id)]
        pages[doc_id] = counts
        df.update(set().union(*counts) if counts else set())
        if n % 50 == 0:
            print(f"  {n}/{len(ids)} documents", flush=True)
    stats = {"pages": pages, "df": df, "n_docs": len(ids)}
    path.write_bytes(pickle.dumps(stats))
    return stats


K1, B = 1.2, 0.75


def bm25_idf(n_docs: int, df: int) -> float:
    return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))


def window_df(stats: dict, window: int) -> tuple[Counter, int]:
    """Document frequency over WINDOWS: every page of every document strung into one sequence,
    cut into consecutive blocks of `window` pages (blocks may straddle documents)."""
    seq = [c for counts in stats["pages"].values() for c in counts]
    wdf: Counter = Counter()
    n = 0
    for i in range(0, len(seq), window):
        n += 1
        wdf.update(set().union(*seq[i:i + window]))
    return wdf, n


def page_scores(stats: dict, window: int | None = None):
    """Yield (term, doc_id, page_no, tf, bm25) for every term on every page.

    bm25 = idf * tf*(K1+1) / (tf + K1*(1 - B + B*page_len/avg_page_len)); the count part
    saturates at K1+1 and a longer-than-average page needs more repeats for the same score.
    idf counts DOCUMENTS containing the term, or with `window` the `window`-page blocks of the
    whole corpus (see window_df): the score stays per page, only the rarity changes.
    """
    if window:
        df, n = window_df(stats, window)
    else:
        df, n = stats["df"], stats["n_docs"]
    idf = {t: bm25_idf(n, c) for t, c in df.items()}
    lens = [sum(c.values()) for v in stats["pages"].values() for c in v]
    avg = sum(lens) / len(lens)
    for doc_id, counts in stats["pages"].items():
        for page_no, c in enumerate(counts, 1):
            norm = K1 * (1 - B + B * sum(c.values()) / avg)
            for t, tf in c.items():
                yield t, doc_id, page_no, tf, idf[t] * tf * (K1 + 1) / (tf + norm)


def page_keywords(stats: dict, top_k: int = 10, window: int | None = None) -> dict:
    """{(doc_id, page_no): [terms]}: the top_k terms of each page by BM25. No document-frequency
    filter: BM25's idf already discounts common words, per page and not globally."""
    best: dict = defaultdict(list)
    for t, doc_id, page_no, tf, sc in page_scores(stats, window):
        best[(doc_id, page_no)].append((sc, t))
    return {k: [t for _, t in sorted(v, reverse=True)[:top_k]] for k, v in best.items()}


THRESHOLD = 1.0   # provisional: highest value keeping every page of rare test words (see INDEX.md)
WINDOW = 10


def build_index(stats: dict, threshold: float = THRESHOLD, window: int = WINDOW) -> dict:
    """{word: {doc_id: [pages]}}: a page is listed for a word when its per-page BM25 score
    (idf from `window`-page blocks of the whole corpus) is >= threshold. No other filter."""
    index: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for t, doc_id, page_no, _, sc in page_scores(stats, window):
        if sc >= threshold:
            index[t][doc_id].append(page_no)
    return {t: dict(d) for t, d in index.items()}


def save_index(index: dict) -> None:
    INDEX.write_text(json.dumps(index))


def load_index() -> dict:
    return json.loads(INDEX.read_text())


def probe(stats: dict, queries: list[str]) -> None:
    idx = build_index(stats)
    for q in queries:
        print(f"\n{q}")
        pages = set()
        for t in dict.fromkeys(tokenize(q)):
            d = idx.get(t)
            if d is None:
                why = "not in dictionary (df=%d)" % stats["df"].get(t, 0)
                print(f"   {t:12} {why}")
                continue
            n = sum(len(p) for p in d.values())
            pages |= {(doc, p) for doc, ps in d.items() for p in ps}
            print(f"   {t:12} df={stats['df'][t]:3}  {len(d):3} docs  {n:5} pages")
        print(f"   union of listed pages: {len(pages)} of 11555")


def rarity_csv(stats: dict, window: int = 10) -> None:
    """All words, rarest first by window count -> data/word_rarity.csv"""
    import csv
    wdf, n = window_df(stats, window)
    mx: dict = {}
    for t, _, _, _, sc in page_scores(stats, window):
        if sc > mx.get(t, 0):
            mx[t] = sc
    rows = sorted(wdf, key=lambda t: (wdf[t], -mx[t]))
    with open(common.DATA / "word_rarity.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "word", "windows", "docs", "window_idf", "max_page_bm25"])
        for i, t in enumerate(rows, 1):
            w.writerow([i, t, wdf[t], stats["df"][t], round(bm25_idf(n, wdf[t]), 3), round(mx[t], 3)])
    print(f"{len(rows)} words, {n} windows -> data/word_rarity.csv")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["stats", "build", "probe", "rarity", "fuzzy"])
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("queries", nargs="*")
    a = ap.parse_args()
    stats = build_stats(a.refresh)
    print(f"{stats['n_docs']} docs, {sum(len(v) for v in stats['pages'].values())} pages, "
          f"{len(stats['df'])} distinct terms")
    if a.cmd == "rarity":
        rarity_csv(stats)
        return
    if a.cmd == "fuzzy":
        vocab = list(stats["df"])
        for w in a.queries:
            m = fuzzy_match(w, vocab)
            print(f"{w:12} -> {len(m):3}: {' '.join(sorted(m, key=lambda x: (len(x), x))[:30])}")
        return
    if a.cmd == "probe":
        probe(stats, a.queries)
        return
    idx = build_index(stats)
    pairs = sum(len(p) for d in idx.values() for p in d.values())
    print(f"window {WINDOW} BM25>={THRESHOLD}: {len(idx)} keywords, {pairs} (term,page) pairs")
    if a.cmd == "build":
        save_index(idx)
        print("saved", INDEX)


if __name__ == "__main__":
    main()
