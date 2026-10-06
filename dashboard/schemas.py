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

from datetime import datetime, timezone
from typing import Annotated, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict

from dashboard.models import ReviewStatus, TranslationJobKind, TranslationJobStatus, TranslationStatus, UploadQueueStatus

T = TypeVar("T")


def _as_utc(value: datetime) -> datetime:
    """Mongo hands dates back naive, but they are UTC. Marked as such, they are
    sent with a "Z", so the browser shows them in local time (IST) instead of
    reading a bare "10:00:00" as 10:00 IST -- which put every time 5h30 behind."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


UTCDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


# -- Lookups (controlled vocabularies for the dashboard's dropdowns) ----------
# States, crops, organisations, districts and KVKs are REFERENCED by id from a
# placement -- the name lives only in the lookup (dashboard/vocabulary.py).
# Crops come from the crop master and are read-only here; the rest are ours.
# District and KVK are optional, so a placement may reference neither.
# Languages are still a plain code.


class LanguageOut(BaseModel):
    code: str  # "kan", or "non_english" for the OCR pass's Non-English verdict
    label: str  # "Kannada" / "Non-English"
    # Whether tessdata_best has an OCR model for it. None for Non-English,
    # which is a verdict rather than a language.
    tessdata_best: bool | None = None


class StateOut(BaseModel):
    """A state, synced from LGD. Read-only, and addressed by `id` -- which is
    the only thing any request ever sends about a state.

    NO `raw_names`. Every vocabulary entry used to carry its older spellings,
    because a form could type one and the backend would resolve it. Nothing
    resolves a name now, so serving them only invited a caller to match on one;
    they are still stored as provenance for the folder names the crawl found.
    """

    id: str  # ObjectId hex -- what a placement's state_id holds
    name: str  # for DISPLAY: the dropdown label and the table cell
    # Placements using it, computed on read.
    document_count: int = 0
    # The LGD state code. This is what languages.STATE_LANG is keyed by, so a
    # state without one has no OCR language.
    code: int | None = None


class CropOut(BaseModel):
    """A crop master entry. Read-only: maintained by another application."""

    id: str  # the master's ObjectId -- what a placement's crop_id holds
    name: str
    document_count: int = 0


class OrganizationOut(BaseModel):
    """A folder that is not a crop: an organisation, department or grouping
    ("ICAR - ...", "General").

    A WorkDrive FOLDER name. Read-only like every other vocabulary -- created
    only by the corpus loader, which is where folder names come from.
    """

    id: str
    name: str
    document_count: int = 0


class DistrictOut(BaseModel):
    """A district, synced from LGD. Read-only: there is no write route, because
    the next sync would undo it."""

    id: str
    name: str
    document_count: int = 0
    # The LGD district code -- null on the "All" entry, which is ours.
    code: int | None = None
    # The state it belongs to. This is what narrows the form's dropdown.
    state_id: str | None = None


class KvkOut(BaseModel):
    """A Krishi Vigyan Kendra, synced from LGD. Its own vocabulary, NOT an
    organisation: a `pop_organizations` entry is a WorkDrive FOLDER name (one of
    which happens to be called "KVK Files"), which is a different question from
    which KVK a document came from. Read-only, like districts."""

    id: str
    name: str
    document_count: int = 0
    # LGD's own id ("K0001"), a string unlike the numeric state/district codes.
    code: str | None = None
    address: str | None = None
    # The district narrows the form's dropdown; the state is carried too because
    # LGD gives it, and it saves a hop.
    district_id: str | None = None
    state_id: str | None = None


class LocationKvk(BaseModel):
    """One KVK in the nested location tree. Leaner than KvkOut: the district and
    state it belongs to are the nodes it hangs under, so repeating them on 1,513
    entries only makes the payload bigger."""

    id: str
    name: str
    code: str | None = None
    address: str | None = None


class LocationDistrict(BaseModel):
    """One district of a state in the nested location tree, with its KVKs."""

    id: str
    name: str
    code: int | None = None
    kvks: list[LocationKvk] = []


class LocationState(BaseModel):
    """One state in the nested location tree, with its districts."""

    id: str
    name: str
    code: int | None = None
    districts: list[LocationDistrict] = []


class FolderOut(BaseModel):
    """One option of the Folder dropdown: a crop or an organisation."""

    id: str
    name: str
    kind: str  # "crop" | "organization" -- send the id as crop_id / organization_id accordingly
    document_count: int = 0


class VocabularyRename(BaseModel):
    """Set the name exactly as given -- it is not re-cased, so the team's
    standard spelling ("Beet root") is kept."""

    name: str


class VocabularyMerge(BaseModel):
    """Fold these entries (ObjectId hex) into the one being POSTed to."""

    absorb: list[str]


class PlacementCreate(BaseModel):
    """POST body for filing an existing document under one more state + folder.

    Ids only, and exactly one folder: `crop_id` (a crop master entry) or
    `organization_id`. No names, because every vocabulary is read-only and a
    name would only be looked up anyway; no district or kvk, because those are
    the DOCUMENT's fields -- a new placement inherits whatever it already says.
    """

    state_id: str
    crop_id: str | None = None
    organization_id: str | None = None


class VocabularyMergeResult(BaseModel):
    id: str
    name: str  # the survivor's name
    absorbed: list[str]  # names of the entries merged in, now deleted
    placements_repointed: int


# -- The document (unique_documents) ------------------------------------------


class DocumentMetadata(BaseModel):
    """The 17 manually-entered fields. All optional so a PATCH can send just
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
    document_status: str | None = None


class CopyLink(BaseModel):
    """One physical copy in WorkDrive. There is one per placement, because
    WorkDrive keeps a separate file in every folder rather than linking one file
    into many -- so a document grouped by sha256 owns several of these."""

    zoho_file_id: str | None = None
    shareable_link: str | None = None
    shareable_name: str | None = None
    # Where the placement named by row_id files this copy, read from that
    # placement -- none of it is stored on the copy. The documents listing has
    # no state or folder of its own, so it reads the entry whose `zoho_file_id`
    # is the document's `representative_file_id`: the copy the document IS.
    #
    # No district or kvk. Those are the DOCUMENT's fields, so they are the same
    # for every copy and the document already carries them.
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
    # Audit trail. *_by is the display name the frontend sent with the action
    # (not verified -- /api/pop has no auth yet); *_at is when the translation
    # or review landed. Both cleared when the file is deleted; null on
    # documents translated before these fields existed.
    translated_by: str | None = None
    translated_at: UTCDatetime | None = None
    reviewed_by: str | None = None
    reviewed_at: UTCDatetime | None = None
    # Who uploaded it (sent by the frontend with the upload). Read-only: it is
    # not on UniqueDocumentUpdate, so a PATCH cannot change it.
    uploaded_by: str | None = None

    placement_count: int = 0  # how many main-table rows point here
    # THE ANCHOR: which entry of duplicate_links is this document, as opposed to
    # another copy of it. Translation acts on this file and no other. Stable
    # across merges; a person can move it with PATCH if the file is a bad scan.
    # ONE anchor field, not two. `representative_row_id` named the anchor
    # PLACEMENT as well; it was derivable from the copy entry carrying this file
    # id, so it was a stored copy of a fact and drifted.
    representative_file_id: str | None = None
    # Where the document applies: one district and one KVK per document, shown
    # on every placement of it. Stored on a placement until the anchor went.
    district: str | None = None
    kvk: str | None = None
    district_id: str | None = None
    kvk_id: str | None = None
    duplicate_links: list[CopyLink] = []
    merged_from: list[str] = []  # ANNAM ids absorbed by a team-approved merge
    created_at: UTCDatetime
    updated_at: UTCDatetime


class UniqueDocumentUpdate(DocumentMetadata):
    """PATCH body for a document. Everything here is shared by every placement
    of it -- that is the point of the split, and district and kvk are no longer
    an exception to it."""

    shareable_name: str | None = None
    language: str | None = None
    num_pages: int | None = None
    format_original: str | None = None
    # Re-anchor the document onto a different one of its own copies -- e.g. the
    # chosen file turns out to be a bad scan. Validated to be a file this
    # document actually owns.
    representative_file_id: str | None = None
    # Where the document applies. The document's own fields: a Package of
    # Practices is written for a place, and filing it in a second folder does
    # not put it in a second district. By id; "" clears.
    district_id: str | None = None
    kvk_id: str | None = None


# -- The main table (documents) ------------------------------------------------


class DocumentOut(BaseModel):
    """One row of the main table, with its document joined in.

    The first block is stored on the row; everything after it is read from the
    unique document and is identical across all of that document's placements.
    """

    model_config = ConfigDict(from_attributes=True)
    id: str  # ObjectId of the PLACEMENT, 24-char hex
    row_id: str  # POP_##### -- names the placement
    state: str  # the name, resolved from state_id
    # The folder under the state: a crop's name, or an organisation's. Which
    # one is `crop_kind`, and exactly one of crop_id / organization_id is set.
    crop: str
    crop_kind: str | None = None  # "crop" | "organization"
    state_id: str | None = None
    crop_id: str | None = None
    organization_id: str | None = None
    # Where the document applies and which KVK it came from. On the DOCUMENT,
    # unlike state and crop, so every placement of one document shows the same
    # pair. Empty on every migrated row -- the WorkDrive tree has no district
    # or KVK level to read them from.
    district: str | None = None
    kvk: str | None = None
    district_id: str | None = None
    kvk_id: str | None = None
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
    # Audit trail. *_by is the display name the frontend sent with the action
    # (not verified -- /api/pop has no auth yet); *_at is when the translation
    # or review landed. Both cleared when the file is deleted; null on
    # documents translated before these fields existed.
    translated_by: str | None = None
    translated_at: UTCDatetime | None = None
    reviewed_by: str | None = None
    reviewed_at: UTCDatetime | None = None
    # How many placements share this row's document, this one included. 1 means
    # the document appears in exactly one folder.
    placement_count: int = 1

    created_at: UTCDatetime
    updated_at: UTCDatetime


class DocumentUpdate(DocumentMetadata):
    """PATCH body for a main-table row.

    The placement moves with `state_id`, and with ONE of `crop_id` (a crop
    master entry) or `organization_id`. Every other field belongs to the
    document and is written there, so it changes for all of its placements --
    the endpoint routes them rather than making the caller know which is which.

    IDS ONLY. `state`, `crop`, `organization`, `district` and `kvk` as NAMES are
    gone. Each form field is a selection from a list this API serves, so the
    caller already holds the id; taking a name as well meant the same edit had
    two spellings and the backend had to guess which entry was meant.
    """

    state_id: str | None = None
    crop_id: str | None = None
    organization_id: str | None = None
    # No district_id / kvk_id. A placement is one folder a document is filed
    # in, and the district is a fact about the document -- PATCH
    # /unique-documents/{id} sets it, for every placement at once.
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
    """A (state, folder) pair, the folder being a crop or an organisation.
    Names for display; ids where the entry already exists (a state or
    organisation the form introduces gets its id when the upload is filed)."""

    state: str
    crop: str  # the folder's name, crop or organisation
    crop_kind: str | None = None  # "crop" | "organization"
    state_id: str | None = None
    crop_id: str | None = None
    organization_id: str | None = None
    # No district or kvk. They are the DOCUMENT's fields, so they arrive as
    # form fields of the upload and are carried in its `metadata`, not once per
    # (state, folder) group. Reporting them here always answered null, which
    # reads as "this placement has none" rather than "not a placement field".


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
    created_at: UTCDatetime
    updated_at: UTCDatetime


# -- Translation queue ---------------------------------------------------------


class TranslationJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str  # ObjectId, 24-char hex
    document_id: str  # the unique document's ObjectId hex
    document_code: str | None = None  # its ANNAM_##### id
    unique_document_id: str | None = None  # same as document_id; the name the frontend keys the modal on
    shareable_name: str | None = None
    kind: TranslationJobKind
    status: TranslationJobStatus
    progress_pct: int
    pages_done: int | None = None
    total_pages: int | None = None
    error_message: str | None = None
    created_at: UTCDatetime
    updated_at: UTCDatetime


class ConfigOut(BaseModel):
    translation_available: bool
