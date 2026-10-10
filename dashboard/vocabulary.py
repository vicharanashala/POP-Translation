"""The vocabularies a placement REFERENCES by id: states, crops, organisations,
districts, KVKs.

A placement stores `state_id`, plus EITHER `crop_id` OR `organization_id` for
the folder under the state, and optionally `district_id` / `kvk_id`. Never a
name.

IDS ONLY. Nothing here resolves a vocabulary from a string, and no route accepts
one. Not a lookup, not a filter, not a PATCH field, not an upload. Every form in
the frontend is a SELECTION from a list this module serves, so the id is what
the caller already has; accepting a name as well meant two ways to say the same
thing, one of which had to guess.

What that replaced, and why none of it is coming back:

  find()/resolve()      matched a typed name, case-insensitively, then through
                        `raw_names`, then through a normaliser that stripped
                        "State " off the front. Three chances to hit the wrong
                        entry, and the normaliser silently made "State Kerala"
                        and "Kerala" different questions once LGD renamed the
                        state to Keralam.
  ids_matching()        backed `filter[state]=<substring>`. A filter is also a
                        selection -- the column dropdown lists exactly the
                        entries that can match -- so it takes ids too.
  crop_spellings()      served `pop_crop_aliases` ("Ground Nut" -> Groundnut) so
                        a typed crop resolved. Nothing types a crop now.
  rename()/merge()      editing a vocabulary this dashboard does not own. Every
                        write route is gone; the next sync would undo them.

  state         pop_states          synced from agriai.states (LGD), on state_code
  district      pop_districts       synced from agriai.districts
  kvk           pop_kvks            synced from agriai.kvks
  crop          the crop master     production reads agriai.crop_master, which
                                    another application edits; staging reads its
                                    copy in pop_crops. Only type "crop" (or no
                                    type) is offered -- see _CROP_VISIBLE.
  organization  pop_organizations   WorkDrive FOLDER names, created ONLY by the
                                    corpus loader, which is the one place a
                                    folder name comes from. It does its own name
                                    matching (migrate_from_corpus._lookup_by_name)
                                    so that this module has none.

`state_raw` / `crop_raw` stay on the placement as the folder names it was found
under. They are PROVENANCE, not references: nothing resolves them, nothing
infers from them -- the OCR language keys off the state's LGD code
(dashboard/languages) -- and they exist so a row stays traceable to the
WorkDrive folder it came from. `raw_names` on an entry is the same thing for the
vocabulary itself: still stored, no longer read, and no longer served.
"""
from __future__ import annotations

import re

from bson import ObjectId
from bson.errors import InvalidId

from dashboard.db import crops_collection
from dashboard.models import (
    COLL_DISTRICTS,
    COLL_DOCUMENTS,
    COLL_KVKS,
    COLL_ORGANIZATIONS,
    COLL_STATES,
    COLL_UPLOAD_QUEUE_ITEMS,
)

# kind -> (our collection or None for the crop master, placement field)
#
# No normaliser and no editable flag: both existed to serve names typed into a
# form. A vocabulary is now read-only and addressed by id, so a kind is just
# where its rows live and which placement field points at them. Adding one is a
# row here plus a collection, an index and its read routes.
KINDS = {
    "state": (COLL_STATES, "state_id"),
    "organization": (COLL_ORGANIZATIONS, "organization_id"),
    "crop": (None, "crop_id"),
    "district": (COLL_DISTRICTS, "district_id"),
    "kvk": (COLL_KVKS, "kvk_id"),
}


# kind -> (parent kind, the field on THIS entry naming its parent).
#
# A district belongs to a state and a KVK to a district, which is how the Add
# Document and Edit forms narrow their dropdowns: pick a state, get that state's
# districts. The parent is stored on the CHILD -- one indexed field -- rather
# than as a list on the parent, so there is no second copy of the relationship
# to drift. GET /dashboard/locations serves the nested view built from it.
PARENT = {"district": ("state", "state_id"), "kvk": ("district", "district_id")}

# kind -> the LGD code field on our entry. The code, not the name, is what a
# re-sync matches on: LGD renames districts (Keralam, "The Dadra And Nagar
# Haveli And Daman And Diu") and a name match would create a duplicate.
CODE = {"state": "state_code", "district": "district_code", "kvk": "kvk_code"}

# The name of the "not specific to one of these" entry. ONE row per collection,
# shared by every parent -- not one per state and one per district, which is
# what this was first built as. 860 rows all meaning the same thing gave the
# idea two representations (an "All" id or no id at all) and no way to keep them
# in step; a single parentless row has one id, and `all_entry()` finds it.
#
# It carries NO parent reference and NO LGD code, so a child query by parent
# cannot see it -- every reader that lists children has to add it back, which
# `children_of()` does and `GET /locations` does by hand.
ALL = "All"
# Marks that one row. Preferred over matching on the name or on a missing code:
# it is what the unique index in db.py keys on, so the collection cannot end up
# with two of them.
IS_ALL = "is_all"
# What a crop dropdown may offer. The master is not a crop list: of its 980
# entries only 472 are type "crop"; the rest are diseases ("Leaf Rust in
# Wheat"), pests, weeds, practices, chemicals, soils. Hiding only chemicals let
# all of those through as though they were crop folders. Untyped entries are
# kept because some are real crops the master never typed -- Okra, Onion,
# Mango, Rapeseed & Mustard and four more carry 476 placements between them.
# No placement on either database points at any other type.
_CROP_VISIBLE = {"type": {"$in": ["crop", None]}}


class VocabularyError(ValueError):
    """A lookup write that cannot be carried out. `status` is the HTTP status
    the route should answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def field(kind: str) -> str:
    return KINDS[kind][1]


def coll(db, kind: str):
    name = KINDS[kind][0]
    return crops_collection(db) if name is None else db[name]


def _visible(kind: str) -> dict:
    return dict(_CROP_VISIBLE) if kind == "crop" else {}


def to_object_id(value) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def parent_field(kind: str) -> str | None:
    """The field naming this kind's parent, or None for a kind with no parent."""
    return PARENT[kind][1] if kind in PARENT else None


def all_entry(db, kind: str) -> dict | None:
    """The one shared "All" row of this kind, or None for a kind without one.

    Parentless by design, so every narrowed list has to union it in rather than
    find it by query. Returned for ANY parent -- the same id comes back whichever
    state or district was asked for, because "no particular district" is one
    fact, not 37 of them.
    """
    if kind not in PARENT:
        return None
    return coll(db, kind).find_one({IS_ALL: True})


def children_of(db, kind: str, parent_id) -> list[dict]:
    """Every entry of `kind` under one parent, A-Z, with the "All" entry first.

    This is what a form dropdown shows. Read by PARENT REFERENCE, not by which
    entries some placement happens to use already -- a form needs the whole
    official list, and until people start tagging documents the "used" set is
    empty, which is why these dropdowns were empty before the LGD sync.

    The shared "All" row has no parent, so the query cannot return it and it is
    appended here. That is also why a parent with no real children of its own --
    `Central` has no districts, and 152 districts have no KVK -- still offers
    something to pick instead of an empty dropdown.
    """
    f = parent_field(kind)
    if f is None:
        raise VocabularyError(400, f"{kind} has no parent to list by")
    rows = [r for r in coll(db, kind).find({f: parent_id}) if not r.get(IS_ALL)]
    shared = all_entry(db, kind)
    if shared is not None:
        rows.append(shared)
    return sorted(rows, key=lambda e: (e["name"] != ALL, (e.get("name") or "").lower()))


def get(db, kind: str, entry_id) -> dict | None:
    oid = to_object_id(entry_id)
    return None if oid is None else coll(db, kind).find_one({"_id": oid, **_visible(kind)})


def entries(db, kind: str, query: dict | None = None):
    return coll(db, kind).find({**(query or {}), **_visible(kind)})


def name_map(db, kind: str) -> dict[ObjectId, str]:
    """id -> name for a whole vocabulary. A few hundred rows, so a listing
    loads it once per request instead of joining inside the query."""
    return {e["_id"]: e["name"] for e in coll(db, kind).find(_visible(kind), {"name": 1})}


def usage_counts(db, kind: str) -> dict[ObjectId, int]:
    """Placements per entry. Computed, never stored: a stored count drifts the
    moment anything is deleted, and grouping ~10k rows is cheap."""
    return {r["_id"]: r["n"] for r in db[COLL_DOCUMENTS].aggregate([
        {"$match": {field(kind): {"$exists": True}}},
        {"$group": {"_id": f"${field(kind)}", "n": {"$sum": 1}}}])}


def usage(db, kind: str, entry_id: ObjectId) -> tuple[int, int]:
    """(placements, pending uploads) that reference an entry."""
    return (
        db[COLL_DOCUMENTS].count_documents({field(kind): entry_id}),
        db[COLL_UPLOAD_QUEUE_ITEMS].count_documents({f"placements.{field(kind)}": entry_id}),
    )


# Which folders an advisory type may be filed under, for the Folder dropdown.
# Matched on letters only, so "Crop Advisory", "crop-advisory" and "CROP
# ADVISORY" are one value. Blank or unrecognised shows everything, like General.
_ADVISORY_FOLDER_KINDS = {
    "comprehensive": ("crop",),
    "cropadvisory": ("crop",),
    "noncropadvisory": ("organization",),
    "general": ("crop", "organization"),
}


def folder_kinds_for_advisory(advisory_type: str | None) -> tuple[str, ...]:
    key = re.sub(r"[^a-z]", "", (advisory_type or "").lower())
    return _ADVISORY_FOLDER_KINDS.get(key, ("crop", "organization"))
