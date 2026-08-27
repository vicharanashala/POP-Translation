def test_root_health(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "service": "pop-server"}


def test_dashboard_config(client):
    resp = client.get("/dashboard/config")
    assert resp.status_code == 200
    body = resp.json()
    assert "translation_available" in body
    assert isinstance(body["translation_available"], bool)
