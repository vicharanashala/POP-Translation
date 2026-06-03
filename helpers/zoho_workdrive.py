"""
Zoho WorkDrive CRUD client for POP-Translation pipeline storage.

Auth credentials are read from env vars:
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
  ZOHO_ROOT_FOLDER_ID  (root WorkDrive folder — parent of Data/ and Workdir/)

No files are written to disk. The access token is obtained fresh from the
refresh token on startup and stored only in memory. When it expires (401),
it is refreshed automatically in-place.
"""

import os
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

WD_BASE = "https://workdrive.zoho.in/api/v1"
_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB per resumable chunk


class ZohoWorkDrive:
    def __init__(self):
        self._client_id = os.environ["ZOHO_CLIENT_ID"]
        self._client_secret = os.environ["ZOHO_CLIENT_SECRET"]
        self._refresh_token = os.environ["ZOHO_REFRESH_TOKEN"]
        self.root_folder_id = os.environ["ZOHO_ROOT_FOLDER_ID"]

        self._access_token = ""
        self._token_obtained_at = 0.0
        self._lock = threading.Lock()
        self._refresh_access_token()
        self._session = self._make_session()
        self._start_proactive_refresh()

    # ── Token management ──────────────────────────────────────────────────────

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({"Authorization": f"Zoho-oauthtoken {self._access_token}"})
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 502, 503])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        return s

    def _refresh_access_token(self) -> bool:
        try:
            resp = requests.post(
                "https://accounts.zoho.in/oauth/v2/token",
                params={
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                },
                timeout=(10, 30),
            )
            data = resp.json()
        except Exception as e:
            print(f"[ZOHO] Token refresh failed: {e}")
            return False
        new_token = data.get("access_token")
        if not new_token:
            print(f"[ZOHO] Token refresh error: {data}")
            return False
        with self._lock:
            self._access_token = new_token
            self._token_obtained_at = time.time()
            if hasattr(self, "_session"):
                self._session.headers.update({"Authorization": f"Zoho-oauthtoken {new_token}"})
        print("[ZOHO] Access token refreshed.")
        return True

    def _ensure_token_fresh(self) -> None:
        """Proactively refresh if the token is older than 50 minutes."""
        with self._lock:
            age = time.time() - self._token_obtained_at
        if age > 50 * 60:
            self._refresh_access_token()

    def _start_proactive_refresh(self, interval: int = 45 * 60) -> None:
        """Background job: refresh token every 45 minutes."""
        def _loop():
            while True:
                time.sleep(interval)
                self._refresh_access_token()

        t = threading.Thread(target=_loop, daemon=True, name="zoho-token-refresh")
        t.start()

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _log_error(resp: requests.Response) -> None:
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        print(f"[ZOHO] HTTP {resp.status_code} {resp.request.method} {resp.url}: {body}")

    def _get(self, url: str, **kwargs) -> requests.Response:
        self._ensure_token_fresh()
        resp = self._session.get(url, **kwargs)
        if resp.status_code == 401:
            self._log_error(resp)
            if self._refresh_access_token():
                resp = self._session.get(url, **kwargs)
        return resp

    def _post(self, url: str, **kwargs) -> requests.Response:
        self._ensure_token_fresh()
        resp = self._session.post(url, **kwargs)
        if resp.status_code == 401:
            self._log_error(resp)
            if self._refresh_access_token():
                resp = self._session.post(url, **kwargs)
                if resp.status_code not in (200, 201, 204, 422):
                    self._log_error(resp)
        elif resp.status_code not in (200, 201, 204, 422):
            self._log_error(resp)
        return resp

    def _patch(self, url: str, **kwargs) -> requests.Response:
        self._ensure_token_fresh()
        resp = self._session.patch(url, **kwargs)
        if resp.status_code == 401:
            self._log_error(resp)
            if self._refresh_access_token():
                resp = self._session.patch(url, **kwargs)
                if resp.status_code not in (200, 204):
                    self._log_error(resp)
        elif resp.status_code not in (200, 204):
            self._log_error(resp)
        return resp

    def _delete_req(self, url: str, **kwargs) -> requests.Response:
        self._ensure_token_fresh()
        resp = self._session.delete(url, **kwargs)
        if resp.status_code == 401:
            self._log_error(resp)
            if self._refresh_access_token():
                resp = self._session.delete(url, **kwargs)
        return resp

    # ── Folder listing ────────────────────────────────────────────────────────

    def list_folder(self, folder_id: str) -> list[dict]:
        """List all items one level deep. Returns [{id, name, type, size}]."""
        items = []
        offset = 0
        limit = 50
        while True:
            resp = self._get(
                f"{WD_BASE}/files/{folder_id}/files",
                params={"page[limit]": limit, "page[offset]": offset},
                timeout=(10, 30),
            )
            if resp.status_code != 200:
                break
            data = resp.json()
            batch = data.get("data", [])
            if not batch:
                break
            for item in batch:
                attrs = item.get("attributes", {})
                storage = attrs.get("storage_info", {})
                raw_size = storage.get("size_in_bytes", attrs.get("size", 0))
                items.append({
                    "id": item["id"],
                    "name": attrs.get("name", ""),
                    "type": attrs.get("type", ""),
                    "size": int(raw_size or 0),
                })
            if len(batch) < limit:
                break
            offset += limit
        return items

    def walk_folder(self, folder_id: str, prefix: str = "") -> Iterator[tuple[str, dict]]:
        """Recursively yield (path, item) for every item under folder_id."""
        for item in self.list_folder(folder_id):
            item_path = f"{prefix}/{item['name']}" if prefix else item["name"]
            yield (item_path, item)
            if item["type"] == "folder":
                yield from self.walk_folder(item["id"], item_path)

    def find_child(self, parent_id: str, name: str) -> Optional[dict]:
        """Find a direct child by exact name. Returns item dict or None."""
        for item in self.list_folder(parent_id):
            if item["name"] == name:
                return item
        return None

    # ── Path resolution ───────────────────────────────────────────────────────

    def resolve_path(self, path: str) -> Optional[tuple[str, str]]:
        """Resolve slash-separated path from root. Returns (id, type) or None."""
        parts = [p for p in path.split("/") if p]
        current_id = self.root_folder_id
        current_type = "folder"
        for part in parts:
            child = self.find_child(current_id, part)
            if child is None:
                return None
            current_id = child["id"]
            current_type = child["type"]
        return (current_id, current_type)

    def ensure_path(self, path: str) -> str:
        """Create all missing folders in path. Returns final folder ID."""
        parts = [p for p in path.split("/") if p]
        current_id = self.root_folder_id
        for part in parts:
            current_id = self.get_or_create_folder(part, current_id)
        return current_id

    # ── Folder CRUD ───────────────────────────────────────────────────────────

    def create_folder(self, name: str, parent_id: str) -> str:
        """Create a folder. Returns new folder ID.

        FIX C: on 422 (already exists), fall back to find_child to handle races.
        """
        resp = self._post(
            f"{WD_BASE}/files",
            json={
                "data": {
                    "attributes": {"name": name, "parent_id": parent_id},
                    "type": "files",
                }
            },
            headers={"Content-Type": "application/vnd.api+json"},
            timeout=(10, 30),
        )
        if resp.status_code == 422:
            existing = self.find_child(parent_id, name)
            if existing:
                return existing["id"]
        resp.raise_for_status()
        return resp.json()["data"]["id"]

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Return existing folder ID or create it."""
        existing = self.find_child(parent_id, name)
        if existing and existing["type"] == "folder":
            return existing["id"]
        return self.create_folder(name, parent_id)

    # ── File upload ───────────────────────────────────────────────────────────

    def upload_file(self, filename: str, content: bytes, parent_id: str) -> str:
        """Upload bytes as a file. Returns file ID."""
        resp = self._post(
            f"{WD_BASE}/upload",
            data={
                "filename": filename,
                "parent_id": parent_id,
                "override-name-exist": "true",
            },
            files={"content": (filename, content, "application/octet-stream")},
            timeout=(30, 300),
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Zoho upload failed (HTTP {resp.status_code}): {resp.text[:500]}"
            )
        data = resp.json()
        items = data.get("data", [])
        if isinstance(items, list) and items:
            if "id" in items[0]:
                return items[0]["id"]
        elif isinstance(items, dict) and "id" in items:
            return items["id"]
        # Override response omits id — find the newest copy by name.
        # Zoho sometimes creates a duplicate instead of replacing; take the last
        # entry (most recently added) so callers don't read a stale file.
        matches = [i for i in self.list_folder(parent_id) if i["name"] == filename]
        return matches[-1]["id"] if matches else ""

    def upload_file_stream(self, filename: str, local_path: Path, parent_id: str) -> str:
        """Upload a local file by streaming — no full read into memory. Returns file ID."""
        with open(local_path, "rb") as fh:
            resp = self._post(
                f"{WD_BASE}/upload",
                data={
                    "filename": filename,
                    "parent_id": parent_id,
                    "override-name-exist": "true",
                },
                files={"content": (filename, fh, "application/octet-stream")},
                timeout=(30, 600),
            )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Zoho upload failed ({resp.status_code}): {resp.text[:500]}"
            )
        data = resp.json()
        items = data.get("data", [])
        if isinstance(items, list) and items:
            return items[0].get("id") or (self.find_child(parent_id, filename) or {}).get("id", "")
        if isinstance(items, dict):
            return items.get("id") or (self.find_child(parent_id, filename) or {}).get("id", "")
        return ""

    def upload_file_resumable(self, filename: str, local_path: Path, parent_id: str) -> str:
        """Chunked upload for files >50 MB. Returns file ID."""
        file_size = local_path.stat().st_size
        # Step 1: initiate
        resp = self._post(
            f"{WD_BASE}/upload/resumable",
            data={
                "filename": filename,
                "parent_id": parent_id,
                "file_size": file_size,
                "override-name-exist": "true",
            },
            timeout=(10, 30),
        )
        resp.raise_for_status()
        upload_id = resp.json()["data"]["attributes"]["upload_id"]
        # Step 2: upload chunks
        with open(local_path, "rb") as fh:
            start = 0
            while True:
                chunk = fh.read(_CHUNK_SIZE)
                if not chunk:
                    break
                end = start + len(chunk) - 1
                self._post(
                    f"{WD_BASE}/upload/resumable/{upload_id}",
                    data=chunk,
                    headers={
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Content-Type": "application/octet-stream",
                    },
                    timeout=(30, 300),
                )
                start += len(chunk)
        # Step 3: commit
        resp = self._post(
            f"{WD_BASE}/upload/resumable/{upload_id}/commit",
            timeout=(10, 60),
        )
        resp.raise_for_status()
        return resp.json()["data"]["id"]

    def upload_local_file(self, local_path: Path, zoho_folder_path: str) -> str:
        """Upload a local file into a Zoho folder path (streaming). Returns file ID."""
        parent_id = (
            self.ensure_path(zoho_folder_path)
            if zoho_folder_path
            else self.root_folder_id
        )
        if local_path.stat().st_size > 50 * 1024 * 1024:
            return self.upload_file_resumable(local_path.name, local_path, parent_id)
        return self.upload_file_stream(local_path.name, local_path, parent_id)

    # ── File download ─────────────────────────────────────────────────────────

    def download_file(self, file_id: str) -> bytes:
        """Download a file by ID and return its bytes."""
        for url in [
            f"{WD_BASE}/download/{file_id}",
            f"{WD_BASE}/files/{file_id}/content",
        ]:
            resp = self._get(url, timeout=(10, 300), stream=True)
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct or "html" in ct:
                    continue
                return b"".join(resp.iter_content(chunk_size=1 << 20))
        raise FileNotFoundError(f"Cannot download file {file_id}")

    def download_file_stream(self, file_id: str) -> requests.Response:
        """Return a streaming response for a file (caller must close)."""
        for url in [
            f"{WD_BASE}/download/{file_id}",
            f"{WD_BASE}/files/{file_id}/content",
        ]:
            resp = self._get(url, timeout=(10, 300), stream=True)
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct or "html" in ct:
                    continue
                return resp
        raise FileNotFoundError(f"Cannot stream file {file_id}")

    # ── Delete / rename / move ────────────────────────────────────────────────

    def delete(self, file_id: str) -> bool:
        """Trash a file or folder (status 51 = moved to trash)."""
        resp = self._patch(
            f"{WD_BASE}/files/{file_id}",
            json={"data": {"attributes": {"status": "51"}, "type": "files"}},
            headers={"Content-Type": "application/vnd.api+json"},
            timeout=(10, 30),
        )
        return resp.status_code in (200, 204)

    def rename(self, file_id: str, new_name: str) -> bool:
        """Rename a file or folder."""
        resp = self._patch(
            f"{WD_BASE}/files/{file_id}",
            json={"data": {"attributes": {"name": new_name}, "type": "files"}},
            headers={"Content-Type": "application/vnd.api+json"},
            timeout=(10, 30),
        )
        return resp.status_code in (200, 204)

    def move(self, file_id: str, new_parent_id: str) -> bool:
        """Move a file or folder to a new parent."""
        resp = self._patch(
            f"{WD_BASE}/files/{file_id}",
            json={"data": {"attributes": {"parent_id": new_parent_id}, "type": "files"}},
            headers={"Content-Type": "application/vnd.api+json"},
            timeout=(10, 30),
        )
        return resp.status_code in (200, 204)

    def get_file_metadata(self, file_id: str) -> Optional[dict]:
        """Get metadata for a single file/folder by ID."""
        resp = self._get(f"{WD_BASE}/files/{file_id}", timeout=(10, 30))
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", {})
        attrs = data.get("attributes", {})
        return {
            "id": data.get("id"),
            "name": attrs.get("name", ""),
            "type": attrs.get("type", ""),
            "size": attrs.get("size", 0),
        }
