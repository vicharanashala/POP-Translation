"""Live updates for the dashboard: GET /dashboard/events (Server-Sent Events).

Replaces the frontend's polling of the upload and translation queues. Every
change to a queue item, a translation job, or a document's translation/review
is published here and pushed to every open stream:

    event: upload        data: UploadQueueItemOut JSON, or {"id", "deleted": true, ...}
    event: translation   data: TranslationJobOut JSON, or {"id", "deleted": true}
    event: document      data: {"unique_document_id": "<hex>"}

In-process only. That is correct for how this runs -- one uvicorn process, and
the workers that change state are threads inside it -- but a second process
would not see the first one's events.

Nothing is replayed: a client that reconnects has missed whatever happened in
between, so it refetches the queues when its stream (re)opens.

Publishing never raises and never blocks. It is called from worker threads in
the middle of real work, and a slow or vanished browser tab must not be able to
stall an upload or a translation. A subscriber whose buffer is full just loses
events -- its next refetch-on-reconnect or fallback poll recovers.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

router = APIRouter()

KEEPALIVE_SECONDS = 15
_BUFFER = 500

_lock = threading.Lock()
_subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()


def _put(queue: asyncio.Queue, message: str) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        pass


def publish(event: str, data: Any) -> None:
    """Send one event to every open stream. Safe from any thread."""
    try:
        message = f"event: {event}\ndata: {json.dumps(jsonable_encoder(data), ensure_ascii=False)}\n\n"
        with _lock:
            targets = list(_subscribers)
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(_put, queue, message)
            except RuntimeError:  # that stream's loop is already closed
                pass
    except Exception as exc:  # noqa: BLE001 -- see module docstring
        print(f"[events] publish {event} failed: {type(exc).__name__}: {exc}", flush=True)


# -- what gets published -------------------------------------------------------
# Each helper re-reads the current state, so a caller only says WHAT changed.


def upload_changed(item_id) -> None:
    try:
        from dashboard.db import get_session
        from dashboard.models import COLL_UPLOAD_QUEUE_ITEMS
        from dashboard.routes_uploads import _item_out

        with get_session() as db:
            item = db[COLL_UPLOAD_QUEUE_ITEMS].find_one({"_id": item_id})
        if item is None:
            publish("upload", {"id": str(item_id), "deleted": True})
        else:
            publish("upload", _item_out(item))
    except Exception as exc:  # noqa: BLE001
        print(f"[events] upload {item_id}: {type(exc).__name__}: {exc}", flush=True)


def upload_removed(item_id, **extra) -> None:
    publish("upload", {"id": str(item_id), "deleted": True, **extra})


def translation_changed(job_id) -> None:
    try:
        from dashboard.db import get_session
        from dashboard.models import COLL_TRANSLATION_JOBS
        from dashboard.routes_translation import _hydrate_jobs

        with get_session() as db:
            job = db[COLL_TRANSLATION_JOBS].find_one({"_id": job_id})
            if job is None:
                publish("translation", {"id": str(job_id), "deleted": True})
            else:
                publish("translation", _hydrate_jobs(db, [job])[0])
    except Exception as exc:  # noqa: BLE001
        print(f"[events] translation {job_id}: {type(exc).__name__}: {exc}", flush=True)


def document_changed(document_id) -> None:
    publish("document", {"unique_document_id": str(document_id)})


# -- the stream ----------------------------------------------------------------


async def _stream(request: Request) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=_BUFFER)
    entry = (loop, queue)
    with _lock:
        _subscribers.add(entry)
    try:
        # Sent at once so the proxy and browser see the stream open immediately
        # (and `retry` tells EventSource how soon to reconnect).
        yield "retry: 3000\n: connected\n\n"
        while True:
            try:
                yield await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                if await request.is_disconnected():
                    break
                yield ": keep-alive\n\n"
    finally:
        with _lock:
            _subscribers.discard(entry)


@router.get("/events")
async def events(request: Request):
    return StreamingResponse(
        _stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Stops nginx-style proxies from buffering the stream into silence.
            "X-Accel-Buffering": "no",
        },
    )
