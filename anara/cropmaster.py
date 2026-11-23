"""The crop master as a local, English-only vocabulary.

Production keeps it in agriai.crop_master (the cluster behind PROD_DB_URL in the repo's root
.env; see dashboard/config.py). We only READ it, once, and cache the result in
data/crop_master.json so searches never touch production.

    load()                 -> [{"name", "type", "forms": [[token, ...], ...]}]
    crops(entries)         -> only type "crop", without placeholder entries
    python anara/cropmaster.py [--refresh]      fetch + print a summary (never prints the URI)

A `form` is one way the document might write the thing, already tokenised like the keyword
index: the name (split at brackets, slashes and commas), the scientific name and the English
rendering of every alias. Native-script aliases are of no use against English text and are dropped.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
import keywords as kw  # noqa: E402

CACHE = common.DATA / "crop_master.json"
DB, COLLECTION = "agriai", "crop_master"
ROOT_ENV = HERE.parent / ".env"          # the repo's .env, not anara/.env (that one holds MONGO_URI)

# Placeholder rows the master files under type "crop": they stand for no single crop.
_PLACEHOLDER = re.compile(r"^(test[_ ].*|all|general|other|various|mixed)$|\bcrops?$", re.I)


def _forms(row: dict) -> list[list[str]]:
    raw = [row.get("name", ""), row.get("scientificName", "")]
    for al in row.get("aliases") or []:
        if isinstance(al, dict):
            raw.append(al.get("english_representation", ""))
    forms, seen = [], set()
    for text in raw:
        for part in re.split(r"[()/,;]| or ", text or ""):
            toks = tuple(kw.tokenize(part))
            if toks and toks not in seen:
                seen.add(toks)
                forms.append(list(toks))
    return forms


def fetch() -> list[dict]:
    from dotenv import dotenv_values
    from pymongo import MongoClient
    uri = dotenv_values(ROOT_ENV).get("PROD_DB_URL")
    if not uri:
        raise SystemExit(f"PROD_DB_URL is not set in {ROOT_ENV}")
    col = MongoClient(uri, serverSelectionTimeoutMS=20000)[DB][COLLECTION]
    return [{"name": r["name"], "type": r.get("type") or "", "forms": _forms(r)} for r in col.find({})]


def load(refresh: bool = False) -> list[dict]:
    if CACHE.exists() and not refresh:
        return json.loads(CACHE.read_text())
    entries = fetch()
    CACHE.write_text(json.dumps(entries, ensure_ascii=False))
    return entries


def crops(entries: list[dict] | None = None) -> list[dict]:
    """Real crops: type "crop", not a placeholder, with at least one usable English form."""
    entries = load() if entries is None else entries
    return [e for e in entries if e["type"] == "crop" and e["forms"] and not _PLACEHOLDER.search(e["name"])]


if __name__ == "__main__":
    entries = load(refresh="--refresh" in sys.argv)
    cs = crops(entries)
    kinds = {}
    for e in entries:
        kinds[e["type"] or "(none)"] = kinds.get(e["type"] or "(none)", 0) + 1
    print(f"{len(entries)} entries {kinds}")
    print(f"{len(cs)} usable crops; dropped placeholders: "
          f"{sorted(e['name'] for e in entries if e['type'] == 'crop' and _PLACEHOLDER.search(e['name']))[:20]}")
    for e in cs[:4]:
        print(e["name"], e["forms"][:5])
