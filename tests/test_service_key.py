"""The shared-secret gate (helpers/service_key.py).

Unlike the rest of this suite these tests start no server: the gate lives in its
own module, so it is exercised on a tiny throwaway app with Starlette's
TestClient and `pop_server` (with its module-level side effects) is never
imported. The wiring in pop_server.py is covered by the live check in the
deployment notes, not here.
"""
import pytest
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from helpers.service_key import HEADER, ServiceKeyMiddleware, configured_keys

KEY = "k" * 32
OTHER = "o" * 40


def _app(*keys: str, cors: bool = True) -> FastAPI:
    app = FastAPI()

    @app.get("/data")
    def data():
        return {"ok": True}

    @app.get("/stream")
    def stream():
        return StreamingResponse((f"{i}\n".encode() for i in range(3)), media_type="text/plain")

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text("hi")
        await socket.close()

    app.add_middleware(ServiceKeyMiddleware, keys=tuple(k.encode() for k in keys))
    if cors:  # same order as pop_server.py: the gate first, so CORS is the outer layer
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    return app


# -- configuration ---------------------------------------------------------------

def test_no_key_configured_means_no_gate():
    assert configured_keys({}) == ()
    assert configured_keys({"POP_SERVICE_KEY": ""}) == ()
    assert configured_keys({"POP_SERVICE_KEY": " , "}) == ()


def test_keys_are_split_on_commas_and_trimmed():
    assert configured_keys({"POP_SERVICE_KEY": f" {KEY} , {OTHER}"}) == (KEY.encode(), OTHER.encode())


@pytest.mark.parametrize("bad", ["test", "k" * 31, f"{KEY},short", "é" * 40])
def test_a_weak_or_non_ascii_key_stops_startup(bad):
    with pytest.raises(ValueError):
        configured_keys({"POP_SERVICE_KEY": bad})


def test_the_middleware_refuses_to_exist_without_a_key():
    with pytest.raises(ValueError):
        ServiceKeyMiddleware(FastAPI(), keys=())


# -- the gate --------------------------------------------------------------------

def test_a_request_with_the_key_goes_through():
    assert TestClient(_app(KEY)).get("/data", headers={HEADER: KEY}).json() == {"ok": True}


@pytest.mark.parametrize("headers", [{}, {HEADER: ""}, {HEADER: "x" * 32}, {HEADER: KEY[:-1]}, {HEADER: KEY + "k"}])
def test_a_missing_or_wrong_key_is_a_401(headers):
    r = TestClient(_app(KEY)).get("/data", headers=headers)
    assert r.status_code == 401
    assert r.json() == {"detail": f"a valid {HEADER} header is required"}


def test_missing_and_wrong_answer_identically():
    c = TestClient(_app(KEY))
    assert c.get("/data").content == c.get("/data", headers={HEADER: "x" * 32}).content


def test_header_name_is_case_insensitive():
    assert TestClient(_app(KEY)).get("/data", headers={"x-SERVICE-key": KEY}).status_code == 200


def test_every_route_is_gated_including_docs_and_unknown_paths():
    c = TestClient(_app(KEY))
    for path in ("/openapi.json", "/docs", "/data", "/no/such/route"):
        assert c.get(path).status_code == 401, path


def test_every_method_is_gated():
    c = TestClient(_app(KEY))
    for method in ("post", "put", "patch", "delete"):
        assert getattr(c, method)("/data").status_code == 401, method


def test_any_one_of_several_keys_is_accepted():
    c = TestClient(_app(KEY, OTHER))
    assert c.get("/data", headers={HEADER: KEY}).status_code == 200
    assert c.get("/data", headers={HEADER: OTHER}).status_code == 200
    assert c.get("/data", headers={HEADER: "x" * 32}).status_code == 401


def test_a_streamed_response_is_not_buffered_or_altered():
    r = TestClient(_app(KEY)).get("/stream", headers={HEADER: KEY})
    assert r.status_code == 200 and r.text == "0\n1\n2\n"


# -- CORS is left exactly as it was ------------------------------------------------

def test_a_cors_preflight_is_answered_without_the_key():
    r = TestClient(_app(KEY)).options(
        "/data", headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"


def test_a_rejection_still_carries_the_cors_headers():
    r = TestClient(_app(KEY)).get("/data", headers={"Origin": "https://example.com"})
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == "*"


# -- websockets ------------------------------------------------------------------

def test_a_websocket_needs_the_key_too():
    c = TestClient(_app(KEY))
    with c.websocket_connect("/ws", headers={HEADER: KEY}) as ws:
        assert ws.receive_text() == "hi"
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws"):
            pass
