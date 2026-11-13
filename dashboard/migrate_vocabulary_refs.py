"""Move placements from state/crop NAME strings to references.

Target: a placement stores `state_id` (pop_states) plus EITHER `crop_id` (a
crop master entry) OR `organization_id` (pop_organizations). See
dashboard/vocabulary.py.

The running image reads the old name strings, so everything before the deploy
is additive and the strings are only removed afterwards.

  PHASE 1        (done on both deployments, 2026-09-15)
                 state_id / crop_id set from the strings, pointing at pop_states /
                 the old pop_crops. Safe under the running image.

  PHASE crops    --mapping exports/crop_master_mapping_review.csv   (reviewed)
                 Safe under the running image.
                 - keeps the old pop_crops as `pop_crops_pre_master`
                 - organisation / grouping entries -> pop_organizations, SAME ids,
                   and their placements move crop_id -> organization_id
                 - crop entries: placements repointed to the master id
                 - our old spellings -> pop_crop_aliases
                 - STAGING only: pop_crops becomes a copy of the crop master

  -- deploy the image with dashboard/vocabulary.py --

  PHASE 2        --mapping (same file)   only once the new image is serving
                 - ids for anything the OLD image wrote in between
                 - removes the state/crop strings from placements and copy links
                 - removes stored document_count; swaps the name indexes
                 - PRODUCTION: renames the old pop_crops to pop_crops_pre_master

  --sync-master  STAGING: refresh pop_crops from the crop master. Re-run whenever
                 the master changes.

Every phase is re-runnable and backs up what it touches to exports/ first.

    python3 -m dashboard.migrate_vocabulary_refs --env STAGING --phase crops --mapping F [--dry-run]
    python3 -m dashboard.migrate_vocabulary_refs --env PROD    --phase 2     --mapping F [--dry-run]
    python3 -m dashboard.migrate_vocabulary_refs --env STAGING --sync-master
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient, UpdateMany, UpdateOne

REPO = Path(__file__).resolve().parent.parent
load_dotenv(REPO / ".env", override=True)

from dashboard.config import CROP_MASTER_COLLECTION, CROP_MASTER_DB_NAME  # noqa: E402
from dashboard.db import CI_COLLATION, crops_collection  # noqa: E402
from dashboard.models import (  # noqa: E402
    COLL_CROP_ALIASES,
    COLL_CROPS,
    COLL_DOCUMENTS,
    COLL_ORGANIZATIONS,
    COLL_STATES,
    COLL_UNIQUE_DOCUMENTS,
    COLL_UPLOAD_QUEUE_ITEMS,
    utcnow,
)

OUT = REPO / "exports"
LEGACY = "pop_crops_pre_master"
ORG_CATEGORIES = {"organisation", "organization", "non-crop category", "blank (no crop folder)"}
SKIP_CATEGORIES = {"unused"}


def _clean(v):
    return (v or "").strip().strip("\"'").strip()


def connect(env: str, database: str | None = None):
    """`database` overrides <ENV>_DB_NAME -- for a rehearsal against a scratch
    copy on the same cluster."""
    return MongoClient(_clean(os.environ[f"{env}_DB_URL"]))[database or _clean(os.environ[f"{env}_DB_NAME"])]


def master_source():
    """The crop master itself, through the production credentials -- the only
    ones that can read it, whichever deployment is being migrated."""
    return connect("PROD").client[CROP_MASTER_DB_NAME][CROP_MASTER_COLLECTION]


def is_production(db) -> bool:
    return crops_collection(db).name == CROP_MASTER_COLLECTION and crops_collection(db).database.name == CROP_MASTER_DB_NAME


def backup(db, env: str, label: str) -> Path:
    path = OUT / f"vocab_refs_backup_{env}_{label}_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    OUT.mkdir(exist_ok=True)
    dump = {name: list(db[name].find()) for name in
            (COLL_STATES, COLL_CROPS, LEGACY, COLL_ORGANIZATIONS, COLL_CROP_ALIASES)}
    dump[COLL_DOCUMENTS] = list(db[COLL_DOCUMENTS].find(
        {}, {"row_id": 1, "state": 1, "crop": 1, "state_id": 1, "crop_id": 1, "organization_id": 1,
             "state_raw": 1, "crop_raw": 1}))
    dump[COLL_UNIQUE_DOCUMENTS] = list(db[COLL_UNIQUE_DOCUMENTS].find({}, {"duplicate_links": 1}))
    dump[COLL_UPLOAD_QUEUE_ITEMS] = list(db[COLL_UPLOAD_QUEUE_ITEMS].find({}, {"placements": 1}))
    json.dump(dump, open(path, "w"), default=str)
    return path


# -- the reviewed mapping -------------------------------------------------------


def load_mapping(path: str, master) -> dict[str, dict]:
    """our crop name -> {kind: "crop"|"organization"|"skip", master_id, master_name}.

    Refuses to continue on anything unresolved: a row still marked as missing
    from the master, a crop row without a master entry, an id the master does
    not have (or that is a pesticide), or a name and id that disagree.
    """
    visible = {m["_id"]: m for m in master.find({"type": {"$ne": "chemical"}}, {"name": 1})}
    by_name: dict[str, list] = {}
    for m in visible.values():
        by_name.setdefault(m["name"], []).append(m)
    from bson import ObjectId

    problems, out = [], {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name, category = row["our_crop"], (row.get("category") or "").strip().lower()
            if category in SKIP_CATEGORIES:
                out[name] = {"kind": "skip"}
            elif category in ORG_CATEGORIES:
                out[name] = {"kind": "organization"}
            elif category == "crop":
                mid, mname = (row.get("proposed_master_id") or "").strip(), (row.get("proposed_master_crop") or "").strip()
                entry = None
                if mid:
                    try:
                        entry = visible.get(ObjectId(mid))
                    except Exception:  # noqa: BLE001
                        entry = None
                    if entry is None:
                        problems.append(f"{name!r}: master id {mid} is not a (non-chemical) master crop")
                    elif mname and mname != entry["name"]:
                        problems.append(f"{name!r}: id {mid} is {entry['name']!r}, but the row says {mname!r}")
                elif mname:
                    hits = by_name.get(mname, [])
                    if len(hits) != 1:
                        problems.append(f"{name!r}: {mname!r} matches {len(hits)} master crops -- give the id")
                    else:
                        entry = hits[0]
                else:
                    problems.append(f"{name!r}: category crop but no master crop given")
                if entry is not None:
                    out[name] = {"kind": "crop", "master_id": entry["_id"], "master_name": entry["name"]}
            else:
                problems.append(f"{name!r}: category {row.get('category')!r} is not crop / organisation / "
                                f"non-crop category / blank / unused")
    if problems:
        raise SystemExit("mapping file has unresolved rows:\n  " + "\n  ".join(problems))
    return out


# -- phase 1 (already run; kept for a fresh deployment) -------------------------


def phase1(db, dry_run: bool) -> None:
    if db[COLL_ORGANIZATIONS].estimated_document_count() or LEGACY in db.list_collection_names():
        raise SystemExit("phase 1 must not run after the crops phase -- it would point crop_id at the old pop_crops")
    for kind, collection in (("state", COLL_STATES), ("crop", COLL_CROPS)):
        f = f"{kind}_id"
        names = Counter(r.get(kind) for r in db[COLL_DOCUMENTS].find({f: {"$exists": False}}, {kind: 1}))
        entries = {e["name"]: e["_id"] for e in db[collection].find({}, {"name": 1})}
        missing = [n for n in names if n is not None and n not in entries]
        if missing and not dry_run:
            for n in missing:
                entries[n] = db[collection].insert_one(
                    {"name": n, "raw_names": [], "created_at": utcnow(), "updated_at": utcnow()}).inserted_id
        ops = [UpdateMany({kind: n, f: {"$exists": False}}, {"$set": {f: entries[n]}}) for n in names if n in entries]
        if ops and not dry_run:
            db[COLL_DOCUMENTS].bulk_write(ops, ordered=False)
        print(f"  {kind}: {sum(names.values())} placement(s) without {f}; names without an entry: {missing}")


# -- phase crops ------------------------------------------------------------------


def phase_crops(db, env: str, mapping: dict, dry_run: bool) -> None:
    master = master_source()
    legacy_coll = db[LEGACY] if LEGACY in db.list_collection_names() else db[COLL_CROPS]
    if legacy_coll.name == COLL_CROPS and db[COLL_CROPS].find_one({"aliases": {"$exists": True}}):
        raise SystemExit("pop_crops already looks like a master copy but pop_crops_pre_master is missing -- stop")
    legacy = list(legacy_coll.find())
    unknown = sorted(e["name"] for e in legacy if e["name"] not in mapping)
    if unknown:
        raise SystemExit(f"{len(unknown)} pop_crops entr(ies) are not in the mapping file: {unknown[:10]}")
    kinds = Counter(mapping[e["name"]]["kind"] for e in legacy)
    placements = Counter()
    for r in db[COLL_DOCUMENTS].find({"crop_id": {"$in": [e["_id"] for e in legacy]}}, {"crop_id": 1}):
        placements[r["crop_id"]] += 1
    by_kind = Counter()
    for e in legacy:
        by_kind[mapping[e["name"]]["kind"]] += placements.get(e["_id"], 0)
    print(f"  entries by kind: {dict(kinds)}; placements to move by kind: {dict(by_kind)}")
    stray = [e["name"] for e in legacy if mapping[e["name"]]["kind"] == "skip" and placements.get(e["_id"])]
    if stray:
        raise SystemExit(f"rows marked unused still have placements: {stray}")
    if dry_run:
        print(f"  {'staging: would replace pop_crops with a copy of the crop master' if not is_production(db) else 'production: pop_crops left for the running image until phase 2'}")
        return

    print(f"  backup -> {backup(db, env, 'crops').relative_to(REPO)}")
    if legacy_coll.name == COLL_CROPS:
        db[LEGACY].insert_many(legacy) if legacy else None
        print(f"  kept the old pop_crops as {LEGACY} ({len(legacy)} entries)")

    now = utcnow()
    # organisations: same ids, so a placement just changes which field holds it
    org_entries = [e for e in legacy if mapping[e["name"]]["kind"] == "organization"]
    db[COLL_ORGANIZATIONS].bulk_write([
        UpdateOne({"_id": e["_id"]},
                  {"$set": {"name": e["name"], "raw_names": e.get("raw_names") or [], "updated_at": now},
                   "$setOnInsert": {"created_at": e.get("created_at") or now}}, upsert=True)
        for e in org_entries], ordered=False) if org_entries else None
    org_ids = [e["_id"] for e in org_entries]
    moved_orgs = db[COLL_DOCUMENTS].update_many(
        {"crop_id": {"$in": org_ids}},
        [{"$set": {"organization_id": "$crop_id", "updated_at": now}}, {"$unset": "crop_id"}]).modified_count

    crop_ops = [UpdateMany({"crop_id": e["_id"]}, {"$set": {"crop_id": mapping[e["name"]]["master_id"], "updated_at": now}})
                for e in legacy if mapping[e["name"]]["kind"] == "crop"]
    moved_crops = sum(r.modified_count for r in [db[COLL_DOCUMENTS].bulk_write(crop_ops[i:i + 500], ordered=False)
                                                 for i in range(0, len(crop_ops), 500)])
    print(f"  placements: {moved_crops} -> crop master ids, {moved_orgs} -> organization_id")

    # pending uploads
    legacy_by_id = {e["_id"]: e for e in legacy}
    for item in db[COLL_UPLOAD_QUEUE_ITEMS].find({"placements.crop_id": {"$in": list(legacy_by_id)}}):
        new = []
        for p in item.get("placements") or []:
            e = legacy_by_id.get(p.get("crop_id"))
            if e is not None:
                m = mapping[e["name"]]
                p = {k: v for k, v in p.items() if k != "crop_id"}
                if m["kind"] == "crop":
                    p.update(crop_id=m["master_id"], crop=m["master_name"], crop_kind="crop")
                else:
                    p.update(organization_id=e["_id"], crop_kind="organization")
            new.append(p)
        db[COLL_UPLOAD_QUEUE_ITEMS].update_one({"_id": item["_id"]}, {"$set": {"placements": new}})

    # our old spellings of master crops
    db[COLL_CROP_ALIASES].create_index([("spelling", ASCENDING)], name="uq_spelling_ci", unique=True, collation=CI_COLLATION)
    conflicts, written = [], 0
    for e in legacy:
        m = mapping[e["name"]]
        if m["kind"] != "crop":
            continue
        for spelling in {e["name"], *(e.get("raw_names") or [])}:
            if not spelling or spelling.strip().lower() == m["master_name"].strip().lower():
                continue
            have = db[COLL_CROP_ALIASES].find_one({"spelling": spelling}, collation=CI_COLLATION)
            if have is None:
                db[COLL_CROP_ALIASES].insert_one({"spelling": spelling, "crop_id": m["master_id"], "created_at": now})
                written += 1
            elif have["crop_id"] != m["master_id"]:
                conflicts.append(spelling)
    print(f"  aliases: {written} written; spellings claimed by two crops (first kept): {conflicts}")

    for c, name, kw in ((COLL_ORGANIZATIONS, "uq_name_ci", {"unique": True, "collation": CI_COLLATION}),):
        db[c].create_index([("name", ASCENDING)], name=name, **kw)
    ensure_placement_indexes(db)

    if not is_production(db):
        mirror_master(db, master)
    verify(db, mapping, legacy)


def mirror_master(db, master) -> None:
    """Make pop_crops a copy of the crop master: same _ids, every field."""
    docs = list(master.find())
    for idx in list(db[COLL_CROPS].list_indexes()):
        if idx["name"] != "_id_":
            db[COLL_CROPS].drop_index(idx["name"])
    ids = {d["_id"] for d in docs}
    db[COLL_CROPS].bulk_write([UpdateOne({"_id": d["_id"]}, {"$set": {k: v for k, v in d.items() if k != "_id"}}, upsert=True)
                               for d in docs], ordered=False)
    stale = [e["_id"] for e in db[COLL_CROPS].find({"_id": {"$nin": list(ids)}}, {"_id": 1})]
    in_use = set(db[COLL_DOCUMENTS].distinct("crop_id", {"crop_id": {"$in": stale}}))
    db[COLL_CROPS].delete_many({"_id": {"$in": [s for s in stale if s not in in_use]}})
    print(f"  pop_crops: mirrored {len(docs)} master entries; removed {len(stale) - len(in_use)} not in the master"
          + (f"; KEPT {len(in_use)} the master dropped but placements still use" if in_use else ""))


def ensure_placement_indexes(db) -> None:
    for keys, name in (([("state_id", ASCENDING), ("crop_id", ASCENDING)], "state_id_crop_id"),
                       ([("crop_id", ASCENDING)], "crop_id"),
                       ([("state_id", ASCENDING), ("organization_id", ASCENDING)], "state_id_organization_id"),
                       ([("organization_id", ASCENDING)], "organization_id")):
        db[COLL_DOCUMENTS].create_index(keys, name=name)


def verify(db, mapping: dict | None = None, legacy: list | None = None) -> None:
    """Every placement: a valid state_id, and exactly one of a valid crop_id
    (visible master crop) or organization_id. Where the old crop string is still
    there, the resolved folder is the one the mapping says."""
    states = {e["_id"]: e["name"] for e in db[COLL_STATES].find({}, {"name": 1})}
    crops = {e["_id"]: e["name"] for e in crops_collection(db).find({"type": {"$ne": "chemical"}}, {"name": 1})}
    orgs = {e["_id"]: e["name"] for e in db[COLL_ORGANIZATIONS].find({}, {"name": 1})}
    bad, wrong, kinds = Counter(), [], Counter()
    for r in db[COLL_DOCUMENTS].find({}, {"state_id": 1, "crop_id": 1, "organization_id": 1, "crop": 1}):
        if r.get("state_id") not in states:
            bad["state_id missing or dangling"] += 1
        has_c, has_o = "crop_id" in r, "organization_id" in r
        if has_c == has_o:
            bad["not exactly one of crop_id / organization_id"] += 1
            continue
        if has_c and r["crop_id"] not in crops:
            bad["crop_id not a master crop"] += 1
        if has_o and r["organization_id"] not in orgs:
            bad["organization_id dangling"] += 1
        kinds["crop" if has_c else "organization"] += 1
        if mapping and "crop" in r and r["crop"] in mapping:
            m = mapping[r["crop"]]
            got = crops.get(r.get("crop_id")) if has_c else orgs.get(r.get("organization_id"))
            want = m.get("master_name") if m["kind"] == "crop" else r["crop"]
            if (m["kind"] == "crop") != has_c or got != want:
                wrong.append((r["crop"], got))
    assert not bad, dict(bad)
    assert not wrong, f"{len(wrong)} placement(s) resolve to the wrong folder: {wrong[:5]}"
    print(f"  verified: {sum(kinds.values())} placements -- {dict(kinds)}; every reference resolves"
          + ("; every old crop string lands where the mapping says" if mapping else ""))


# -- phase 2 ----------------------------------------------------------------------


def phase2(db, env: str, mapping: dict, dry_run: bool) -> None:
    if LEGACY not in db.list_collection_names() and not db[COLL_ORGANIZATIONS].estimated_document_count():
        raise SystemExit("run --phase crops first")
    # anything the old image wrote after phase crops: strings, no ids
    states = {e["name"]: e["_id"] for e in db[COLL_STATES].find({}, {"name": 1})}
    orgs_by_name = {e["name"]: e["_id"] for e in db[COLL_ORGANIZATIONS].find({}, {"name": 1})}
    todo = list(db[COLL_DOCUMENTS].find(
        {"$or": [{"state_id": {"$exists": False}},
                 {"crop_id": {"$exists": False}, "organization_id": {"$exists": False}}]},
        {"state": 1, "crop": 1, "state_id": 1}))
    unresolved, ops = [], []
    for r in todo:
        s = {}
        if "state_id" not in r:
            if r.get("state") not in states:
                unresolved.append(("state", r.get("state"))); continue
            s["state_id"] = states[r["state"]]
        m = mapping.get(r.get("crop"))
        if m and m["kind"] == "crop":
            s["crop_id"] = m["master_id"]
        elif r.get("crop") in orgs_by_name:
            s["organization_id"] = orgs_by_name[r["crop"]]
        else:
            unresolved.append(("crop", r.get("crop"))); continue
        ops.append(UpdateOne({"_id": r["_id"]}, {"$set": s}))
    print(f"  placements written by the old image since: {len(todo)}; resolvable: {len(ops)}")
    if unresolved:
        raise SystemExit(f"cannot place {len(unresolved)} placement(s) -- add them to the mapping: {sorted(set(unresolved))[:10]}")
    if dry_run:
        n = db[COLL_DOCUMENTS].count_documents({"$or": [{"state": {"$exists": True}}, {"crop": {"$exists": True}}]})
        print(f"  would remove name strings from {n} placement(s) and from every copy link")
        return
    print(f"  backup -> {backup(db, env, 'phase2').relative_to(REPO)}")
    if ops:
        db[COLL_DOCUMENTS].bulk_write(ops, ordered=False)
    verify(db, mapping)

    print("  placements: removed name strings from",
          db[COLL_DOCUMENTS].update_many({}, {"$unset": {"state": "", "crop": ""}}).modified_count)
    print("  copy links: removed state/crop on",
          db[COLL_UNIQUE_DOCUMENTS].update_many(
              {"duplicate_links.0": {"$exists": True}},
              {"$unset": {"duplicate_links.$[].state": "", "duplicate_links.$[].crop": ""}}).modified_count,
          "document(s)")
    for c in (COLL_STATES, COLL_ORGANIZATIONS):
        db[c].update_many({}, {"$unset": {"document_count": ""}})
        if "uq_name" in {i["name"] for i in db[c].list_indexes()}:
            db[c].drop_index("uq_name")
        db[c].create_index([("name", ASCENDING)], name="uq_name_ci", unique=True, collation=CI_COLLATION)
    existing = {i["name"] for i in db[COLL_DOCUMENTS].list_indexes()}
    for old in ("state_crop", "crop"):
        if old in existing:
            db[COLL_DOCUMENTS].drop_index(old)
    ensure_placement_indexes(db)
    if is_production(db) and COLL_CROPS in db.list_collection_names():
        if LEGACY in db.list_collection_names():
            db[COLL_CROPS].drop()
        else:
            db[COLL_CROPS].rename(LEGACY)
        print(f"  production: the old pop_crops is now {LEGACY} (crops come from the master)")
    left = db[COLL_DOCUMENTS].count_documents({"$or": [{"state": {"$exists": True}}, {"crop": {"$exists": True}}]})
    assert left == 0, f"{left} placement(s) still carry a name string"
    verify(db)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", required=True, choices=["STAGING", "PROD"])
    ap.add_argument("--phase", choices=["1", "crops", "2"])
    ap.add_argument("--mapping", help="the reviewed crop_master_mapping_review.csv")
    ap.add_argument("--sync-master", action="store_true", help="STAGING: refresh pop_crops from the crop master")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--database", help="rehearse against this database instead of <ENV>_DB_NAME")
    args = ap.parse_args()
    db = connect(args.env, args.database)
    print(f"{args.env} {db.name}: {'sync-master' if args.sync_master else 'phase ' + str(args.phase)}"
          f"{' (dry run)' if args.dry_run else ''}")

    if args.sync_master:
        if is_production(db):
            raise SystemExit("production reads the crop master directly; there is nothing to sync")
        if not args.dry_run:
            mirror_master(db, master_source())
        return
    if args.phase == "1":
        phase1(db, args.dry_run)
        return
    if not args.mapping:
        raise SystemExit("--mapping is required for this phase")
    mapping = load_mapping(args.mapping, master_source())
    print(f"  mapping: {len(mapping)} rows -- {dict(Counter(m['kind'] for m in mapping.values()))}")
    if args.phase == "crops":
        phase_crops(db, args.env, mapping, args.dry_run)
    elif args.phase == "2":
        phase2(db, args.env, mapping, args.dry_run)
    print("  done")


if __name__ == "__main__":
    main()
