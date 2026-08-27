def test_list_states(client):
    resp = client.get("/dashboard/states")
    assert resp.status_code == 200
    states = resp.json()
    assert isinstance(states, list)
    # 32 states from STATE_LANG + "Central Advisories", seeded by the
    # pops.csv/main.csv migration -- fixed enumeration, shouldn't drift.
    assert len(states) == 33
    names = {s["name"] for s in states}
    assert "Central Advisories" in names
    assert "State Karnataka" in names
    for s in states:
        assert isinstance(s["id"], int)
        assert s["name"]


def test_list_crops(client):
    resp = client.get("/dashboard/crops")
    assert resp.status_code == 200
    crops = resp.json()
    assert isinstance(crops, list)
    # Grows over time as new documents/crops get added -- just check it's
    # populated at a sane floor (493 at the time pops.csv/main.csv were built).
    assert len(crops) >= 400
    for c in crops:
        assert isinstance(c["id"], int)
        assert c["name"]


def test_create_crop_is_idempotent(client):
    """POST /crops should not create a duplicate for an existing name
    (case-insensitive) -- read-only from the caller's perspective."""
    before = client.get("/dashboard/crops").json()
    existing_name = before[0]["name"]

    resp = client.post("/dashboard/crops", json={"name": existing_name.upper()})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == existing_name  # returns the existing row, not a new one

    after = client.get("/dashboard/crops").json()
    assert len(after) == len(before)
