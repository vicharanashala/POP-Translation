"""Per-page text store for documents whose PDF is not kept on disk.

    data/pagetext/<doc_id>.json.gz            JSON list of page strings (page 1 = first element), same text as
                                              textcheck.extract_pages
    data/pagetext/<doc_id>.headings.json.gz   heading rows in the zone_headings.json format
                                              [[page, text, ratio, bold, y], ...]
pages(doc_id) reads the PDF when it is on disk, else the store.
"""
from __future__ import annotations

import gzip
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402

DIR = common.DATA / "pagetext"


def _write(path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    tmp.replace(path)


def save(doc_id: str, pages: list[str], heads: list[list]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    _write(DIR / f"{doc_id}.json.gz", pages)
    _write(DIR / f"{doc_id}.headings.json.gz", heads)


def stored_pages(doc_id: str) -> list[str] | None:
    p = DIR / f"{doc_id}.json.gz"
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def stored_headings() -> dict[str, list]:
    out = {}
    for p in DIR.glob("*.headings.json.gz") if DIR.exists() else []:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            out[p.name[:-len(".headings.json.gz")]] = json.load(f)
    return out


def pages(doc_id: str) -> list[str]:
    """Page texts of a document: from its PDF when present, else from the store."""
    pdf = common.pdf_path(doc_id)
    if pdf.exists():
        from textcheck import extract_pages
        return extract_pages(pdf)
    got = stored_pages(doc_id)
    if got is None:
        raise FileNotFoundError(f"no PDF and no stored page text for {doc_id}")
    return got
