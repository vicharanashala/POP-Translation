"""Generic file view/download endpoints backing the eye/copy/download icons
(frontend plan §6). Same streaming-proxy pattern as pop_server.py's existing
`GET /download/{path:path}`.

No dedicated Zoho "create public share link" API is wired up (see backend
plan §1 -- confirmed to reuse the existing direct-file-URL style instead),
so both the "view" and "download" actions route through this proxy; "view"
just adds an inline Content-Disposition instead of attachment.

The proxy is keyed by Zoho file id, and works the same for all three kinds of
file the dashboard holds -- the original (`representative_file_id`), the
translation (`translation_file_id`) and the review (`review_file_id`). Nothing
here knows or cares which is which.

Large files are the reason this is not a single pass-through stream. Zoho caps
throughput per TCP connection at ~2.2-2.5 MB/s, so a few hundred MB on one
connection takes minutes and dies partway. Instead the size is probed up front,
`Content-Length` and `Accept-Ranges` are returned so the browser shows real
progress and can resume, and the body is assembled from several concurrent
Range requests (helpers/zoho_workdrive.iter_range).
"""
from __future__ import annotations

import mimetypes
import re

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from dashboard.db import get_db
from dashboard.models import COLL_UNIQUE_DOCUMENTS

from helpers.http_headers import content_disposition

router = APIRouter()

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _stream_and_close(resp):
    try:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if chunk:
                yield chunk
    finally:
        resp.close()


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """`Range: bytes=<start>-<end>` -> inclusive offsets, or None if the header
    is malformed/unsupported (in which case the whole file is served, which is
    what RFC 9110 asks for). A suffix range (`bytes=-500`) means the LAST 500
    bytes. Raises ValueError for a syntactically valid but unsatisfiable range,
    which the caller turns into a 416."""
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    raw_start, raw_end = m.group(1), m.group(2)
    if not raw_start and not raw_end:
        return None
    if not raw_start:  # suffix: the last N bytes
        length = int(raw_end)
        if length <= 0:
            raise ValueError("unsatisfiable")
        return max(size - length, 0), size - 1
    start = int(raw_start)
    end = int(raw_end) if raw_end else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        raise ValueError("unsatisfiable")
    return start, end


@router.get("/files/{zoho_file_id}/link")
def file_link(zoho_file_id: str):
    from pop_server import _get_zoho

    wd = _get_zoho()
    meta = wd.get_file_metadata(zoho_file_id)
    if meta is None:
        raise HTTPException(404, "file not found")
    return {
        "view_url": f"/dashboard/files/{zoho_file_id}/download?inline=1",
        "download_url": f"/dashboard/files/{zoho_file_id}/download",
    }


@router.get("/files/{zoho_file_id}/download")
def download_file(zoho_file_id: str, request: Request, inline: bool = False):
    return _serve(zoho_file_id, request, inline)


# The name a translation / review downloads as: the document's shareable name
# plus this suffix, keeping the stored file's own extension.
_NAMED_KINDS = {
    "translation": ("translation_zoho_file_id", "_translation"),
    "review": ("review_zoho_file_id", "_reviewed"),
}


def download_name(shareable_name: str | None, stored_name: str | None, suffix: str) -> str:
    """"Paddy_KA_2021.pdf" + stored "x_translated.docx" + "_translation"
    -> "Paddy_KA_2021_translation.docx"."""
    stem = (shareable_name or "").strip()
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    ext = stored_name.rsplit(".", 1)[1] if stored_name and "." in stored_name else ""
    stem = stem or (stored_name.rsplit(".", 1)[0] if stored_name and "." in stored_name else (stored_name or "download"))
    return f"{stem}{suffix}" + (f".{ext}" if ext else "")


@router.get("/unique-documents/{document_id}/{kind}/download")
def download_named(document_id: str, kind: str, request: Request, inline: bool = False, db=Depends(get_db)):
    """The document's translation or review, named after the document:
    `<shareable name>_translation.<ext>` / `<shareable name>_reviewed.<ext>`."""
    if kind not in _NAMED_KINDS:
        raise HTTPException(404, "not found")
    field, suffix = _NAMED_KINDS[kind]
    try:
        oid = ObjectId(document_id)
    except (InvalidId, TypeError):
        raise HTTPException(422, "malformed document id")
    doc = db[COLL_UNIQUE_DOCUMENTS].find_one({"_id": oid}, {"shareable_name": 1, field: 1})
    if doc is None:
        raise HTTPException(404, "document not found")
    if not doc.get(field):
        raise HTTPException(404, f"this document has no {kind}")
    return _serve(doc[field], request, inline,
                  name=lambda stored: download_name(doc.get("shareable_name"), stored, suffix))


def _serve(zoho_file_id: str, request: Request, inline: bool, name=None):
    from pop_server import _get_zoho

    wd = _get_zoho()
    meta = wd.get_file_metadata(zoho_file_id)
    if meta is None:
        raise HTTPException(404, "file not found")
    filename = meta.get("name") or zoho_file_id
    if name is not None:
        filename = name(meta.get("name"))
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    headers = {
        "Content-Disposition": content_disposition(filename, inline=inline),
        # Without this the browser cannot resume, and a partially-transferred
        # large file just fails from the start again.
        "Accept-Ranges": "bytes",
    }

    try:
        url, size = wd.resolve_download_url(zoho_file_id)
    except Exception as exc:  # network/auth failure talking to Zoho
        raise HTTPException(502, f"could not reach file storage: {type(exc).__name__}") from exc

    if size is None:
        # This URL does not honour Range. Fall back to the plain single
        # connection -- correct, just slow, and the only option left.
        try:
            resp = wd.download_file_stream(zoho_file_id)
        except FileNotFoundError:
            raise HTTPException(404, "file not found")
        return StreamingResponse(
            _stream_and_close(resp), media_type=media_type, headers=headers
        )

    start, end, status = 0, size - 1, 200
    range_header = request.headers.get("range")
    if range_header:
        try:
            window = _parse_range(range_header, size)
        except ValueError:
            raise HTTPException(
                416, "requested range not satisfiable",
                headers={"Content-Range": f"bytes */{size}"},
            )
        if window is not None:
            start, end = window
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(
        wd.iter_range(url, start, end),
        status_code=status,
        media_type=media_type,
        headers=headers,
    )
