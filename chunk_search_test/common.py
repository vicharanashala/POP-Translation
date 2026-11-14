"""Shared helpers for chunk_search_test. Every script imports from here; see INDEX.md.

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
# from chunk_search_test/.env so the test DB never mixes with POP's.
load_dotenv(ROOT / ".env")
load_dotenv(HERE / ".env", override=True)
for p in (str(ROOT),):
    if p not in sys.path:
        sys.path.insert(0, p)


def load_documents() -> pd.DataFrame:
    """One row per UNIQUE document (the sheet has one row per placement).

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
    return out


def pdf_path(doc_id: str, fmt: str = "pdf") -> Path:
    return PDF_DIR / f"{doc_id}.{fmt}"


def zoho():
    """The existing WorkDrive client (needs ZOHO_* vars from the repo .env)."""
    from helpers.zoho_workdrive import ZohoWorkDrive
    return ZohoWorkDrive()
