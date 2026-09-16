"""Collection names, enums and factories for the document management dashboard.

TWO core collections plus three lookups.

  documents          the MAIN TABLE. One row per (file x state x crop) placement
                     exactly as it appears under the Zoho WorkDrive corpus root.
                     It is a pure ASSOCIATION: a state id, a crop id, and a
                     pointer to the unique document. It carries no metadata and no links of its
                     own -- the API joins those in for display.
  unique_documents   one row per distinct document, keyed by sha256. Everything
                     about the CONTENT lives here: the ANNAM id, the 18 manual
                     metadata fields, page count, language, translation/review
                     state, and the (empty) per-chunk embeddings.

Why the split. Metadata on the placement meant editing "the document" was
editing up to 66 rows, and translating it cost 66x. Now a document is one row
and its placements point at it.

WHERE THE LINKS LIVE. WorkDrive stores a SEPARATE PHYSICAL COPY of a file in
every folder -- 9,811 placements are 9,811 distinct Zoho files over ~7,950
distinct sha256. Grouping by sha256 therefore has to keep every copy's link, so
a unique document carries `duplicate_links`: one entry per physical copy, with
its Zoho file id, link, name and the placement it belongs to. Nothing is lost;
the main table simply does not surface them.

  main_row_ids     every `documents` row that points at this document
  duplicate_links  every physical copy behind those rows

Both are maintained together by `link_placement()` / `unlink_placement()` below,
so the load, the upload and the merge cannot drift apart.

MERGING is manual and team-approved. `unique_documents` starts grouped only by
sha256, because byte-identical is a fact rather than a judgement. Near-duplicates
(a re-scan, a re-export) stay separate until the algorithm proposes them and a
person accepts -- see dashboard/routes_merge.py. A merge repoints placements and
deletes the absorbed DOCUMENT; it never deletes a `documents` row, because each
one is a real file sitting in a real folder.

Lookups. States, crops and organisations are REFERENCED: a placement stores
`state_id` plus `crop_id` or `organization_id`, and the name lives only in the
lookup, so a rename or a merge is one write -- see dashboard/vocabulary.py.
Crops come from the crop master, which another application edits; this backend
only reads them. (An earlier schema abandoned lookup ids because pruned crops
came back with a different id; entries are now never pruned while in use.)
`languages` is still a plain code on the document.

Field names stay snake_case, matching the API contract in dashboard/schemas.py.
"""
from __future__ import annotations

import enum
import re
from datetime import datetime, timezone

# -- Collection names ---------------------------------------------------------

# EVERY collection is prefixed `pop_`. This schema shares a database with a
# different application (DB_NAME in .env), which already has its own `states`,
# `crops` and `users` -- the prefix is what keeps the two sets of collections
# from colliding, and what makes it obvious at a glance which are ours.
COLL_DOCUMENTS = "pop_documents"
COLL_UNIQUE_DOCUMENTS = "pop_unique_documents"
# Controlled vocabularies. Placements reference states and crops by id; see
# dashboard/vocabulary.py.
COLL_STATES = "pop_states"
# Staging's copy of the crop master. Production reads agriai.crop_master
# instead -- see dashboard/db.py:crops_collection. Never written by the API.
COLL_CROPS = "pop_crops"
# Everything a folder names that is NOT a crop: organisations and departments
# ("ICAR - Indian Council Of Agriculture Research") and groupings ("General",
# "Pulses"). Ours, and editable, unlike crops.
COLL_ORGANIZATIONS = "pop_organizations"
# Our own spellings of master crops ("Ground Nut" -> Groundnut), so a folder or
# a form using an old spelling still lands on the master entry. The master's
# `aliases` are regional names ("sajje"), a different thing.
COLL_CROP_ALIASES = "pop_crop_aliases"
COLL_LANGUAGES = "pop_languages"
COLL_UPLOAD_QUEUE_ITEMS = "pop_upload_queue_items"
COLL_TRANSLATION_JOBS = "pop_translation_jobs"
COLL_CONFIG = "pop_config"


# -- Enums --------------------------------------------------------------------
# Values are the wire format the frontend reads -- changing one is a breaking
# API change, not a rename.


class TranslationStatus(str, enum.Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    done = "done"


class ReviewStatus(str, enum.Enum):
    not_started = "not_started"
    in_progress = "in_progress"
    done = "done"


class UploadQueueStatus(str, enum.Enum):
    """queued -> hashing -> checking_duplicate -> awaiting_review -> uploading
    -> done, or -> failed from anywhere.

    EVERY upload stops at `awaiting_review` and waits for a person, whether or
    not a candidate duplicate was found -- the check is a placeholder (see
    dashboard/queue_worker.py) and the human is the real check for now. From
    there the only two moves are POST .../add (go ahead and create the row) and
    POST .../cancel (drop it). Nothing is ever auto-created or auto-discarded.

    The pause happens BEFORE the Zoho upload, so cancelling leaves nothing
    behind in WorkDrive.
    """

    queued = "queued"
    hashing = "hashing"
    checking_duplicate = "checking_duplicate"
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


# -- Name normalisation -------------------------------------------------------
# Applied on every write path so the catalogue cannot drift out of shape.
#
# CAUTION: the OCR language for a document is keyed off the ORIGINAL state name
# as it appears in the source corpus ("State Karnataka"), not the normalised
# one. Rows keep `state_raw` alongside their `state_id` for exactly that reason
# -- replacing it with the standard name would silently break non-English OCR.

# Words stripped from state names: the source data prefixed every state with
# "State" and used "Central Advisories" for the non-state, all-India category.
_STATE_NOISE_WORDS = {"state", "advisories"}


def normalize_state_name(name: str) -> str:
    """'State Karnataka' -> 'Karnataka', 'State  Jammu and Kashmir' ->
    'Jammu and Kashmir', 'Central Advisories' -> 'Central'.

    Note the double space in the Jammu and Kashmir source value -- whitespace
    is collapsed rather than assumed to be single.
    """
    if not name:
        return name
    words = [w for w in name.split() if w.lower() not in _STATE_NOISE_WORDS]
    return " ".join(words).strip()


def normalize_crop_name(name: str) -> str:
    """Title Case, preserving genuine acronyms.

    An all-caps token inside an otherwise mixed-case name is an acronym and is
    left alone ('... Institute (ATARI), Hyderabad'). A name that is ENTIRELY
    upper case is shouting rather than an acronym, so it is title-cased in full
    ('ALL INDIA NETWORK PROJECT ON ...' -> 'All India Network Project On ...') --
    without that distinction, short words like 'ALL' and 'ON' would be mistaken
    for acronyms and preserved.
    """
    if not name:
        return name
    name = " ".join(name.split())  # collapse whitespace
    shouting = name.isupper()

    def fix(token: str) -> str:
        core = token.strip("()[]{}.,;:'\"")
        if not shouting and core.isupper() and core.isalpha() and 2 <= len(core) <= 6:
            return token  # acronym -- leave exactly as written
        # Title-case each alphabetic run so hyphenated and parenthesised words
        # ("bengal gram (chickpea)", "kharif-rabi") capitalise correctly.
        return re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), token)

    return " ".join(fix(t) for t in name.split())


def utcnow() -> datetime:
    """Timestamp for created_at/updated_at.

    Stored as a real BSON date (not a string) so range queries and sorts work.
    tzinfo is stripped: BSON dates are UTC by definition, and the driver
    returns them naive, so storing naive keeps round-trips symmetric.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_format(value: str | None) -> str | None:
    """format_original as stored: lower-case, trimmed, blank -> None.

    Stored values are lower-case file extensions (pdf, png, docx), plus `url`
    for a web page and `printed` for a printed document. The column filter
    matches exactly, so a person picking "PDF" in a form must not store a
    second, upper-case spelling that the filter for `pdf` would then miss.
    """
    value = (value or "").strip().lower()
    return value or None


# Every manually-entered metadata field on a document row. Kept as one list so
# the upload payload, the migration and the document factory can't drift apart.
# Mirrors DocumentMetadata in dashboard/schemas.py.
MANUAL_METADATA_FIELDS = (
    "advisory_type",
    "advisory_scope",
    "season",
    "edition_revision_volume",
    "date_of_release",
    "month_of_release",
    "year_of_release",
    "date_of_collection",
    "month_of_collection",
    "year_of_collection",
    "advisory_name",
    "advisory_released_org",
    "advisory_org_address",
    "live_source_link",
    "domain",
    "verification_status",
    "verified_by",
    "document_status",
)


def new_document(*, row_id: int, unique_document_id, state_id, state_raw: str,
                 crop_raw: str, crop_id=None, organization_id=None, **fields) -> dict:
    """One row of the main table: this document, filed under this state and crop.

    An ASSOCIATION and nothing more. No sha256, no link, no metadata -- those
    belong to the unique document this row points at, and the API joins them in.

    `state_id` references pop_states. The folder under it is EITHER a crop --
    `crop_id`, a crop master entry -- OR an organisation / grouping --
    `organization_id`, into pop_organizations. Exactly one of the two is set.
    `state_raw`/`crop_raw` keep the original folder names from Zoho, because the
    OCR language lookup keys off the raw state name and because a folder has to
    stay findable by the name it actually has in WorkDrive.
    """
    now = utcnow()
    doc = {
        # Human-readable sequential id for the PLACEMENT (POP_00000..). The
        # ANNAM id names the document and lives on unique_documents; this names
        # the row. A merge repoints the row and never renumbers it.
        "row_id": row_id,
        "unique_document_id": unique_document_id,
        # -- placement: references, plus the folder names as found --
        "state_id": state_id,
        "state_raw": state_raw,
        **({"crop_id": crop_id} if crop_id is not None else {"organization_id": organization_id}),
        "crop_raw": crop_raw,
        # Anything nested deeper than <state>/<crop>/<file> in WorkDrive. Empty
        # for the corpus as it stands; recorded rather than flattened so an
        # unexpected extra folder level is visible instead of silently changing
        # which crop a file appears to belong to.
        "subpath": "",
        "created_at": now,
        "updated_at": now,
    }
    doc.update(fields)
    return doc


def new_unique_document(*, display_id: int, sha256: str | None = None, **fields) -> dict:
    """One distinct document: everything true of the CONTENT.

    date_of_release / date_of_collection are deliberately strings, not dates:
    they hold free-form values from the source corpus (partial dates, ranges,
    prose). Coercing them would lose data.
    """
    now = utcnow()
    doc = {
        # The ANNAM number (ANNAM_00000..ANNAM_50000) -- see
        # dashboard/display_id.py. It names the DOCUMENT.
        "display_id": display_id,
        # The grouping key, and unique here: two rows with the same sha256 are
        # the same document by definition. Also the join key to the on-disk
        # chunk fingerprints in fix/out/.
        "sha256": sha256,
        "num_pages": None,
        "format_original": "pdf",
        # The document's own name and link: those of its ANCHOR copy (see
        # representative_file_id below). Individual copies keep their own in
        # duplicate_links, because two copies of one document can be named
        # differently and each has its own file in WorkDrive.
        "shareable_name": None,
        "shareable_link": None,
        # A code from the `languages` collection (the 14 tessdata_best
        # languages). Filled by the rule in dashboard/languages.resolve_language:
        # a document the OCR pass read as English is English, and everything
        # else takes the language of the state it is filed in.
        "language": None,
        # WHERE `language` CAME FROM. Exactly two values, because there is only
        # one distinction anyone acts on -- which rows are guesses:
        #   "detected"   known: the OCR pass read it as English, or a person set
        #                it through the dashboard. Never overwritten by
        #                scripts/fill_language_from_state.py.
        #   "state"      inferred from the state's language. Plausible, not
        #                measured, and 3,566 of 8,748 documents are like this.
        "language_source": None,
        # -- manual metadata: what a human fills in through the dashboard --
        **{name: None for name in MANUAL_METADATA_FIELDS},
        # -- translation / review: ONCE per document --
        # This is the point of the split. Previously the corpus's most-placed
        # file would have been translated 66 times.
        "translation_zoho_file_id": None,
        "translation_shareable_link": None,
        "translation_status": TranslationStatus.not_started.value,
        "review_zoho_file_id": None,
        "review_shareable_link": None,
        "review_status": ReviewStatus.not_started.value,
        "translated_by": None,
        "translated_at": None,
        "reviewed_by": None,
        "reviewed_at": None,
        # One vector per text chunk, in chunk order. ALWAYS EMPTY for now, by
        # design -- nothing writes it. The vectors are already computed and live
        # on local disk (fix/out/fingerprint_v3_chunks.f32, keyed by sha256);
        # the Atlas cluster is a 512 MB free tier and cannot hold them. The
        # column exists so filling it in later needs no schema change, and there
        # is no vector index on it (see dashboard/db.py).
        "chunk_embeddings": [],
        # -- placements and physical copies, kept in step by link_placement() --
        "main_row_ids": [],
        "duplicate_links": [],
        # THE ANCHOR. Which entry of duplicate_links is *this document*, as
        # opposed to another copy of it. Everything that has to act on the
        # bytes -- translation above all -- uses this one file and no other.
        #
        # It matters more later than it does now. Everything grouped so far is
        # byte-identical, so any copy would do; once the algorithm merges
        # NEAR-duplicates (a re-scan, a re-export), the other entries are no
        # longer the same bytes and picking one at random would translate the
        # wrong artefact. Anchoring now means that day changes nothing.
        #
        # Stable across merges: absorbing documents adds copies but never moves
        # the anchor. A person can re-anchor through
        # PATCH /unique-documents/{id} if the chosen file turns out to be a bad
        # scan, and it is validated to be one of this document's own copies.
        "representative_file_id": None,
        "representative_row_id": None,
        # ANNAM ids absorbed into this document by a team-approved merge. An
        # audit trail, so a merge can be explained (and reversed by hand).
        "merged_from": [],
        "created_at": now,
        "updated_at": now,
    }
    doc.update(fields)
    return doc


def new_copy_link(*, zoho_file_id: str | None, shareable_link: str | None,
                  shareable_name: str | None, row_id: int | None = None) -> dict:
    """One entry of a unique document's `duplicate_links`.

    A physical copy in WorkDrive. There is one of these per `documents` row,
    because WorkDrive keeps a separate file in every folder rather than linking
    one file into many.

    No state or crop: the entry names its placement by `row_id`, and the API
    reads the folder from there. Copying the names in is how the two drifted --
    the corpus load wrote raw folder spellings here and normalised ones on the
    placement.
    """
    return {
        "zoho_file_id": zoho_file_id,
        "shareable_link": shareable_link,
        "shareable_name": shareable_name,
        "row_id": row_id,
    }


def new_upload_queue_item(*, filename: str, placements: list[dict], metadata: dict) -> dict:
    """Status tracking for an in-flight upload -- drives the Add Document box.

    `placements` is the (state, crop) list the form asked for, already flattened
    from the per-state crop groups the API accepts. `metadata` is the rest of the
    submitted form. Both are held here because the document does not exist yet:
    the item waits at `awaiting_review` for a person, and the form has to survive
    until they decide. On a decision they are unpacked into real fields and this
    row is only history.

    `candidates` is what the duplicate check found -- up to three existing
    documents, best first, each carrying which of THIS upload's placements it
    does not already have. That is what makes the three-way choice meaningful:
    "add" is only offered when there is something to add.
    """
    now = utcnow()
    return {
        "filename": filename,
        "status": UploadQueueStatus.queued.value,
        "progress_pct": 0,
        "error_message": None,
        # Informational (non-error) message -- e.g. what a decision would do.
        "note": None,
        # Filled in once known, so the queue row can show them before the
        # document exists for real.
        "num_pages": None,
        "sha256": None,
        # The (state, crop) pairs this upload is asking to create.
        "placements": placements,
        # Up to three existing documents this might be, best score first. Empty
        # means nothing matched -- NOT that nothing is similar; see
        # queue_worker.find_candidates.
        "candidates": [],
        # The rest of the submitted form (language + the 18 metadata fields).
        "metadata": metadata,
        # What the decision produced.
        "created_document_id": None,   # ANNAM id used or created
        "created_row_ids": [],         # POP ids of the placements created
        "created_at": now,
        "updated_at": now,
    }


def new_upload_candidate(*, document, score: float, match_type: str,
                         new_placements: list[dict]) -> dict:
    """One row of the duplicate-review list shown before a person decides.

    `new_placements` is the part that drives the UI: the pairs this upload asks
    for that the candidate does not already have. Empty means adding to this
    document would create nothing, so only "new" and "discard" make sense.

    `can_create_new` is False for an exact sha256 match, because sha256 is
    unique on `unique_documents` -- a second document for byte-identical content
    is not merely undesirable, it cannot be stored. For a near-duplicate (a
    re-scan, a different hash) it is True and creating a separate document is a
    legitimate answer.
    """
    return {
        "document_id": str(document["_id"]),
        "document_code": None,  # filled by the caller, which owns the id format
        "shareable_name": document.get("shareable_name"),
        "score": score,
        "match_type": match_type,
        "placement_count": len(document.get("main_row_ids") or []),
        "new_placements": new_placements,
        "can_add": bool(new_placements),
        "can_create_new": match_type != "sha",
    }


def new_translation_job(*, document_id, kind: TranslationJobKind) -> dict:
    """DB-backed version of pop_server.py's in-memory `_jobs` dict pattern --
    same shape, but survives a server restart.

    Scoped to ONE row. Rows that happen to share a sha256 are independent here:
    translating one does not mark the others translated, because nothing in
    this schema knows they are the same file yet.
    """
    now = utcnow()
    return {
        "document_id": document_id,
        "kind": kind.value if isinstance(kind, TranslationJobKind) else kind,
        "status": TranslationJobStatus.queued.value,
        "progress_pct": 0,
        # Page-level progress -- set once the source PDF is split (see
        # dashboard/routes_translation.py's on_progress callback into
        # pop_server._run_one_doc). Both None while still queued/splitting.
        "pages_done": None,
        "total_pages": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }


# -- Keeping placements and copies in step ------------------------------------
# `main_row_ids` and `duplicate_links` are two views of the same relationship,
# so they are only ever changed together, here. The load, the upload worker and
# the merge endpoint all go through these two functions rather than each
# writing their own $push -- that is what stops the two lists drifting apart.


def link_placement(db, unique_document_id, *, row_obj_id, row_id: int, copy_link: dict) -> None:
    """Record that a `documents` row uses this unique document.

    $addToSet on main_row_ids rather than $push: re-running the corpus load, or
    retrying an upload, must not add the same placement twice.

    duplicate_links counts PHYSICAL FILES, not placements. In the corpus the two
    are the same number, because WorkDrive keeps a separate copy of a file in
    every folder -- 9,811 placements over 9,811 distinct Zoho file ids. A
    dashboard upload is the other case: it writes ONE file into one flat folder
    and then files it in several places, so N placements share a single file id.
    Listing that file once per placement would show a document as having several
    identical "copies" that are all the same object, and would offer a choice of
    anchor where there is only one file to anchor on.

    So an entry is keyed by `zoho_file_id`: a file already listed is not listed
    again. `row_id` on the entry names the placement it was first filed under --
    for the corpus that is its only placement, for an upload it is one of
    several, which is why unlink_placement cannot simply remove it.
    """
    copy_link = {**copy_link, "row_id": row_id}
    file_id = copy_link.get("zoho_file_id")
    # Re-running the same placement replaces its entry; a placement whose file
    # is already listed adds nothing.
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": unique_document_id},
        {"$pull": {"duplicate_links": {"row_id": row_id}}},
    )
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": unique_document_id},
        {"$addToSet": {"main_row_ids": row_obj_id}, "$set": {"updated_at": utcnow()}},
    )
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": unique_document_id, "duplicate_links.zoho_file_id": {"$ne": file_id}},
        {"$push": {"duplicate_links": copy_link}},
    )
    # If this copy IS the document's anchor, record which placement it is. The
    # file id is known when the document is created but the row id only exists
    # once the placement is inserted, so it can only be filled in here.
    #
    # Only when it is not set yet: where several placements share one file, the
    # anchor is that single copy, and its row_id must stay the one the copy
    # entry carries. Overwriting it with each later placement would leave
    # representative_row_id naming a placement that is not in duplicate_links,
    # and anything matching the two up would find no anchor at all.
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": unique_document_id,
         "representative_file_id": file_id,
         "$or": [{"representative_row_id": None},
                 {"representative_row_id": {"$exists": False}}]},
        {"$set": {"representative_row_id": row_id}},
    )


def unlink_placement(db, unique_document_id, *, row_obj_id, row_id: int) -> None:
    """Drop a placement, and its copy link if no other placement still uses it.

    The document itself survives, even with no placements left, so its metadata
    and translation are not lost with the last folder entry.

    The copy link is the careful part. When each placement has its own physical
    file (the corpus), removing the placement removes its file from the list.
    When several placements share one file (a dashboard upload), the file is
    still there after one of them goes -- deleting its entry would strand the
    document with no link at all, and with it the download and the anchor. The
    two cases are told apart by counting: fewer distinct files than placements
    means they are shared.
    """
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one(
        {"_id": unique_document_id}, {"main_row_ids": 1, "duplicate_links": 1}
    ) or {}
    links = doc.get("duplicate_links") or []
    placements = len(doc.get("main_row_ids") or [])
    shared = len({c.get("zoho_file_id") for c in links}) < placements

    pull: dict = {"main_row_ids": row_obj_id}
    if not shared:
        pull["duplicate_links"] = {"row_id": row_id}
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": unique_document_id},
        {"$pull": pull, "$set": {"updated_at": utcnow()}},
    )
    if not shared:
        return
    # The surviving entry still names the placement that just went. Point it at
    # one that is left, so the row_id on a copy is always a real placement.
    remaining = [r["row_id"] for r in db[COLL_DOCUMENTS].find(
        {"unique_document_id": unique_document_id}, {"row_id": 1})]
    if not remaining:
        return
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": unique_document_id, "duplicate_links.row_id": row_id},
        {"$set": {"duplicate_links.$.row_id": remaining[0]}},
    )
    db[COLL_UNIQUE_DOCUMENTS].update_one(
        {"_id": unique_document_id, "representative_row_id": row_id},
        {"$set": {"representative_row_id": remaining[0]}},
    )
