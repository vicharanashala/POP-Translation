"""Documents of states other than Punjab -> data/other_state_docs.csv (read-only on the POP database).

    python anara/other_state_docs.py            write the csv and print the counts

A document's states come from the placement collection `pop_documents` (state_raw, joined on
unique_document_id; the unique document itself has no state field), normalised to the names of `pop_states`.
Selected: advisory_scope State or empty (7,645 documents have no scope; they are placed under one state) with at least
one known state other than Punjab, not already in the sheet or data/central_docs.csv. Not selected, and counted:
Central scope, "Central Advisories" placement without a scope, District scope, Punjab-only documents missing from the
sheet, documents with no state placement. The csv keeps every selected document (any format); other_states_stream.py
only fetches fmt == pdf. Columns as central_docs.csv. Needs PROD_DB_URL / PROD_DB_NAME in the repo-root .env.
"""
from __future__ import annotations

import ast
import collections
import json
import os
import re
import sys

import pandas as pd
from pymongo import MongoClient

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common  # noqa: E402

OUT = common.DATA / "other_state_docs.csv"
EXTRA = {"kerala": "Keralam", "chattisgarh": "Chhattisgarh", "tamilnadu": "Tamil Nadu", "centraladvisories": "Central"}


def _key(s: str) -> str:
    s = re.sub(r"^\s*state\s+", "", str(s), flags=re.I)
    return re.sub(r"[^a-z]", "", s.lower())


def main() -> None:
    db = MongoClient(os.environ["PROD_DB_URL"], serverSelectionTimeoutMS=20000)[os.environ["PROD_DB_NAME"]]
    canon: dict[str, str] = {}
    for s in db.pop_states.find():
        canon[_key(s["name"])] = s["name"]
        for r in s.get("raw_names", []):
            canon[_key(r)] = s["name"]
    canon.update(EXTRA)
    placed = collections.defaultdict(set)
    for d in db.pop_documents.find({}, {"unique_document_id": 1, "state_raw": 1}):
        raw = d.get("state_raw") or ""
        placed[str(d["unique_document_id"])].add(canon.get(_key(raw), raw.strip() or "?"))
    indexed = set(common.load_documents(central=False)["doc_id"]) | common.central_ids()   # sheet + Central, not this csv
    skipped = collections.Counter()
    rows = []
    for d in db.pop_unique_documents.find({}, {"chunk_embeddings": 0}):
        doc = str(d["_id"])
        scope, states = d.get("advisory_scope"), placed.get(doc, set())
        other = sorted(states - {"Punjab", "Central", "?"})
        if doc in indexed:
            skipped["already indexed"] += 1
        elif scope == "Central":
            skipped["central scope"] += 1
        elif scope == "District":
            skipped["district scope"] += 1
        elif not states:
            skipped["no state placement"] += 1
        elif not other:
            skipped["Central Advisories placement without scope" if "Central" in states else
                    "Punjab only, not in sheet" if "Punjab" in states else "unknown state"] += 1
        else:
            ids = [str(d.get("representative_file_id"))] + re.findall(r"/file/([A-Za-z0-9]+)", str(d.get("shareable_link")))
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
            rows.append({"doc_id": doc, "name": d.get("shareable_name"), "link": d.get("shareable_link"),
                         "file_id": d.get("representative_file_id"), "pages": pages, "language": d.get("language"),
                         "fmt": d.get("format_original"), "states": json.dumps(sorted(states - {"?"})),
                         "alt_ids": json.dumps(sorted({i for i in ids if i and i != "None"}))})
    out = pd.DataFrame(rows)
    out.to_csv(OUT, index=False)
    print(len(out), "documents ->", OUT)
    print("not selected:", dict(skipped))
    out["st"] = out["states"].map(lambda s: ", ".join(json.loads(s)))
    pdf = out[out.fmt == "pdf"]
    g = pd.DataFrame({"docs": out.groupby("st").size(), "pdf": pdf.groupby("st").size(),
                      "pdf_pages": pdf.groupby("st")["pages"].sum()}).fillna(0).astype(int)
    print(g.sort_values("docs", ascending=False).to_string())
    print("formats:", out.fmt.value_counts().to_dict())
    print("pdf pages by catalogued language:", pdf.groupby("language", dropna=False)["pages"].agg(["size", "sum"]).to_dict("index"))
    print("pdf total pages:", int(pdf.pages.sum()))


if __name__ == "__main__":
    main()
