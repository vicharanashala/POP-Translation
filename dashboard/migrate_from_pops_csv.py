"""One-off CLI: load unique_documents + document_associations from
results/pops.csv + results/main.csv (built by scripts/build_pops_csv.py /
scripts/build_main_csv.py), joining in the embedding vector for each sha256
from results/report_true_embeddings.json.

- pops.csv: one row per unique document (already deduped by sha + name +
  embedding methods) -> one unique_documents document each. A handful of shas
  have no entry in report_true_embeddings.json -- those still get inserted,
  just with embedding=None; duplicate detection on upload simply can't compare
  against them via vector search until they're re-embedded.
- main.csv: sha256/state/folder rows -> one document_associations document
  each, pointing at the unique_documents document for that sha256 (states and
  crops seeded from main.csv's own distinct values first).

Resumable: unique documents already present (by sha256) and placements already
present (by the same (sha256, state, folder) identity) are skipped, not
re-inserted -- safe to re-run after a fresh pops.csv/main.csv build.

States and crops keep INTEGER ids (MongoDB accepts any _id type) so the API
contract matches the Postgres version exactly and the frontend needs no change.

display_id is assigned in bulk here rather than through
display_id.allocate_display_id(), which rescans the collection on every call --
fine for one upload, quadratic across 7,500 inserts. The free-number set is
computed once and drawn from in order, which produces the identical result.

Usage:
    .venv/bin/python3 -m dashboard.migrate_from_pops_csv
    .venv/bin/python3 -m dashboard.migrate_from_pops_csv --limit 50   # smoke test
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

POPS_CSV = REPO_ROOT / "results" / "pops.csv"
MAIN_CSV = REPO_ROOT / "results" / "main.csv"
EMBEDDINGS_JSON = REPO_ROOT / "results" / "report_true_embeddings.json"

# Documents carry a 768-float embedding, stored as an Atlas float32 binary
# vector (~3 KB each; see models.to_stored_vector), so inserts are batched well
# under the 16 MB command limit.
_DOC_BATCH = 500
_ASSOC_BATCH = 2000


def _int_or_none(v: str | None) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _str_or_none(v: str | None) -> str | None:
    return v if v else None


def _seed_lookup(db, collection_name: str, names: list[str]) -> dict[str, int]:
    """Insert any missing names with sequential integer ids; return name -> id
    for every name in the collection."""
    collection = db[collection_name]
    existing = {row["name"]: row["_id"] for row in collection.find({}, {"name": 1})}
    next_id = (max(existing.values()) + 1) if existing else 1
    new_docs = []
    for name in names:
        if name in existing:
            continue
        existing[name] = next_id
        new_docs.append({"_id": next_id, "name": name})
        next_id += 1
    if new_docs:
        collection.insert_many(new_docs)
    return existing


def _free_display_ids(db, count: int) -> list[int]:
    """`count` smallest unused display_id values, in ascending order."""
    from dashboard.display_id import DISPLAY_ID_MAX
    from dashboard.models import COLL_UNIQUE_DOCUMENTS

    used = {
        row["display_id"]
        for row in db[COLL_UNIQUE_DOCUMENTS].find({"display_id": {"$exists": True}}, {"display_id": 1, "_id": 0})
        if row.get("display_id") is not None
    }
    free, candidate = [], 0
    while len(free) < count:
        if candidate > DISPLAY_ID_MAX:
            raise RuntimeError(f"display_id pool exhausted (0..{DISPLAY_ID_MAX})")
        if candidate not in used:
            free.append(candidate)
        candidate += 1
    return free


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Only migrate this many unique documents (smoke test).")
    args = parser.parse_args()

    for path in (POPS_CSV, MAIN_CSV, EMBEDDINGS_JSON):
        if not path.exists():
            print(f"Missing {path}", file=sys.stderr)
            sys.exit(1)

    embeddings: dict[str, list[float]] = json.loads(EMBEDDINGS_JSON.read_text(encoding="utf-8"))

    with POPS_CSV.open("r", newline="", encoding="utf-8") as fh:
        pops_rows = list(csv.DictReader(fh))
    with MAIN_CSV.open("r", newline="", encoding="utf-8") as fh:
        main_rows = list(csv.DictReader(fh))

    if args.limit is not None:
        keep_shas = {r["sha256"] for r in pops_rows[: args.limit]}
        pops_rows = [r for r in pops_rows if r["sha256"] in keep_shas]
        main_rows = [r for r in main_rows if r["sha256"] in keep_shas]

    missing_embedding = sum(1 for r in pops_rows if r["sha256"] not in embeddings)
    print(f"[migrate] {len(pops_rows)} unique document(s) in pops.csv, {len(main_rows)} placement(s) in main.csv, "
          f"{missing_embedding} document(s) with no embedding (inserted with embedding=None)")

    from dashboard.db import get_database, init_db
    from dashboard.models import (
        COLL_CROPS,
        COLL_DOCUMENT_ASSOCIATIONS,
        COLL_STATES,
        COLL_UNIQUE_DOCUMENTS,
        new_document_association,
        new_unique_document,
    )

    # The vector index is created separately (it takes ~25s to build and does
    # not block the load); ensure_vector_index() is still called by init_db.
    init_db()
    db = get_database()
    print(f"[migrate] target database: {db.name}")

    state_id_by_name = _seed_lookup(db, COLL_STATES, sorted({r["state"] for r in main_rows if r.get("state")}))
    crop_id_by_name = _seed_lookup(db, COLL_CROPS, sorted({r["folder"] for r in main_rows if r.get("folder")}))
    print(f"[migrate] states: {len(state_id_by_name)}, crops: {len(crop_id_by_name)}")

    already_migrated = {
        row["sha256"] for row in db[COLL_UNIQUE_DOCUMENTS].find({}, {"sha256": 1, "_id": 0})
    }
    todo = [r for r in pops_rows if r["sha256"] not in already_migrated]
    skipped = len(pops_rows) - len(todo)

    display_ids = _free_display_ids(db, len(todo))
    batch, migrated = [], 0
    for i, (row, display_id) in enumerate(zip(todo, display_ids), start=1):
        sha = row["sha256"]
        batch.append(
            new_unique_document(
                sha256=sha,
                display_id=display_id,
                embedding=embeddings.get(sha),
                advisory_type=_str_or_none(row.get("advisory_type")),
                advisory_scope=_str_or_none(row.get("advisory_scope")),
                season=_str_or_none(row.get("season")),
                edition_revision_volume=_str_or_none(row.get("edition_revision_volume")),
                date_of_release=_str_or_none(row.get("date_of_release")),
                month_of_release=_int_or_none(row.get("month_of_release")),
                year_of_release=_int_or_none(row.get("year_of_release")),
                date_of_collection=_str_or_none(row.get("date_of_collection")),
                month_of_collection=_int_or_none(row.get("month_of_collection")),
                year_of_collection=_int_or_none(row.get("year_of_collection")),
                advisory_name=_str_or_none(row.get("advisory_name")),
                advisory_released_org=_str_or_none(row.get("advisory_released_org")),
                advisory_org_address=_str_or_none(row.get("advisory_org_address")),
                live_source_link=_str_or_none(row.get("live_source_link")),
                domain=_str_or_none(row.get("domain")),
                verification_status=_str_or_none(row.get("verification_status")),
                verified_by=_str_or_none(row.get("verified_by")),
                document_status=_str_or_none(row.get("document_status")),
                shareable_name=_str_or_none(row.get("shareable_name")),
                shareable_link=_str_or_none(row.get("shareable_link")),
                language=_str_or_none(row.get("language")),
                format_original=row.get("format_original") or "pdf",
                num_pages=_int_or_none(row.get("num_pages")),
                zoho_file_id=_str_or_none(row.get("zoho_file_id")),
                translation_zoho_file_id=_str_or_none(row.get("translation_zoho_file_id")),
                translation_shareable_link=_str_or_none(row.get("translation_shareable_link")),
                translation_status=row.get("translation_status") or "not_started",
                review_zoho_file_id=_str_or_none(row.get("review_zoho_file_id")),
                review_shareable_link=_str_or_none(row.get("review_shareable_link")),
                review_status=row.get("review_status") or "not_started",
            )
        )
        if len(batch) >= _DOC_BATCH:
            db[COLL_UNIQUE_DOCUMENTS].insert_many(batch, ordered=False)
            migrated += len(batch)
            batch = []
            print(f"[migrate] [{i}/{len(todo)}] unique documents...")
    if batch:
        db[COLL_UNIQUE_DOCUMENTS].insert_many(batch, ordered=False)
        migrated += len(batch)

    print(f"[migrate] unique_documents: {migrated} inserted, {skipped} already present")

    doc_id_by_sha = {
        row["sha256"]: row["_id"] for row in db[COLL_UNIQUE_DOCUMENTS].find({}, {"sha256": 1})
    }
    existing_assoc = {
        (row["unique_document_id"], row["state_id"], row["crop_id"])
        for row in db[COLL_DOCUMENT_ASSOCIATIONS].find(
            {}, {"unique_document_id": 1, "state_id": 1, "crop_id": 1, "_id": 0}
        )
    }

    assoc_batch = []
    assoc_created = assoc_skipped = assoc_missing_doc = 0
    for i, row in enumerate(main_rows, start=1):
        doc_id = doc_id_by_sha.get(row["sha256"])
        if doc_id is None:
            assoc_missing_doc += 1
            continue
        state_id = state_id_by_name.get(row.get("state", ""))
        crop_id = crop_id_by_name.get(row.get("folder", ""))
        if state_id is None or crop_id is None:
            continue
        key = (doc_id, state_id, crop_id)
        if key in existing_assoc:
            assoc_skipped += 1
            continue
        existing_assoc.add(key)
        assoc_batch.append(
            new_document_association(unique_document_id=doc_id, state_id=state_id, crop_id=crop_id)
        )
        assoc_created += 1
        if len(assoc_batch) >= _ASSOC_BATCH:
            db[COLL_DOCUMENT_ASSOCIATIONS].insert_many(assoc_batch, ordered=False)
            assoc_batch = []
            print(f"[migrate] [{i}/{len(main_rows)}] placements...")
    if assoc_batch:
        db[COLL_DOCUMENT_ASSOCIATIONS].insert_many(assoc_batch, ordered=False)

    print(f"[migrate] document_associations: {assoc_created} inserted, {assoc_skipped} already present, "
          f"{assoc_missing_doc} referencing a sha256 not in pops.csv (skipped)")
    print("[migrate] done.")


if __name__ == "__main__":
    main()
