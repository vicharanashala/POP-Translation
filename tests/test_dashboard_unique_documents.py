import uuid

UNIQUE_DOC_FIELDS = {
    "id", "document_id", "sha256", "shareable_name", "shareable_link", "states", "crops",
    "language", "format_original", "num_pages", "translation_status", "translation_shareable_link",
    "review_status", "review_shareable_link", "created_at", "updated_at",
}


def test_list_unique_documents_paginated(client):
    resp = client.get("/dashboard/unique-documents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1
    assert body["page_size"] == 100
    assert len(body["items"]) == 100
    # pops.csv migration: 7513 unique documents at load time.
    assert body["total"] >= 7000

    row = body["items"][0]
    assert UNIQUE_DOC_FIELDS <= set(row.keys())
    assert "zoho_file_id" not in row  # internal Zoho id -- shareable_link is the public equivalent
    uuid.UUID(row["id"])
    assert len(row["sha256"]) == 64  # sha256 hex digest
    assert row["document_id"].startswith("ANNAM_")


def test_filter_unique_documents_translation_status_done(client):
    resp = client.get("/dashboard/unique-documents", params={"filter[translation_status]": "done", "page_size": 200})
    assert resp.status_code == 200
    body = resp.json()
    # 104 documents had a translation attached at migration time (see
    # scripts/build_pops_csv.py's run log) -- only grows from here as more
    # get translated.
    assert body["total"] >= 104
    for row in body["items"]:
        assert row["translation_status"] == "done"
        assert row["translation_shareable_link"]


def test_filter_unique_documents_review_status_done(client):
    resp = client.get("/dashboard/unique-documents", params={"filter[review_status]": "done"})
    assert resp.status_code == 200
    body = resp.json()
    # 87 documents had a review attached at migration time.
    assert body["total"] >= 87
    for row in body["items"]:
        assert row["review_status"] == "done"
        assert row["review_shareable_link"]


def test_filter_unique_documents_by_language(client):
    resp = client.get("/dashboard/unique-documents", params={"filter[language]": "Non-English", "page_size": 200})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    for row in body["items"]:
        assert row["language"] == "Non-English"


def test_get_unique_document_by_id(client):
    listed = client.get("/dashboard/unique-documents").json()["items"][0]
    resp = client.get(f"/dashboard/unique-documents/{listed['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == listed["id"]
    assert body["sha256"] == listed["sha256"]
    # Manual-metadata fields (blank for migrated rows, editable later via
    # the dashboard) should still be present in the response shape.
    assert "advisory_name" in body


def test_get_unique_document_404_for_random_id(client):
    resp = client.get(f"/dashboard/unique-documents/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_filter_unique_documents_by_document_id(client):
    listed = client.get("/dashboard/unique-documents").json()["items"][0]
    resp = client.get("/dashboard/unique-documents", params={"filter[document_id]": listed["document_id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == listed["id"]


def test_filter_unique_documents_by_num_pages(client):
    resp = client.get("/dashboard/unique-documents", params={"filter[num_pages]": "4", "page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    for row in body["items"]:
        assert row["num_pages"] == 4


def test_filter_unique_documents_by_state(client):
    resp = client.get("/dashboard/unique-documents", params={"filter[state]": "Karnataka", "page_size": 200})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    for row in body["items"]:
        assert any("Karnataka" in s for s in row["states"])


def test_filter_unique_documents_by_crop(client):
    resp = client.get("/dashboard/unique-documents", params={"filter[crop]": "Onion", "page_size": 200})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    for row in body["items"]:
        assert any("Onion" in c for c in row["crops"])


def test_filter_unique_documents_by_state_and_crop(client):
    # Both filters apply to the same document_associations row (not
    # independently across a document's different placements) -- a document
    # placed under State X / Crop A and, separately, State Y / Crop B must
    # NOT match filter[state]=Y&filter[crop]=A.
    resp = client.get(
        "/dashboard/unique-documents",
        params={"filter[state]": "Karnataka", "filter[crop]": "Onion", "page_size": 200},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    for row in body["items"]:
        assert any("Karnataka" in s for s in row["states"])
        assert any("Onion" in c for c in row["crops"])


def test_filter_unique_documents_bad_values_dont_500(client):
    for params in (
        {"filter[num_pages]": "not-a-number"},
        {"filter[document_id]": "not-annam"},
        {"filter[created_at]": "not-a-date"},
    ):
        resp = client.get("/dashboard/unique-documents", params=params)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
