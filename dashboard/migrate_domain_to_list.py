"""Make every document's `domain` a list of names, as the API now reads and
writes it (models.DOMAINS, schemas.Domains).

It was one string. A document often covers several domains, so it is a list of
names picked from the fixed list now. This rewrites what is stored:

    "Weed Management"   ->  ["Weed Management"]
    None / "" / absent  ->  []

on pop_unique_documents, and the same on `metadata.domain` of anything still
waiting in pop_upload_queue_items (the worker copes with a string there too,
but the queue's edit form should see the new shape).

A stored name that is not in DOMAINS is reported and left as it was -- a
person decides what it should be, the script does not guess.

Run it AFTER the new image is serving, never before: the old image's schema
is a string and fails on a list, while the new one serves a stored string as a
one-name list, so the deploy-then-migrate window is harmless. Re-runnable; a
dry run unless --apply.

    POP_ENV=staging python3 -m dashboard.migrate_domain_to_list [--apply]
    POP_ENV=prod    python3 -m dashboard.migrate_domain_to_list [--apply]
"""
from __future__ import annotations

import argparse

from pymongo import UpdateOne

from dashboard.db import get_session
from dashboard.models import COLL_UNIQUE_DOCUMENTS, COLL_UPLOAD_QUEUE_ITEMS, DOMAINS


def _as_list(value) -> list[str] | None:
    """The list `value` becomes, or None when it is a name we do not know."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() in DOMAINS else None
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args()

    with get_session() as db:
        for coll, field in ((COLL_UNIQUE_DOCUMENTS, "domain"), (COLL_UPLOAD_QUEUE_ITEMS, "metadata.domain")):
            if coll == COLL_UPLOAD_QUEUE_ITEMS:
                # Only items whose metadata carries a string; absent stays absent.
                query = {field: {"$type": "string"}}
            else:
                query = {field: {"$not": {"$type": "array"}}}
            ops, unknown = [], []
            for doc in db[coll].find(query, {field.split(".")[0]: 1, "display_id": 1}):
                value = doc.get("metadata", {}).get("domain") if "." in field else doc.get("domain")
                new = _as_list(value)
                if new is None:
                    unknown.append((doc.get("display_id", doc["_id"]), value))
                    continue
                ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {field: new}}))
            print(f"{coll}: {len(ops)} to rewrite, {len(unknown)} unknown name(s) left as they are")
            for who, value in unknown:
                print(f"  unknown: {who} {value!r}")
            if args.apply and ops:
                result = db[coll].bulk_write(ops, ordered=False)
                print(f"  rewrote {result.modified_count}")
    if not args.apply:
        print("dry run -- nothing written; pass --apply")


if __name__ == "__main__":
    main()
