"""Split a document's pages into overlapping word-window chunks that keep their page.

    chunks = chunk_pages(extract_pages(path))   # [{"page": 1, "chunk_index": 0, "text": ...}]

A chunk never spans two pages, so a hit always names one real PDF page. A short
page is one chunk; a long page is cut into WORDS-sized windows overlapping by
OVERLAP. Windows under MIN_WORDS are dropped unless they are all a page has.
"""
from __future__ import annotations

import re

WORDS = 180
OVERLAP = 30
MIN_WORDS = 15


def _clean(text: str) -> list[str]:
    return re.sub(r"\s+", " ", text).strip().split(" ") if text.strip() else []


def chunk_pages(pages: list[str], words: int = WORDS, overlap: int = OVERLAP) -> list[dict]:
    out = []
    for page_no, text in enumerate(pages, 1):
        toks = _clean(text)
        if not toks:
            continue
        step = words - overlap
        windows = [toks[i:i + words] for i in range(0, len(toks), step)]
        # the last window is often a sliver already covered by the overlap
        if len(windows) > 1 and len(windows[-1]) <= overlap:
            windows.pop()
        kept = [w for w in windows if len(w) >= MIN_WORDS] or windows[:1]
        for idx, w in enumerate(kept):
            out.append({"page": page_no, "chunk_index": idx, "text": " ".join(w)})
    return out
