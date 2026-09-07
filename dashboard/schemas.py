"""Pydantic request/response models for the dashboard API.

TWO shapes, matching the two collections:

  DocumentOut        a row of the MAIN TABLE. Its own fields are just the
                     placement (state, crop); everything else is joined in from
                     the unique document it points at, so the frontend renders a
                     row without a second request. Stored as an association,
                     served as a complete row.
  UniqueDocumentOut  the document itself: the ANNAM id, all 18 manual metadata
                     fields, translation/review state, and the list of physical
                     copies behind it.

`id` is a MongoDB ObjectId serialised as a 24-character hex string. It is opaque
to the frontend, which only echoes it back in URLs. The readable ids are
`row_id` (POP_#####) on a placement and `document_id` (ANNAM_#####) on a
document -- see dashboard/display_id.py for why there are two.

`chunk_embeddings` is deliberately absent from every model here. It is always
empty today, and once populated it is thousands of floats per document; a page
of 100 rows must not carry them.
"""
from __future__ import annotations

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


# -- Lookups (controlled vocabularies for the dashboard's dropdowns) ----------
# Not foreign keys: rows store the name/code as a plain string. These exist so
# the agri team picks from a list instead of typing a new spelling of a state
# that already exists.


class LanguageOut(BaseModel):
    code: str  # "kan", or "non_english" for the OCR pass's Non-English verdict
    label: str  # "Kannada" / "Non-English"


class StateOut(BaseModel):
    name: str
    # Every source spelling that normalised to this name ("State Karnataka").
    # Kept so the lookup stays reversible -- the OCR language for a document is
    # keyed off the RAW state name, not this one.
    raw_names: list[str] = []
    document_count: int = 0


class CropOut(BaseModel):
    name: str
    raw_names: list[str] = []
    document_count: int = 0


# -- The document (unique_documents) ------------------------------------------


class DocumentMetadata(BaseModel):
    """The 18 manually-entered fields. All optional so a PATCH can send just
    the ones being changed."""

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


class CopyLink(BaseModel):
    """One physical copy in WorkDrive. There is one per placement, because
    WorkDrive keeps a separate file in every folder rather than linking one file
    into many -- so a document grouped by sha256 owns several of these."""

    zoho_file_id: str | None = None
    shareable_link: str | None = None
    shareable_name: str | None = None
    state: str | None = None
    crop: str | None = None
    row_id: int | None = None


class UniqueDocumentOut(DocumentMetadata):
    model_config = ConfigDict(from_attributes=True)
    id: str  # ObjectId, 24-char hex
    document_id: str  # ANNAM_##### -- names the DOCUMENT

    sha256: str | None = None
    # The anchor copy's name and link -- the document's own. Every copy's link
    # is in duplicate_links.
    shareable_name: str | None = None
    shareable_link: str | None = None
    num_pages: int | None = None
    format_original: str | None = None

    # A code from GET /dashboard/languages.
    language: str | None = None
    # Where `language` came from -- a closed vocabulary of three:
    #   "detected"  known -- the OCR pass read it off the file
    #   "manual"    known -- a person chose it on upload or via PATCH
    #   "state"     inferred from the state -- plausible, not measured
    # 3,565 of 8,749 are "state", and that is the distinction the field exists
    # for: without it a guess is indistinguishable from a measurement.
    language_source: str | None = None
    translation_status: TranslationStatus
    # The translated/reviewed copies, when they exist. `*_file_id` is a Zoho
    # file id for GET /dashboard/files/{id}/download -- the same proxy the
    # original uses via representative_file_id, so all three icons work the
    # same way. `*_shareable_link` is the raw WorkDrive URL, which opens Zoho's
    # own viewer and requires a Zoho login; prefer the proxy.
    translation_file_id: str | None = None
    translation_shareable_link: str | None = None
    review_status: ReviewStatus
    review_file_id: str | None = None
    review_shareable_link: str | None = None

    placement_count: int = 0  # how many main-table rows point here
    # THE ANCHOR: which entry of duplicate_links is this document, as opposed to
    # another copy of it. Translation acts on this file and no other. Stable
    # across merges; a person can move it with PATCH if the file is a bad scan.
    representative_file_id: str | None = None
    representative_row_id: int | None = None
    duplicate_links: list[CopyLink] = []
    merged_from: list[str] = []  # ANNAM ids absorbed by a team-approved merge
    created_at: datetime
    updated_at: datetime


class UniqueDocumentUpdate(DocumentMetadata):
    """PATCH body for a document. Everything here is shared by every placement
    of it -- that is the point of the split."""

    shareable_name: str | None = None
    language: str | None = None
    num_pages: int | None = None
    format_original: str | None = None
    # Re-anchor the document onto a different one of its own copies -- e.g. the
    # chosen file turns out to be a bad scan. Validated to be a file this
    # document actually owns.
    representative_file_id: str | None = None


# -- The main table (documents) ------------------------------------------------


class DocumentOut(BaseModel):
    """One row of the main table, with its document joined in.

    The first block is stored on the row; everything after it is read from the
    unique document and is identical across all of that document's placements.
    """

    model_config = ConfigDict(from_attributes=True)
    id: str  # ObjectId of the PLACEMENT, 24-char hex
    row_id: str  # POP_##### -- names the placement
    state: str
    crop: str
    # Anything nested deeper than <state>/<crop>/<file> in WorkDrive. Normally
    # empty; present so an unexpected folder level is visible rather than
    # silently reassigning the file to another crop.
    subpath: str | None = None

    # -- joined from unique_documents --
    unique_document_id: str
    document_id: str  # ANNAM_#####
    shareable_name: str | None = None
    shareable_link: str | None = None
    sha256: str | None = None
    num_pages: int | None = None
    format_original: str | None = None
    language: str | None = None
    language_source: str | None = None
    # The three downloadable files, all keyed for
    # GET /dashboard/files/{id}/download. representative_file_id is the anchor:
    # the copy that IS this document, the one translation acts on.
    representative_file_id: str | None = None
    translation_status: TranslationStatus | None = None
    translation_file_id: str | None = None
    translation_shareable_link: str | None = None
    review_status: ReviewStatus | None = None
    review_file_id: str | None = None
    review_shareable_link: str | None = None
    # How many placements share this row's document, this one included. 1 means
    # the document appears in exactly one folder.
    placement_count: int = 1

    created_at: datetime
    updated_at: datetime


class DocumentUpdate(DocumentMetadata):
    """PATCH body for a main-table row.

    `state`/`crop` change the placement. Every other field belongs to the
    document and is written there, so it changes for all of its placements --
    the endpoint routes them rather than making the caller know which is which.
    """

    state: str | None = None
    crop: str | None = None
    shareable_name: str | None = None
    language: str | None = None
    num_pages: int | None = None


# -- Duplicate review ----------------------------------------------------------


class DuplicateCandidate(BaseModel):
    """A document the algorithm proposes as the same as another. Always empty
    for now -- see dashboard/routes_merge.py."""

    id: str
    document_id: str
    shareable_name: str | None = None
    score: float | None = None
    reason: str | None = None


class DuplicateCandidatesOut(BaseModel):
    document_id: str  # the ANNAM id the search started from
    candidates: list[DuplicateCandidate] = []
    # Why the list is empty, in words the UI can show. The algorithm is not
    # wired up yet and this endpoint must say so rather than implying "no
    # duplicates exist".
    note: str | None = None


class MergeRequest(BaseModel):
    """Absorb these documents into the one being POSTed to.

    ObjectId hex or ANNAM ids. The absorbed documents are deleted; their
    placements are repointed and NEVER deleted, because each one is a real file
    in a real folder.
    """

    absorb: list[str]


class MergeResult(BaseModel):
    document_id: str  # the surviving ANNAM id
    absorbed: list[str]  # ANNAM ids that were merged in
    placements_repointed: int
    placement_count: int  # of the survivor, after the merge


# -- Upload queue --------------------------------------------------------------


class Placement(BaseModel):
    """A (state, crop) pair, normalised."""

    state: str
    crop: str


class UploadCandidate(BaseModel):
    """An existing document this upload might be. The check returns up to three,
    best score first."""

    document_id: str  # ObjectId hex
    document_code: str | None = None  # ANNAM_#####
    shareable_name: str | None = None
    score: float | None = None  # 1.0 for an exact sha match; a real score later
    match_type: str | None = None  # "sha" today; "embedding" once the algorithm lands
    placement_count: int = 0  # where this document is already filed

    # The pairs THIS upload asks for that the candidate does not already have.
    # This is what makes the decision meaningful rather than a yes/no.
    new_placements: list[Placement] = []
    # Whether each action is offered. The backend enforces the same rules, so
    # these are for rendering, not for trusting.
    can_add: bool = False  # false when there is nothing new to file
    # False for an exact sha256 match: sha256 is unique on unique_documents, so
    # byte-identical content cannot become a second document.
    can_create_new: bool = True


class UploadQueueItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str  # ObjectId, 24-char hex
    filename: str
    status: UploadQueueStatus
    progress_pct: int
    num_pages: int | None = None
    sha256: str | None = None
    error_message: str | None = None
    note: str | None = None  # the decision explained in words, for the UI

    # What the form asked for, held here until a person decides -- the document
    # does not exist yet, so it has nowhere else to live.
    placements: list[Placement] = []
    metadata: dict = {}

    # Up to three matches, best first. Empty means nothing matched -- NOT that
    # nothing is similar; the check is exact-sha only for now.
    candidates: list[UploadCandidate] = []

    # What the decision produced.
    created_document_id: str | None = None  # ANNAM id used or created
    created_row_ids: list[str] = []  # POP ids of the placements created
    created_at: datetime
    updated_at: datetime


# -- Translation queue ---------------------------------------------------------


class TranslationJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str  # ObjectId, 24-char hex
    document_id: str  # the unique document's ObjectId hex
    document_code: str | None = None  # its ANNAM_##### id
    shareable_name: str | None = None
    kind: TranslationJobKind
    status: TranslationJobStatus
    progress_pct: int
    pages_done: int | None = None
    total_pages: int | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ConfigOut(BaseModel):
    translation_available: bool
