"""Write-path coverage for the MongoDB backend.

The rest of tests/ is largely read-only -- listing, filtering, 404/422 shapes.
These exercise the paths the Postgres -> MongoDB migration actually changed the
most: in-place updates, the delete fan-out that replaced ON DELETE CASCADE /
SET NULL, and display_id allocation and reuse.

Seed data is created and removed directly through pymongo (there is no HTTP
endpoint that creates a document without running OCR), while the behaviour
under test is driven over HTTP like every other test here. Everything is
namespaced with a recognisable sha256/filename prefix and torn down in a
fixture, so a failed run leaves nothing behind that a later run would trip on.
"""
from __future__ import annotations

import pytest
from bson import ObjectId

from dashboard.db import get_database
from dashboard.display_id import allocate_display_id, format_display_id
from dashboard.models import new_document_association, new_translation_job, new_unique_document
from dashboard.models import TranslationJobKind

# Distinctive so seeded rows are obvious in the database and easy to clean up.
_SHA_PREFIX = "feedfacecafe"


@pytest.fixture
def db():
    return get_database()


@pytest.fixture
def seeded(db):
    """A document with two placements, a translation job and a queue item
    pointing at it -- i.e. one of every reference the delete fan-out has to
    clean up."""
    state = db.states.find_one()
    crops = list(db.crops.find().limit(2))
    assert state and len(crops) == 2, "migration data missing -- run migrate_from_pops_csv first"

    sha = _SHA_PREFIX + "0" * (64 - len(_SHA_PREFIX))
    doc = new_unique_document(
        sha256=sha,
        display_id=allocate_display_id(db),
        shareable_name="write-test.pdf",
        language="kan",
    )
    doc["_id"] = db.unique_documents.insert_one(doc).inserted_id

    for crop in crops:
        db.document_associations.insert_one(
            new_document_association(unique_document_id=doc["_id"], state_id=state["_id"], crop_id=crop["_id"])
        )
    db.translation_jobs.insert_one(
        new_translation_job(unique_document_id=doc["_id"], kind=TranslationJobKind.translate)
    )
    queue_id = db.upload_queue_items.insert_one(
        {
            "filename": "write-test-queue.pdf",
            "status": "awaiting_review",
            "progress_pct": 100,
            "duplicate_of_id": doc["_id"],
            "similarity_score": 0.99,
            "match_type": "embedding",
            "error_message": None,
            "note": "seeded by tests",
            "num_pages": 1,
            "language": "kan",
            "metadata_payload": {},
            "created_at": doc["created_at"],
            "updated_at": doc["updated_at"],
        }
    ).inserted_id

    yield {"doc": doc, "state": state, "crops": crops, "queue_id": queue_id}

    db.document_associations.delete_many({"unique_document_id": doc["_id"]})
    db.translation_jobs.delete_many({"unique_document_id": doc["_id"]})
    db.upload_queue_items.delete_many({"_id": queue_id})
    db.unique_documents.delete_many({"sha256": sha})


def test_patch_unique_document_persists_metadata(client, seeded):
    doc_id = str(seeded["doc"]["_id"])
    resp = client.patch(
        f"/dashboard/unique-documents/{doc_id}",
        json={"advisory_type": "Write Test", "year_of_release": 2026},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["advisory_type"] == "Write Test"
    assert body["year_of_release"] == 2026
    # Round-trips through a fresh GET, not just the PATCH response.
    assert client.get(f"/dashboard/unique-documents/{doc_id}").json()["advisory_type"] == "Write Test"


def test_patch_unique_document_leaves_unsent_fields_alone(client, seeded):
    doc_id = str(seeded["doc"]["_id"])
    client.patch(f"/dashboard/unique-documents/{doc_id}", json={"season": "Kharif"})
    client.patch(f"/dashboard/unique-documents/{doc_id}", json={"domain": "Agriculture"})
    body = client.get(f"/dashboard/unique-documents/{doc_id}").json()
    assert body["season"] == "Kharif" and body["domain"] == "Agriculture"


def test_patch_association_changes_crop(client, db, seeded):
    assoc = db.document_associations.find_one({"unique_document_id": seeded["doc"]["_id"]})
    other_crop = next(c for c in seeded["crops"] if c["_id"] != assoc["crop_id"])
    # That pairing is already taken by the second seeded placement, so this is
    # also a check that the unique index is enforced rather than silently
    # creating a duplicate placement.
    resp = client.patch(f"/dashboard/documents/{assoc['_id']}", json={"crop_id": other_crop["_id"]})
    assert resp.status_code == 409

    fresh = db.crops.find_one({"_id": {"$nin": [c["_id"] for c in seeded["crops"]]}})
    resp = client.patch(f"/dashboard/documents/{assoc['_id']}", json={"crop_id": fresh["_id"]})
    assert resp.status_code == 200
    assert resp.json()["crop"]["id"] == fresh["_id"]
    assert resp.json()["crop"]["name"] == fresh["name"]


def test_unique_document_lists_its_states_and_crops(client, seeded):
    body = client.get(f"/dashboard/unique-documents/{seeded['doc']['_id']}").json()
    assert body["states"] == [seeded["state"]["name"]]
    assert sorted(body["crops"]) == sorted(c["name"] for c in seeded["crops"])


def test_document_id_is_the_annam_form(client, seeded):
    body = client.get(f"/dashboard/unique-documents/{seeded['doc']['_id']}").json()
    assert body["document_id"] == format_display_id(seeded["doc"]["display_id"])


def test_delete_cascades_to_placements_jobs_and_queue(client, db, seeded):
    doc_id = seeded["doc"]["_id"]
    resp = client.delete(f"/dashboard/unique-documents/{doc_id}")
    assert resp.status_code == 204

    # Replaces Postgres ON DELETE CASCADE ...
    assert db.unique_documents.count_documents({"_id": doc_id}) == 0
    assert db.document_associations.count_documents({"unique_document_id": doc_id}) == 0
    assert db.translation_jobs.count_documents({"unique_document_id": doc_id}) == 0
    # ... and ON DELETE SET NULL.
    queue_item = db.upload_queue_items.find_one({"_id": seeded["queue_id"]})
    assert queue_item is not None, "queue item must survive its matched document"
    assert queue_item["duplicate_of_id"] is None


def test_deleted_display_id_is_reused(client, db, seeded):
    freed = seeded["doc"]["display_id"]
    client.delete(f"/dashboard/unique-documents/{seeded['doc']['_id']}")
    # Postgres allocated the smallest free number and freed it on delete; the
    # MongoDB allocator has to behave identically.
    assert allocate_display_id(db) == freed


def test_create_crop_is_case_insensitively_idempotent(client, db):
    name = "Zzz Write Test Crop"
    try:
        first = client.post("/dashboard/crops", json={"name": name})
        assert first.status_code == 201
        crop_id = first.json()["id"]
        assert isinstance(crop_id, int), "crop ids stay integers so the frontend contract is unchanged"

        again = client.post("/dashboard/crops", json={"name": name.upper()})
        assert again.status_code in (200, 201)
        assert again.json()["id"] == crop_id, "differing only by case must return the existing crop"
    finally:
        db.crops.delete_many({"name": {"$in": [name, name.upper()]}})


def test_create_crop_rejects_blank(client):
    assert client.post("/dashboard/crops", json={"name": "   "}).status_code == 400


def test_delete_association_removes_only_that_placement(client, db, seeded):
    assocs = list(db.document_associations.find({"unique_document_id": seeded["doc"]["_id"]}))
    assert len(assocs) == 2
    resp = client.delete(f"/dashboard/documents/{assocs[0]['_id']}")
    assert resp.status_code == 204
    remaining = list(db.document_associations.find({"unique_document_id": seeded["doc"]["_id"]}))
    assert len(remaining) == 1 and remaining[0]["_id"] == assocs[1]["_id"]
    # The document itself is untouched.
    assert client.get(f"/dashboard/unique-documents/{seeded['doc']['_id']}").status_code == 200


def test_association_ids_are_object_ids(client):
    row = client.get("/dashboard/documents").json()["items"][0]
    assert ObjectId.is_valid(row["id"])
    assert ObjectId.is_valid(row["unique_document_id"])
