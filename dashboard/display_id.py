"""Human-readable sequential document IDs (ANNAM_00000 .. ANNAM_50000) shown
in place of unique_documents' raw UUID primary key.

Allocation always picks the SMALLEST number not currently used by any row --
when a document is deleted its number is immediately free again for the next
upload, because "in use" is derived live from unique_documents.display_id
itself (via generate_series/NOT EXISTS) rather than tracked in a separate
free-list table that could drift out of sync with reality. Concurrent
allocations are serialized with a Postgres advisory lock scoped to the
calling transaction (released automatically at commit/rollback), so two
uploads racing to allocate can't both grab the same number.

50000 is a hard ceiling per the user: this pipeline will never hold more
than 50k unique documents.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

DISPLAY_ID_PREFIX = "ANNAM"
DISPLAY_ID_MAX = 50000
_ADVISORY_LOCK_KEY = 891773  # arbitrary fixed key shared by every allocation call


def allocate_display_id(session: Session) -> int:
    """Must be called on the same session/transaction that will INSERT the
    row using the returned id -- the advisory lock is held until that
    transaction commits, so no other allocator can observe the id as free in
    the meantime."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})
    next_id = session.execute(
        text(
            "SELECT MIN(n) FROM generate_series(0, :max_id) AS n "
            "WHERE NOT EXISTS (SELECT 1 FROM unique_documents WHERE display_id = n)"
        ),
        {"max_id": DISPLAY_ID_MAX},
    ).scalar()
    if next_id is None:
        raise RuntimeError(f"display_id pool exhausted (0..{DISPLAY_ID_MAX})")
    return next_id


def format_display_id(display_id: int | None) -> str | None:
    if display_id is None:
        return None
    return f"{DISPLAY_ID_PREFIX}_{display_id:05d}"
