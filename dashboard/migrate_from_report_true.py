"""One-off CLI: backfill unique_documents + document_associations from
results/state_language_report/report_true.csv +
results/report_true_embeddings.json.

This is what makes duplicate detection meaningful from day one -- new
uploads get compared against the full existing corpus, not an empty table.
NO re-hashing or re-embedding happens here: sha256 and embedding vectors are
read directly from files scripts/hash_and_embed_report_true.py has already
produced (that script is checkpointed, so this can run against a partial
result -- rows without a matching embedding entry yet are simply skipped
and can be migrated in a later run once that sha is embedded).

Column placement matches docs/dashboard_backend_plan.md §2 (confirmed with
the user): today's CSV has one collection-date per (state, folder, pdf) row,
but Season/Date/Month/Year of Collection are DOCUMENT-level in the new
schema -- for a sha with multiple CSV rows, the first-encountered row's
collection date is used as the representative value (the same convention
scripts/hash_and_embed_report_true.py already uses to resolve OCR language
per sha). `season` has no source column in the CSV at all and is left NULL.

Original PDF files are NOT copied into the new Zoho mega-folder layout by
this migration (see backend plan §9) -- zoho_file_id is left NULL and
shareable_link stays pointing at the existing doc_link-style URL for these
migrated rows; only genuinely new uploads through the dashboard get a
zoho_file_id, under one of the fixed ZOHO_DASHBOARD_*_FOLDER_ID subfolders
(see dashboard/zoho_layout.py).

Usage:
    .venv/bin/python3 -m dashboard.migrate_from_report_true
    .venv/bin/python3 -m dashboard.migrate_from_report_true --limit 50   # smoke test
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

REPORT_TRUE_CSV = REPO_ROOT / "results" / "state_language_report" / "report_true.csv"
EMBEDDINGS_JSON = REPO_ROOT / "results" / "report_true_embeddings.json"


def _int_or_none(v: str | None) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Only migrate this many unique documents (smoke test).")
    args = parser.parse_args()

    if not REPORT_TRUE_CSV.exists():
        print(f"Missing {REPORT_TRUE_CSV}", file=sys.stderr)
        sys.exit(1)
    if not EMBEDDINGS_JSON.exists():
        print(f"Missing {EMBEDDINGS_JSON} -- run scripts/hash_and_embed_report_true.py first", file=sys.stderr)
        sys.exit(1)

    embeddings: dict[str, list[float]] = json.loads(EMBEDDINGS_JSON.read_text(encoding="utf-8"))

    with REPORT_TRUE_CSV.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    rows_by_sha: dict[str, list[dict]] = {}
    for row in rows:
        sha = (row.get("sha") or "").strip()
        if sha:
            rows_by_sha.setdefault(sha, []).append(row)

    shas_with_embedding = [sha for sha in rows_by_sha if sha in embeddings]
    if args.limit is not None:
        shas_with_embedding = shas_with_embedding[: args.limit]

    print(
        f"[migrate] {len(rows_by_sha)} unique sha(s) in CSV, {len(embeddings)} embedded, "
        f"{len(shas_with_embedding)} to migrate this run"
    )

    from dashboard.db import get_session, init_db
    from dashboard.display_id import allocate_display_id
    from dashboard.models import Crop, DocumentAssociation, State, UniqueDocument

    init_db()

    all_state_names = sorted({r["state"] for r in rows if r.get("state")})
    all_crop_names = sorted({r["folder"] for r in rows if r.get("folder")})

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

    migrated = skipped = assoc_created = 0
    for i, sha in enumerate(shas_with_embedding, start=1):
        if sha in already_migrated:
            skipped += 1
            continue

        group = rows_by_sha[sha]
        rep = group[0]  # first-encountered row is representative, same convention as the hash/embed script

        with get_session() as session:
            doc = UniqueDocument(
                sha256=sha,
                display_id=allocate_display_id(session),
                embedding=embeddings[sha],
                shareable_name=rep.get("pdf"),
                shareable_link=rep.get("doc_link"),
                language=rep.get("language"),
                format_original=rep.get("format") or "pdf",
                num_pages=_int_or_none(rep.get("num_pages")),
                date_of_collection=rep.get("Date of Collection") or None,
                month_of_collection=_int_or_none(rep.get("Month of Collection")),
                year_of_collection=_int_or_none(rep.get("Year of Collection")),
            )
            session.add(doc)
            session.flush()

            seen_pairs: set[tuple[int, int]] = set()
            for row in group:
                state_id = state_id_by_name.get(row.get("state", ""))
                crop_id = crop_id_by_name.get(row.get("folder", ""))
                if state_id is None or crop_id is None:
                    continue
                if (state_id, crop_id) in seen_pairs:
                    continue
                seen_pairs.add((state_id, crop_id))
                session.add(DocumentAssociation(unique_document_id=doc.id, state_id=state_id, crop_id=crop_id))
                assoc_created += 1

        migrated += 1
        if i % 200 == 0:
            print(f"[migrate] [{i}/{len(shas_with_embedding)}] ...")

    print(f"[migrate] done: {migrated} unique document(s) migrated, {assoc_created} placement(s) created, "
          f"{skipped} already migrated (skipped)")


if __name__ == "__main__":
    main()
