"""Env-var config for the dashboard package.

Follows the same plain os.environ + python-dotenv convention pop_server.py
and helpers/zoho_workdrive.py already use -- no config class, no
pydantic-settings, matching this repo's existing style.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# Deliberately os.environ.get(), NOT os.environ[...] -- pop_server.py now
# imports the dashboard routers unconditionally at module import time
# (see pop_server.py's app.include_router calls), so a hard-required env var
# read here would crash the ALREADY-WORKING pipeline server on startup if
# these new vars aren't set yet. Missing values are validated lazily, at
# first actual use (dashboard/db.py's engine init, dashboard/zoho_layout.py's
# upload calls), not at import time -- same reasoning as ZohoWorkDrive()
# itself only being constructed lazily via pop_server._get_zoho(), never at
# import.
DATABASE_URL = os.environ.get("DATABASE_URL")

# Fixed, pre-created Zoho WorkDrive subfolder IDs for the dashboard's three
# upload kinds -- see dashboard/zoho_layout.py.
ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID = os.environ.get("ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID")
ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID = os.environ.get("ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID")
ZOHO_DASHBOARD_REVIEWS_FOLDER_ID = os.environ.get("ZOHO_DASHBOARD_REVIEWS_FOLDER_ID")
DUPLICATE_EMBEDDING_THRESHOLD = float(os.environ.get("DUPLICATE_EMBEDDING_THRESHOLD", "0.95"))

# Kill switch for the dashboard's per-document translate button (2026-08-27):
# production is Gemini-only for now (MiniMax isn't available there) and the
# translate flow hasn't been properly load-tested/wired for that yet, so this
# release ships document management only -- see dashboard/routes_translation.py.
# Defaults to "on" so this doesn't silently disable translation anywhere this
# var isn't explicitly set; this release's .env sets TRANS=off.
TRANS_ENABLED = os.environ.get("TRANS", "on").strip().lower() not in ("off", "0", "false")

PAGE_SIZE = 100
