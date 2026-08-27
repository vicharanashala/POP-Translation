"""Shared fixtures for the (live-server) integration tests in tests/.

These tests hit a REAL running pop_server.py instance over HTTP -- they do
not use FastAPI's TestClient / import the app in-process, since pop_server's
module-level side effects (Zoho singleton, thread pools, dashboard DB init
via lifespan) make importing it directly awkward, and the actual deployed
shape (docker-compose's `pipeline` service) already IS "uvicorn pop_server:app"
as a separate process. Start the server yourself first:

    .venv/bin/uvicorn pop_server:app --host 0.0.0.0 --port 8032

then run:

    .venv/bin/python3 -m pytest tests/ -v

If the server isn't reachable, every test using the `client` fixture is
skipped (not failed) with a clear reason.
"""
from __future__ import annotations

import httpx
import pytest

BASE_URL = "http://localhost:8032"


@pytest.fixture(scope="session")
def client():
    c = httpx.Client(base_url=BASE_URL, timeout=30.0)
    try:
        resp = c.get("/")
        if resp.status_code != 200:
            pytest.skip(f"pop_server not healthy at {BASE_URL} (status {resp.status_code})")
    except httpx.ConnectError:
        pytest.skip(f"pop_server not running at {BASE_URL} -- start it first (see conftest.py docstring)")
    yield c
    c.close()
