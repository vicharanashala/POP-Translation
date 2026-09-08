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
# MongoDB Atlas. There are two deployments, named by env-var PREFIX in .env:
#
#   STAGING_DB_URL / STAGING_DB_NAME   the shared agriai-test-riya database
#   PROD_DB_URL    / PROD_DB_NAME      the dashboard's own database
#
# POP_ENV picks which one this process uses; it defaults to staging so a
# deploy that forgets to set it does not write to production. Plain DB_URL /
# DB_NAME still win when set, so an older .env (and the docker-compose that
# passes them straight through) keeps working unchanged.
#
# The staging database is SHARED with a different application, which has its
# own `users`, `questions`, `states`, `crops` and ~18k audit rows in it. Every
# collection of ours is therefore prefixed `pop_` (see dashboard/models.py).
# The prefix is not cosmetic: without it our `states` and `crops` would
# collide with theirs.
#
# Nothing here reads any other database.
#
# The staging cluster is a 512 MB free tier. That is the reason
# `chunk_embeddings` is always empty: the metadata is ~16 MB, while one
# 768-float vector per document is another ~27 MB plus an Atlas Vector Search
# index, and per-CHUNK vectors are many times that. Fingerprints live on local
# disk instead (see fix/out/), keyed back to a document by sha256.


def _env(name: str) -> str:
    """Read an env var, stripped of whitespace AND of surrounding quotes.

    The quotes matter in production. `.env` here writes the values quoted
    (PROD_DB_URL="mongodb+srv://..."), and `docker compose` strips those
    during ${VAR} interpolation -- which is why a local `docker compose up`
    has never hit this. `docker run --env-file .env` does NOT strip them: the
    value arrives beginning with a literal `"`, and MongoClient then raises
    `InvalidURI: URI must begin with 'mongodb://' or 'mongodb+srv://'`.
    """
    value = (os.environ.get(name) or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


POP_ENV = _env("POP_ENV").lower() or "staging"
_PREFIX = "PROD" if POP_ENV in ("prod", "production") else "STAGING"

# The plain DB_URL override is only honoured when it really is a MongoDB URI.
# It is read before the prefixed vars purely for backwards compatibility with
# an older .env; anything else there (a bare database name, a leftover from
# another service) is ignored rather than handed to MongoClient. DB_NAME
# follows DB_URL rather than being read on its own -- a plain DB_NAME belongs
# to whatever set that plain DB_URL, not to us.
_plain_url = _env("DB_URL")
if not _plain_url.startswith(("mongodb://", "mongodb+srv://")):
    _plain_url = ""

DB_URL = _plain_url or _env(f"{_PREFIX}_DB_URL")
DB_NAME = (
    (_env("DB_NAME") if _plain_url else "")
    or _env(f"{_PREFIX}_DB_NAME")
    or "agriai-test-riya"
)

# Fixed, pre-created Zoho WorkDrive subfolder IDs for the dashboard's three
# upload kinds -- see dashboard/zoho_layout.py.
ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID = os.environ.get("ZOHO_DASHBOARD_ORIGINALS_FOLDER_ID")
ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID = os.environ.get("ZOHO_DASHBOARD_TRANSLATIONS_FOLDER_ID")
ZOHO_DASHBOARD_REVIEWS_FOLDER_ID = os.environ.get("ZOHO_DASHBOARD_REVIEWS_FOLDER_ID")

# Kill switch for the dashboard's per-document translate button. Defaults to
# "on" so this doesn't silently disable translation anywhere the var isn't
# explicitly set.
TRANS_ENABLED = os.environ.get("TRANS", "on").strip().lower() not in ("off", "0", "false")

PAGE_SIZE = 100
