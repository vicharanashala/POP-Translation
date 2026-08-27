"""Background worker for the upload/dedup queue (upload_queue_items).

Same ThreadPoolExecutor-driven async-job shape pop_server.py already uses
for the translation pipeline (_jobs dict + ThreadPoolExecutor), but
DB-backed so queue state survives a server restart instead of living only
in an in-memory dict.

Deliberately its own small pool (2 workers), separate from both the
translation pipeline's pool and the currently-running
scripts/hash_and_embed_report_true.py batch job -- this is per-upload,
single-middle-page OCR + one embedding call, not the batch job's
download/OCR-pool-decoupled design, so it doesn't need or want that much
concurrency, and keeping it small avoids competing for CPU with whichever
of those two is running.
"""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dashboard import zoho_layout
from dashboard.config import DUPLICATE_EMBEDDING_THRESHOLD, REPO_ROOT
from dashboard.db import get_session
from dashboard.dedup import compute_signature, find_closest_match
from dashboard.display_id import allocate_display_id
from dashboard.models import Crop, DocumentAssociation, State, UniqueDocument, UploadQueueItem, UploadQueueStatus

_UPLOAD_WORKERS = 2
_executor = ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS, thread_name_prefix="upload-dedup")

# Every item's original bytes are cached here once it reaches
# awaiting_review, since ANY item -- matched or not -- might later get
# resolved via .../new, and by then the original request that carried the
# bytes is long gone. Cleared on any resolution (add/new/cancel).
_PENDING_UPLOADS_ROOT = REPO_ROOT / "pop-data" / "POP_Work" / "Dashboard_PendingUploads"


def _pending_upload_path(item_id: uuid.UUID) -> Path:
    return _PENDING_UPLOADS_ROOT / f"{item_id}.pdf"


def discard_pending_upload(item_id: uuid.UUID) -> None:
    _pending_upload_path(item_id).unlink(missing_ok=True)

_METADATA_FIELDS = (
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


def _classify_placements(
    session, unique_document_id, state_names: list[str], crop_names: list[str]
) -> tuple[list[tuple[int, int]], list[str], list[str]]:
    """Cross product of state_names x crop_names against this document's
    EXISTING placements -- returns (new_pairs as (state_id, crop_id),
    new_labels, already_existing_labels). Read-only, no writes -- used both
    to preview a pending duplicate's would-be effect and, at approval time,
    to know exactly what to create."""
    state_ids = {s.name: s.id for s in session.query(State).filter(State.name.in_(state_names)).all()}
    crop_ids = {c.name: c.id for c in session.query(Crop).filter(Crop.name.in_(crop_names)).all()}
    existing = {
        (a.state_id, a.crop_id)
        for a in session.query(DocumentAssociation).filter_by(unique_document_id=unique_document_id).all()
    }
    new_pairs, new_labels, already_labels = [], [], []
    for state_name in state_names:
        state_id = state_ids.get(state_name)
        if state_id is None:
            continue
        for crop_name in crop_names:
            crop_id = crop_ids.get(crop_name)
            if crop_id is None:
                continue
            label = f"{state_name} / {crop_name}"
            if (state_id, crop_id) in existing:
                already_labels.append(label)
            else:
                new_pairs.append((state_id, crop_id))
                new_labels.append(label)
    return new_pairs, new_labels, already_labels


def attach_new_placements(session, unique_document_id, state_names: list[str], crop_names: list[str]) -> list[str]:
    """Create a DocumentAssociation for every (state, crop) combo that
    doesn't already exist for this document; returns the labels actually
    created. Used both when creating a brand-new unique_documents row
    (nothing pre-existing to skip) and by /uploads/{id}/add once a matched
    document is confirmed."""
    new_pairs, new_labels, _already = _classify_placements(session, unique_document_id, state_names, crop_names)
    for state_id, crop_id in new_pairs:
        session.add(DocumentAssociation(unique_document_id=unique_document_id, state_id=state_id, crop_id=crop_id))
    return new_labels


def _review_note(
    match_type: str | None,
    similarity_score: float | None,
    matched_display_id: int | None,
    new_labels: list[str],
    already_labels: list[str],
) -> str:
    """Every item lands here needing a human decision (add/new/cancel) --
    never auto-resolved. match_type/similarity_score tell the human WHY:
    an exact sha256 hit, a fuzzy embedding match (shown regardless of
    confidence -- even below DUPLICATE_EMBEDDING_THRESHOLD, since a human,
    not the threshold, makes the final call), or no candidate at all
    (unique_documents was empty)."""
    from dashboard.display_id import format_display_id

    doc_ref = format_display_id(matched_display_id) if matched_display_id is not None else None

    if match_type == "sha":
        parts = [f"Exact SHA-256 match: byte-for-byte identical to {doc_ref}."]
    elif match_type == "embedding":
        pct = f"{similarity_score * 100:.1f}%"
        confidence = (
            "likely duplicate"
            if similarity_score >= DUPLICATE_EMBEDDING_THRESHOLD
            else "low confidence, shown for reference only"
        )
        parts = [f"Closest existing document by embedding similarity: {doc_ref} ({pct} similar, {confidence})."]
    else:
        parts = ["No existing documents to compare against yet (this would be the first)."]

    parts.append("Awaiting a decision (add / new / cancel) -- nothing linked or created yet.")
    if new_labels:
        parts.append(f"'add' would link: {', '.join(new_labels)}.")
    if already_labels:
        parts.append(f"Already present on the matched document: {', '.join(already_labels)}.")
    return " ".join(parts)


def _set_status(item_id: uuid.UUID, **fields) -> None:
    with get_session() as session:
        item = session.get(UploadQueueItem, item_id)
        if item is None:
            return
        for k, v in fields.items():
            setattr(item, k, v)


def enqueue(item_id: uuid.UUID, pdf_bytes: bytes) -> None:
    _executor.submit(_process, item_id, pdf_bytes)


def _process(item_id: uuid.UUID, pdf_bytes: bytes) -> None:
    try:
        with get_session() as session:
            item = session.get(UploadQueueItem, item_id)
            if item is None:
                return
            payload = item.metadata_payload
            filename = item.filename
        state_names = payload.get("state_names", [])
        crop_names = payload.get("crop_names", [])
        language = payload["language"]

        _set_status(item_id, status=UploadQueueStatus.hashing, progress_pct=10)

        _set_status(item_id, status=UploadQueueStatus.embedding, progress_pct=30)
        sha, embedding, _transcript = compute_signature(pdf_bytes, language)

        import fitz

        with fitz.open(stream=pdf_bytes, filetype="pdf") as _doc:
            num_pages = _doc.page_count
        # Known regardless of what happens next -- persist now so the queue
        # row can show it immediately.
        _set_status(item_id, num_pages=num_pages, language=language)

        _set_status(item_id, status=UploadQueueStatus.checking_duplicate, progress_pct=60)
        with get_session() as session:
            existing_doc = None
            match_type = None
            similarity_score = None

            exact = session.query(UniqueDocument).filter_by(sha256=sha).first()
            if exact is not None:
                existing_doc, match_type, similarity_score = exact, "sha", 1.0
            else:
                # Shown regardless of DUPLICATE_EMBEDDING_THRESHOLD -- a human
                # makes the final call now, not the threshold. None only when
                # unique_documents is completely empty (nothing to compare to).
                match, similarity = find_closest_match(session, embedding)
                if match is not None:
                    existing_doc, match_type, similarity_score = match, "embedding", similarity

            if existing_doc is not None:
                _new_pairs, new_labels, already_labels = _classify_placements(
                    session, existing_doc.id, state_names, crop_names
                )
                matched_display_id = existing_doc.display_id
            else:
                new_labels, already_labels, matched_display_id = [], [], None

            # Every item ends up here needing a human decision -- nothing is
            # ever auto-created or auto-removed, even when there's no match
            # at all. Bytes are cached now (not only on a match) since /new
            # can be called from this status regardless of match_type.
            _PENDING_UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
            _pending_upload_path(item_id).write_bytes(pdf_bytes)

            item = session.get(UploadQueueItem, item_id)
            item.status = UploadQueueStatus.awaiting_review
            item.duplicate_of_id = existing_doc.id if existing_doc is not None else None
            item.similarity_score = similarity_score
            item.match_type = match_type
            item.note = _review_note(match_type, similarity_score, matched_display_id, new_labels, already_labels)
            item.progress_pct = 100

    except Exception as e:  # noqa: BLE001
        _set_status(item_id, status=UploadQueueStatus.failed, error_message=str(e))


def enqueue_as_new(item_id: uuid.UUID) -> None:
    """POST /uploads/{id}/new -- the human has decided an awaiting_review
    item should become its own unique_documents row: either there was no
    candidate match at all, or there was one but the human judged it a false
    positive. Same create path either way, using the cached bytes from when
    this item first reached awaiting_review."""
    _executor.submit(_process_as_new, item_id)


def _process_as_new(item_id: uuid.UUID) -> None:
    pdf_path = _pending_upload_path(item_id)
    try:
        pdf_bytes = pdf_path.read_bytes()

        with get_session() as session:
            item = session.get(UploadQueueItem, item_id)
            if item is None:
                return
            payload = item.metadata_payload
            filename = item.filename
            num_pages = item.num_pages
        state_names = payload.get("state_names", [])
        crop_names = payload.get("crop_names", [])
        language = payload["language"]

        # Re-derived from the cached bytes rather than persisted at
        # duplicate-flag time -- avoids adding a sha256/embedding column to
        # upload_queue_items just for this rare path; cost is the same one
        # OCR+embed call the original upload already paid once.
        sha, embedding, _transcript = compute_signature(pdf_bytes, language)

        from pop_server import _get_zoho

        wd = _get_zoho()
        zoho_file_id = zoho_layout.upload_original(wd, filename, pdf_bytes)

        with get_session() as session:
            doc = UniqueDocument(
                sha256=sha,
                display_id=allocate_display_id(session),
                zoho_file_id=zoho_file_id,
                embedding=embedding,
                shareable_name=filename,
                shareable_link=zoho_layout.shareable_link(zoho_file_id),
                language=language,
                format_original="pdf",
                num_pages=num_pages,
                **{k: payload.get(k) for k in _METADATA_FIELDS},
            )
            session.add(doc)
            session.flush()

            attach_new_placements(session, doc.id, state_names, crop_names)

            item = session.get(UploadQueueItem, item_id)
            if item is not None:
                session.delete(item)

    except Exception as e:  # noqa: BLE001
        _set_status(item_id, status=UploadQueueStatus.failed, error_message=f"'new' failed: {e}")
    finally:
        pdf_path.unlink(missing_ok=True)
