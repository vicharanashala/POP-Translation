"""Name standardisation: states, crops, and the language fields.

Covers the invariants scripts/standardize_names.py established and the code
paths that keep the catalogue from drifting back out of shape -- crop names are
normalised on every write, unknown crops are created rather than dropped, and a
crop that loses its last placement is removed.
"""
from __future__ import annotations

import pytest

from dashboard.db import get_database
from dashboard.models import normalize_crop_name, normalize_state_name


@pytest.fixture
def db():
    return get_database()


# -- pure normalisation ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("State Karnataka", "Karnataka"),
        ("State  Jammu and Kashmir", "Jammu and Kashmir"),  # double space in source data
        ("Central Advisories", "Central"),
        ("Kerala", "Kerala"),
    ],
)
def test_normalize_state_name(raw, expected):
    assert normalize_state_name(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Black gram", "Black Gram"),
        ("carrot", "Carrot"),
        ("ChickPea", "Chickpea"),
        ("Bengal gram (Chickpea)", "Bengal Gram (Chickpea)"),
        # An acronym inside a mixed-case name is preserved ...
        ("Research Institute (ATARI), Hyderabad", "Research Institute (ATARI), Hyderabad"),
        # ... but a wholly upper-case name is shouting, not an acronym, so short
        # words like ALL/ON must not survive as though they were.
        ("ALL INDIA NETWORK PROJECT ON PEST MANAGEMENT", "All India Network Project On Pest Management"),
    ],
)
def test_normalize_crop_name(raw, expected):
    assert normalize_crop_name(raw) == expected


def test_normalize_crop_name_is_idempotent(db):
    for crop in db.crops.find({}, {"name": 1}).limit(200):
        assert normalize_crop_name(crop["name"]) == crop["name"]


# -- the catalogue itself ----------------------------------------------------


def test_states_carry_no_noise_words(client):
    names = [s["name"] for s in client.get("/dashboard/states").json()]
    assert names, "states endpoint returned nothing"
    for name in names:
        assert not name.lower().startswith("state "), name
        assert "advisories" not in name.lower(), name
        assert "  " not in name, f"double space in {name!r}"


def test_crops_are_title_case_and_unique_case_insensitively(client):
    names = [c["name"] for c in client.get("/dashboard/crops").json()]
    assert names
    lowered = [n.lower() for n in names]
    assert len(set(lowered)) == len(lowered), "case-only duplicate crop names remain"
    for name in names:
        assert normalize_crop_name(name) == name, f"{name!r} is not in canonical form"


def test_crop_ids_are_integers(client):
    for crop in client.get("/dashboard/crops").json()[:5]:
        assert isinstance(crop["id"], int)
    for state in client.get("/dashboard/states").json()[:5]:
        assert isinstance(state["id"], int)


# -- write paths keep it that way --------------------------------------------


def test_create_crop_normalises_case(client, db):
    try:
        resp = client.post("/dashboard/crops", json={"name": "zzz test PUMPKIN gourd"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "Zzz Test Pumpkin Gourd"
    finally:
        db.crops.delete_many({"name": "Zzz Test Pumpkin Gourd"})


def test_create_crop_matches_existing_regardless_of_case(client, db):
    existing = client.get("/dashboard/crops").json()[0]
    resp = client.post("/dashboard/crops", json={"name": existing["name"].lower()})
    assert resp.status_code in (200, 201)
    assert resp.json()["id"] == existing["id"], "should return the existing crop, not create a variant"


def test_orphaned_crop_is_removed_when_its_last_placement_goes(client, db):
    """Crops are created on demand by uploads, so a typo would otherwise live
    forever. The last placement leaving must take the crop with it."""
    from bson import ObjectId

    from dashboard.display_id import allocate_display_id
    from dashboard.models import new_document_association, new_unique_document

    sha = "beef" + "0" * 60
    crop_name = "Zzz Orphan Test Crop"
    db.crops.delete_many({"name": crop_name})
    db.unique_documents.delete_many({"sha256": sha})

    doc = new_unique_document(sha256=sha, display_id=allocate_display_id(db), shareable_name="orphan.pdf")
    doc["_id"] = db.unique_documents.insert_one(doc).inserted_id
    crop_id = client.post("/dashboard/crops", json={"name": crop_name}).json()["id"]
    state_id = db.states.find_one()["_id"]
    assoc_id = db.document_associations.insert_one(
        new_document_association(unique_document_id=doc["_id"], state_id=state_id, crop_id=crop_id)
    ).inserted_id
    try:
        assert db.crops.count_documents({"_id": crop_id}) == 1
        assert client.delete(f"/dashboard/documents/{assoc_id}").status_code == 204
        assert db.crops.count_documents({"_id": crop_id}) == 0, "orphaned crop should have been pruned"
    finally:
        db.document_associations.delete_many({"_id": ObjectId(str(assoc_id))})
        db.unique_documents.delete_many({"sha256": sha})
        db.crops.delete_many({"name": crop_name})


def test_in_use_crop_survives_a_placement_delete(client, db):
    """The mirror of the above: pruning must only remove crops nothing else
    references."""
    from dashboard.display_id import allocate_display_id
    from dashboard.models import new_document_association, new_unique_document

    busy = next(
        c for c in db.crops.find({}, {"_id": 1, "name": 1})
        if db.document_associations.count_documents({"crop_id": c["_id"]}) >= 2
    )
    sha = "cafe" + "0" * 60
    db.unique_documents.delete_many({"sha256": sha})
    doc = new_unique_document(sha256=sha, display_id=allocate_display_id(db), shareable_name="busy.pdf")
    doc["_id"] = db.unique_documents.insert_one(doc).inserted_id
    assoc_id = db.document_associations.insert_one(
        new_document_association(
            unique_document_id=doc["_id"], state_id=db.states.find_one()["_id"], crop_id=busy["_id"]
        )
    ).inserted_id
    try:
        assert client.delete(f"/dashboard/documents/{assoc_id}").status_code == 204
        assert db.crops.count_documents({"_id": busy["_id"]}) == 1, "in-use crop must not be pruned"
    finally:
        db.document_associations.delete_many({"_id": assoc_id})
        db.unique_documents.delete_many({"sha256": sha})


# -- language ----------------------------------------------------------------


def test_languages_endpoint_is_the_tessdata_code_list(client):
    """The language config lives in code (dashboard/dedup.py), not the
    database, because it must match the tessdata_best models actually installed
    on disk. The frontend populates its dropdown from this endpoint."""
    from dashboard.dedup import LANGUAGES

    body = client.get("/dashboard/languages").json()
    assert {row["code"] for row in body} == set(LANGUAGES)
    assert all(row["label"] for row in body)


def test_unique_document_exposes_both_language_fields(client):
    row = client.get("/dashboard/unique-documents").json()["items"][0]
    assert "language" in row, "detected language"
    assert "ocr_language" in row, "tessdata code chosen at upload"
