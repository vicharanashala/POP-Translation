"""Read-only check of the upload queue listing. Deliberately does NOT POST a
real upload here -- that enqueues a real hash/embed/dedup-check/Zoho-upload
job (dashboard.queue_worker), which is exactly the kind of side-effecting
action meant to be checked manually rather than by an automated test run
before manual QA. add/new/cancel are only exercised against a nonexistent
id for the same reason -- resolving a real pending duplicate is a manual QA
action, not something an automated run should do to shared queue state.
"""

import uuid


def test_list_uploads_returns_a_list(client):
    resp = client.get("/dashboard/uploads")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    for item in body:
        assert "id" in item
        assert "status" in item
        assert "filename" in item
        assert "num_pages" in item
        assert "language" in item
        assert "metadata_payload" in item


def test_upload_requires_exactly_one_state(client):
    resp = client.post(
        "/dashboard/uploads",
        data={
            "states_json": '["State Karnataka", "State Kerala"]',
            "crops_json": '["Onion"]',
            "language": "kan",
        },
        files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert resp.status_code == 400


def test_upload_requires_at_least_one_crop(client):
    resp = client.post(
        "/dashboard/uploads",
        data={
            "states_json": '["State Karnataka"]',
            "crops_json": "[]",
            "language": "kan",
        },
        files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert resp.status_code == 400


def test_upload_requires_language(client):
    resp = client.post(
        "/dashboard/uploads",
        data={
            "states_json": '["State Karnataka"]',
            "crops_json": '["Onion"]',
        },
        files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert resp.status_code == 422  # missing required Form field


def test_upload_rejects_unknown_language(client):
    resp = client.post(
        "/dashboard/uploads",
        data={
            "states_json": '["State Karnataka"]',
            "crops_json": '["Onion"]',
            "language": "xx-not-a-real-language",
        },
        files={"file": ("test.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert resp.status_code == 400


def test_list_languages_returns_known_codes(client):
    resp = client.get("/dashboard/languages")
    assert resp.status_code == 200
    body = resp.json()
    codes = {row["code"] for row in body}
    assert "kan" in codes and "eng" in codes


def test_add_upload_404_for_random_id(client):
    resp = client.post(f"/dashboard/uploads/{uuid.uuid4()}/add")
    assert resp.status_code == 404


def test_new_upload_404_for_random_id(client):
    resp = client.post(f"/dashboard/uploads/{uuid.uuid4()}/new")
    assert resp.status_code == 404


def test_cancel_upload_duplicate_404_for_random_id(client):
    resp = client.post(f"/dashboard/uploads/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404
