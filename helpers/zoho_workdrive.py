"""
Zoho WorkDrive CRUD client for POP-Translation pipeline storage.

Auth credentials are read from env vars:
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
  ZOHO_ROOT_FOLDER_ID  (root WorkDrive folder — parent of Data/ and Workdir/)

No files are written to disk. The access token is obtained fresh from the
refresh token on startup and stored only in memory. When it expires (401),
it is refreshed automatically in-place.
"""

import concurrent.futures
import os
import re
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

WD_BASE = "https://workdrive.zoho.in/api/v1"
_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB per resumable chunk
_MIN_REQUEST_INTERVAL = 0.15  # self-throttle to ~6-7 req/s to avoid tripping Zoho's burst rate limit
_HARD_TIMEOUT = 45  # seconds -- wall-clock backstop around every send() (see _send)


class ZohoWorkDrive:
    def __init__(self):
        self._client_id = os.environ["ZOHO_CLIENT_ID"]
        self._client_secret = os.environ["ZOHO_CLIENT_SECRET"]
        self._refresh_token = os.environ["ZOHO_REFRESH_TOKEN"]
        self.root_folder_id = os.environ["ZOHO_ROOT_FOLDER_ID"]

        self._access_token = ""
        self._token_obtained_at = 0.0
        self._lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0
        self._refresh_access_token()
        self._session = self._make_session()
        self._start_proactive_refresh()
        # Persistent (not per-call) so we're not paying thread-pool setup cost
        # on every request. Every _send() call -- regardless of which caller
        # thread it came from -- is submitted here, and future.result(timeout=
        # hard_timeout) starts counting from submission, not from when the
        # task actually starts running. This used to be sized at 2 workers,
        # which was fine when callers were effectively serialized by
        # _throttle(); it became a bottleneck once a caller (the report_true
        # OCR pipeline) started driving this class from 8 concurrent worker
        # threads doing full-body PDF downloads (hard_timeout=180 each) --
        # 6 of the 8 would sit queued behind the 2 running ones, and a task
        # still queued near the 180s mark would get killed and retried as a
        # false "timeout" even though nothing was actually stuck (confirmed:
        # the same file downloaded instantly via a direct browser request).
        # Sized to comfortably exceed the largest known concurrent caller
        # (8 workers) with headroom for a couple of abandoned/retrying tasks.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="zoho-send")

    # ── Token management ──────────────────────────────────────────────────────

    def _make_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({"Authorization": f"Zoho-oauthtoken {self._access_token}"})
        # Every retry -- bad status codes AND connection-level failures
        # (timeouts, resets, DNS hiccups) -- is handled explicitly, with
        # visible logging, in _send/_wait_for_retryable_error. urllib3's own
        # Retry is disabled (total=0) so it can't also retry underneath us
        # silently; that used to let a connection error burn ~2 minutes
        # (3 attempts x up to 40s each) with zero output before finally
        # surfacing as an exception.
        retry = Retry(total=0)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        return s

    def _throttle(self) -> None:
        """Self-imposed minimum gap between requests, since a burst of hundreds of
        sequential calls in a few seconds (e.g. a state with 100+ topic folders)
        can trip Zoho's rate limiter."""
        with self._throttle_lock:
            now = time.monotonic()
            wait = self._last_request_at + _MIN_REQUEST_INTERVAL - now
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    _MAX_RATE_LIMIT_RETRIES = 8
    _RETRYABLE_STATUSES = {429, 502, 503}

    def _wait_for_retryable_error(self, resp: requests.Response, url: str, attempt: int) -> bool:
        """If resp is 429/502/503, sleep (honoring Retry-After if present) and
        return True so the caller retries. `attempt` is the 1-based retry count
        so far, tracked by the caller's loop. Prints so a rate-limit or
        gateway-error stretch is visible instead of looking like a silent hang
        -- 502/503 used to retry invisibly inside urllib3's own Retry (which
        also honors a server-sent Retry-After), so a slow/degraded gateway
        looked identical to a dead process. Capped so a persistent error still
        surfaces as a real error rather than looping forever."""
        if resp.status_code not in self._RETRYABLE_STATUSES:
            return False
        if attempt > self._MAX_RATE_LIMIT_RETRIES:
            print(f"[ZOHO] Still {resp.status_code} on {url} after {self._MAX_RATE_LIMIT_RETRIES} retries -- giving up.", flush=True)
            return False
        try:
            retry_after = float(resp.headers.get("Retry-After", 5))
        except ValueError:
            retry_after = 5.0
        retry_after = max(retry_after, 1.0)
        print(
            f"[ZOHO] HTTP {resp.status_code} on {url} -- waiting {retry_after:.1f}s and retrying "
            f"(attempt {attempt}/{self._MAX_RATE_LIMIT_RETRIES})",
            flush=True,
        )
        time.sleep(retry_after)
        return True

    def _send(self, verb: str, url: str, *, hard_timeout: float = _HARD_TIMEOUT, **kwargs) -> requests.Response:
        """Every actual socket send goes through here, wrapped in a hard
        wall-clock timeout (`hard_timeout`, default _HARD_TIMEOUT) run on a
        background thread.

        requests' own timeout=(connect, read) kwarg does NOT bound DNS
        resolution -- socket.getaddrinfo() is called before any connection
        exists and ignores it entirely, so a stalled resolver (flaky DNS, VPN
        hiccup) can hang forever with no exception ever raised -- neither
        _wait_for_retryable_error nor a RequestException catch below would
        ever see it (confirmed: a real run sat for 3+ minutes with zero
        output on exactly this). It also does NOT bound reading a streamed
        response body via iter_content() -- that happens after this call
        already returned, entirely outside this wrapper (see download_file,
        which deliberately avoids stream=True for this reason: the whole
        request, body included, needs to happen inside one _send call to be
        covered). `hard_timeout` is overridable per-call so a large-file
        download can get a realistic budget instead of the short one meant
        for small metadata/listing calls.

        Running the call on a thread and bounding it with
        future.result(timeout=...) catches all of the above: if the thread
        doesn't finish in time we just abandon it (it may still be stuck in
        the background and dies on its own later or at process exit --
        harmless, just a leaked thread) and retry fresh, with the same
        visible logging used for bad-status and connection-exception
        retries. urllib3's own Retry is disabled (total=0) so nothing
        retries silently underneath this."""
        method = getattr(self._session, verb)
        attempt = 1
        while True:
            future = self._executor.submit(method, url, **kwargs)
            try:
                return future.result(timeout=hard_timeout)
            except (requests.exceptions.RequestException, concurrent.futures.TimeoutError) as e:
                if isinstance(e, concurrent.futures.TimeoutError):
                    label = f"no response within {hard_timeout}s (hard timeout -- likely DNS stall or stuck body read, requests' own timeout= doesn't cover either)"
                else:
                    label = f"{e.__class__.__name__}: {e}"
                if attempt > self._MAX_RATE_LIMIT_RETRIES:
                    print(
                        f"[ZOHO] Still failing on {url} after {self._MAX_RATE_LIMIT_RETRIES} retries "
                        f"({label}) -- giving up.",
                        flush=True,
                    )
                    raise
                wait = min(2 ** attempt, 30)
                print(
                    f"[ZOHO] {label} on {url} -- waiting {wait}s and retrying "
                    f"(attempt {attempt}/{self._MAX_RATE_LIMIT_RETRIES})",
                    flush=True,
                )
                time.sleep(wait)
                attempt += 1

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

    def _get(self, url: str, *, hard_timeout: float = _HARD_TIMEOUT, **kwargs) -> requests.Response:
        self._ensure_token_fresh()
        self._throttle()
        resp = self._send("get", url, hard_timeout=hard_timeout, **kwargs)
        attempt = 1
        while self._wait_for_retryable_error(resp, url, attempt):
            self._throttle()
            resp = self._send("get", url, hard_timeout=hard_timeout, **kwargs)
            attempt += 1
        if resp.status_code == 401:
            self._log_error(resp)
            if self._refresh_access_token():
                self._throttle()
                resp = self._send("get", url, hard_timeout=hard_timeout, **kwargs)
        return resp

    def _post(self, url: str, *, hard_timeout: float = _HARD_TIMEOUT, **kwargs) -> requests.Response:
        self._ensure_token_fresh()
        self._throttle()
        resp = self._send("post", url, hard_timeout=hard_timeout, **kwargs)
        attempt = 1
        while self._wait_for_retryable_error(resp, url, attempt):
            self._throttle()
            resp = self._send("post", url, hard_timeout=hard_timeout, **kwargs)
            attempt += 1
        if resp.status_code == 401:
            self._log_error(resp)
            if self._refresh_access_token():
                self._throttle()
                resp = self._send("post", url, hard_timeout=hard_timeout, **kwargs)
                if resp.status_code not in (200, 201, 204, 422):
                    self._log_error(resp)
        elif resp.status_code not in (200, 201, 204, 422):
            self._log_error(resp)
        return resp

    def _patch(self, url: str, *, hard_timeout: float = _HARD_TIMEOUT, **kwargs) -> requests.Response:
        self._ensure_token_fresh()
        self._throttle()
        resp = self._send("patch", url, hard_timeout=hard_timeout, **kwargs)
        attempt = 1
        while self._wait_for_retryable_error(resp, url, attempt):
            self._throttle()
            resp = self._send("patch", url, hard_timeout=hard_timeout, **kwargs)
            attempt += 1
        if resp.status_code == 401:
            self._log_error(resp)
            if self._refresh_access_token():
                self._throttle()
                resp = self._send("patch", url, hard_timeout=hard_timeout, **kwargs)
                if resp.status_code not in (200, 204):
                    self._log_error(resp)
        elif resp.status_code not in (200, 204):
            self._log_error(resp)
        return resp

    def _delete_req(self, url: str, *, hard_timeout: float = _HARD_TIMEOUT, **kwargs) -> requests.Response:
        self._ensure_token_fresh()
        self._throttle()
        resp = self._send("delete", url, hard_timeout=hard_timeout, **kwargs)
        attempt = 1
        while self._wait_for_retryable_error(resp, url, attempt):
            self._throttle()
            resp = self._send("delete", url, hard_timeout=hard_timeout, **kwargs)
            attempt += 1
        if resp.status_code == 401:
            self._log_error(resp)
            if self._refresh_access_token():
                self._throttle()
                resp = self._send("delete", url, hard_timeout=hard_timeout, **kwargs)
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
        """Find a direct child by name. Returns item dict or None.

        Matching is case-insensitive: Zoho's filesystem treats names
        case-insensitively, so two children can never legitimately differ only
        by case. Using the same match model for both lookup and creation keeps
        reads (resolve_path) and writes (create_folder, uploads) consistent and
        prevents case-variant duplicates (e.g. asking for "test" when "Test"
        already exists).
        """
        target = name.strip().lower()
        for item in self.list_folder(parent_id):
            if item["name"].strip().lower() == target:
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
        """Create a folder idempotently. Returns the folder ID.

        Zoho WorkDrive does not reliably 422 on a duplicate name. For a
        case-variant collision (e.g. creating "test" when "Test" exists) it
        silently creates a copy with a " DD-MM-YYYY HH:MM:SS:mmm" suffix
        appended instead of erroring. So we:
          1. Look first, case-insensitively, and reuse any match.
          2. Still handle the 422 path for exact-name races.
          3. After creating, verify Zoho kept our name. If it appended a
             suffix, a collision existed — trash the dupe and return the
             pre-existing folder.
        """
        name = name.strip()
        existing = self.find_child(parent_id, name)
        if existing and existing["type"] == "folder":
            return existing["id"]

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
        data = resp.json()["data"]
        created_id = data["id"]
        created_name = data.get("attributes", {}).get("name", name)

        # Detect Zoho's silent collision-rename and undo it.
        if created_name.strip().lower() != name.lower():
            existing = self.find_child(parent_id, name)
            if existing and existing["id"] != created_id:
                self.delete(created_id)
                return existing["id"]
        return created_id

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Return existing folder ID (case-insensitive) or create it."""
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
        target = filename.strip().lower()
        matches = [i for i in self.list_folder(parent_id) if i["name"].strip().lower() == target]
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

    _PARALLEL_CHUNK_THRESHOLD = 20 * 1024 * 1024  # below this, a plain single request is plenty
    _PARALLEL_CHUNK_WORKERS = 8

    def _probe_content_length(self, url: str) -> int | None:
        """Tiny bytes=0-0 Range request, just to learn the file's total size
        (and confirm the server honors Range at all) without pulling any
        real body. Cheap: ~1 byte of response."""
        resp = self._get(url, timeout=(10, 30), hard_timeout=30, headers={"Range": "bytes=0-0"})
        if resp.status_code != 206:
            return None
        m = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
        return int(m.group(1)) if m else None

    def _download_parallel(self, url: str, size: int) -> bytes:
        """Fetch `url` as N concurrent Range-chunked requests and reassemble.

        Confirmed live on a ~300MB outlier file in the report_true dataset:
        Zoho's download endpoint appears to cap throughput PER TCP
        CONNECTION (~2.2-2.5MB/s observed, highly consistent across many
        single-connection trials -- not per account/token, since a fresh
        session/adapter/token each time made no difference). Splitting one
        file's download into 8 concurrent Range requests measured ~4.3x
        higher aggregate throughput (300MB: ~140s on one connection -> ~30s
        with 8 parallel chunks, reproduced twice, consistent both times).
        """
        n = self._PARALLEL_CHUNK_WORKERS
        chunk_size = -(-size // n)  # ceil div

        def fetch(i: int) -> bytes:
            start = i * chunk_size
            end = min(start + chunk_size, size) - 1
            resp = self._get(url, timeout=(10, 400), hard_timeout=400, headers={"Range": f"bytes={start}-{end}"})
            if resp.status_code != 206:
                raise RuntimeError(f"range chunk {i} got HTTP {resp.status_code}, expected 206")
            return resp.content

        with concurrent.futures.ThreadPoolExecutor(max_workers=n, thread_name_prefix="zoho-chunk") as ex:
            parts = list(ex.map(fetch, range(n)))
        return b"".join(parts)

    def download_file(self, file_id: str) -> bytes:
        """Download a file by ID and return its bytes.

        Files above _PARALLEL_CHUNK_THRESHOLD are fetched via
        _download_parallel (see its docstring for why: per-connection
        throughput capping on Zoho's side makes N parallel connections to
        the SAME file substantially faster in aggregate). Below the
        threshold a plain single request is used -- for a normal multi-MB
        PDF the probe-then-maybe-fallback overhead isn't worth it, and it
        finishes in a second or two either way.

        Deliberately NOT stream=True for the plain-request path: with
        streaming, _get()/_send() only cover getting the response headers --
        the actual body then gets read afterward via iter_content(), on the
        calling thread, completely outside _send's hard-timeout/retry
        protection (governed only by the raw per-chunk requests timeout,
        which can silently sit for minutes on a stalled transfer with
        nothing to catch it -- confirmed by a real run). Without
        stream=True, the whole request -- headers AND body -- happens
        inside the one _send call, so a stalled download is bounded and
        retried just like everything else. hard_timeout is bumped up from
        the default (meant for small metadata/listing calls) to give a
        multi-MB PDF a realistic transfer budget.
        """
        for url in [
            f"{WD_BASE}/download/{file_id}",
            f"{WD_BASE}/files/{file_id}/content",
        ]:
            size = self._probe_content_length(url)
            if size is not None and size > self._PARALLEL_CHUNK_THRESHOLD:
                return self._download_parallel(url, size)

            resp = self._get(url, timeout=(10, 400), hard_timeout=400)
            if resp.status_code == 200:
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct or "html" in ct:
                    continue
                return resp.content
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
        """Get metadata for a single file/folder by ID.

        For PDFs, Zoho precomputes page count (and other pdf_metadata) but
        only exposes it on this single-file detail endpoint -- the bulk
        `files/{folder_id}/files` listing used by list_folder() always
        returns an empty meta_info, even for the same file. So getting a
        PDF's page count without downloading it costs one of these calls per
        file, not zero.
        """
        resp = self._get(f"{WD_BASE}/files/{file_id}", timeout=(10, 30))
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", {})
        attrs = data.get("attributes", {})
        pdf_metadata = attrs.get("meta_info", {}).get("metadata", {}).get("pdf_metadata", {})
        return {
            "id": data.get("id"),
            "name": attrs.get("name", ""),
            "type": attrs.get("type", ""),
            "size": attrs.get("size", 0),
            "pdf_pages": pdf_metadata.get("pages"),  # str, e.g. "6" -- None if not a PDF or not yet computed
            # This is the PDF's own embedded /Lang catalog attribute (set by
            # whatever tool produced the PDF -- e.g. Chrome's print-to-PDF
            # copies it from the source page's <html lang="...">), NOT
            # anything Zoho computes itself. Present roughly half the time in
            # practice; None the rest (tools like Ghostscript/PDF24 don't set
            # it). When present it's a free, ~instant, fully trustworthy
            # signal -- no download needed. When absent, it's just unknown,
            # not "not English"; callers must fall back to another method.
            "pdf_language": pdf_metadata.get("language"),
            "created_time_ms": attrs.get("created_time_in_millisecond"),
            "extn": attrs.get("extn", ""),
        }
