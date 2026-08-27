"""Zoho WorkDrive folder layout for the dashboard's mega folder -- flat by
doc-type only (originals/translations/reviews), no state/folder
subfolders. All relations (which states/crops a document is placed under)
live in Postgres, not folder structure -- storage here doesn't need to know
or care.

Each of the three subfolders is a fixed, pre-created WorkDrive folder ID
supplied via .env (ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID /
_TRANSLATIONS_FOLDER_ID / _REVIEWS_FOLDER_ID) -- no discovery or
get_or_create_folder() call at request time, and no dependency on a shared
dashboard root folder id.
"""
from __future__ import annotations

from dashboard.config import (
    ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID,
    ZOHO_DASHBOARD_REVIEWS_FOLDER_ID,
    ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID,
)
from helpers.zoho_workdrive import ZohoWorkDrive

_SUBFOLDER_IDS = {
    "originals": ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID,
    "translations": ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID,
    "reviews": ZOHO_DASHBOARD_REVIEWS_FOLDER_ID,
}


def get_subfolder_id(kind: str) -> str:
    if kind not in _SUBFOLDER_IDS:
        raise ValueError(f"unknown dashboard subfolder kind {kind!r}")
    folder_id = _SUBFOLDER_IDS[kind]
    if not folder_id:
        raise RuntimeError(
            f"ZOHO_DASHBOARD_{kind.upper()}_FOLDER_ID is not set in .env"
        )
    return folder_id


def upload_original(wd: ZohoWorkDrive, filename: str, content: bytes) -> str:
    return wd.upload_file(filename, content, get_subfolder_id("originals"))


def upload_translation(wd: ZohoWorkDrive, filename: str, content: bytes) -> str:
    return wd.upload_file(filename, content, get_subfolder_id("translations"))


def upload_review(wd: ZohoWorkDrive, filename: str, content: bytes) -> str:
    return wd.upload_file(filename, content, get_subfolder_id("reviews"))


def shareable_link(zoho_file_id: str) -> str:
    """The "direct file URL" style already used throughout this pipeline
    (report_true.csv's doc_link column, e.g.
    https://workdrive.zoho.in/file/x3ibeca5fff7455a84dacbb6443fb8d144f52) --
    confirmed with the user as the reused link style rather than a separate
    Zoho "create public share link" API call (backend plan §1)."""
    return f"https://workdrive.zoho.in/file/{zoho_file_id}"
