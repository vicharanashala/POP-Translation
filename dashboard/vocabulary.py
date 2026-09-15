"""The vocabularies a placement REFERENCES by id: states, crops, organisations.

A placement stores `state_id`, plus EITHER `crop_id` OR `organization_id` for
the folder under the state. Never the name -- the name lives in one place, so
a rename is one write and every placement shows it.

  state          pop_states          ours; editable here
  organization   pop_organizations   ours; editable here. Everything a folder
                                     names that is not a crop: organisations and
                                     departments ("ICAR - ..."), and groupings
                                     ("General", "Pulses").
  crop           the crop master     NOT ours. Production reads agriai.crop_master,
                                     which another application edits; staging
                                     reads its copy in pop_crops. Read-only here:
                                     rename/merge/delete answer 403, and a form
                                     cannot invent a crop. Pesticides in the
                                     master (type: "chemical") are never offered.

WHAT A NAME IS. A state or organisation `name` is stored exactly as the team
spells it; normalize_*_name() is applied only to NEW names typed into a form,
never to a rename. Lookups are case-insensitive and also search the other
spellings -- `raw_names` on our entries, and pop_crop_aliases for crops (our old
spellings of master crops, "Ground Nut" -> Groundnut). The master's own
`aliases` are regional names ("sajje") and are deliberately not searched: they
map "General" onto "All".

`state_raw` / `crop_raw` stay on the placement as the ORIGINAL folder names.
They are not references: the OCR language lookup keys off the raw state name
(dashboard/languages.STATE_LANG), and a folder has to stay findable by the name
it has in WorkDrive.
"""
from __future__ import annotations

import re

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from dashboard.db import CI_COLLATION, crops_collection
from dashboard.models import (
    COLL_CROP_ALIASES,
    COLL_DOCUMENTS,
    COLL_ORGANIZATIONS,
    COLL_STATES,
    COLL_UPLOAD_QUEUE_ITEMS,
    normalize_crop_name,
    normalize_state_name,
    utcnow,
)

# kind -> (our collection or None for the crop master, normaliser for NEW
# names, placement field, editable here)
KINDS = {
    "state": (COLL_STATES, normalize_state_name, "state_id", True),
    "organization": (COLL_ORGANIZATIONS, normalize_crop_name, "organization_id", True),
    "crop": (None, None, "crop_id", False),
}
# What a crop dropdown may offer. The master also lists pesticides.
_CROP_VISIBLE = {"type": {"$ne": "chemical"}}
READ_ONLY_CROPS = ("crops come from the crop master, which is maintained elsewhere -- "
                   "this dashboard can only reference them")


class VocabularyError(ValueError):
    """A lookup write that cannot be carried out. `status` is the HTTP status
    the route should answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def field(kind: str) -> str:
    return KINDS[kind][2]


def coll(db, kind: str):
    name = KINDS[kind][0]
    return crops_collection(db) if name is None else db[name]


def _visible(kind: str) -> dict:
    return dict(_CROP_VISIBLE) if kind == "crop" else {}


def _collapse(value) -> str:
    return " ".join(str(value or "").split())


def to_object_id(value) -> ObjectId | None:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def find(db, kind: str, raw) -> dict | None:
    """The entry a typed name means, or None. Case-insensitive; also matches
    the entry's other known spellings."""
    if raw is None:
        return None
    raw = _collapse(raw)
    c = coll(db, kind)
    if not raw:
        # Files sitting directly in a state folder have a folder whose name is
        # "". It is a real organisation entry; matched exactly, never created.
        return None if kind == "crop" else c.find_one({"name": ""})
    if kind == "crop":
        entry = c.find_one({"name": raw, **_CROP_VISIBLE}, collation=CI_COLLATION)
        if entry is None:
            alias = db[COLL_CROP_ALIASES].find_one({"spelling": raw}, collation=CI_COLLATION)
            if alias is not None:
                entry = c.find_one({"_id": alias["crop_id"], **_CROP_VISIBLE})
        return entry
    normalize = KINDS[kind][1]
    for candidate in dict.fromkeys((raw, normalize(raw))):
        if not candidate:
            continue
        entry = c.find_one({"name": candidate}, collation=CI_COLLATION)
        if entry is None:
            entry = c.find_one({"raw_names": candidate}, collation=CI_COLLATION)
        if entry is not None:
            return entry
    return None


def resolve(db, kind: str, raw, *, create: bool = True) -> dict | None:
    """The entry for a typed name, creating it if nobody has used it before --
    for states and organisations only. A crop is looked up, never created.

    The typed spelling is remembered in `raw_names`, which keeps a state or
    organisation lookup reversible.
    """
    raw = _collapse(raw)
    if not raw:
        return None
    entry = find(db, kind, raw)
    if kind == "crop":
        return entry
    c, normalize = coll(db, kind), KINDS[kind][1]
    now = utcnow()
    if entry is None:
        if not create:
            return None
        name = normalize(raw)
        if not name:
            return None
        try:
            c.insert_one({"name": name, "raw_names": [raw], "created_at": now, "updated_at": now})
        except DuplicateKeyError:
            pass  # a concurrent writer created it first; take theirs
        return find(db, kind, name)
    if raw not in (entry.get("raw_names") or []) and raw != entry["name"]:
        c.update_one({"_id": entry["_id"]},
                     {"$addToSet": {"raw_names": raw}, "$set": {"updated_at": now}})
    return entry


def get(db, kind: str, entry_id) -> dict | None:
    oid = to_object_id(entry_id)
    return None if oid is None else coll(db, kind).find_one({"_id": oid, **_visible(kind)})


def entries(db, kind: str, query: dict | None = None):
    return coll(db, kind).find({**(query or {}), **_visible(kind)})


def name_map(db, kind: str) -> dict[ObjectId, str]:
    """id -> name for a whole vocabulary. A few hundred rows, so a listing
    loads it once per request instead of joining inside the query."""
    return {e["_id"]: e["name"] for e in coll(db, kind).find(_visible(kind), {"name": 1})}


def ids_matching(db, kind: str, values: list[str]) -> list[ObjectId]:
    """Ids whose name -- or one of its other known spellings -- contains any of
    `values`, case-insensitively. The spellings matter for crops: a filter
    typed as "Black Pepper" still finds the master's "Pepper"."""
    if not values:
        return []
    regex = {"$regex": "|".join(re.escape(v) for v in values), "$options": "i"}
    if kind == "crop":
        ids = {e["_id"] for e in coll(db, kind).find({"name": regex, **_CROP_VISIBLE}, {"_id": 1})}
        aliased = {a["crop_id"] for a in db[COLL_CROP_ALIASES].find({"spelling": regex}, {"crop_id": 1})}
        ids |= {e["_id"] for e in coll(db, kind).find({"_id": {"$in": list(aliased)}, **_CROP_VISIBLE}, {"_id": 1})}
        return list(ids)
    return [e["_id"] for e in coll(db, kind).find(
        {"$or": [{"name": regex}, {"raw_names": regex}]}, {"_id": 1})]


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


def crop_spellings(db) -> dict[ObjectId, list[str]]:
    """crop id -> our other spellings of it, for the crops dropdown."""
    out: dict[ObjectId, list[str]] = {}
    for a in db[COLL_CROP_ALIASES].find({}, {"spelling": 1, "crop_id": 1}):
        out.setdefault(a["crop_id"], []).append(a["spelling"])
    return out


def _editable(kind: str) -> None:
    if not KINDS[kind][3]:
        raise VocabularyError(403, READ_ONLY_CROPS)


def rename(db, kind: str, entry_id, new_name: str) -> dict:
    """Rename in place. Every placement shows the new name immediately.

    Stored as typed (whitespace collapsed), NOT normalised -- the whole point
    is to set the team's standard spelling. The old name joins raw_names so it
    still resolves. A name another entry already has is refused with 409: that
    is a merge, and silently turning it into one would be a surprise.
    """
    _editable(kind)
    c = coll(db, kind)
    entry = get(db, kind, entry_id)
    if entry is None:
        raise VocabularyError(404, f"{kind} not found")
    name = _collapse(new_name)
    if not name:
        raise VocabularyError(400, "name is required")
    clash = c.find_one({"name": name, "_id": {"$ne": entry["_id"]}}, collation=CI_COLLATION)
    if clash is not None:
        raise VocabularyError(
            409, f"{kind} {clash['name']!r} already exists -- merge into it instead "
                 f"(POST /dashboard/{_path(kind)}/{clash['_id']}/merge)")
    if name == entry["name"]:
        return entry
    update = {"$set": {"name": name, "updated_at": utcnow()}}
    if entry["name"] not in (entry.get("raw_names") or []):
        update["$addToSet"] = {"raw_names": entry["name"]}
    c.update_one({"_id": entry["_id"]}, update)
    _refresh_queue_names(db, kind, [entry["_id"]], name)
    return c.find_one({"_id": entry["_id"]})


def merge(db, kind: str, survivor_id, absorb_ids: list) -> dict:
    """Fold other entries into one: repoint their placements and pending
    uploads, keep every spelling in raw_names, delete the absorbed entries.

    No placement is deleted, even where a document ends up filed twice under
    what is now the same name -- in the corpus each row is a separate physical
    file in its own WorkDrive folder.
    """
    _editable(kind)
    c, f = coll(db, kind), field(kind)
    survivor = get(db, kind, survivor_id)
    if survivor is None:
        raise VocabularyError(404, f"{kind} not found")
    oids = []
    for raw in absorb_ids or []:
        oid = to_object_id(raw)
        if oid is None:
            raise VocabularyError(422, f"malformed {kind} id: {raw!r}")
        if oid != survivor["_id"] and oid not in oids:
            oids.append(oid)
    if not oids:
        raise VocabularyError(400, f"absorb must name at least one other {kind}")
    absorbed = list(c.find({"_id": {"$in": oids}}))
    missing = set(oids) - {a["_id"] for a in absorbed}
    if missing:
        raise VocabularyError(404, f"{kind}(s) not found: {', '.join(map(str, missing))}")

    now = utcnow()
    repointed = db[COLL_DOCUMENTS].update_many(
        {f: {"$in": oids}}, {"$set": {f: survivor["_id"], "updated_at": now}}).modified_count
    db[COLL_UPLOAD_QUEUE_ITEMS].update_many(
        {f"placements.{f}": {"$in": oids}},
        {"$set": {f"placements.$[p].{f}": survivor["_id"], "updated_at": now}},
        array_filters=[{f"p.{f}": {"$in": oids}}])
    _refresh_queue_names(db, kind, [survivor["_id"]], survivor["name"])

    spellings = set(survivor.get("raw_names") or [])
    for a in absorbed:
        spellings.update(a.get("raw_names") or [])
        spellings.add(a["name"])
    spellings.discard(survivor["name"])
    c.update_one({"_id": survivor["_id"]},
                 {"$set": {"raw_names": sorted(spellings), "updated_at": now}})
    c.delete_many({"_id": {"$in": oids}})
    return {"survivor": c.find_one({"_id": survivor["_id"]}),
            "absorbed": [a["name"] for a in absorbed],
            "placements_repointed": repointed}


def delete(db, kind: str, entry_id) -> None:
    """Remove an entry nothing uses. Refused while any placement or pending
    upload references it -- MongoDB does not enforce references, so this is
    the only thing standing between a delete and placements naming nothing."""
    _editable(kind)
    entry = get(db, kind, entry_id)
    if entry is None:
        raise VocabularyError(404, f"{kind} not found")
    placements, pending = usage(db, kind, entry["_id"])
    if placements or pending:
        raise VocabularyError(
            409, f"{kind} {entry['name']!r} is still used by {placements} placement(s) and "
                 f"{pending} pending upload(s) -- merge it into another {kind} instead")
    coll(db, kind).delete_one({"_id": entry["_id"]})


def folder(db, raw, *, create: bool = True) -> tuple[str, dict] | None:
    """What a folder name under a state means: ("crop", master entry) when the
    crop master knows it, else ("organization", entry) -- created if new and
    `create`. None for a blank name that matches nothing.

    Crop first: a name that IS a master crop must never become an
    organisation. The reverse cannot happen, because crops are never created.
    """
    crop = find(db, "crop", raw)
    if crop is not None:
        return "crop", crop
    if not _collapse(raw):
        org = find(db, "organization", "")
    elif create:
        org = resolve(db, "organization", raw)
    else:
        org = find(db, "organization", raw)
    return ("organization", org) if org is not None else None


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


def _path(kind: str) -> str:
    return {"state": "states", "crop": "crops", "organization": "organizations"}[kind]


def _refresh_queue_names(db, kind: str, ids: list[ObjectId], name: str) -> None:
    """Pending uploads show the name next to the id; keep it current so the
    queue's "files it under ..." note does not name something that is gone."""
    label = "state" if kind == "state" else "crop"
    db[COLL_UPLOAD_QUEUE_ITEMS].update_many(
        {f"placements.{field(kind)}": {"$in": ids}},
        {"$set": {f"placements.$[p].{label}": name}},
        array_filters=[{f"p.{field(kind)}": {"$in": ids}}])
