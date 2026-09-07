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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

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
    from pop_server import _get_zoho

    wd = _get_zoho()
    meta = wd.get_file_metadata(zoho_file_id)
    if meta is None:
        raise HTTPException(404, "file not found")
    filename = meta.get("name") or zoho_file_id
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
