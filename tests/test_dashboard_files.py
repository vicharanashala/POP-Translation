"""Read-only checks of the file view/download proxy -- only ever fetches
metadata/streams bytes from Zoho, never mutates anything.
"""


def _a_real_zoho_file_id(client) -> str:
    # zoho_file_id itself isn't exposed via the API (shareable_link is the
    # public equivalent) -- extract it from shareable_link's
    # https://workdrive.zoho.in/file/<id> shape instead.
    items = client.get("/dashboard/unique-documents", params={"page_size": 20}).json()["items"]
    for row in items:
        link = row.get("shareable_link")
        if link:
            return link.rsplit("/", 1)[-1]
    raise AssertionError("no unique document with a shareable_link found in the first page")


def test_file_link(client):
    file_id = _a_real_zoho_file_id(client)
    resp = client.get(f"/dashboard/files/{file_id}/link")
    assert resp.status_code == 200
    body = resp.json()
    assert body["view_url"] == f"/dashboard/files/{file_id}/download?inline=1"
    assert body["download_url"] == f"/dashboard/files/{file_id}/download"


def test_file_link_404_for_bogus_id(client):
    resp = client.get("/dashboard/files/not-a-real-file-id/link")
    assert resp.status_code == 404


def test_file_download_streams_bytes(client):
    file_id = _a_real_zoho_file_id(client)
    resp = client.get(f"/dashboard/files/{file_id}/download")
    assert resp.status_code == 200
    assert len(resp.content) > 0
    assert "attachment" in resp.headers.get("content-disposition", "")
