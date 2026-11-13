"""Sync states, districts and KVKs from the LGD tables in `agriai`.

Run occasionally, by hand. `agriai` is the ONLY source for these three: nothing
in the dashboard creates or edits them (dashboard/vocabulary.py marks all three
not-editable, so rename/merge/delete answer 403), because an edit here would be
silently undone by the next run.

    POP_ENV=staging    PYTHONPATH=. .venv/bin/python scripts/sync_lgd.py --dry-run
    POP_ENV=staging    PYTHONPATH=. .venv/bin/python scripts/sync_lgd.py
    POP_ENV=production PYTHONPATH=. .venv/bin/python scripts/sync_lgd.py

SOURCE (read-only; the dashboard's user has a read grant on these):
    agriai.states      37 rows: stateCode, stateNameEnglish
    agriai.districts  799 rows: districtCode, districtNameEnglish, stateCode
    agriai.kvks       727 rows: kvkId, kvkName, kvkAddress, districtCode, stateCode

MATCHED ON CODE, NOT NAME. LGD renames places -- it says `Keralam` where we said
`Kerala`, and `The Dadra And Nagar Haveli And Daman And Diu` with the article --
so a name match would create a duplicate on the second run instead of renaming
the first. The code is the identity; the name is data the sync overwrites.

STATES ALREADY EXIST (37 of them, from the corpus load), so the first run has to
adopt them rather than insert duplicates: an existing state is matched by name
once, given its `state_code`, and renamed to LGD's spelling. The old spelling is
kept in `raw_names`, so a filter or an upload still typing `Kerala` resolves.

    agriai `All` (stateCode 39) IS our `Central` -- the entry for advisories that
    are not one state's. Mapped onto it rather than added beside it. Its two LGD
    districts (`All`, `Faq`) are junk and skipped, as are the 12 districts
    literally named `All`; ours are added below instead.

OUR OWN "All" ENTRY. After syncing, districts get ONE shared `All` row and KVKs
get one of their own, so a document that is not specific to a single district
still has something to select in the form. They carry no LGD code and no parent
and are marked `is_all`; readers union the row into every parent's list. One row
rather than one per parent -- add_all_entry() says why that changed.

NEVER TOUCHES a placement. Documents keep whatever district_id/kvk_id they
already have; this only maintains the lookups they point at.
"""
import argparse
import collections
import os

from dotenv import load_dotenv

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

from pymongo import UpdateOne  # noqa: E402

from dashboard import vocabulary  # noqa: E402
from dashboard.db import get_session  # noqa: E402
from dashboard.models import COLL_DISTRICTS, COLL_KVKS, COLL_STATES, utcnow  # noqa: E402

SOURCE_DB = "agriai"
# Our entry for advisories that belong to no single state. LGD calls it "All".
CENTRAL = "Central"
# Rows in the LGD tables that are placeholders rather than places.
JUNK_DISTRICT_NAMES = {"all", "faq"}

# LGD spellings that are the SAME state under a different name from the one the
# corpus load gave it. Without these the first run inserts a second entry and
# orphans the original -- the one thousands of placements already point at. Only
# needed for names a case fold and a dropped article cannot bridge.
STATE_ALIASES = {"keralam": "Kerala"}


def _match_state(lgd_name: str, by_name: dict) -> dict | None:
    """Our existing state entry for an LGD name, or None if it is genuinely new.

    Tried in order: the name as given, the alias table above, and the name with a
    leading article dropped ("The Dadra And Nagar Haveli And Daman And Diu").
    All case-insensitive, because LGD capitalises "And" and we do not.
    """
    folded = lgd_name.casefold()
    for candidate in (folded, STATE_ALIASES.get(folded, "").casefold(),
                      folded[4:] if folded.startswith("the ") else ""):
        if candidate and candidate in by_name:
            return by_name[candidate]
    return None


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _source_db():
    """The LGD tables, which live on the PRODUCTION cluster whichever database
    this run writes to.

    Staging's cluster has an `agriai` database but no LGD tables in it, so a
    staging sync reads production and writes locally -- the same arrangement
    crops already use (dashboard/config.py: staging keeps its own copy because
    it cannot reach the master). Read-only, and the URL is never printed.
    """
    from pymongo import MongoClient

    from dashboard.config import _env

    url = _env("PROD_DB_URL")
    if not url:
        raise SystemExit("PROD_DB_URL is not set -- the LGD tables live on that cluster.")
    return MongoClient(url, appname="pop-lgd-sync")[SOURCE_DB]


def _source(db):
    """The three LGD tables, cleaned and keyed. Read-only."""
    src = _source_db()
    states = {s["stateCode"]: _clean(s.get("stateNameEnglish"))
              for s in src["states"].find({}, {"stateCode": 1, "stateNameEnglish": 1})
              if s.get("stateCode") is not None}
    all_code = next((c for c, n in states.items() if n.lower() == "all"), None)
    districts = [
        {"code": d["districtCode"], "name": _clean(d.get("districtNameEnglish")),
         "state_code": d.get("stateCode")}
        for d in src["districts"].find({}, {"districtCode": 1, "districtNameEnglish": 1, "stateCode": 1})
        if d.get("districtCode") is not None
    ]
    # Drop the placeholders: the "All" state's own rows, and every district
    # literally named All. Ours are added instead, one per state.
    districts = [d for d in districts
                 if d["state_code"] != all_code and d["name"].lower() not in JUNK_DISTRICT_NAMES]
    kvks = [
        {"code": _clean(k.get("kvkId")), "name": _clean(k.get("kvkName")),
         "address": _clean(k.get("kvkAddress")) or None,
         "district_code": k.get("districtCode"), "state_code": k.get("stateCode")}
        for k in src["kvks"].find({})
        if _clean(k.get("kvkId")) and _clean(k.get("kvkName"))
    ]
    return states, all_code, districts, kvks


def sync_states(db, states: dict, all_code, dry_run: bool) -> dict:
    """stateCode -> our state's _id. Adopts the 37 existing entries by name."""
    ours = list(db[COLL_STATES].find())
    by_name = {s["name"].casefold(): s for s in ours}
    by_code = {s["state_code"]: s for s in ours if s.get("state_code") is not None}
    ops, mapping, adopted, renamed, inserted = [], {}, 0, 0, 0

    for code, name in sorted(states.items()):
        # LGD's "All" is our "Central" -- mapped onto it, never added beside it.
        target_name = CENTRAL if code == all_code else name
        entry = by_code.get(code) or _match_state(target_name, by_name)
        if entry is None and code == all_code:
            entry = by_name.get(CENTRAL.casefold())
        if entry is None:
            inserted += 1
            print(f"  new state (not one of ours under any spelling): {target_name!r} [{code}]")
            if not dry_run:
                now = utcnow()
                res = db[COLL_STATES].insert_one(
                    {"name": target_name, "raw_names": [], "state_code": code,
                     "created_at": now, "updated_at": now})
                mapping[code] = res.inserted_id
            continue
        mapping[code] = entry["_id"]
        if entry.get("state_code") is None:
            adopted += 1
        update: dict = {"state_code": code, "updated_at": utcnow()}
        add_raw = None
        # Central keeps its own name; every other state takes LGD's spelling,
        # with the old one kept as an alternative so existing filters still hit.
        if code != all_code and entry["name"] != name:
            renamed += 1
            update["name"] = name
            if entry["name"] not in (entry.get("raw_names") or []):
                add_raw = entry["name"]
        op = {"$set": update}
        if add_raw:
            op["$addToSet"] = {"raw_names": add_raw}
        ops.append(UpdateOne({"_id": entry["_id"]}, op))

    print(f"states: {len(states)} in LGD | adopting codes for {adopted} | renaming {renamed} | "
          f"inserting {inserted}")
    if ops and not dry_run:
        db[COLL_STATES].bulk_write(ops, ordered=False)
    return mapping


def sync_children(db, coll: str, kind: str, rows: list[dict], parents: dict, dry_run: bool,
                  state_ids: dict | None = None) -> dict:
    """Upsert one synced vocabulary, matched on its LGD code. Returns code -> _id.

    `parents` maps a row's parent CODE to the parent's _id; a row whose parent is
    unknown is skipped and reported rather than stored parentless, because a
    dropdown narrows by parent and an orphan would never appear in one.

    `state_ids` is only for KVKs, whose parent map is keyed by DISTRICT code: LGD
    gives each KVK its stateCode too, so the state is stored alongside the
    district and saves a hop.
    """
    code_field, parent_field = vocabulary.CODE[kind], vocabulary.PARENT[kind][1]
    existing = {e[code_field]: e for e in db[coll].find({code_field: {"$ne": None}})}
    ops, skipped = [], collections.Counter()
    now = utcnow()
    for row in rows:
        parent_id = parents.get(row.get(f"{vocabulary.PARENT[kind][0]}_code"))
        if parent_id is None:
            skipped[f"no {vocabulary.PARENT[kind][0]} for it"] += 1
            continue
        doc = {"name": row["name"], code_field: row["code"], parent_field: parent_id,
               "updated_at": now}
        if kind == "kvk":
            doc["address"] = row.get("address")
            # Carried because LGD gives it and it saves a hop; the dropdown
            # still narrows by district.
            doc["state_id"] = (state_ids or {}).get(row.get("state_code"))
        ops.append(UpdateOne({code_field: row["code"]},
                             {"$set": doc, "$setOnInsert": {"raw_names": [], "created_at": now}},
                             upsert=True))
    new = sum(1 for r in rows if r["code"] not in existing)
    print(f"{kind}s: {len(rows)} in LGD | {new} new | {len(rows) - new} updated", end="")
    for why, n in skipped.items():
        print(f" | skipped ({why}): {n}", end="")
    print()
    if ops and not dry_run:
        for start in range(0, len(ops), 500):
            db[coll].bulk_write(ops[start:start + 500], ordered=False)
    return {e[code_field]: e["_id"] for e in db[coll].find({code_field: {"$ne": None}})}


def add_all_entry(db, coll: str, kind: str, dry_run: bool) -> None:
    """The ONE shared "All" entry for this collection.

    No parent reference and no LGD code: it stands for "not specific to any one
    of these", which is a single fact, so it is a single row that every parent's
    dropdown offers. `vocabulary.all_entry()` reads it; `children_of()` and
    GET /locations union it into each list, since no parent query can reach it.

    This replaces an earlier per-parent version -- 37 "All" districts and 823
    "All" KVKs, one under each parent. They all meant the same thing while
    carrying different ids, which gave "no particular district" two
    representations (an "All" id, or no id at all) with nothing keeping them in
    step. The old rows are removed here, which is safe because no placement has
    ever referenced one.
    """
    parent_field = vocabulary.PARENT[kind][1]
    stale_q = {"name": vocabulary.ALL, parent_field: {"$exists": True}}
    stale = db[coll].count_documents(stale_q)
    have = db[coll].count_documents({vocabulary.IS_ALL: True})
    print(f'{kind}s: one shared "{vocabulary.ALL}" entry '
          f'({"already there" if have else "adding"})'
          + (f", removing {stale} old per-{vocabulary.PARENT[kind][0]} one(s)" if stale else ""))
    if dry_run:
        return
    if stale:
        db[coll].delete_many(stale_q)
    now = utcnow()
    db[coll].update_one(
        {vocabulary.IS_ALL: True},
        {"$setOnInsert": {"name": vocabulary.ALL, vocabulary.IS_ALL: True,
                          "raw_names": [], "created_at": now},
         "$set": {"updated_at": now}},
        upsert=True,
    )


def drop_stale_name_index(db, coll: str, dry_run: bool) -> None:
    """The first version of these collections had a GLOBALLY unique name index.
    District names are not globally unique -- Bilaspur is in Chhattisgarh and in
    Himachal Pradesh -- so that index rejects the real list and has to go."""
    if "uq_name_ci" not in db[coll].index_information():
        return
    print(f"{coll}: dropping the stale global uq_name_ci index")
    if not dry_run:
        db[coll].drop_index("uq_name_ci")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing.")
    args = parser.parse_args()
    print("env:", os.environ.get("POP_ENV", "staging"), "(dry run)" if args.dry_run else "")

    with get_session() as db:
        for coll in (COLL_DISTRICTS, COLL_KVKS):
            drop_stale_name_index(db, coll, args.dry_run)
        states, all_code, districts, kvks = _source(db)
        print(f"source: {len(states)} states, {len(districts)} districts, {len(kvks)} kvks "
              f'(LGD "All" state is code {all_code} -> our {CENTRAL!r})')

        state_ids = sync_states(db, states, all_code, args.dry_run)
        if args.dry_run and not state_ids:
            print("dry run: states not written, so district/kvk parents are unknown -- stopping")
            return
        district_ids = sync_children(db, COLL_DISTRICTS, "district", districts, state_ids, args.dry_run)
        # KVKs hang off districts, so their parent map is keyed by district code;
        # state_ids is passed separately for the state they also record.
        sync_children(db, COLL_KVKS, "kvk", kvks, district_ids, args.dry_run, state_ids=state_ids)

        # One shared entry each, parentless -- so there is nothing to iterate
        # parents for, and a parent with no real children still has it to offer.
        add_all_entry(db, COLL_DISTRICTS, "district", args.dry_run)
        add_all_entry(db, COLL_KVKS, "kvk", args.dry_run)
        if args.dry_run and not district_ids:
            print("\nnote: a dry run writes no districts, so every KVK reads as "
                  '"no district for it". That resolves on the real run.')

        if not args.dry_run:
            print(f"\nnow: {db[COLL_STATES].count_documents({})} states, "
                  f"{db[COLL_DISTRICTS].count_documents({})} districts, "
                  f"{db[COLL_KVKS].count_documents({})} kvks")


if __name__ == "__main__":
    main()
