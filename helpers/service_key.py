"""A shared-secret gate in front of the whole server.

WHY. The Cloud Run URL is public, and nothing behind it asked who was calling:
the dashboard's own identity (`X-User-Id` / `X-User-Role`, dashboard/access.py)
is taken from headers and never verified, and the catalogue's read routes need
no identity at all. The only legitimate callers are the frontend's backend --
its `/api/pop` reverse proxy and its upload forwarder -- so the first layer is
simply that: every request must carry a secret only those two services hold.
Anyone else, including a browser pointed straight at this URL, is turned away
before a single route runs.

    X-Service-Key: <the secret>

WHAT THIS IS NOT. It says nothing about WHICH USER is calling; that is the
frontend backend's job (verify the Firebase token, then set the identity
headers itself). It is also not CORS: a browser's cross-origin rules are left
exactly as they were. And it does not make an unauthenticated request cheaper
for an attacker than it was -- it is one header comparison -- so rate limiting
belongs in front of the service (Cloud Armor), not here.

CONFIGURATION. One variable, `POP_SERVICE_KEY`.

  * UNSET or empty: no gate. Requests are accepted as before, and a warning is
    logged at startup. This is deliberate -- it is the local and test default,
    and it is what lets the frontend backend be deployed sending the header
    BEFORE this server starts requiring it.
  * SET: every HTTP request (and websocket) needs a matching header, or the
    answer is 401 with a fixed body that does not say whether the key was
    missing or wrong.
  * Comma-separated means "any of these", so a key can be rotated with no
    downtime: add the new one next to the old, move the callers over, then drop
    the old one.

A key that is set but too short (under 32 characters) or not ASCII stops the
server at startup instead of being accepted. A gate that quietly accepts
`POP_SERVICE_KEY=test` is worse than no gate, because everyone believes it is
there. Generate one with `openssl rand -hex 32`.

The comparison is constant-time (`hmac.compare_digest`) against every
configured key, with no early exit, so response timing says nothing about how
much of a guess was right.

PLACEMENT. Added to the app BEFORE the CORS middleware, which makes CORS the
outer layer: a browser's preflight (OPTIONS, which by design carries no custom
headers) is answered by CORS as it always was, and a 401 from this gate still
carries the CORS headers, so a frontend developer sees the real status instead
of an opaque network error. A pure ASGI middleware rather than
`BaseHTTPMiddleware`, because the latter buffers and wraps the body and this
server streams uploads, downloads and server-sent events.
"""
from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Mapping

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ENV_VAR = "POP_SERVICE_KEY"
HEADER = "X-Service-Key"
MIN_KEY_LENGTH = 32

_HEADER_BYTES = HEADER.lower().encode("latin-1")
_REJECTION = {"detail": f"a valid {HEADER} header is required"}

log = logging.getLogger("pop.service_key")


def configured_keys(environ: Mapping[str, str] | None = None) -> tuple[bytes, ...]:
    """The accepted keys from `POP_SERVICE_KEY`, or () when the gate is off.

    Raises ValueError for a key that is too short or not ASCII -- see the module
    docstring for why that is a startup failure rather than a warning."""
    raw = (os.environ if environ is None else environ).get(ENV_VAR, "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    for key in keys:
        if len(key) < MIN_KEY_LENGTH or not key.isascii():
            raise ValueError(
                f"{ENV_VAR} must hold ASCII keys of at least {MIN_KEY_LENGTH} characters "
                f"(comma-separated for more than one); generate one with `openssl rand -hex 32`")
    return tuple(k.encode("ascii") for k in keys)


class ServiceKeyMiddleware:
    """Reject every request that does not carry one of `keys`."""

    def __init__(self, app: ASGIApp, keys: tuple[bytes, ...]) -> None:
        if not keys:
            raise ValueError("ServiceKeyMiddleware needs at least one key; leave it out to run without the gate")
        self.app = app
        self._keys = keys

    def _accepts(self, presented: bytes) -> bool:
        # No `any()`: it would stop at the first match and make the time taken
        # depend on which key was used.
        ok = False
        for key in self._keys:
            ok |= hmac.compare_digest(presented, key)
        return ok

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)  # lifespan
            return
        presented = next((v for k, v in scope["headers"] if k == _HEADER_BYTES), b"")
        if self._accepts(presented):
            await self.app(scope, receive, send)
            return
        # Method and path only: the query string can carry tokens, and the
        # rejected key itself is never logged.
        client = dict(scope["headers"]).get(b"x-forwarded-for", b"").decode("latin-1")[:100] \
            or (scope.get("client") or ("?",))[0]
        log.warning("rejected %s %s from %s: missing or wrong %s",
                    scope.get("method", scope["type"]), scope.get("path", ""), client, HEADER)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse(_REJECTION, status_code=401)(scope, receive, send)
