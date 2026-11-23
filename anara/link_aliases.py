"""Original link + every duplicate Zoho file id of each document -> data/link_aliases.json (read-only on the POP database).

    python anara/link_aliases.py build

Scope: every document of common.load_documents() (sheet + Central) and every document in the zone index, read from
pop_unique_documents (shareable_link, representative_file_id, duplicate_links[].zoho_file_id / shareable_link).
common.link_map() uses the file first, so a pasted link to ANY copy resolves to the document and
common.load_documents() shows the DB's original link. A file id claimed by two documents keeps the first
document (indexed ones first) and is listed under "conflicts". Needs PROD_DB_URL / PROD_DB_NAME in the repo-root .env.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys

from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402

OUT = common.DATA / "link_aliases.json"
FILE_ID = re.compile(r"/file/([A-Za-z0-9]+)")
FIELDS = ["shareable_link", "representative_file_id", "duplicate_links", "shareable_name", "advisory_scope"]


def _ids(d: dict) -> tuple[str | None, list[str]]:
    """(original file id, [representative id, duplicate ids...]) of one pop_unique_documents record."""
    m = FILE_ID.search(str(d.get("shareable_link")))
    orig = m.group(1) if m else None
    dup = d.get("duplicate_links") or []
    if isinstance(dup, str):
        try:
            dup = ast.literal_eval(dup)
        except (ValueError, SyntaxError):
            dup = []
    ids = [str(d.get("representative_file_id"))]
    for x in dup:
        if isinstance(x, dict):
            ids.append(str(x.get("zoho_file_id")))
            ids += FILE_ID.findall(str(x.get("shareable_link")))
    return orig, ids


def build() -> None:
    wanted = [str(i) for i in json.loads((common.DATA / "zone_meta.json").read_text())["docs"]]
    wanted += [i for i in common.load_documents()["doc_id"] if i not in set(wanted)]
    col = MongoClient(os.environ["PROD_DB_URL"], serverSelectionTimeoutMS=20000)[os.environ["PROD_DB_NAME"]]["pop_unique_documents"]
    found = {str(d["_id"]): d for d in col.find({"_id": {"$in": [ObjectId(i) for i in wanted]}}, dict.fromkeys(FIELDS, 1))}
    docs, ids, conflicts = {}, {}, {}
    for doc in wanted:
        d = found.get(doc)
        if d is None:
            continue
        orig, rest = _ids(d)
        rep = str(d.get("representative_file_id"))
        allids = list(dict.fromkeys(i for i in [orig] + rest if i and i != "None"))
        docs[doc] = {"original_link": d.get("shareable_link"), "original_file_id": orig or rep,
                     "name": d.get("shareable_name"),
                     "aliases": [i for i in allids if i != (orig or rep)]}
        for i in allids:
            if ids.setdefault(i, doc) != doc:
                conflicts.setdefault(i, [ids[i]]).append(doc)
    OUT.write_text(json.dumps({"docs": docs, "ids": ids, "conflicts": conflicts}, indent=1))
    scope = {s: col.count_documents({"advisory_scope": s}) for s in ("Central", "State")}
    print(f"wanted {len(wanted)}, found in DB {len(docs)}, missing {len(wanted) - len(docs)}")
    print("docs with aliases:", sum(bool(v["aliases"]) for v in docs.values()),
          "| alias ids:", sum(len(v["aliases"]) for v in docs.values()),
          "| ids:", len(ids), "| conflicts:", len(conflicts))
    for i, ds in conflicts.items():
        print("  conflict", i, ds)
    print("DB advisory_scope counts:", scope)
    print("Central docs in DB not in scope:", col.count_documents({"advisory_scope": "Central", "_id": {"$nin": [ObjectId(i) for i in docs]}}))
    print("State docs in DB not in scope:", col.count_documents({"advisory_scope": "State", "_id": {"$nin": [ObjectId(i) for i in docs]}}))
    print("->", OUT)


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build()
    else:
        raise SystemExit(__doc__)
