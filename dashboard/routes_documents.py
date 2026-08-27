"""CRUD routes for document_associations (the "main table", one row per
document x state x crop placement) and unique_documents (the "unique
table", one row per distinct document by content hash).

Pagination fixed at 100/page (frontend plan §5). Every displayed column is
filterable via `filter[<column>]=<value>` query params, validated against a
per-endpoint whitelist so arbitrary column names can't be injected into the
query.
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import false, func
from sqlalchemy.orm import Session, joinedload

from dashboard.config import PAGE_SIZE
from dashboard.db import get_db
from dashboard.dedup import LANGUAGES
from dashboard.display_id import format_display_id
from dashboard.models import Crop, DocumentAssociation, State, UniqueDocument
from dashboard.schemas import (
    CropCreate,
    CropOut,
    DocumentAssociationOut,
    DocumentAssociationUpdate,
    LanguageOut,
    Paginated,
    StateOut,
    UniqueDocumentOut,
    UniqueDocumentUpdate,
)

router = APIRouter()


def _pagination(request: Request) -> tuple[int, int]:
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    return page, PAGE_SIZE


def _parse_filters(request: Request, allowed: dict) -> list:
    clauses = []
    for key, build_clause in allowed.items():
        val = request.query_params.get(f"filter[{key}]")
        if val:
            clauses.append(build_clause(val))
    return clauses


def _int_filter(column):
    def build(v: str):
        try:
            return column == int(v)
        except ValueError:
            return false()

    return build


def _date_filter(column):
    def build(v: str):
        try:
            d = date.fromisoformat(v)
        except ValueError:
            return false()
        return func.date(column) == d

    return build


def _display_id_filter(column):
    def build(v: str):
        digits = v.strip().upper()
        if digits.startswith("ANNAM_"):
            digits = digits[len("ANNAM_") :]
        try:
            return column == int(digits)
        except ValueError:
            return false()

    return build


def _uuid_filter(column):
    def build(v: str):
        try:
            return column == uuid.UUID(v)
        except ValueError:
            return false()

    return build


def _association_out(row: DocumentAssociation) -> DocumentAssociationOut:
    return DocumentAssociationOut(
        id=row.id,
        unique_document_id=row.unique_document_id,
        document_id=format_display_id(row.unique_document.display_id),
        state=StateOut.model_validate(row.state),
        crop=CropOut.model_validate(row.crop),
        shareable_name=row.unique_document.shareable_name,
        language=row.unique_document.language,
        translation_status=row.unique_document.translation_status,
        review_status=row.unique_document.review_status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _states_crops_for_docs(db: Session, doc_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[list[str], list[str]]]:
    """One query for the whole page instead of one per row -- avoids both
    N+1 queries and the row-multiplication problem a joinedload() on a
    one-to-many collection would cause together with LIMIT/OFFSET."""
    if not doc_ids:
        return {}
    rows = (
        db.query(DocumentAssociation.unique_document_id, State.name, Crop.name)
        .join(State, DocumentAssociation.state_id == State.id)
        .join(Crop, DocumentAssociation.crop_id == Crop.id)
        .filter(DocumentAssociation.unique_document_id.in_(doc_ids))
        .all()
    )
    states: dict[uuid.UUID, set[str]] = {doc_id: set() for doc_id in doc_ids}
    crops: dict[uuid.UUID, set[str]] = {doc_id: set() for doc_id in doc_ids}
    for doc_id, state_name, crop_name in rows:
        states[doc_id].add(state_name)
        crops[doc_id].add(crop_name)
    return {doc_id: (sorted(states[doc_id]), sorted(crops[doc_id])) for doc_id in doc_ids}


def _unique_document_out(row: UniqueDocument, states: list[str], crops: list[str]) -> UniqueDocumentOut:
    data = {
        name: getattr(row, name) for name in UniqueDocumentOut.model_fields if name not in ("document_id", "states", "crops")
    }
    data["document_id"] = format_display_id(row.display_id)
    data["states"] = states
    data["crops"] = crops
    return UniqueDocumentOut(**data)


# -- Main table (document_associations) --------------------------------------

_ASSOC_FILTERS = {
    "state": lambda v: State.name.ilike(f"%{v}%"),
    "crop": lambda v: Crop.name.ilike(f"%{v}%"),
    "document_id": _display_id_filter(UniqueDocument.display_id),
    "unique_document_id": _uuid_filter(DocumentAssociation.unique_document_id),
    "shareable_name": lambda v: UniqueDocument.shareable_name.ilike(f"%{v}%"),
    "language": lambda v: UniqueDocument.language.ilike(f"%{v}%"),
    "translation_status": lambda v: UniqueDocument.translation_status == v,
    "review_status": lambda v: UniqueDocument.review_status == v,
    "created_at": _date_filter(DocumentAssociation.created_at),
    "updated_at": _date_filter(DocumentAssociation.updated_at),
}


@router.get("/documents", response_model=Paginated[DocumentAssociationOut])
def list_documents(request: Request, db: Session = Depends(get_db)):
    page, page_size = _pagination(request)
    q = (
        db.query(DocumentAssociation)
        .join(State, DocumentAssociation.state_id == State.id)
        .join(Crop, DocumentAssociation.crop_id == Crop.id)
        .join(UniqueDocument, DocumentAssociation.unique_document_id == UniqueDocument.id)
        .options(
            joinedload(DocumentAssociation.state),
            joinedload(DocumentAssociation.crop),
            joinedload(DocumentAssociation.unique_document),
        )
    )
    for clause in _parse_filters(request, _ASSOC_FILTERS):
        q = q.filter(clause)
    total = q.count()
    # Secondary sort by id: created_at alone isn't a stable order once rows
    # tie (e.g. a bulk migration inserts many rows in one transaction, so
    # they all get the identical func.now() value) -- without a tiebreaker,
    # Postgres can return the same row on two different pages, or skip one
    # entirely, since OFFSET/LIMIT over a non-unique ORDER BY is undefined.
    rows = (
        q.order_by(DocumentAssociation.created_at.desc(), DocumentAssociation.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return Paginated(items=[_association_out(r) for r in rows], total=total, page=page, page_size=page_size)


@router.get("/documents/{association_id}", response_model=DocumentAssociationOut)
def get_document(association_id: uuid.UUID, db: Session = Depends(get_db)):
    row = db.get(DocumentAssociation, association_id)
    if row is None:
        raise HTTPException(404, "association not found")
    return _association_out(row)


@router.patch("/documents/{association_id}", response_model=DocumentAssociationOut)
def update_document(association_id: uuid.UUID, body: DocumentAssociationUpdate, db: Session = Depends(get_db)):
    row = db.get(DocumentAssociation, association_id)
    if row is None:
        raise HTTPException(404, "association not found")
    if body.state_id is not None:
        row.state_id = body.state_id
    if body.crop_id is not None:
        row.crop_id = body.crop_id
    db.flush()
    db.refresh(row)
    return _association_out(row)


@router.delete("/documents/{association_id}", status_code=204)
def delete_document(association_id: uuid.UUID, db: Session = Depends(get_db)):
    row = db.get(DocumentAssociation, association_id)
    if row is None:
        raise HTTPException(404, "association not found")
    db.delete(row)


# -- Unique documents ----------------------------------------------------------

_UNIQUE_FILTERS = {
    "document_id": _display_id_filter(UniqueDocument.display_id),
    "advisory_type": lambda v: UniqueDocument.advisory_type.ilike(f"%{v}%"),
    "advisory_scope": lambda v: UniqueDocument.advisory_scope.ilike(f"%{v}%"),
    "season": lambda v: UniqueDocument.season.ilike(f"%{v}%"),
    "edition_revision_volume": lambda v: UniqueDocument.edition_revision_volume.ilike(f"%{v}%"),
    "date_of_release": lambda v: UniqueDocument.date_of_release.ilike(f"%{v}%"),
    "month_of_release": _int_filter(UniqueDocument.month_of_release),
    "year_of_release": _int_filter(UniqueDocument.year_of_release),
    "date_of_collection": lambda v: UniqueDocument.date_of_collection.ilike(f"%{v}%"),
    "month_of_collection": _int_filter(UniqueDocument.month_of_collection),
    "year_of_collection": _int_filter(UniqueDocument.year_of_collection),
    "advisory_name": lambda v: UniqueDocument.advisory_name.ilike(f"%{v}%"),
    "advisory_released_org": lambda v: UniqueDocument.advisory_released_org.ilike(f"%{v}%"),
    "advisory_org_address": lambda v: UniqueDocument.advisory_org_address.ilike(f"%{v}%"),
    "live_source_link": lambda v: UniqueDocument.live_source_link.ilike(f"%{v}%"),
    "shareable_name": lambda v: UniqueDocument.shareable_name.ilike(f"%{v}%"),
    "shareable_link": lambda v: UniqueDocument.shareable_link.ilike(f"%{v}%"),
    "language": lambda v: UniqueDocument.language.ilike(f"%{v}%"),
    "domain": lambda v: UniqueDocument.domain.ilike(f"%{v}%"),
    "format_original": lambda v: UniqueDocument.format_original.ilike(f"%{v}%"),
    "num_pages": _int_filter(UniqueDocument.num_pages),
    "verification_status": lambda v: UniqueDocument.verification_status.ilike(f"%{v}%"),
    "verified_by": lambda v: UniqueDocument.verified_by.ilike(f"%{v}%"),
    "document_status": lambda v: UniqueDocument.document_status.ilike(f"%{v}%"),
    "translation_status": lambda v: UniqueDocument.translation_status == v,
    "review_status": lambda v: UniqueDocument.review_status == v,
    "created_at": _date_filter(UniqueDocument.created_at),
    "updated_at": _date_filter(UniqueDocument.updated_at),
}


@router.get("/unique-documents", response_model=Paginated[UniqueDocumentOut])
def list_unique_documents(request: Request, db: Session = Depends(get_db)):
    page, page_size = _pagination(request)
    q = db.query(UniqueDocument)
    for clause in _parse_filters(request, _UNIQUE_FILTERS):
        q = q.filter(clause)
    total = q.count()
    # Same non-unique-ORDER-BY tiebreaker issue as list_documents above.
    rows = (
        q.order_by(UniqueDocument.created_at.desc(), UniqueDocument.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    states_crops = _states_crops_for_docs(db, [r.id for r in rows])
    items = [_unique_document_out(r, *states_crops.get(r.id, ([], []))) for r in rows]
    return Paginated(items=items, total=total, page=page, page_size=page_size)


@router.get("/unique-documents/{doc_id}", response_model=UniqueDocumentOut)
def get_unique_document(doc_id: uuid.UUID, db: Session = Depends(get_db)):
    row = db.get(UniqueDocument, doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    states, crops = _states_crops_for_docs(db, [doc_id]).get(doc_id, ([], []))
    return _unique_document_out(row, states, crops)


@router.patch("/unique-documents/{doc_id}", response_model=UniqueDocumentOut)
def update_unique_document(doc_id: uuid.UUID, body: UniqueDocumentUpdate, db: Session = Depends(get_db)):
    row = db.get(UniqueDocument, doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    db.flush()
    db.refresh(row)
    states, crops = _states_crops_for_docs(db, [doc_id]).get(doc_id, ([], []))
    return _unique_document_out(row, states, crops)


@router.delete("/unique-documents/{doc_id}", status_code=204)
def delete_unique_document(doc_id: uuid.UUID, db: Session = Depends(get_db)):
    row = db.get(UniqueDocument, doc_id)
    if row is None:
        raise HTTPException(404, "document not found")

    from pop_server import _get_zoho

    wd = _get_zoho()
    for file_id in (row.zoho_file_id, row.translation_zoho_file_id, row.review_zoho_file_id):
        if file_id:
            try:
                wd.delete(file_id)
            except Exception:
                pass  # best-effort -- the DB row (about to be deleted) is the source of truth for existence
    db.delete(row)  # cascades to document_associations via ON DELETE CASCADE, frees its display_id for reuse


@router.delete("/unique-documents/{doc_id}/original", status_code=204)
def delete_original(doc_id: uuid.UUID, db: Session = Depends(get_db)):
    """Delete only the original file (Zoho object + zoho_file_id/
    shareable_link) -- the document row, its metadata, translation, review,
    and state/crop placements are all left untouched."""
    row = db.get(UniqueDocument, doc_id)
    if row is None:
        raise HTTPException(404, "document not found")
    if row.zoho_file_id:
        from pop_server import _get_zoho

        try:
            _get_zoho().delete(row.zoho_file_id)
        except Exception:
            pass
    row.zoho_file_id = None
    row.shareable_link = None


# -- Lookups (states / crops) --------------------------------------------------


@router.get("/states", response_model=list[StateOut])
def list_states(db: Session = Depends(get_db)):
    return db.query(State).order_by(State.name).all()


@router.get("/crops", response_model=list[CropOut])
def list_crops(db: Session = Depends(get_db)):
    return db.query(Crop).order_by(Crop.name).all()


@router.get("/languages", response_model=list[LanguageOut])
def list_languages():
    """Fixed list, not DB-backed -- one entry per tessdata_best OCR model we
    actually have installed (see dashboard.dedup.LANGUAGES). Populate the
    upload form's required `language` dropdown from this, not a hardcoded
    frontend list, so it always matches what OCR can actually run."""
    return [LanguageOut(code=code, label=label) for code, label in sorted(LANGUAGES.items(), key=lambda kv: kv[1])]


@router.post("/crops", response_model=CropOut, status_code=201)
def create_crop(body: CropCreate, db: Session = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "crop name cannot be empty")
    existing = db.query(Crop).filter(Crop.name.ilike(name)).first()
    if existing:
        return existing
    crop = Crop(name=name)
    db.add(crop)
    db.flush()
    db.refresh(crop)
    return crop
