"""One-off CLI: load unique_documents + document_associations from
results/pops.csv + results/main.csv (built by scripts/build_pops_csv.py /
scripts/build_main_csv.py), joining in the embedding vector for each sha256
from results/report_true_embeddings.json.

This supersedes dashboard/migrate_from_report_true.py as the migration path:
that script grouped report_true.csv by raw sha256 only, missing the
embedding-based near-duplicate merging results/report_true_duplicates.csv
already computed (see scripts/find_duplicates_from_embeddings.py), and had
no translation/review columns at all. pops.csv/main.csv already carry both,
so this script is a much more direct load -- no re-deriving anything, no
re-hashing, no re-embedding.

- pops.csv: one row per unique document (already deduped by sha + name +
  embedding methods) -> one unique_documents row each. A handful of shas
  (8, as of the run that built pops.csv) have no entry in
  report_true_embeddings.json -- those still get inserted, just with
  embedding=NULL (nullable in the schema); duplicate detection on upload
  simply can't compare against them via pgvector until they're re-embedded.
- main.csv: sha256/state/folder rows -> one document_associations row each,
  pointing at the unique_documents row for that sha256 (states/crops
  lookup tables seeded from main.csv's own distinct values first).

Resumable: unique_documents already present (by sha256) and
document_associations already present (by the same (sha256, state, folder)
identity) are skipped, not re-inserted -- safe to re-run after a fresh
pops.csv/main.csv build.

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


def _int_or_none(v: str | None) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _str_or_none(v: str | None) -> str | None:
    return v if v else None


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
          f"{missing_embedding} document(s) with no embedding (inserted with embedding=NULL)")

    from dashboard.db import get_session, init_db
    from dashboard.display_id import allocate_display_id
    from dashboard.models import Crop, DocumentAssociation, State, UniqueDocument

    init_db()

    all_state_names = sorted({r["state"] for r in main_rows if r.get("state")})
    all_crop_names = sorted({r["folder"] for r in main_rows if r.get("folder")})

    with get_session() as session:
        existing_states = {s.name for s in session.query(State).all()}
        for name in all_state_names:
            if name not in existing_states:
                session.add(State(name=name))
        existing_crops = {c.name for c in session.query(Crop).all()}
        for name in all_crop_names:
            if name not in existing_crops:
                session.add(Crop(name=name))

    with get_session() as session:
        state_id_by_name = {s.name: s.id for s in session.query(State).all()}
        crop_id_by_name = {c.name: c.id for c in session.query(Crop).all()}
        already_migrated = {d.sha256 for d in session.query(UniqueDocument.sha256).all()}

    migrated = skipped = 0
    for i, row in enumerate(pops_rows, start=1):
        sha = row["sha256"]
        if sha in already_migrated:
            skipped += 1
            continue

        with get_session() as session:
            doc = UniqueDocument(
                sha256=sha,
                display_id=allocate_display_id(session),
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
            session.add(doc)

        migrated += 1
        if i % 500 == 0:
            print(f"[migrate] [{i}/{len(pops_rows)}] unique documents...")

    print(f"[migrate] unique_documents: {migrated} inserted, {skipped} already present")

    with get_session() as session:
        doc_id_by_sha = {d.sha256: d.id for d in session.query(UniqueDocument.id, UniqueDocument.sha256).all()}
        existing_assoc = {
            (str(a.unique_document_id), a.state_id, a.crop_id)
            for a in session.query(DocumentAssociation.unique_document_id, DocumentAssociation.state_id,
                                    DocumentAssociation.crop_id).all()
        }

    assoc_created = assoc_skipped = assoc_missing_doc = 0
    with get_session() as session:
        for i, row in enumerate(main_rows, start=1):
            sha = row["sha256"]
            doc_id = doc_id_by_sha.get(sha)
            if doc_id is None:
                assoc_missing_doc += 1
                continue
            state_id = state_id_by_name.get(row.get("state", ""))
            crop_id = crop_id_by_name.get(row.get("folder", ""))
            if state_id is None or crop_id is None:
                continue
            key = (str(doc_id), state_id, crop_id)
            if key in existing_assoc:
                assoc_skipped += 1
                continue
            existing_assoc.add(key)
            session.add(DocumentAssociation(unique_document_id=doc_id, state_id=state_id, crop_id=crop_id))
            assoc_created += 1
            if i % 1000 == 0:
                print(f"[migrate] [{i}/{len(main_rows)}] placements...")

    print(f"[migrate] document_associations: {assoc_created} inserted, {assoc_skipped} already present, "
          f"{assoc_missing_doc} referencing a sha256 not in pops.csv (skipped)")
    print(f"[migrate] done.")


if __name__ == "__main__":
    main()
