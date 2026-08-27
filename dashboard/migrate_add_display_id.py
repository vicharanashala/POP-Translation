"""One-off, idempotent migration: adds unique_documents.display_id (human
sequential id, see dashboard/display_id.py) and upload_queue_items.note
(informational text for the upload queue, e.g. "already exists for state X
crop Y" duplicate messages) to the live schema -- Base.metadata.create_all()
(dashboard/db.py's init_db()) only creates missing TABLES, it never alters
columns on a table that already exists, so these need a real ALTER TABLE.

Safe to run more than once -- every step checks information_schema first.

Usage:
    .venv/bin/python3 -m dashboard.migrate_add_display_id
"""
from __future__ import annotations

from sqlalchemy import text

from dashboard.db import get_session
from dashboard.display_id import DISPLAY_ID_MAX


def _column_exists(session, table: str, column: str) -> bool:
    row = session.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name = :table AND column_name = :column"),
        {"table": table, "column": column},
    ).first()
    return row is not None


def main() -> None:
    with get_session() as session:
        if not _column_exists(session, "unique_documents", "display_id"):
            print("[migrate] adding unique_documents.display_id ...")
            session.execute(text("ALTER TABLE unique_documents ADD COLUMN display_id INTEGER"))
            # One-time bulk backfill in original insertion order -- ongoing
            # allocation after this point goes through
            # dashboard.display_id.allocate_display_id() instead, which
            # reuses numbers freed by deletions.
            session.execute(
                text(
                    "UPDATE unique_documents SET display_id = sub.rn - 1 "
                    "FROM (SELECT id, ROW_NUMBER() OVER (ORDER BY created_at, id) AS rn "
                    "      FROM unique_documents) AS sub "
                    "WHERE unique_documents.id = sub.id"
                )
            )
            max_assigned = session.execute(text("SELECT MAX(display_id) FROM unique_documents")).scalar()
            if max_assigned is not None and max_assigned > DISPLAY_ID_MAX:
                raise RuntimeError(
                    f"backfill assigned display_id up to {max_assigned}, over the "
                    f"{DISPLAY_ID_MAX} pool ceiling -- raise DISPLAY_ID_MAX before proceeding"
                )
            session.execute(text("ALTER TABLE unique_documents ALTER COLUMN display_id SET NOT NULL"))
            session.execute(
                text("ALTER TABLE unique_documents ADD CONSTRAINT uq_unique_documents_display_id UNIQUE (display_id)")
            )
            print(f"[migrate] backfilled display_id for {(max_assigned or 0) + 1} document(s)")
        else:
            print("[migrate] unique_documents.display_id already exists, skipping")

        if not _column_exists(session, "upload_queue_items", "note"):
            print("[migrate] adding upload_queue_items.note ...")
            session.execute(text("ALTER TABLE upload_queue_items ADD COLUMN note TEXT"))
        else:
            print("[migrate] upload_queue_items.note already exists, skipping")


if __name__ == "__main__":
    main()
