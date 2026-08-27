"""Pydantic request/response models for the dashboard API."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from dashboard.models import ReviewStatus, TranslationJobKind, TranslationJobStatus, TranslationStatus, UploadQueueStatus

T = TypeVar("T")


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class StateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class CropOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str


class LanguageOut(BaseModel):
    code: str  # tessdata_best code, e.g. "kan" -- what the upload form's `language` field expects
    label: str  # display name, e.g. "Kannada"


class CropCreate(BaseModel):
    name: str


# -- Unique document ---------------------------------------------------------


class UniqueDocumentMetadata(BaseModel):
    """Every manually-entered field on unique_documents. All optional so a
    PATCH can send just the fields being changed."""

    advisory_type: str | None = None
    advisory_scope: str | None = None
    season: str | None = None
    edition_revision_volume: str | None = None
    date_of_release: str | None = None
    month_of_release: int | None = None
    year_of_release: int | None = None
    date_of_collection: str | None = None
    month_of_collection: int | None = None
    year_of_collection: int | None = None
    advisory_name: str | None = None
    advisory_released_org: str | None = None
    advisory_org_address: str | None = None
    live_source_link: str | None = None
    domain: str | None = None
    verification_status: str | None = None
    verified_by: str | None = None
    document_status: str | None = None


class UniqueDocumentOut(UniqueDocumentMetadata):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    document_id: str  # human-readable ANNAM_##### -- see dashboard/display_id.py
    sha256: str
    shareable_name: str | None
    shareable_link: str | None
    states: list[str] = []  # every state this document is placed under, deduped (via document_associations)
    crops: list[str] = []  # every crop this document is placed under, deduped (via document_associations)
    language: str | None
    format_original: str | None
    num_pages: int | None
    # zoho_file_id (the internal Zoho WorkDrive id) is deliberately not
    # exposed -- shareable_link is the public-facing equivalent.
    translation_status: TranslationStatus
    translation_shareable_link: str | None
    review_status: ReviewStatus
    review_shareable_link: str | None
    created_at: datetime
    updated_at: datetime


class UniqueDocumentUpdate(UniqueDocumentMetadata):
    pass


# -- Document association (main table row) -----------------------------------


class DocumentAssociationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    unique_document_id: uuid.UUID
    document_id: str  # the underlying document's human-readable ANNAM_##### id
    state: StateOut
    crop: CropOut
    shareable_name: str | None = None
    language: str | None = None
    translation_status: TranslationStatus | None = None
    review_status: ReviewStatus | None = None
    created_at: datetime
    updated_at: datetime


class DocumentAssociationUpdate(BaseModel):
    state_id: int | None = None
    crop_id: int | None = None


# -- Upload queue --------------------------------------------------------------


class UploadQueueItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    filename: str
    status: UploadQueueStatus
    progress_pct: int
    num_pages: int | None
    language: str | None
    duplicate_of_id: uuid.UUID | None
    similarity_score: float | None
    match_type: str | None  # "sha" | "embedding" | None -- see dashboard/models.py
    error_message: str | None
    note: str | None
    # The full form the item was submitted with (manual metadata fields +
    # state_names/crop_names) -- lets the queue row show everything the
    # eventual unique_documents row would have, before it exists for real.
    metadata_payload: dict
    created_at: datetime
    updated_at: datetime


# -- Translation queue ---------------------------------------------------------


class TranslationJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    unique_document_id: uuid.UUID
    document_id: str  # the document's human-readable ANNAM_##### id
    shareable_name: str | None
    kind: TranslationJobKind
    status: TranslationJobStatus
    progress_pct: int
    pages_done: int | None
    total_pages: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ConfigOut(BaseModel):
    translation_available: bool
