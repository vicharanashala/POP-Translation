"""Heading lines of every PDF, from font size and weight (no AI, no OCR).

    extract(pdf_path)   -> [{"page", "text", "ratio", "bold", "y"}]
    build()             -> data/zone_headings.json for every english_text document
    load()              -> {doc_id: [[page, text, ratio, bold, y], ...]}

A line is a heading when it is short and either clearly larger than the document's body text
(ratio >= HEAD_RATIO) or bold and very short. `ratio` is the line's font size over the
document's body size (the character-weighted median size), so chapter titles (2. COTTON, 24 pt over
11 pt body = 2.2) can be told from sub-headings (1.1-1.3). `y` is the line's top as a share of the page height (0 = top). A text that repeats on many pages is a
running header or footer and is dropped.
    python anara/headings.py            build, then print a summary
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter

import fitz

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402

OUT = common.DATA / "zone_headings.json"
HEAD_RATIO = 1.15       # larger than the body by this factor
BOLD_MAX_CHARS = 40     # a bold line of the body size counts only when this short
MAX_CHARS = 70
REPEAT_PAGES = 5        # a text on this many pages and on >= REPEAT_SHARE of them is running text
REPEAT_SHARE = 0.2


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def extract(path) -> list[dict]:
    lines, weight = [], Counter()
    with fitz.open(path) as pdf:
        n_pages = len(pdf)
        for pn, pg in enumerate(pdf, 1):
            for b in pg.get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    sp = [s for s in l["spans"] if s["text"].strip()]
                    if not sp:
                        continue
                    text = " ".join("".join(s["text"] for s in sp).split())
                    size = max(s["size"] for s in sp)
                    bold = all(s["flags"] & 16 or "Bold" in s["font"] for s in sp)
                    weight[round(size, 1)] += len(text)
                    lines.append((pn, text, size, bold, l["bbox"][1] / pg.rect.height))
    if not weight:
        return []
    total, acc, body = sum(weight.values()), 0, 0.0
    for size in sorted(weight):                       # character-weighted median size
        acc += weight[size]
        if acc >= total / 2:
            body = size
            break
    cand = []
    for pn, text, size, bold, y in lines:
        if not 2 <= len(text) <= MAX_CHARS or not re.search(r"[A-Za-z]{2}", text):
            continue
        ratio = size / body
        if ratio >= HEAD_RATIO or (bold and len(text) <= BOLD_MAX_CHARS):
            cand.append({"page": pn, "text": text, "ratio": round(ratio, 2), "bold": int(bold), "y": round(y, 2)})
    pages_of: dict[str, set] = {}
    for c in cand:
        pages_of.setdefault(_norm(c["text"]), set()).add(c["page"])
    running = {t for t, p in pages_of.items() if len(p) >= REPEAT_PAGES and len(p) >= REPEAT_SHARE * n_pages}
    return [c for c in cand if _norm(c["text"]) not in running]


def build() -> dict:
    import pandas as pd
    report = pd.read_csv(common.DATA / "text_report.csv")
    ids = report.loc[report.verdict == "english_text", "doc_id"].tolist()
    out = {}
    for n, d in enumerate(ids, 1):
        if common.pdf_path(d).exists():
            out[d] = [[h["page"], h["text"], h["ratio"], h["bold"], h["y"]] for h in extract(common.pdf_path(d))]
        if n % 50 == 0:
            print(f"  {n}/{len(ids)} documents", flush=True)
    # documents without a PDF on disk keep their headings in data/pagetext (pagetext.stored_headings)
    OUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def load() -> dict[str, list]:
    import pagetext
    return {**json.loads(OUT.read_text(encoding="utf-8")), **pagetext.stored_headings()}


if __name__ == "__main__":
    data = build()
    sizes = sorted(len(v) for v in data.values())
    print(f"{len(data)} documents, {sum(sizes):,} heading lines; per document median {sizes[len(sizes) // 2]}, "
          f"max {sizes[-1]}, with none: {sum(1 for s in sizes if s == 0)}")
