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


def main() -> None:
    col = MongoClient(os.environ["PROD_DB_URL"], serverSelectionTimeoutMS=20000)[os.environ["PROD_DB_NAME"]]["pop_unique_documents"]
    rows = []
    for d in col.find({"advisory_scope": "Central"}, {"chunk_embeddings": 0}):
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
        rows.append({"doc_id": str(d["_id"]), "name": d.get("shareable_name"), "link": d.get("shareable_link"),
                     "file_id": d.get("representative_file_id"), "pages": pages, "language": d.get("language"),
                     "fmt": d.get("format_original"), "states": json.dumps(["Central"]),
                     "alt_ids": json.dumps(sorted({i for i in ids if i and i != "None"}))})
    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False)
    print(len(out), "central documents ->", OUT)
    print(pd.crosstab(out["fmt"], out["language"]).to_string())
    sheet = pd.read_excel(common.XLSX)
    ids = set(sheet["unique_document__id"])
    print("central ids also in the sheet:", len(ids & set(out["doc_id"])))


if __name__ == "__main__":
    main()
