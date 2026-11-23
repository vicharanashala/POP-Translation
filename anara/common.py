"""Shared helpers for anara. Every script imports from here; see INDEX.md.

Standalone: nothing in dashboard/ is imported. The only repo code reused is
helpers/zoho_workdrive.py (read-only file download).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA = HERE / "data"
PDF_DIR = DATA / "pdfs"
XLSX = HERE / "pop-documents-state-2026-11-12.xlsx"

# Zoho credentials come from the repo's .env (read only). Mongo settings come
# from anara/.env so the test DB never mixes with POP's.
load_dotenv(ROOT / ".env")
load_dotenv(HERE / ".env", override=True)
for p in (str(ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


CENTRAL = DATA / "central_docs.csv"   # written by central_docs.py
ALIASES = DATA / "link_aliases.json"  # written by link_aliases.py


def _central() -> pd.DataFrame:
    if not CENTRAL.exists():
        return pd.DataFrame()
    import json
    c = pd.read_csv(CENTRAL, keep_default_na=False)
    c["states"] = c["states"].map(json.loads)
    return c


_central_set = None


def central_ids() -> set[str]:
    """doc_ids with advisory scope Central (data/central_docs.csv); every other document in the sheet is state."""
    global _central_set
    if _central_set is None:
        _central_set = set(pd.read_csv(CENTRAL, keep_default_na=False)["doc_id"]) if CENTRAL.exists() else set()
    return _central_set


def in_scope(doc_id: str, scope: str) -> bool:
    """scope: "all", "state" or "central"."""
    return scope == "all" or (doc_id in central_ids()) == (scope == "central")


def load_documents(central: bool = True) -> pd.DataFrame:
    """One row per UNIQUE document (the sheet has one row per placement), then, if `central`,
    the Central-scope documents of data/central_docs.csv whose doc_id is not in the sheet.

    Columns: doc_id (pop_unique_documents _id), name, link, file_id, pages,
    language (as catalogued), fmt, states.
    """
    df = pd.read_excel(XLSX)
    states = df.groupby("unique_document__id")["state_raw"].agg(lambda s: sorted(set(s)))
    u = df.drop_duplicates("unique_document__id").copy()
    out = pd.DataFrame({
        "doc_id": u["unique_document__id"],
        "name": u["unique_document_shareable_name"],
        "link": u["unique_document_shareable_link"],
        "file_id": u["unique_document_representative_file_id"],
        "pages": u["unique_document_num_pages"],
        "language": u["unique_document_language"],
        "fmt": u["unique_document_format_original"],
    }).reset_index(drop=True)
    out["states"] = out["doc_id"].map(states)
    if central:
        c = _central()
        if len(c):
            c = c[~c["doc_id"].isin(out["doc_id"])][list(out.columns)]
            out = pd.concat([out, c], ignore_index=True)
    orig = {k: v["original_link"] for k, v in aliases()["docs"].items() if v["original_link"]}
    out["link"] = out["doc_id"].map(orig).fillna(out["link"])   # the DB's original link when known
    return out


_aliases = None


def aliases() -> dict:
    """data/link_aliases.json ({"docs", "ids", "conflicts"}; built by link_aliases.py), empty if not built."""
    global _aliases
    if _aliases is None:
        import json
        _aliases = json.loads(ALIASES.read_text()) if ALIASES.exists() else {"docs": {}, "ids": {}, "conflicts": {}}
    return _aliases


def pdf_path(doc_id: str, fmt: str = "pdf") -> Path:
    return PDF_DIR / f"{doc_id}.{fmt}"


def zoho():
    """The existing WorkDrive client (needs ZOHO_* vars from the repo .env)."""
    from helpers.zoho_workdrive import ZohoWorkDrive
    return ZohoWorkDrive()


_link_map = None


def link_map() -> dict[str, str]:
    """{zoho file id: unique-document id}: data/link_aliases.json (DB original + every duplicate), then
    for EVERY copy of every document in the sheet
    (representative file, shareable link and each duplicate_links entry), so a pasted link
    to any copy finds its document."""
    global _link_map
    if _link_map is None:
        import json
        import re
        df = pd.read_excel(XLSX)
        m: dict[str, str] = {}
        for r in df.drop_duplicates("unique_document__id").itertuples():
            doc = r.unique_document__id
            ids = [str(r.unique_document_representative_file_id)]
            ids += re.findall(r"/file/([A-Za-z0-9]+)", str(r.unique_document_shareable_link))
            try:
                ids += [d["zoho_file_id"] for d in json.loads(r.unique_document_duplicate_links)]
            except (TypeError, ValueError):
                pass
            m.update({i: doc for i in ids if i and i != "nan"})
        c = _central()
        if len(c):
            import json
            for r in c.itertuples():
                for i in json.loads(r.alt_ids):
                    m.setdefault(i, r.doc_id)
        _link_map = {**m, **aliases()["ids"]}   # the DB's original + duplicate ids win; the sheet fills the rest
    return _link_map


def link_id(url: str) -> str | None:
    """The Zoho file id of a pasted WorkDrive link (or a bare file id)."""
    import re
    found = re.search(r"/file/([A-Za-z0-9]+)", url) or re.fullmatch(r"\s*([A-Za-z0-9]+)\s*", url)
    return found.group(1) if found else None


def doc_for_link(url: str) -> str | None:
    """Unique-document id for a pasted WorkDrive link (or bare file id), else None."""
    return link_map().get(link_id(url))
