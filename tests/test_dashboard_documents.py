import uuid

ASSOC_FIELDS = {
    "id", "unique_document_id", "document_id", "state", "crop", "shareable_name", "language",
    "translation_status", "review_status", "created_at", "updated_at",
}


def test_list_documents_paginated(client):
    resp = client.get("/dashboard/documents")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"items", "total", "page", "page_size"}
    assert body["page"] == 1
    assert body["page_size"] == 100
    assert len(body["items"]) == 100
    # document_associations, from main.csv -- 8571 at migration time, only grows.
    assert body["total"] >= 8000

    row = body["items"][0]
    assert ASSOC_FIELDS <= set(row.keys())
    assert row["state"]["name"]
    assert row["crop"]["name"]
    uuid.UUID(row["id"])  # well-formed uuid
    uuid.UUID(row["unique_document_id"])
    assert row["document_id"].startswith("ANNAM_")


def test_list_documents_page_2_is_different(client):
    page1 = client.get("/dashboard/documents", params={"page": 1}).json()["items"]
    page2 = client.get("/dashboard/documents", params={"page": 2}).json()["items"]
    ids_1 = {r["id"] for r in page1}
    ids_2 = {r["id"] for r in page2}
    assert ids_1.isdisjoint(ids_2)


def test_filter_documents_by_state(client):
    resp = client.get("/dashboard/documents", params={"filter[state]": "Karnataka"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    for row in body["items"]:
        assert "karnataka" in row["state"]["name"].lower()


def test_filter_documents_by_unknown_state_returns_empty(client):
    resp = client.get("/dashboard/documents", params={"filter[state]": "Atlantis"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_get_document_by_id(client):
    listed = client.get("/dashboard/documents").json()["items"][0]
    resp = client.get(f"/dashboard/documents/{listed['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == listed["id"]
    assert body["unique_document_id"] == listed["unique_document_id"]


def test_get_document_404_for_random_id(client):
    resp = client.get(f"/dashboard/documents/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_get_document_422_for_malformed_id(client):
    resp = client.get("/dashboard/documents/not-a-uuid")
    assert resp.status_code == 422


def test_filter_documents_by_document_id(client):
    listed = client.get("/dashboard/documents").json()["items"][0]
    resp = client.get("/dashboard/documents", params={"filter[document_id]": listed["document_id"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    for row in body["items"]:
        assert row["document_id"] == listed["document_id"]


def test_filter_documents_bad_values_dont_500(client):
    for params in (
        {"filter[document_id]": "not-annam"},
        {"filter[unique_document_id]": "not-a-uuid"},
        {"filter[created_at]": "not-a-date"},
    ):
        resp = client.get("/dashboard/documents", params=params)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


def test_delete_original_404_for_random_id(client):
    resp = client.delete(f"/dashboard/unique-documents/{uuid.uuid4()}/original")
    assert resp.status_code == 404
