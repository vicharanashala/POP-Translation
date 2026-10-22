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

import enum
from datetime import datetime, timezone
from typing import Annotated, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, model_validator

from dashboard.models import (
    DOMAINS, ReviewStatus, TranslationJobKind, TranslationJobStatus, TranslationMode, TranslationStatus, UploadQueueStatus,
)

T = TypeVar("T")


def _as_utc(value: datetime) -> datetime:
    """Mongo hands dates back naive, but they are UTC. Marked as such, they are
    sent with a "Z", so the browser shows them in local time (IST) instead of
    reading a bare "10:00:00" as 10:00 IST -- which put every time 5h30 behind."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


UTCDatetime = Annotated[datetime, AfterValidator(_as_utc)]


def as_domain_list(value):
    """`domain` is a list. None is the empty list, and a bare string -- every
    value stored before it became one, and what an older client still sends --
    is a list of that one name."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return value


def _known_domains(value: list[str]) -> list[str]:
    unknown = [d for d in value if d not in DOMAINS]
    if unknown:
        raise ValueError(f"unknown domain {unknown[0]!r} -- pick from GET /dashboard/domains")
    return list(dict.fromkeys(value))  # a name picked twice is stored once


# What is served (tolerant: always a list) and what is accepted (checked
# against DOMAINS as well).
DomainsOut = Annotated[list[str] | None, BeforeValidator(as_domain_list)]
Domains = Annotated[list[str] | None, BeforeValidator(as_domain_list), AfterValidator(_known_domains)]


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
    """A folder that is not a crop: an entry of the organisation master
    (agriai.organizations), e.g. "Central Rice Research Institute" or "General".

    Read-only like every other vocabulary. Until 2026-10 these were WorkDrive
    folder names of our own; `crop_raw` on a placement still says which folder
    it was found in.
    """

    id: str
    name: str
    document_count: int = 0
    # As the master stores them, so two entries with one name can be told
    # apart in the dropdown. `type` is "central", "state" or "district";
    # `state` is a state NAME, not a pop_states id -- the master is another
    # application's and is not keyed on LGD codes.
    type: str | None = None
    state: str | None = None


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
    # Organisations only (see OrganizationOut); None for a crop.
    type: str | None = None
    state: str | None = None


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
    domain: Domains = None  # several of models.DOMAINS; [] for none
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
    # WHERE THE FILE CAME FROM. "system" for every copy the document was built
    # with (the corpus load, an upload) -- stored or not, which is why it is a
    # default here rather than a backfill. A merge tags each file it brings in
    # with the merged document's NAME (not its ANNAM id, which is handed out
    # again once that document is gone), who merged it and when; the frontend
    # groups the links by `merged_document`, null being the document's own.
    merged_document: str | None = None
    merged_by: str = "system"
    merged_at: UTCDatetime | None = None


_STORED_INTS = ("month_of_release", "year_of_release", "month_of_collection", "year_of_collection")


class UniqueDocumentOut(DocumentMetadata):
    model_config = ConfigDict(from_attributes=True)
    id: str  # ObjectId, 24-char hex
    domain: DomainsOut = []  # served as stored, not re-checked

    @model_validator(mode="before")
    @classmethod
    def _blank_is_none(cls, data):
        """A blank month/year as stored is no value, not a 500.

        A bulk edit on 2026-09-18 wrote nine years as STRINGS, two of them ""
        (ANNAM_05977, ANNAM_05982). "2022" parses; "" does not, and one such
        row failed every listing page it landed on -- the whole page, not the
        row. Only the output is lenient: a PATCH still has to send a number."""
        if isinstance(data, dict):
            for f in _STORED_INTS:
                if isinstance(data.get(f), str) and not data[f].strip():
                    data = {**data, f: None}
        return data
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
    # Who passed it through the approval chain (see routes_uploads): the upload
    # approver who approved it, and the admin who filed it as a new document.
    # Display names, sent by the frontend with the action like translated_by.
    # Read-only, like uploaded_by. Null on documents from before the chain and
    # where a stage was skipped (an admin's or approver's own upload).
    approved_by_approver: str | None = None
    approved_by_admin: str | None = None
    # The same three people by agriai.users id. Null on documents filed before
    # the ids were stored.
    uploaded_by_id: str | None = None
    approved_by_approver_id: str | None = None
    approved_by_admin_id: str | None = None

    placement_count: int = 0  # how many main-table rows point here
    # THE ANCHOR: which entry of duplicate_links is this document, as opposed to
    # another copy of it. Translation acts on this file and no other. Fixed
    # when the document is created: not moved by a merge, not editable.
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
    # Accepted only so it can be REFUSED (400): the anchor copy is fixed when
    # the document is created and cannot be moved. Without the field here a
    # stale client's re-anchor would be silently ignored and answer 200.
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
    """Merge these documents into the one being POSTed to.

    ObjectId hex or ANNAM ids. The merged documents are deleted; their
    placements move to this document unless it is already filed in that state +
    folder, and their files stay in WorkDrive, listed in this document's
    duplicate_links. `merged_by` is the display name to record on those links,
    as the frontend sends `translated_by`; blank falls back to the caller's id.
    """

    absorb: list[str]
    merged_by: str | None = None


class MergeResult(BaseModel):
    document_id: str  # the surviving ANNAM id
    absorbed: list[str]  # the merged documents' file names
    placements_moved: int  # filed under this document now, POP id kept
    placements_dropped: int  # this document was already filed there
    links_added: int  # copy links added to duplicate_links
    # Whose translation + review this document took, when it had none; null if
    # it kept its own or nobody had one.
    translation_taken_from: str | None = None
    placement_count: int  # of the survivor, after the merge
    # Translation/review files WorkDrive refused to trash. The merge itself is
    # complete; these are only left over.
    not_trashed: list[str] = []


class UnlinkRequest(BaseModel):
    """Which merged group to split back out: the `merged_document` value its
    links carry in duplicate_links."""

    merged_document: str


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


class UploadStage(str, enum.Enum):
    """Who an upload is waiting on, in words. Derived from `level` (0 expert,
    1 approver, 2 admin), never stored -- see routes_uploads._stage."""

    approver = "approver"  # the upload approvers
    admin = "admin"  # the admins
    expert = "expert"  # declined back to whoever uploaded it


class UploadActions(BaseModel):
    can_edit: bool = False  # PATCH the metadata / placements
    can_approve: bool = False  # approver: send it on to the admins
    can_decline: bool = False  # send it back a stage, with a note
    can_decide: bool = False  # admin: add / new (see each candidate's flags)
    can_discard: bool = False  # admin: drop it
    can_resubmit: bool = False  # uploader: send a declined item back to the approvers
    can_remove: bool = False  # uploader: clear an item returned to them


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

    # The approval chain -- see routes_uploads. `expert` is the uploader's
    # agriai.users id (None on items queued before the chain existed).
    # `level` is where it waits: 0 its uploader, 1 the upload approvers, 2 the
    # admins. `upload_level` is what its uploader is, on the same scale: 0
    # anyone, 1 an upload approver, 2 an admin. It is never declined below
    # `upload_level`. Each note is the latest decline from that stage.
    expert: str | None = None
    # The uploader's display name, as the upload form sent it (metadata's
    # `uploaded_by`). For showing; `expert` stays the id that decides access.
    uploaded_by: str | None = None
    level: int
    upload_level: int = 0
    approver_note: str | None = None
    admin_note: str | None = None
    # Who approved it at each stage so far, by name and by agriai.users id;
    # copied onto the document when it is filed as a new one.
    approved_by_approver: str | None = None
    approved_by_approver_id: str | None = None
    approved_by_admin: str | None = None
    approved_by_admin_id: str | None = None
    stage: UploadStage
    # What THIS caller may do with the item. Empty on SSE events, which have no
    # caller; the frontend refetches the queue on those.
    actions: UploadActions = UploadActions()
    created_at: UTCDatetime
    updated_at: UTCDatetime


class UploadEdit(BaseModel):
    """PATCH body for an upload still in review: any subset of what the upload
    form sent, by the approver or admin holding it. Same rules as the form --
    ids only, `placements` in the form's per-state group shape. `uploaded_by`
    is not here: who uploaded it is not something a reviewer corrects."""

    model_config = ConfigDict(extra="forbid")
    language: str | None = None
    placements: list[dict] | None = None
    district_id: str | None = None
    kvk_id: str | None = None
    format_original: str | None = None
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
    domain: Domains = None
    verification_status: str | None = None
    document_status: str | None = None


class UploadDecline(BaseModel):
    note: str


# -- Access ----------------------------------------------------------------------


class MeOut(BaseModel):
    user_id: str
    role: str
    is_admin: bool
    can_edit: bool
    can_approve_uploads: bool
    can_translate: bool


class ApproverIn(BaseModel):
    user_id: str  # agriai.users _id
    name: str | None = None
    email: str | None = None


class ApproverOut(BaseModel):
    user_id: str
    name: str | None = None
    email: str | None = None
    added_by: str | None = None
    added_at: UTCDatetime | None = None


# -- Translation queue ---------------------------------------------------------


class TranslationJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str  # ObjectId, 24-char hex
    document_id: str  # the unique document's ObjectId hex
    document_code: str | None = None  # its ANNAM_##### id
    unique_document_id: str | None = None  # same as document_id; the name the frontend keys the modal on
    shareable_name: str | None = None
    kind: TranslationJobKind
    mode: TranslationMode = TranslationMode.normal  # normal / flex / batch -- the tag on the queue item
    # Batch only: Google's state for the batch (JOB_STATE_PENDING, _RUNNING,
    # ...), "finishing" while its pages are made into the .docx; null otherwise.
    batch_state: str | None = None
    status: TranslationJobStatus
    progress_pct: int
    pages_done: int | None = None
    total_pages: int | None = None
    error_message: str | None = None
    # One per page: the WorkDrive link of the page's translated .docx, "" for a
    # page not translated yet. Kept on a failed or cancelled job; empty once done.
    page_links: list[str] = []
    created_at: UTCDatetime
    updated_at: UTCDatetime


class ConfigOut(BaseModel):
    translation_available: bool
