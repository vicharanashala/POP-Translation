"""Generic file view/download endpoints backing the eye/copy/download icons
(frontend plan §6). Same streaming-proxy pattern as pop_server.py's existing
`GET /download/{path:path}`.

No dedicated Zoho "create public share link" API is wired up (see backend
plan §1 -- confirmed to reuse the existing direct-file-URL style instead),
so both the "view" and "download" actions route through this proxy; "view"
just adds an inline Content-Disposition instead of attachment.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

router = APIRouter()


def _stream_and_close(resp):
    try:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if chunk:
                yield chunk
    finally:
        resp.close()


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
def download_file(zoho_file_id: str, inline: bool = False):
    from pop_server import _get_zoho

    wd = _get_zoho()
    meta = wd.get_file_metadata(zoho_file_id)
    if meta is None:
        raise HTTPException(404, "file not found")
    filename = meta.get("name") or zoho_file_id

    resp = wd.download_file_stream(zoho_file_id)
    disposition = "inline" if inline else "attachment"
    return StreamingResponse(
        _stream_and_close(resp),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )
