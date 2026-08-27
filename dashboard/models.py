"""ORM models for the document management dashboard.

Two core tables (unique_documents, document_associations) plus two lookup
tables (states, crops) and two queue/status tables (upload_queue_items,
translation_jobs). See docs/dashboard_backend_plan.md for the full schema
rationale -- most notably which metadata columns live on which table. This
was confirmed with the user rather than guessed: Season and Date/Month/Year
of Collection are DOCUMENT-level (unique_documents), not per state/crop
placement, even though today's report_true.csv has one collection date per
state row (see migrate_from_report_true.py for how that's collapsed).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from dashboard.db import Base

EMBEDDING_DIM = 768  # intfloat/multilingual-e5-base, matches scripts/hash_and_embed_report_true.py


class TranslationStatus(str, enum.Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    done = "done"


class ReviewStatus(str, enum.Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    done = "done"


class UploadQueueStatus(str, enum.Enum):
    queued = "queued"
    hashing = "hashing"
    embedding = "embedding"
    checking_duplicate = "checking_duplicate"
    # Renamed from duplicate_found (2026-08-26): reached by EVERY upload now,
    # not just confident duplicates -- see dashboard/queue_worker.py. Nothing
    # is ever auto-created or auto-removed from the queue; add/new/cancel are
    # the only way an item leaves this status.
    awaiting_review = "awaiting_review"
    uploading = "uploading"
    done = "done"
    failed = "failed"


class TranslationJobKind(str, enum.Enum):
    translate = "translate"
    review_upload = "review_upload"


class TranslationJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


def _uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class State(Base):
    __tablename__ = "states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class Crop(Base):
    __tablename__ = "crops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class UniqueDocument(Base):
    __tablename__ = "unique_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # Human-readable sequential id (ANNAM_00000..ANNAM_50000) shown to users
    # instead of `id` above -- see dashboard/display_id.py for allocation.
    display_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)

    # -- manual metadata --
    advisory_type: Mapped[str | None] = mapped_column(Text)
    advisory_scope: Mapped[str | None] = mapped_column(Text)
    season: Mapped[str | None] = mapped_column(Text)
    edition_revision_volume: Mapped[str | None] = mapped_column(Text)
    date_of_release: Mapped[str | None] = mapped_column(Text)
    month_of_release: Mapped[int | None] = mapped_column(Integer)
    year_of_release: Mapped[int | None] = mapped_column(Integer)
    date_of_collection: Mapped[str | None] = mapped_column(Text)
    month_of_collection: Mapped[int | None] = mapped_column(Integer)
    year_of_collection: Mapped[int | None] = mapped_column(Integer)
    advisory_name: Mapped[str | None] = mapped_column(Text)
    advisory_released_org: Mapped[str | None] = mapped_column(Text)
    advisory_org_address: Mapped[str | None] = mapped_column(Text)
    live_source_link: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[str | None] = mapped_column(Text)
    document_status: Mapped[str | None] = mapped_column(Text)

    # -- programmatic metadata --
    shareable_name: Mapped[str | None] = mapped_column(Text)
    shareable_link: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    format_original: Mapped[str | None] = mapped_column(Text, default="pdf")
    num_pages: Mapped[int | None] = mapped_column(Integer)

    # -- storage + dedup --
    zoho_file_id: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    # -- translation / review --
    translation_zoho_file_id: Mapped[str | None] = mapped_column(Text)
    translation_shareable_link: Mapped[str | None] = mapped_column(Text)
    translation_status: Mapped[TranslationStatus] = mapped_column(
        Enum(TranslationStatus, name="translation_status"),
        default=TranslationStatus.not_started,
        nullable=False,
    )
    review_zoho_file_id: Mapped[str | None] = mapped_column(Text)
    review_shareable_link: Mapped[str | None] = mapped_column(Text)
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status"),
        default=ReviewStatus.not_started,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)

    associations: Mapped[list["DocumentAssociation"]] = relationship(
        back_populates="unique_document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentAssociation(Base):
    """One row per (unique document x state x crop) placement -- the "main
    table" with the state/folder columns. Equivalent to one row of today's
    report_true.csv, minus all the unique-doc-only metadata."""

    __tablename__ = "document_associations"
    __table_args__ = (UniqueConstraint("unique_document_id", "state_id", "crop_id", name="uq_doc_state_crop"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    unique_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("unique_documents.id", ondelete="CASCADE"), nullable=False
    )
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"), nullable=False)
    crop_id: Mapped[int] = mapped_column(ForeignKey("crops.id"), nullable=False)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)

    unique_document: Mapped["UniqueDocument"] = relationship(back_populates="associations")
    state: Mapped["State"] = relationship()
    crop: Mapped["Crop"] = relationship()


class UploadQueueItem(Base):
    """Status tracking for a queued upload -- drives the "checking status"
    box in the Add Document mode of the frontend. metadata_payload holds the
    full upload form (manual metadata fields + selected state/crop names)
    until the item resolves to awaiting_review/failed. Nothing is ever
    auto-created or auto-removed -- every successful hash/embed run lands on
    awaiting_review and waits for an explicit add/new/cancel, even when no
    candidate match exists at all (see dashboard/queue_worker.py)."""

    __tablename__ = "upload_queue_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[UploadQueueStatus] = mapped_column(
        Enum(UploadQueueStatus, name="upload_queue_status"), default=UploadQueueStatus.queued, nullable=False
    )
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("unique_documents.id", ondelete="SET NULL")
    )
    similarity_score: Mapped[float | None] = mapped_column(Float)
    # "sha" (byte-for-byte identical, similarity_score always 1.0), "embedding"
    # (closest existing document by embedding cosine similarity, whatever
    # that similarity happens to be -- shown even below the duplicate
    # threshold, purely for the human to judge), or None (unique_documents
    # was empty -- no candidate exists to compare against at all).
    match_type: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    # Informational (non-error) message -- e.g. what a pending duplicate
    # would link once approved. Distinct from error_message (implies
    # failure).
    note: Mapped[str | None] = mapped_column(Text)
    # Filled in once known (after hashing/OCR), same as the eventual
    # unique_documents columns -- lets the queue row show them before the
    # document exists for real.
    num_pages: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(Text)
    metadata_payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)


class TranslationJob(Base):
    """DB-backed version of pop_server.py's existing in-memory `_jobs` dict
    pattern -- same shape, but survives a server restart."""

    __tablename__ = "translation_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    unique_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("unique_documents.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[TranslationJobKind] = mapped_column(Enum(TranslationJobKind, name="translation_job_kind"), nullable=False)
    status: Mapped[TranslationJobStatus] = mapped_column(
        Enum(TranslationJobStatus, name="translation_job_status"), default=TranslationJobStatus.queued, nullable=False
    )
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Page-level progress -- set once the source PDF is split (see
    # dashboard/routes_translation.py's on_progress callback into
    # pop_server._run_one_doc). Both null while still queued/splitting.
    pages_done: Mapped[int | None] = mapped_column(Integer)
    total_pages: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)

    unique_document: Mapped["UniqueDocument"] = relationship()
