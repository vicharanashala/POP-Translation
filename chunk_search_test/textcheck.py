"""Per-document text-extractability and English check -> data/text_report.csv.

For each downloaded PDF: extract text per page with PyMuPDF (no OCR), then
    chars        total characters extracted
    pages_text   pages with >= MIN_CHARS characters
    latin_ratio  share of letters that are Latin (a script test; robust where
                 language detection is not, e.g. Gurmukhi vs English)
    lang         langdetect on a sample of the text
    verdict      english_text | scanned_no_text | non_english | mixed | not_pdf

Reusable: extract_pages(path) returns [page text, ...] and is what the chunker uses.
    python chunk_search_test/textcheck.py
"""
from __future__ import annotations

import sys
import unicodedata

import fitz
import pandas as pd
from langdetect import DetectorFactory, detect_langs

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from common import DATA, load_documents, pdf_path  # noqa: E402

DetectorFactory.seed = 0
MIN_CHARS = 50  # a page with fewer characters counts as having no text layer


def extract_pages(path) -> list[str]:
    with fitz.open(path) as pdf:
        return [page.get_text("text") for page in pdf]


def latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum("LATIN" in unicodedata.name(c, "") for c in letters) / len(letters)


def lang_of(text: str) -> str:
    try:
        top = detect_langs(text[:5000])[0]
        return f"{top.lang}:{top.prob:.2f}"
    except Exception:
        return ""


def check(path, fmt: str) -> dict:
    if fmt != "pdf":
        return {"verdict": "not_pdf"}
    try:
        pages = extract_pages(path)
    except Exception as e:
        return {"verdict": f"unreadable: {e!r}"[:80]}
    text = "\n".join(pages)
    with_text = sum(len(p.strip()) >= MIN_CHARS for p in pages)
    ratio = latin_ratio(text)
    lang = lang_of(text)
    if with_text == 0:
        verdict = "scanned_no_text"
    elif ratio >= 0.9 and lang.startswith("en"):
        verdict = "english_text"
    elif ratio < 0.5:
        verdict = "non_english"
    else:
        verdict = "mixed"
    return {"pdf_pages": len(pages), "pages_text": with_text, "chars": len(text),
            "latin_ratio": round(ratio, 3), "lang": lang, "verdict": verdict}


def main() -> None:
    rows = []
    for d in load_documents().itertuples():
        p = pdf_path(d.doc_id, d.fmt)
        if not p.exists():
            continue
        rows.append({"doc_id": d.doc_id, "name": d.name, "fmt": d.fmt,
                     "catalogued_lang": d.language, **check(p, d.fmt)})
    out = pd.DataFrame(rows)
    out.to_csv(DATA / "text_report.csv", index=False)
    print(len(out), "checked")
    print(out["verdict"].value_counts().to_string())
    print(pd.crosstab(out["catalogued_lang"], out["verdict"]).to_string())


if __name__ == "__main__":
    main()
