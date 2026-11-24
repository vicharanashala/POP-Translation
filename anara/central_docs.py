"""Central-scope documents of the POP dashboard -> data/central_docs.csv (read-only on the POP database).

    python anara/central_docs.py

common.load_documents() appends these rows to the sheet's documents (sheet ids win). `alt_ids` holds
every Zoho file id of the document (shareable link and duplicate_links) for common.link_map().
Needs PROD_DB_URL / PROD_DB_NAME in the repo-root .env.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys

import pandas as pd
from pymongo import MongoClient

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402

OUT = common.DATA / "central_docs.csv"
ADV_OUT = common.DATA / "central_adv_docs.csv"    # just the Central-Advisories rows (not in the sheet), the ones to ingest
EXTRA = {"centraladvisories": "Central"}


def _key(s: str) -> str:
    return re.sub(r"[^a-z]", "", re.sub(r"^\s*state\s+", "", str(s), flags=re.I).lower())


def _advisory_ids(db) -> set[str]:
    """Documents with no Central scope whose placements are all "Central Advisories" (plus possibly Punjab): they are
    Central documents that the scope field misses. Documents also placed in another state stay state documents."""
    canon = {_key(s["name"]): s["name"] for s in db.pop_states.find()}
    canon.update(EXTRA)
    placed: dict[str, set] = {}
    for d in db.pop_documents.find({}, {"unique_document_id": 1, "state_raw": 1}):
        raw = d.get("state_raw") or ""
        placed.setdefault(str(d["unique_document_id"]), set()).add(canon.get(_key(raw), raw.strip() or "?"))
    return {u for u, st in placed.items() if "Central" in st and not (st - {"Central", "Punjab", "?"})}


def main() -> None:
    db = MongoClient(os.environ["PROD_DB_URL"], serverSelectionTimeoutMS=20000)[os.environ["PROD_DB_NAME"]]
    col = db["pop_unique_documents"]
    adv = _advisory_ids(db)
    sheet_ids = set(pd.read_excel(common.XLSX)["unique_document__id"])
    rows, extra = [], []
    for d in col.find({}, {"chunk_embeddings": 0}):
        scope, doc = d.get("advisory_scope"), str(d["_id"])
        if scope != "Central" and not (scope != "District" and doc in adv and doc not in sheet_ids):
            continue
        ids = [str(d.get("representative_file_id"))]
        ids += re.findall(r"/file/([A-Za-z0-9]+)", str(d.get("shareable_link")))
        dup = d.get("duplicate_links") or []
        if isinstance(dup, str):
            try:
                dup = ast.literal_eval(dup)
            except (ValueError, SyntaxError):
                dup = []
        ids += [x["zoho_file_id"] for x in dup if isinstance(x, dict) and x.get("zoho_file_id")]
        try:
            pages = int(d.get("num_pages"))
        except (TypeError, ValueError):
            pages = 0
        (rows if scope == "Central" else extra).append({"doc_id": doc, "name": d.get("shareable_name"), "link": d.get("shareable_link"),
                     "file_id": d.get("representative_file_id"), "pages": pages, "language": d.get("language"),
                     "fmt": d.get("format_original"), "states": json.dumps(["Central"]),
                     "alt_ids": json.dumps(sorted({i for i in ids if i and i != "None"}))})
    out = pd.DataFrame(rows + extra)
    out.to_csv(OUT, index=False)
    pd.DataFrame(extra).to_csv(ADV_OUT, index=False)
    print(len(rows), "Central-scope +", len(extra), "Central-Advisories documents ->", OUT, "; the latter alone ->", ADV_OUT)
    print(pd.crosstab(out["fmt"], out["language"]).to_string())
    print("central ids also in the sheet:", len(sheet_ids & set(out["doc_id"])))


if __name__ == "__main__":
    main()
