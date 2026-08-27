"""Read-only checks of the translation queue. Deliberately does NOT POST
/translate for a real document here -- that kicks off a real, costly LLM
translation job, exactly the kind of side-effecting action meant to be
checked manually rather than by an automated test run.
"""

import uuid


def test_list_translation_jobs_returns_a_list(client):
    resp = client.get("/dashboard/translation-jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    for row in body:
        assert row["status"] in ("queued", "running")  # default view is active jobs only
        assert "pages_done" in row
        assert "total_pages" in row
        assert row["document_id"].startswith("ANNAM_")


def test_list_translation_jobs_by_status(client):
    resp = client.get("/dashboard/translation-jobs", params={"status": "done"})
    assert resp.status_code == 200
    body = resp.json()
    for row in body:
        assert row["status"] == "done"


def test_cancel_translation_job_404_for_random_id(client):
    resp = client.post(f"/dashboard/translation-jobs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404
