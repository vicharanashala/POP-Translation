"""One-off, idempotent migration: adds the columns needed for the
translation queue (page-level progress) and the duplicate-approval upload
queue -- again things Base.metadata.create_all() can't add to tables that
already exist.

- translation_jobs.pages_done / .total_pages (page-level progress, see
  dashboard/routes_translation.py's on_progress callback)
- translation_job_status gets a new 'cancelled' enum value (real
  cancellation via _job_ctl, see routes_translation.py)
- upload_queue_items.num_pages / .language (known once hashing/OCR
  finishes, shown on a still-pending queue row)

Safe to run more than once -- every step checks information_schema /
pg_enum first.

Usage:
    .venv/bin/python3 -m dashboard.migrate_add_queue_tracking
"""
from __future__ import annotations

from sqlalchemy import text

from dashboard.db import get_session


def _column_exists(session, table: str, column: str) -> bool:
    row = session.execute(
        text("SELECT 1 FROM information_schema.columns WHERE table_name = :table AND column_name = :column"),
        {"table": table, "column": column},
    ).first()
    return row is not None


def _enum_value_exists(session, enum_name: str, value: str) -> bool:
    row = session.execute(
        text(
            "SELECT 1 FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
            "WHERE t.typname = :enum_name AND e.enumlabel = :value"
        ),
        {"enum_name": enum_name, "value": value},
    ).first()
    return row is not None


def main() -> None:
    with get_session() as session:
        if not _column_exists(session, "translation_jobs", "pages_done"):
            print("[migrate] adding translation_jobs.pages_done / .total_pages ...")
            session.execute(text("ALTER TABLE translation_jobs ADD COLUMN pages_done INTEGER"))
            session.execute(text("ALTER TABLE translation_jobs ADD COLUMN total_pages INTEGER"))
        else:
            print("[migrate] translation_jobs.pages_done already exists, skipping")

        if not _column_exists(session, "upload_queue_items", "num_pages"):
            print("[migrate] adding upload_queue_items.num_pages / .language ...")
            session.execute(text("ALTER TABLE upload_queue_items ADD COLUMN num_pages INTEGER"))
            session.execute(text("ALTER TABLE upload_queue_items ADD COLUMN language TEXT"))
        else:
            print("[migrate] upload_queue_items.num_pages already exists, skipping")

    # ALTER TYPE ... ADD VALUE can't be used in the same transaction that
    # added it (even though it can run inside one) -- separate session/
    # transaction, committed on its own, before anything tries to use it.
    with get_session() as session:
        if not _enum_value_exists(session, "translation_job_status", "cancelled"):
            print("[migrate] adding 'cancelled' to translation_job_status enum ...")
            session.execute(text("ALTER TYPE translation_job_status ADD VALUE 'cancelled'"))
        else:
            print("[migrate] translation_job_status already has 'cancelled', skipping")


if __name__ == "__main__":
    main()
